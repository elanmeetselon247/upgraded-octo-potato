"""Tests for ``sol.py`` -- the GE Sol lamp frame gateway.

The captured frames below are real wire traffic from a C by GE / Cync device.
They are the ground truth: the encoder must reproduce them byte for byte, and
the parser must read them back without loss.
"""

from __future__ import annotations

import asyncio
import ssl
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import sol

# Real capture, device toggled ON then OFF ~2s apart (SetPowerStateCommand).
POWER_ON = bytes.fromhex(
    "73 00 00 00 1f 10 d0 97 8b 49 41 00 7e 06 00 00 00 f8 d0 0d 00 06 00 00"
    " 00 00 0f 80 d0 11 02 01 00 00 56 7e"
)
POWER_OFF = bytes.fromhex(
    "73 00 00 00 1f 10 d0 97 8b 49 43 00 7e 07 00 00 00 f8 d0 0d 00 07 00 00"
    " 00 00 0f 80 d0 11 02 00 00 00 56 7e"
)

DEVICE_ID = 0x10D0978B
MSG_ON = 0x4941
MSG_OFF = 0x4943
SEQ_ON = 0x06000000
SEQ_OFF = 0x07000000


class TestHeader(unittest.TestCase):
    def test_encode_header_is_big_endian_length(self):
        frame = sol.Frame(type=sol.PKT_PIPE, body=b"\xaa" * 31)
        raw = frame.encode()
        self.assertEqual(raw[0], 0x73)
        self.assertEqual(struct.unpack(">I", raw[1:5])[0], 31)
        self.assertEqual(len(raw), 36)

    def test_response_bit_sets_low_nibble(self):
        self.assertEqual(sol.Frame(type=0x07).type_byte, 0x73)
        self.assertEqual(sol.Frame(type=0x07, is_response=True).type_byte, 0x7B)
        self.assertEqual(sol.Frame(type=0x08, is_response=True).type_byte, 0x88)

    def test_response_bytes_match_documented_values(self):
        # These exact bytes come from the protocol notes / captures; using one
        # arithmetic rule for all channels gets several of them wrong.
        expected = {
            sol.PKT_AUTH: 0x28,
            sol.PKT_STATUS: 0x48,
            sol.PKT_PIPE: 0x7B,
            sol.PKT_PIPE_SYNC: 0x88,
            sol.PKT_D3: 0xD8,
            sol.PKT_A3: 0xAB,
        }
        for ptype, byte in expected.items():
            self.assertEqual(
                sol.Frame(type=ptype, is_response=True).type_byte, byte,
                "response byte for type 0x%X" % ptype,
            )

    def test_request_byte_is_type_nibble_or_3(self):
        for ptype in (sol.PKT_AUTH, sol.PKT_STATUS, sol.PKT_PIPE, sol.PKT_D3):
            self.assertEqual(sol.Frame(type=ptype).type_byte, (ptype << 4) | 0x3)

    def test_response_roundtrip_through_parser(self):
        for raw in (0x7B, 0x88, 0xD8, 0x48, 0x28, 0xAB):
            frame = sol.FrameBuffer().feed(bytes((raw,)) + b"\x00\x00\x00\x00")[0]
            self.assertTrue(frame.is_response, "0x%02x should decode as a response" % raw)
            self.assertEqual(frame.encode()[0], raw)

    def test_parse_header_roundtrip(self):
        for ftype, body, resp in (
            (sol.PKT_PIPE, b"x" * 5, False),
            (sol.PKT_PIPE, b"", True),
            (sol.PKT_STATUS, b"y" * 300, False),
        ):
            raw = sol.Frame(type=ftype, body=body, is_response=resp).encode()
            got_type, length, is_resp = sol.parse_header(raw)
            self.assertEqual((got_type, length, is_resp), (ftype, len(body), resp))

    def test_oversized_length_rejected(self):
        raw = bytes((0x73,)) + struct.pack(">I", sol.MAX_BODY + 1)
        with self.assertRaises(sol.FrameError):
            sol.parse_header(raw)

    def test_oversized_body_rejected_on_encode(self):
        with self.assertRaises(sol.FrameError):
            sol.Frame(type=sol.PKT_PIPE, body=b"\x00" * (sol.MAX_BODY + 1)).encode()


