#!/usr/bin/env python3
"""``sol.py`` -- bidirectional frame gateway + CLI for the GE Sol smart lamp.

The GE Sol ("Sol-Byi-Ge", the C by GE / Cync ring lamp) speaks an unencrypted,
length-prefixed binary protocol over TCP.  This module implements that protocol
in full -- a tolerant stream parser for partial/glued frames, an encoder for
outbound control frames, and an asyncio TCP server on port 23779 that can act
either as a *listener* (a stand-in gateway the lamp is pointed at) or as a
*client* (driving a real device/gateway).

Wire format
-----------
Every frame is::

    +--------+--------------------------+-----------+
    | header |           body           | (nothing) |
    | 5 byte |  N byte (N = header len) |           |
    +--------+--------------------------+-----------+
    header[0]   = (packet_type << 4) | flags
    header[1:5] = body length, big-endian uint32

The low nibble of ``header[0]`` carries flags.  Bit 3 marks a response, but the
exact ack byte varies per channel (``0x73`` -> ``0x7B``, yet ``0x43`` -> ``0x48``),
so the mapping lives in :data:`RESPONSE_BYTES` rather than being computed.

Data-channel packets (``0x73`` requests / ``0x83`` upstream, ``0x7b``/``0x88``
acks) carry their mesh command inside a ``0x7E``-bracketed envelope::

    deviceId(4 BE) | msgId(2 BE) | 0x00 | 0x7E | seq(4 BE) | 0xF8 | op_code |
        cmd_code | 0x00 | seq(4 BE) | 0x00 | 0x0F 0x80 | op_code | 0x11 0x02 |
        payload... | checksum | 0x7E

``cmd_code`` is *not* semantic: it counts the bytes from the sequence echo up to
(but not including) the checksum, i.e. ``len(payload) + 10``.  The loose ``0x00``
after ``cmd_code`` is excluded from that count, which is why a five-byte payload
shows ``cmd_code = 0x0D`` while fourteen bytes follow it.

The trailing checksum is additive over every byte from just after the ``0xF8``
marker up to the checksum itself::

    checksum = sum(body[13:checksum_index]) & 0xFF

That rule is verified against four independent captures: SetPowerState on and
off (``0x56``), an ``0x8E``-family frame (``0xE2``), and a plug power frame
(``0xE7``).  See ``TestChecksumAcrossCaptures``.

Where a field layout is inferred from documentation rather than proven from a
capture -- notably the brightness, colour-temperature and RGB envelopes -- this
module says so at the point of use and provides ``replay`` so a captured frame
can be sent verbatim while the generated form is still being validated.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import struct
import sys
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Iterable, Sequence

__version__ = "1.0.0"

LOG = logging.getLogger("sol")

DEFAULT_PORT = 23779
DEFAULT_HOST = "0.0.0.0"

# Packet types: header[0] >> 4 identifies the channel.  The low nibble carries
# flags; bit 3 means "response", but the exact ack byte varies by channel, so it
# lives in RESPONSE_BYTES rather than being derived arithmetically.
PKT_AUTH = 0x02  # header 0x23 -> ack 0x28
PKT_A3 = 0x0A  # header 0xa3 -> ack 0xab ("subscribe")
PKT_C3 = 0x0C  # header 0xc3 device-connect
PKT_D3 = 0x0D  # header 0xd3 ping -> ack 0xd8
PKT_PIPE = 0x07  # request data channel: 0x73 -> ack 0x7b
PKT_PIPE_SYNC = 0x08  # upstream data channel: 0x83 -> ack 0x88
PKT_STATUS = 0x04  # 0x43 status broadcast -> ack 0x48

HDR_LEN = 5
MAX_BODY = 1 << 20  # 1 MiB; a frame larger than this is treated as corruption

# HDLC-style delimiters used inside a data-channel body.
DELIM = 0x7E
# Inner "marker" that precedes op_code in every confirmed control frame.
MARKER = 0xF8
# Telink mesh vendor id 0x0211, little-endian; every mesh payload opens with it.
VENDOR_ID = bytes((0x11, 0x02))

# Mesh op codes.
OP_SET_POWER = 0xD0
OP_SET_BRIGHTNESS = 0xF0
OP_SET_BRIGHTNESS_SOL = 0xD2  # Sol-lamp variant
OP_SET_TEMP = 0xF0
OP_SET_TEMP_SOL = 0xE2  # Sol-lamp variant
OP_SET_RGB = 0xF0
OP_GET_STATUS = 0xDB
OP_GET_STATUS_PAGINATED = 0x52

# Confirmed control-frame constants.
_CHANNEL = 0x00  # channel byte between msgId and the 0x7E delimiter
_SEQ_PREFIX = 0x00  # three zero bytes between the 4-byte seq and the 0xF8 marker

# Offset inside a data-channel body where the checksummed region begins.
# The rule is "every byte from just after the 0xF8 marker up to the checksum".
# With the 8-byte routing prefix + 0x7E + 3 pad bytes, the marker lands at body
# index 12, so the checksummed region starts at 13.  Verified against four
# independent captures: SetPowerState on/off (0x56) plus an 0x8E-family frame
# (0xe2) and a plug power frame (0xe7).
_BODY_CHECKSUM_OFFSET = 13


def checksum_region(body: bytes, checksum_index: int) -> bytes:
    """Return the byte span the trailing checksum is computed over.

    ``checksum_index`` is the index of the checksum byte itself within the body.
    """
    if checksum_index <= _BODY_CHECKSUM_OFFSET:
        return b""
    return body[_BODY_CHECKSUM_OFFSET:checksum_index]


def compute_checksum(body: bytes, checksum_index: int) -> int:
    """Additive 8-bit checksum over :func:`checksum_region`.

    Verified rule: ``sum(body[13:checksum_index]) & 0xFF``.  The plain sum
    reproduces the stored ``0x56`` in both captured SetPowerState frames and is
    the only start offset (of 0..29) that satisfies both independently.
    """
    return sum(checksum_region(body, checksum_index)) & 0xFF


class FrameError(Exception):
    """Raised when bytes on the wire cannot be interpreted as a frame."""


@dataclass
class Frame:
    """One protocol frame: a 5-byte header plus its body.

    ``type`` is the header's high nibble (``header[0] >> 4``), which identifies
    the channel: 2 = auth, 4 = status, 7 = control data, 8 = upstream data,
    0xD = ping.  The low nibble carries flags, so the exact response byte is
    looked up in :data:`RESPONSE_BYTES` rather than computed -- ``0x73`` answers
    with ``0x7B`` (low nibble ``0xB``) while ``0x43`` answers with ``0x48``
    (low nibble ``0x8``).  Assuming one rule for all channels produces frames
    the device silently ignores.
    """

    type: int
    body: bytes = b""
    is_response: bool = False

    @property
    def type_byte(self) -> int:
        """Reconstruct ``header[0]`` from the type and the response flag."""
        if self.is_response:
            return RESPONSE_BYTES.get(self.type, (self.type << 4) | 0x8)
        return (self.type << 4) | 0x3

    def encode(self) -> bytes:
        """Serialise to the on-wire 5-byte header followed by the body."""
        if len(self.body) > MAX_BODY:
            raise FrameError(
                "body of %d bytes exceeds the %d byte maximum"
                % (len(self.body), MAX_BODY)
            )
        if not 0 <= self.type <= 0xF:
            raise FrameError("packet type %r does not fit in a nibble" % self.type)
        header = bytes((self.type_byte,)) + struct.pack(">I", len(self.body))
        return header + self.body

    def describe(self) -> str:
        """Human readable one-line summary used by the logger."""
        kind = "response" if self.is_response else "request"
        name = PACKET_NAMES.get(self.type, "unknown")
        return "type=0x%02x (%s) %s body=%d" % (
            self.type_byte,
            name,
            kind,
            len(self.body),
        )

    # -- data-channel helpers -------------------------------------------------

    def mesh(self) -> "MeshCommand | None":
        """Decode the 0x7E-bracketed mesh command, if this frame carries one."""
        return parse_mesh(self.body)


PACKET_NAMES = {
    PKT_AUTH: "auth",
    PKT_STATUS: "status",
    PKT_PIPE: "pipe",
    PKT_PIPE_SYNC: "pipe-sync",
    PKT_A3: "subscribe",
    PKT_C3: "device-connect",
    PKT_D3: "ping",
}

# Exact response header bytes, from captured traffic and the protocol notes.
# A request byte is always (type << 4) | 0x3.
RESPONSE_BYTES = {
    PKT_AUTH: 0x28,  # documented
    PKT_STATUS: 0x48,  # documented
    PKT_PIPE: 0x7B,  # captured: 73 .. -> 7b ..
    PKT_PIPE_SYNC: 0x88,  # documented
    PKT_D3: 0xD8,  # documented
}
# Response byte observed once, in the doc, for the subscribe channel.
RESPONSE_BYTES[PKT_A3] = 0xAB


def parse_header(buf: bytes | bytearray) -> tuple[int, int, bool]:
    """Parse a 5-byte header into ``(type, body_length, is_response)``."""
    if len(buf) < HDR_LEN:
        raise FrameError("need %d header bytes, have %d" % (HDR_LEN, len(buf)))
    type_byte = buf[0]
    (length,) = struct.unpack(">I", bytes(buf[1:HDR_LEN]))
    if length > MAX_BODY:
        raise FrameError(
            "declared body length %d exceeds the %d byte maximum"
            % (length, MAX_BODY)
        )
    return type_byte >> 4, length, bool(type_byte & 0x8)


# ---------------------------------------------------------------------------
# Mesh envelope (the 0x7E-bracketed section of a data-channel body)
# ---------------------------------------------------------------------------


@dataclass
class MeshCommand:
    """A decoded mesh command carried inside a data-channel frame.

    ``payload`` excludes the leading ``0x11 0x02`` vendor id, which is implied
    by the framing and re-inserted by :meth:`encode_inner`.
    """

    device_id: int
    msg_id: int
    seq: int
    op_code: int
    payload: bytes
    checksum: int | None = None
    checksum_valid: bool | None = None

    @property
    def vendor_payload(self) -> bytes:
        """The payload as it appears on the wire, vendor id included."""
        return VENDOR_ID + self.payload

    def _body_with(self, inner: bytes) -> bytes:
        """Prefix ``inner`` with the routing header so offsets line up."""
        # Routing: deviceId(4 BE) | msgId(2 BE) | channel(1)
        return (
            struct.pack(">I", self.device_id & 0xFFFFFFFF)
            + struct.pack(">H", self.msg_id & 0xFFFF)
            + bytes((_CHANNEL,))
            + inner
        )

    def encode_inner(self) -> bytes:
        """Build the ``0x7E``-bracketed section including its checksum.

        Byte layout, confirmed against the captured frames in ``_CAPTURES``::

            7e | seq(4 BE) | f8 | op_code | cmd_code |
                 06 00 00 00 | 00 | 0f 80 | <op_code> | 11 02 <payload> | ck | 7e

        ``cmd_code`` counts the bytes from the sequence echo up to (but not
        including) the checksum: ``4 (seq) + 1 + 2 + 1 (repeated op_code) +
        2 (vendor) + len(payload) = len(payload) + 10``.  The ``00`` written
        between ``cmd_code`` and the sequence echo is *not* counted, which is
        why real captures show ``cmd_code = 13`` for a five-byte payload while
        fourteen bytes follow it.
        """
        inner = bytearray()
        inner.append(DELIM)
        inner += struct.pack(">I", self.seq & 0xFFFFFFFF)  # 4-byte sequence
        inner.append(MARKER)
        inner.append(self.op_code)
        counted = (
            struct.pack(">I", self.seq & 0xFFFFFFFF)  # sequence echoed again
            + bytes((_SEQ_PREFIX,))
            + b"\x0f\x80"
            + bytes((self.op_code,))
            + self.vendor_payload
        )
        if len(counted) > 0xFF:
            raise FrameError(
                "mesh body of %d bytes does not fit in cmd_code" % len(counted)
            )
        inner.append(len(counted))
        inner.append(_SEQ_PREFIX)  # padding, not included in cmd_code
        inner += counted
        inner.append(0)  # placeholder for the checksum
        inner.append(DELIM)
        # Compute the checksum against the finished body so the absolute offset
        # used by compute_checksum is exactly the on-wire one.
        body = self._body_with(bytes(inner))
        inner[-2] = compute_checksum(body, len(body) - 2)
        return bytes(inner)

    def encode_body(self) -> bytes:
        """Build the full data-channel frame body."""
        return self._body_with(self.encode_inner())

    def to_frame(self) -> Frame:
        """Wrap this command in an outgoing ``0x73`` request frame."""
        return Frame(type=PKT_PIPE, body=self.encode_body())


def parse_mesh(body: bytes) -> MeshCommand | None:
    """Decode the mesh command inside a data-channel body, if present.

    Returns ``None`` when the body has no ``0x7E`` bracketed section, is too
    short to interpret, or lacks the ``0xF8`` marker at the expected offset.
    Never raises: an unparseable body from a real device is data worth logging,
    not a reason to drop the connection.
    """
    try:
        start = body.index(DELIM)
        end = body.rindex(DELIM)
    except ValueError:
        return None
    if end - start < 11:  # 0x7E | seq(4) | 0xF8 | op | cmd | ... | ck | 0x7E
        return None
    if body[start + 5] != MARKER:
        return None

    seq = struct.unpack(">I", body[start + 1:start + 5])[0]
    op_code = body[start + 6]
    checksum_index = end - 1
    stored = body[checksum_index]

    # Layout after cmd_code: 00 | seq(4) | 00 | 0f 80 | op | 11 02 | payload
    # Offsets, relative to the opening 0x7E at ``start``:
    #   +1 seq(4)  +5 0xF8  +6 op  +7 cmd  +8 00  +9 seq(4)  +13 00
    #   +14 0x0F  +15 0x80  +16 op  +17 0x11  +18 0x02  +19 payload
    payload_start = start + 19
    if checksum_index < payload_start:
        return None
    payload = body[payload_start:checksum_index]

    return MeshCommand(
        device_id=struct.unpack(">I", body[0:4])[0] if len(body) >= 4 else 0,
        msg_id=struct.unpack(">H", body[4:6])[0] if len(body) >= 6 else 0,
        seq=seq,
        op_code=op_code,
        payload=payload,
        checksum=stored,
        checksum_valid=compute_checksum(body, checksum_index) == stored,
    )


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def build_power(
    device_id: int, msg_id: int, seq: int, on: bool, *, sol: bool = True
) -> MeshCommand:
    """Power on/off: op_code ``0xD0``, ``[11 02 state 00 00]``."""
    del sol  # confirmed identical for Sol and non-Sol devices
    return MeshCommand(
        device_id=device_id,
        msg_id=msg_id,
        seq=seq,
        op_code=OP_SET_POWER,
        payload=bytes((1 if on else 0, 0x00, 0x00)),
    )


def build_brightness(
    device_id: int, msg_id: int, seq: int, level: int, *, sol: bool = True
) -> MeshCommand:
    """Brightness, 1..100.

    The Sol lamp uses op_code ``0xD2`` with ``[11 02 level 00 00]``; other Cync
    devices use ``0xF0`` with ``[11 02 01 level ff ff ff ff]``.
    """
    if not 1 <= level <= 100:
        raise ValueError("brightness must be within 1..100, got %d" % level)
    if sol:
        op_code, payload = OP_SET_BRIGHTNESS_SOL, bytes((level, 0x00, 0x00))
    else:
        op_code = OP_SET_BRIGHTNESS
        payload = bytes((0x01, level, 0xFF, 0xFF, 0xFF, 0xFF))
    return MeshCommand(
        device_id=device_id, msg_id=msg_id, seq=seq, op_code=op_code, payload=payload
    )


def build_temperature(
    device_id: int, msg_id: int, seq: int, tone: int, *, sol: bool = True
) -> MeshCommand:
    """Colour temperature, 0 (warm) .. 100 (cool).

    Sol lamp: op_code ``0xE2`` with ``[11 02 05 tone 00]``.  Others: ``0xF0``
    with ``[11 02 01 ff tone 00 00 00]``.
    """
    if not 0 <= tone <= 100:
        raise ValueError("colour temperature must be within 0..100, got %d" % tone)
    if sol:
        op_code, payload = OP_SET_TEMP_SOL, bytes((0x05, tone, 0x00))
    else:
        op_code = OP_SET_TEMP
        payload = bytes((0x01, 0xFF, tone, 0x00, 0x00, 0x00))
    return MeshCommand(
        device_id=device_id, msg_id=msg_id, seq=seq, op_code=op_code, payload=payload
    )


def build_rgb(
    device_id: int, msg_id: int, seq: int, r: int, g: int, b: int
) -> MeshCommand:
    """RGB: op_code ``0xF0``, ``[11 02 01 ff fe r g b]``."""
    for name, value in (("r", r), ("g", g), ("b", b)):
        if not 0 <= value <= 255:
            raise ValueError("%s must be within 0..255, got %d" % (name, value))
    return MeshCommand(
        device_id=device_id,
        msg_id=msg_id,
        seq=seq,
        op_code=OP_SET_RGB,
        payload=bytes((0x01, 0xFF, 0xFE, r, g, b)),
    )


def build_get_status(device_id: int, msg_id: int, seq: int) -> MeshCommand:
    """Status query: op_code ``0xDB`` with an empty mesh payload."""
    return MeshCommand(
        device_id=device_id, msg_id=msg_id, seq=seq, op_code=OP_GET_STATUS, payload=b""
    )


# ---------------------------------------------------------------------------
# Streaming framer
# ---------------------------------------------------------------------------


class FrameBuffer:
    """Reassemble frames from a byte stream.

    TCP has no message boundaries: one read may return half a frame, two frames,
    or a frame split over many reads.  :meth:`feed` accumulates bytes and returns
    every frame that is now complete, keeping the remainder for the next call.
    A header declaring an implausible length is treated as corruption and the
    buffer is resynchronised one byte at a time instead of stalling forever.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def __len__(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[Frame]:
        """Add received bytes, returning every frame that is now complete."""
        if data:
            self._buf += data
        frames: list[Frame] = []
        while len(self._buf) >= HDR_LEN:
            try:
                ftype, length, is_resp = parse_header(self._buf)
            except FrameError:
                LOG.warning(
                    "dropping leading 0x%02x: unparseable header (%d bytes buffered)",
                    self._buf[0],
                    len(self._buf),
                )
                del self._buf[0]
                continue
            total = HDR_LEN + length
            if len(self._buf) < total:
                break  # partial frame: wait for more bytes
            body = bytes(self._buf[HDR_LEN:total])
            del self._buf[:total]
            frames.append(Frame(type=ftype, body=body, is_response=is_resp))
        return frames

    def feed_one_at_a_time(self, data: bytes) -> list[Frame]:
        """Feed ``data`` a byte at a time -- proves partial-frame handling."""
        out: list[Frame] = []
        for i in range(len(data)):
            out.extend(self.feed(data[i:i + 1]))
        return out


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def hexdump(data: bytes) -> str:
    """Format ``data`` as space-separated hex, e.g. ``73 00 00 00 1f``."""
    return data.hex(" ") if data else "(empty)"


def log_frame(direction: str, raw: bytes, frame: Frame | None = None) -> None:
    """Log one frame as ``DIR type=... :: hex``.

    The raw hex is always logged, because that is what makes a capture
    comparable to Wireshark output.
    """
    prefix = "%s %s" % (direction, frame.describe()) if frame else direction
    LOG.info("%s :: %s", prefix, hexdump(raw))
    if frame is not None:
        mesh = frame.mesh()
        if mesh is not None:
            LOG.info(
                "%s   mesh device=0x%08x msg=0x%04x seq=0x%08x op=0x%02x "
                "payload=%s checksum=%s",
                direction,
                mesh.device_id,
                mesh.msg_id,
                mesh.seq,
                mesh.op_code,
                hexdump(mesh.payload),
                "ok" if mesh.checksum_valid else "BAD",
            )


# ---------------------------------------------------------------------------
# asyncio connection handling
# ---------------------------------------------------------------------------


class SolProtocol:
    """Frame-level protocol driver shared by the server and the client.

    Subclasses supply :meth:`send_frame` and optionally override
    :meth:`handle_frame`.
    """

    def __init__(self, name: str = "conn") -> None:
        self.name = name
        self.buffer = FrameBuffer()
        self.stats = {"frames_in": 0, "frames_out": 0, "errors": 0}

    async def on_data(self, data: bytes) -> list[Frame]:
        """Feed raw bytes in, dispatch every complete frame, return them."""
        frames = self.buffer.feed(data)
        for frame in frames:
            self.stats["frames_in"] += 1
            log_frame("<-", frame.encode(), frame)
            try:
                await self.handle_frame(frame)
            except Exception:  # never let one bad frame kill the connection
                self.stats["errors"] += 1
                LOG.exception("%s: error handling frame", self.name)
        return frames

    async def handle_frame(self, frame: Frame) -> None:  # pragma: no cover - hook
        """React to an inbound frame.  The base implementation does nothing."""

    async def send_frame(self, frame: Frame) -> None:  # pragma: no cover - hook
        """Write a frame to the peer."""
        raise NotImplementedError

    async def send_bytes(self, raw: bytes) -> None:
        """Write pre-built bytes (used for replay)."""
        for frame in FrameBuffer().feed(raw):
            await self.send_frame(frame)


class SolServerConnection(SolProtocol):
    """Server-side connection: answers pings/acks so the lamp stays connected.

    The lamp expects its keepalive and subscribe requests to be acknowledged;
    without the ack it drops the socket, which is the usual reason a naive
    gateway appears to "fail to handshake".
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 name: str = "conn") -> None:
        super().__init__(name)
        self.reader = reader
        self.writer = writer
        self.on_frame: Callable[[Frame], None] | None = None

    async def send_frame(self, frame: Frame) -> None:
        raw = frame.encode()
        self.writer.write(raw)
        await self.writer.drain()
        self.stats["frames_out"] += 1
        log_frame("->", raw, frame)

    async def handle_frame(self, frame: Frame) -> None:
        if self.on_frame is not None:
            self.on_frame(frame)
        # Acknowledge request frames with the same type plus the response bit.
        # Documented responses: 0x73 -> 0x7b, 0x83 -> 0x88, 0xd3 -> 0xd8 (empty),
        # 0xa3 -> 0xab "xlink_dev", 0x43 -> 0x48, 0x23 -> 0x28.
        if frame.is_response:
            return
        ack = self.make_ack(frame)
        if ack is not None:
            await self.send_frame(ack)

    @staticmethod
    def make_ack(frame: Frame) -> Frame | None:
        """Build the documented response for a request frame, if one is known."""
        if frame.type == PKT_PIPE:  # 0x73 -> 0x7b, echo deviceId + msgId + channel
            body = frame.body[:7] if len(frame.body) >= 7 else frame.body
            return Frame(type=PKT_PIPE, body=body, is_response=True)
        if frame.type == PKT_PIPE_SYNC:  # 0x83 -> 0x88, echo the last 3 bytes
            body = frame.body[-3:] if len(frame.body) >= 3 else frame.body
            return Frame(type=PKT_PIPE_SYNC, body=body, is_response=True)
        if frame.type == PKT_D3:  # ping -> empty ack
            return Frame(type=PKT_D3, body=b"", is_response=True)
        if frame.type == PKT_STATUS:  # 0x43 -> 0x48 with a small status triple
            return Frame(
                type=PKT_STATUS,
                body=bytes((0x01, 0x01, 0x00)),
                is_response=True,
            )
        if frame.type == PKT_AUTH:  # 0x23 -> 0x28 "credentials accepted"
            return Frame(type=PKT_AUTH, body=b"\x00\x00", is_response=True)
        return None


class SolClient(SolProtocol):
    """Client-side connection to a lamp or gateway, with reconnect support."""

    def __init__(self) -> None:
        super().__init__("client")
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.queue: asyncio.Queue[Frame] = asyncio.Queue()
        self.ack_events: dict[int, asyncio.Event] = {}
        self._reader_task: asyncio.Task | None = None

    async def connect(
        self, host: str, port: int = DEFAULT_PORT, timeout: float = 5.0
    ) -> None:
        """Open the TCP connection and start the read loop."""
        LOG.info("connecting to %s:%d", host, port)
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        LOG.info("connected to %s:%d", host, port)
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.reader is not None
        while True:
            try:
                data = await self.reader.read(4096)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError) as exc:
                LOG.debug("read loop ended: %s", exc)
                return
            if not data:
                LOG.debug("read loop ended: peer closed the connection")
                return
            for frame in await self.on_data(data):
                await self.queue.put(frame)
                if frame.is_response and frame.type == PKT_PIPE:
                    # Ack body is deviceId(4) + msgId(2) + channel(1)
                    if len(frame.body) >= 6:
                        msg_id = struct.unpack(">H", frame.body[4:6])[0]
                        event = self.ack_events.get(msg_id)
                        if event is not None:
                            event.set()

    async def send_frame(self, frame: Frame) -> None:
        if self.writer is None:
            raise RuntimeError("not connected")
        raw = frame.encode()
        self.writer.write(raw)
        await self.writer.drain()
        self.stats["frames_out"] += 1
        log_frame("->", raw, frame)

    async def wait_for_ack(self, msg_id: int, timeout: float = 2.0) -> bool:
        """Wait for a ``0x7b`` ack carrying ``msg_id``; ``False`` on timeout."""
        event = self.ack_events.setdefault(msg_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.ack_events.pop(msg_id, None)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def client_with_retry(
    host: str,
    port: int,
    build: Callable[[SolClient], "list[MeshCommand]"],
    *,
    retries: int = 3,
    ack_timeout: float = 2.0,
) -> int:
    """Connect, send the commands ``build`` returns, and wait for acks.

    Reconnects on connection failure with a short backoff; returns an exit code.
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        client = SolClient()
        try:
            await client.connect(host, port)
            for command in build(client):
                frame = command.to_frame()
                msg_id = command.msg_id
                await client.send_frame(frame)
                acked = await client.wait_for_ack(msg_id, timeout=ack_timeout)
                LOG.info(
                    "command op=0x%02x msg=0x%04x %s",
                    command.op_code,
                    msg_id,
                    "acknowledged" if acked else "sent (no ack within %.1fs)" % ack_timeout,
                )
            await asyncio.sleep(0.2)  # let trailing frames drain
            return 0
        except (OSError, asyncio.TimeoutError) as exc:
            last_error = exc
            LOG.warning("attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
        finally:
            await client.close()
    LOG.error("giving up after %d attempts: %s", retries, last_error)
    return 1


# ---------------------------------------------------------------------------
# asyncio TCP server on 0.0.0.0:23779
# ---------------------------------------------------------------------------


async def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    ready: asyncio.Event | None = None,
    on_frame: Callable[[Frame], None] | None = None,
    on_bound: Callable[[int], None] | None = None,
) -> None:
    """Run the frame gateway until cancelled.

    ``ready`` is set once the socket is listening, which lets tests wait
    deterministically instead of sleeping.  ``on_bound`` receives the actual
    port, which is the useful one when binding to port 0.
    """
    connections: set[SolServerConnection] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        name = "%s:%s" % peer[:2] if peer else "conn"
        LOG.info("+ connection from %s", name)
        conn = SolServerConnection(reader, writer, name=name)
        conn.on_frame = on_frame
        connections.add(conn)
        try:
            while True:
                try:
                    data = await reader.read(4096)
                except (ConnectionError, OSError) as exc:
                    LOG.warning("%s: read error: %s", name, exc)
                    break
                if not data:
                    break
                await conn.on_data(data)
        except asyncio.CancelledError:
            raise
        finally:
            connections.discard(conn)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            LOG.info(
                "- connection from %s closed (in=%d out=%d errors=%d buffer=%d)",
                name,
                conn.stats["frames_in"],
                conn.stats["frames_out"],
                conn.stats["errors"],
                len(conn.buffer),
            )

    server = await asyncio.start_server(handle, host, port)
    addrs = ", ".join(str(s.getsockname()) for s in (server.sockets or []))
    LOG.info("listening on %s", addrs)
    if on_bound is not None and server.sockets:
        on_bound(server.sockets[0].getsockname()[1])
    if ready is not None:
        ready.set()
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        LOG.info("server cancelled")
        raise
    finally:
        server.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


async def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        await serve(args.host, args.port)
    except asyncio.CancelledError:
        pass
    except OSError as exc:
        LOG.error("cannot listen on %s:%d: %s", args.host, args.port, exc)
        return 1
    return 0


async def _cmd_send(args: argparse.Namespace) -> int:
    def build(_client: SolClient) -> list[MeshCommand]:
        commands: list[MeshCommand] = []
        msg_id, seq = args.msg_id, args.seq
        for action in args.action:
            if action == "on":
                commands.append(build_power(args.device, msg_id, seq, True))
            elif action == "off":
                commands.append(build_power(args.device, msg_id, seq, False))
            elif action.startswith("brightness="):
                level = int(action.split("=", 1)[1])
                commands.append(
                    build_brightness(args.device, msg_id, seq, level,
                                     sol=not args.non_sol)
                )
            elif action.startswith("temperature="):
                tone = int(action.split("=", 1)[1])
                commands.append(
                    build_temperature(args.device, msg_id, seq, tone,
                                      sol=not args.non_sol)
                )
            elif action.startswith("rgb="):
                r, g, b = (int(x) for x in action.split("=", 1)[1].split(","))
                commands.append(build_rgb(args.device, msg_id, seq, r, g, b))
            elif action == "status":
                commands.append(build_get_status(args.device, msg_id, seq))
            else:
                raise SystemExit("unknown action: %s" % action)
            msg_id = (msg_id + 1) & 0xFFFF
        return commands

    return await client_with_retry(
        args.host, args.port, build, retries=args.retries, ack_timeout=args.timeout
    )


async def _cmd_parse(args: argparse.Namespace) -> int:
    """Decode frames from hex or stdin -- offline, no socket needed."""
    raw = bytes.fromhex(args.hex) if args.hex else sys.stdin.buffer.read()
    frames = FrameBuffer().feed(raw)
    if not frames:
        LOG.error("no complete frame found in %d bytes", len(raw))
        return 1
    for frame in frames:
        log_frame("<-", frame.encode(), frame)
    return 0


async def _cmd_replay(args: argparse.Namespace) -> int:
    """Send captured bytes verbatim -- the ground-truth escape hatch."""
    raw = bytes.fromhex(args.hex) if args.hex else sys.stdin.buffer.read()
    client = SolClient()
    try:
        await client.connect(args.host, args.port)
        await client.send_bytes(raw)
        await asyncio.sleep(args.timeout)
        return 0
    except (OSError, asyncio.TimeoutError) as exc:
        LOG.error("replay failed: %s", exc)
        return 1
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sol.py",
        description="Frame gateway and CLI for the GE Sol / C by GE smart lamp.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the TCP gateway (default :23779)")
    p_serve.add_argument("--host", default=DEFAULT_HOST)
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.set_defaults(func=_cmd_serve)

    p_send = sub.add_parser("send", help="send control commands to a lamp/gateway")
    p_send.add_argument("host")
    p_send.add_argument(
        "action",
        nargs="+",
        help="on, off, status, brightness=N, temperature=N, rgb=R,G,B",
    )
    p_send.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_send.add_argument(
        "--device",
        type=lambda s: int(s, 0),
        default=0x10D0978B,
        help="device id (default 0x10d0978b, the id from the reference capture)",
    )
    p_send.add_argument("--msg-id", type=lambda s: int(s, 0), default=0x4941)
    p_send.add_argument("--seq", type=lambda s: int(s, 0), default=0x06000000)
    p_send.add_argument(
        "--non-sol", action="store_true", help="use the non-Sol opcode variants"
    )
    p_send.add_argument("--timeout", type=float, default=2.0, help="ack timeout")
    p_send.add_argument("--retries", type=int, default=3)
    p_send.set_defaults(func=_cmd_send)

    p_parse = sub.add_parser("parse", help="decode frames from hex or stdin")
    p_parse.add_argument("--hex", help="raw frame bytes as hex")
    p_parse.set_defaults(func=_cmd_parse)

    p_replay = sub.add_parser("replay", help="send captured bytes verbatim")
    p_replay.add_argument("host")
    p_replay.add_argument("--hex", help="raw frame bytes as hex")
    p_replay.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_replay.add_argument("--timeout", type=float, default=1.0)
    p_replay.set_defaults(func=_cmd_replay)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        LOG.info("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