class TestCapturedFrames(unittest.TestCase):
    """The encoder must reproduce real captured traffic exactly."""

    def test_power_on_matches_capture(self):
        cmd = sol.build_power(DEVICE_ID, MSG_ON, SEQ_ON, True)
        self.assertEqual(cmd.to_frame().encode(), POWER_ON)

    def test_power_off_matches_capture(self):
        cmd = sol.build_power(DEVICE_ID, MSG_OFF, SEQ_OFF, False)
        self.assertEqual(cmd.to_frame().encode(), POWER_OFF)

    def test_parses_captured_power_on(self):
        frames = sol.FrameBuffer().feed(POWER_ON)
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(frame.type, sol.PKT_PIPE)
        self.assertFalse(frame.is_response)
        cmd = frame.mesh()
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.device_id, DEVICE_ID)
        self.assertEqual(cmd.msg_id, MSG_ON)
        self.assertEqual(cmd.seq, SEQ_ON)
        self.assertEqual(cmd.op_code, sol.OP_SET_POWER)
        self.assertEqual(cmd.payload, b"\x01\x00\x00")
        self.assertTrue(cmd.checksum_valid)

    def test_capture_roundtrips(self):
        for raw in (POWER_ON, POWER_OFF):
            cmd = sol.FrameBuffer().feed(raw)[0].mesh()
            self.assertEqual(cmd.to_frame().encode(), raw)

    def test_power_state_byte_distinguishes_on_and_off(self):
        on = sol.FrameBuffer().feed(POWER_ON)[0].mesh()
        off = sol.FrameBuffer().feed(POWER_OFF)[0].mesh()
        self.assertEqual(on.payload[0], 1)
        self.assertEqual(off.payload[0], 0)

    def test_command_length_field_is_payload_plus_ten(self):
        cmd = sol.build_power(DEVICE_ID, MSG_ON, SEQ_ON, True)
        body = cmd.encode_body()
        start = body.index(sol.DELIM)
        self.assertEqual(body[start + 7], len(cmd.payload) + 10)

    def test_corrupted_checksum_detected(self):
        bad = bytearray(POWER_ON)
        bad[-2] ^= 0xFF
        cmd = sol.FrameBuffer().feed(bytes(bad))[0].mesh()
        self.assertFalse(cmd.checksum_valid)


class TestChecksumAcrossCaptures(unittest.TestCase):
    """The checksum rule must hold for every captured frame, not just power.

    These two extra captures come from the protocol notes with their checksums
    recorded by the capture author (a different op family and a different
    device), so they prove the rule generalises beyond SetPowerState.
    """

    @staticmethod
    def _body(tail_hex: str, checksum: int, device: int, msg: int, seq: int = 0x06000000) -> bytes:
        """Rebuild a full body around a capture that starts at the 0xF8 marker.

        A real body is ``prefix | ... | checksum | 0x7E``:
        deviceId(4) | msgId(2) | 00 | 0x7E | seq(4) then the marker onward.
        ``tail_hex`` must include the trailing ``0x7E`` delimiter; the checksum
        is inserted immediately before it, which is where the wire puts it.
        """
        prefix = (
            struct.pack(">I", device)
            + struct.pack(">H", msg)
            + b"\x00\x7e"
            + struct.pack(">I", seq)
        )
        tail = bytes.fromhex(tail_hex)
        if not tail.endswith(bytes((sol.DELIM,))):
            raise AssertionError("tail must end with the 0x7E delimiter")
        return prefix + tail[:-1] + bytes((checksum,)) + tail[-1:]

    def test_eight_e_family_capture(self):
        body = self._body(
            "f8 8e 0b 00 20 00 00 00 00 ff ff f7 11 02 21 7e", 0xE2, 0x10D0978B, 0x4941
        )
        self.assertEqual(body.index(0xF8), 12)
        self.assertEqual(sol.compute_checksum(body, len(body) - 2), 0xE2)

    def test_plug_power_capture(self):
        body = self._body(
            "f8 d0 0d 00 21 00 00 00 00 05 00 d0 11 02 01 00 00 7e",
            0xE7,
            0x10D0978B,
            0x4941,
        )
        self.assertEqual(sol.compute_checksum(body, len(body) - 2), 0xE7)

    def test_helper_reproduces_real_captured_body(self):
        """Guards the helper itself: it must rebuild the capture byte for byte."""
        real_body = POWER_ON[5:]
        start = real_body.index(0xF8)
        # Exclude the checksum byte itself; the helper inserts it before 0x7E.
        tail_without_checksum = real_body[start:-2] + real_body[-1:]
        rebuilt = self._body(
            tail_without_checksum.hex(), real_body[-2], DEVICE_ID, MSG_ON, SEQ_ON
        )
        self.assertEqual(rebuilt, real_body)

    def test_power_captures_still_consistent(self):
        for raw in (POWER_ON, POWER_OFF):
            frame = sol.FrameBuffer().feed(raw)[0]
            cmd = frame.mesh()
            self.assertTrue(cmd.checksum_valid)

    def test_checksum_offset_is_the_marker_plus_one(self):
        # The rule reads "from the byte after the 0xF8 marker"; assert that
        # mapping holds so the constant can't silently drift.
        body = self._body(
            "f8 8e 0b 00 20 00 00 00 00 ff ff f7 11 02 21 7e", 0xE2, 0x1, 0x2
        )
        self.assertEqual(sol._BODY_CHECKSUM_OFFSET, body.index(0xF8) + 1)


class TestCommandBuilders(unittest.TestCase):
    def test_brightness_sol_and_non_sol_opcodes(self):
        sol_cmd = sol.build_brightness(DEVICE_ID, 1, 0, 42, sol=True)
        self.assertEqual(sol_cmd.op_code, sol.OP_SET_BRIGHTNESS_SOL)
        self.assertEqual(sol_cmd.payload, bytes((42, 0, 0)))

        cync_cmd = sol.build_brightness(DEVICE_ID, 1, 0, 42, sol=False)
        self.assertEqual(cync_cmd.op_code, sol.OP_SET_BRIGHTNESS)
        self.assertEqual(cync_cmd.payload, bytes((0x01, 42, 0xFF, 0xFF, 0xFF, 0xFF)))

    def test_temperature_sol_and_non_sol_opcodes(self):
        sol_cmd = sol.build_temperature(DEVICE_ID, 1, 0, 70, sol=True)
        self.assertEqual(sol_cmd.op_code, sol.OP_SET_TEMP_SOL)
        self.assertEqual(sol_cmd.payload, bytes((0x05, 70, 0x00)))

        cync_cmd = sol.build_temperature(DEVICE_ID, 1, 0, 70, sol=False)
        self.assertEqual(cync_cmd.payload, bytes((0x01, 0xFF, 70, 0, 0, 0)))

    def test_rgb_payload(self):
        cmd = sol.build_rgb(DEVICE_ID, 1, 0, 0x12, 0x34, 0x56)
        self.assertEqual(cmd.payload, bytes((0x01, 0xFF, 0xFE, 0x12, 0x34, 0x56)))

    def test_vendor_id_prefixed_on_the_wire(self):
        cmd = sol.build_power(DEVICE_ID, MSG_ON, SEQ_ON, True)
        self.assertEqual(cmd.vendor_payload[:2], sol.VENDOR_ID)

    def test_out_of_range_values_rejected(self):
        with self.assertRaises(ValueError):
            sol.build_brightness(DEVICE_ID, 1, 0, 0)
        with self.assertRaises(ValueError):
            sol.build_brightness(DEVICE_ID, 1, 0, 101)
        with self.assertRaises(ValueError):
            sol.build_temperature(DEVICE_ID, 1, 0, 101)
        with self.assertRaises(ValueError):
            sol.build_rgb(DEVICE_ID, 1, 0, 256, 0, 0)

    def test_status_query(self):
        cmd = sol.build_get_status(DEVICE_ID, 1, 0)
        self.assertEqual(cmd.op_code, sol.OP_GET_STATUS)
        self.assertEqual(cmd.payload, b"")


class TestFraming(unittest.TestCase):
    """Partial, glued and corrupted streams -- the part that usually breaks."""

    def test_partial_frame_byte_at_a_time(self):
        buf = sol.FrameBuffer()
        frames = buf.feed_one_at_a_time(POWER_ON)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].encode(), POWER_ON)
        self.assertEqual(len(buf), 0)

    def test_partial_frame_split_in_two(self):
        buf = sol.FrameBuffer()
        self.assertEqual(buf.feed(POWER_ON[:10]), [])
        self.assertEqual(len(buf), 10)
        frames = buf.feed(POWER_ON[10:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].encode(), POWER_ON)

    def test_glued_frames_one_read(self):
        buf = sol.FrameBuffer()
        frames = buf.feed(POWER_ON + POWER_OFF)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].encode(), POWER_ON)
        self.assertEqual(frames[1].encode(), POWER_OFF)

    def test_many_glued_frames(self):
        buf = sol.FrameBuffer()
        frames = buf.feed(POWER_ON * 50)
        self.assertEqual(len(frames), 50)

    def test_glued_with_trailing_partial_retained(self):
        buf = sol.FrameBuffer()
        frames = buf.feed(POWER_ON + POWER_OFF[:12])
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(buf), 12)
        frames = buf.feed(POWER_OFF[12:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].encode(), POWER_OFF)

    def test_empty_feed_is_noop(self):
        buf = sol.FrameBuffer()
        self.assertEqual(buf.feed(b""), [])
        self.assertEqual(len(buf), 0)

    def test_garbage_prefix_is_resynchronised(self):
        buf = sol.FrameBuffer()
        with self.assertLogs("sol", level="WARNING"):
            frames = buf.feed(b"\xff\xff\xff" + POWER_ON)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].encode(), POWER_ON)

    def test_empty_body_frame(self):
        raw = sol.Frame(type=sol.PKT_D3, body=b"").encode()
        self.assertEqual(len(raw), 5)
        frames = sol.FrameBuffer().feed(raw)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].body, b"")

    def test_mesh_absent_returns_none(self):
        self.assertIsNone(sol.parse_mesh(b""))
        self.assertIsNone(sol.parse_mesh(b"\x10\xd0\x97\x8b"))
        self.assertIsNone(sol.parse_mesh(b"\x10\xd0\x97\x8b\x49\x41\x00\x7e\x00\x00"))

    def test_hexdump_format(self):
        self.assertEqual(sol.hexdump(b"\x01\xab"), "01 ab")
        self.assertEqual(sol.hexdump(b""), "(empty)")


class TestServerAcks(unittest.TestCase):
    """The gateway must answer keepalives, or the lamp drops the socket."""

    def test_pipe_ack_echoes_routing(self):
        frame = sol.FrameBuffer().feed(POWER_ON)[0]
        ack = sol.SolServerConnection.make_ack(frame)
        self.assertIsNotNone(ack)
        self.assertEqual(ack.type_byte, 0x7B)
        self.assertTrue(ack.is_response)
        self.assertEqual(ack.body, POWER_ON[5:12])

    def test_ping_ack_is_empty(self):
        ack = sol.SolServerConnection.make_ack(
            sol.Frame(type=sol.PKT_D3, body=b"\x00\x01")
        )
        self.assertEqual(ack.type_byte, 0xD8)
        self.assertEqual(ack.body, b"")

    def test_status_ack(self):
        ack = sol.SolServerConnection.make_ack(
            sol.Frame(type=sol.PKT_STATUS, body=b"\x00" * 10)
        )
        self.assertEqual(ack.type_byte, 0x48)

    def test_auth_ack_is_two_zero_bytes(self):
        ack = sol.SolServerConnection.make_ack(
            sol.Frame(type=sol.PKT_AUTH, body=b"\x00" * 20)
        )
        self.assertEqual(ack.type_byte, 0x28)
        self.assertEqual(ack.body, b"\x00\x00")

    def test_unknown_type_has_no_ack(self):
        self.assertIsNone(sol.SolServerConnection.make_ack(sol.Frame(type=0x0F)))


class TestSocketRoundTrip(unittest.IsolatedAsyncioTestCase):
    """Gateway <-> client over a real TCP socket on an ephemeral port."""

    async def asyncSetUp(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        conn = sol.SolServerConnection(reader, writer, name="test")
        while True:
            data = await reader.read(4096)
            if not data:
                break
            await conn.on_data(data)
        writer.close()

    async def test_client_sends_power_on_and_gets_ack(self):
        client = sol.SolClient()
        await client.connect("127.0.0.1", self.port)
        try:
            cmd = sol.build_power(DEVICE_ID, MSG_ON, SEQ_ON, True)
            with self.assertLogs("sol", level="INFO"):
                await client.send_frame(cmd.to_frame())
                acked = await client.wait_for_ack(MSG_ON, timeout=3.0)
            self.assertTrue(acked, "expected a 0x7b ack echoing the msg id")
        finally:
            await client.close()

    async def test_fragmented_write_is_reassembled_by_server(self):
        """A frame split across two TCP segments must decode as one frame."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(POWER_ON[:7])
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(POWER_ON[7:])
            await writer.drain()
            # 5-byte header + 7-byte ack body (deviceId + msgId + channel).
            ack = await asyncio.wait_for(reader.readexactly(12), timeout=3)
            self.assertEqual(ack[0], 0x7B)
            self.assertEqual(ack[5:12], POWER_ON[5:12])
        finally:
            writer.close()

    async def test_glued_frames_over_socket_produce_two_acks(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(POWER_ON + POWER_OFF)
            await writer.drain()
            data = await asyncio.wait_for(reader.readexactly(24), timeout=3)
            self.assertEqual(data[:12], bytes.fromhex("7b 00 00 00 07 10 d0 97 8b 49 41 00"))
            self.assertEqual(data[12:], bytes.fromhex("7b 00 00 00 07 10 d0 97 8b 49 43 00"))
        finally:
            writer.close()

    async def test_client_retry_gives_up_on_closed_port(self):
        def build(_client):
            return [sol.build_power(DEVICE_ID, MSG_ON, SEQ_ON, True)]

        with self.assertLogs("sol", level="WARNING"):
            code = await sol.client_with_retry(
                "127.0.0.1", 1, build, retries=2, ack_timeout=0.2
            )
        self.assertEqual(code, 1)

    async def test_serve_binds_and_accepts(self):
        """serve() must actually listen, and the port must be discoverable."""
        ready = asyncio.Event()
        bound: list[int] = []
        task = asyncio.create_task(
            sol.serve("127.0.0.1", 0, ready=ready, on_bound=bound.append)
        )
        await asyncio.wait_for(ready.wait(), timeout=5)
        try:
            self.assertEqual(len(bound), 1, "serve() should report its bound port")
            reader, writer = await asyncio.open_connection("127.0.0.1", bound[0])
            try:
                writer.write(POWER_ON)
                await writer.drain()
                ack = await asyncio.wait_for(reader.readexactly(12), timeout=3)
                self.assertEqual(ack[0], 0x7B)
            finally:
                writer.close()
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


class TestTls(unittest.TestCase):
    """TLS is required by real devices, so the context setup must be right."""

    def test_device_ciphers_include_the_negotiated_suite(self):
        # Captured hardware negotiation: ECDHE-RSA-AES256-GCM-SHA384.
        self.assertEqual(sol.DEVICE_CIPHERS[0], "ECDHE-RSA-AES256-GCM-SHA384")

    def test_cloud_hostname_matches_certificate_cn(self):
        self.assertEqual(sol.CLOUD_HOSTNAME, "cm.gelighting.com")

    def test_client_context_does_not_verify_by_default(self):
        # Devices present self-signed certificates via DNS interception.
        ctx = sol.client_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)

    def test_generate_and_load_certificate(self):
        """A generated cert must load into a server context with the right CN."""
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            try:
                sol.generate_self_signed_cert(cert, key)
            except (FileNotFoundError, subprocess.CalledProcessError):
                self.skipTest("openssl not available")
            self.assertTrue(cert.exists() and key.exists())
            ctx = sol.server_ssl_context(cert, key)
            self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
            self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertEqual(ctx.maximum_version, ssl.TLSVersion.TLSv1_2)

    def test_legacy_flag_permits_tls1_0_on_the_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "c.pem", Path(tmp) / "k.pem"
            try:
                sol.generate_self_signed_cert(cert, key)
            except (FileNotFoundError, subprocess.CalledProcessError):
                self.skipTest("openssl not available")
            ctx = sol.server_ssl_context(cert, key, allow_legacy_tls=True)
            self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1)
            self.assertFalse(bool(ctx.options & ssl.OP_NO_TLSv1))


class TestTlsSocketRoundTrip(unittest.IsolatedAsyncioTestCase):
    """Full encrypted round trip: the shape a real device connection takes."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cert = Path(self.tmp.name) / "cert.pem"
        self.key = Path(self.tmp.name) / "key.pem"
        try:
            sol.generate_self_signed_cert(self.cert, self.key)
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.tmp.cleanup()
            self.skipTest("openssl not available")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_control_frame_over_tls_gets_ack(self):
        ctx = sol.server_ssl_context(self.cert, self.key)
        received: list[sol.Frame] = []

        async def handle(reader, writer):
            conn = sol.SolServerConnection(reader, writer, name="tls")
            conn.on_frame = received.append
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await conn.on_data(data)
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
        port = server.sockets[0].getsockname()[1]
        try:
            client = sol.SolClient()
            await client.connect("127.0.0.1", port, tls=True)
            try:
                with self.assertLogs("sol", level="INFO"):
                    await client.send_frame(
                        sol.build_power(DEVICE_ID, MSG_ON, SEQ_ON, True).to_frame()
                    )
                    acked = await client.wait_for_ack(MSG_ON, timeout=5.0)
                self.assertTrue(acked, "expected an ack over TLS")
            finally:
                await client.close()
        finally:
            server.close()
            await server.wait_closed()

        self.assertEqual(len(received), 1)
        mesh = received[0].mesh()
        self.assertEqual(mesh.op_code, sol.OP_SET_POWER)
        self.assertTrue(mesh.checksum_valid)

    async def _handshake_version(self, allow_legacy: bool, version) -> bool:
        """Return True if a client pinned to ``version`` completes a handshake."""
        ctx = sol.server_ssl_context(
            self.cert, self.key, allow_legacy_tls=allow_legacy
        )
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        # A refused handshake is this helper's expected outcome, so keep the
        # loop from printing a traceback for it.
        loop.set_exception_handler(lambda _loop, _ctx: None)

        async def handle(reader, writer):
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
        port = server.sockets[0].getsockname()[1]
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        client_ctx.minimum_version = version
        client_ctx.maximum_version = version
        # The client must also permit old suites, or we would only be measuring
        # the client's own floor rather than the server's.
        client_ctx.set_ciphers("ALL:@SECLEVEL=0")
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port, ssl=client_ctx), 5
            )
            writer.close()
            return True
        except (ssl.SSLError, OSError, asyncio.TimeoutError):
            return False
        finally:
            await asyncio.sleep(0)  # let the server see the close
            server.close()
            await server.wait_closed()
            loop.set_exception_handler(previous_handler)

    async def test_strict_server_rejects_tls1_0(self):
        self.assertFalse(await self._handshake_version(False, ssl.TLSVersion.TLSv1))

    async def test_legacy_server_accepts_tls1_0(self):
        self.assertTrue(await self._handshake_version(True, ssl.TLSVersion.TLSv1))

    async def test_both_accept_tls1_2(self):
        self.assertTrue(await self._handshake_version(False, ssl.TLSVersion.TLSv1_2))
        self.assertTrue(await self._handshake_version(True, ssl.TLSVersion.TLSv1_2))

    async def test_plaintext_client_against_tls_server_does_not_silently_succeed(self):

        """The classic mistake must surface a failure, not appear to work.

        A plaintext client pointed at a TLS port does not get an exception from
        connect(): it blocks waiting for a ServerHello it cannot parse, so the
        connection times out on the first read instead.  Either way the caller
        must see an error rather than a phantom success.
        """
        ctx = sol.server_ssl_context(self.cert, self.key)

        async def handle(reader, writer):
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                with self.assertRaises(
                    (OSError, ssl.SSLError, asyncio.TimeoutError, ConnectionError)
                ):
                    # No data can arrive: the peer is waiting for a handshake.
                    await asyncio.wait_for(reader.read(16), timeout=1.5)
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()


class TestCli(unittest.TestCase):
    def test_parser_accepts_subcommands(self):
        parser = sol.build_parser()
        args = parser.parse_args(["send", "10.0.0.5", "on", "brightness=50"])
        self.assertEqual(args.host, "10.0.0.5")
        self.assertEqual(args.action, ["on", "brightness=50"])
        self.assertEqual(args.port, sol.DEFAULT_PORT)
        self.assertEqual(args.device, 0x10D0978B)

    def test_serve_defaults_to_lamp_port(self):
        args = sol.build_parser().parse_args(["serve"])
        self.assertEqual(args.port, 23779)
        self.assertEqual(args.host, "0.0.0.0")

    def test_parse_command_succeeds_on_capture(self):
        args = sol.build_parser().parse_args(["parse", "--hex", POWER_ON.hex()])
        with self.assertLogs("sol", level="INFO"):
            self.assertEqual(asyncio.run(sol._cmd_parse(args)), 0)

    def test_parse_command_fails_on_garbage(self):
        args = sol.build_parser().parse_args(["parse", "--hex", "ff"])
        with self.assertLogs("sol", level="ERROR"):
            self.assertEqual(asyncio.run(sol._cmd_parse(args)), 1)


if __name__ == "__main__":
    unittest.main()

