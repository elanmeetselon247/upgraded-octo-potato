# Connecting `sol.py` to a real GE Sol lamp

This is the operational half of the project. The protocol and codecs live in
`sol.py`; this document is what you do on your own network to make the lamp talk
to it.

**Read this first:** the lamp is not a server. It never listens on 23779 — it
*dials out* to GE's cloud and holds that socket open. So "it's on the same
Wi-Fi" is necessary but nowhere near sufficient. You have to give the lamp a
reason to connect to you instead.

---

## 0. The four things that actually block people

| Blocker | What happens | Fix |
|---|---|---|
| **TLS, not plaintext** | Lamp connects, handshake fails, no frames | `--tls` (see §2) |
| **DNS still points at the cloud** | Lamp never connects to you at all | DNS redirect (§3) |
| **Certificate CN mismatch** | Device aborts handshake | `certs` uses `cm.gelighting.com` |
| **Device not power-cycled** | Old DNS cached, still talking to cloud | Power-cycle after the redirect |

---

## 1. Find the lamp on your LAN

Windows:

```powershell
arp -a                      # look for the device; note its MAC and IP
ping <lamp-ip>              # confirm it answers
```

Also worth checking your router's client list. Write down the IP and MAC — you
may want to pin the IP via a DHCP reservation so it doesn't move.

## 2. Generate a certificate and start the gateway

Devices expect TLS on 23779, with a certificate for the cloud hostname it
believes it's talking to. Generate a self-signed one:

```powershell
python sol.py certs --cert certs/cert.pem --key certs/key.pem
```

Then run the gateway:

```powershell
python sol.py serve --tls --cert certs/cert.pem --key certs/key.pem -v
```

You should see, in order:

```
TLS enabled with certificate certs/cert.pem
listening on ('0.0.0.0', 23779)
```

When a device connects you'll get a line naming the negotiated cipher:

```
+ connection from 192.168.1.57:50112 (TLS ECDHE-RSA-AES256-GCM-SHA384)
```

That cipher is the one real hardware uses, so seeing it means the handshake is
genuine.

If you'd rather not deal with certificates yet, the gateway also runs in
plaintext (`serve` without `--tls`) — useful for testing the framing logic
locally, but a real lamp will not complete a handshake against it.

## 3. Point the lamp at your machine (DNS redirect)

This is the one hard requirement. The lamp resolves a cloud hostname at boot and
after every network loss. Override that name to your machine's LAN IP, either on
your router or with a local DNS server:

- **Pi-hole**: Local DNS → DNS Records → `cm.gelighting.com` → `<your-ip>`
- **AdGuard Home**: Filters → DNS rewrites → same mapping
- **dnsmasq**: `address=/cm.gelighting.com/<your-ip>`
- **OPNsense/pfSense**: a host override for the same name

**Then power-cycle the lamp.** It caches DNS; without the reboot it keeps its
existing cloud socket and you'll see nothing but an empty log. This single step
is the most common reason a correct setup appears not to work.

**Allow the machine through the firewall** on port 23779. If you block the lamp
from the internet entirely, that's fine — but it must still reach your PC.

## 4. Capture before you trust

Run the gateway and watch. The log prints raw hex for every frame in both
directions, which is directly comparable to Wireshark output:

```
<- type=0x73 (pipe) request body=31 :: 73 00 00 00 1f 10 d0 97 8b 49 41 00 ...
<-   mesh device=0x10d0978b msg=0x4941 seq=0x06000000 op=0xd0 payload=01 00 00 checksum=ok
```

What you learn from the first real capture:

1. **Your lamp's device id** — the 4 bytes after the header. The default
   `0x10d0978b` comes from a reference capture and is almost certainly not yours.
2. **Whether its envelopes match the implemented ones.**

If the generated frames look wrong, you can still drive the lamp immediately by
replaying captured bytes verbatim:

```powershell
python sol.py replay <lamp-ip> --tls --hex "73 00 00 00 1f ..."
```

## 5. Send commands

Once you know your device id:

```powershell
python sol.py send <ip> on   --device 0xYOURID --tls
python sol.py send <ip> off  --device 0xYOURID --tls
python sol.py send <ip> brightness=40 --device 0xYOURID --tls
python sol.py send <ip> temperature=70 --device 0xYOURID --tls
python sol.py send <ip> rgb=255,0,0 --device 0xYOURID --tls
python sol.py send <ip> status --device 0xYOURID --tls
```

A working command logs `acknowledged`. `sent (no ack within 2.0s)` means the
frame went out but the device didn't answer — usually a wrong device id.

---

## Honest status of each command

The power path is proven: the encoder reproduces captured hardware frames byte
for byte, and the tests assert that.

| Command | Opcode | Status |
|---|---|---|
| `on` / `off` | `0xD0` | **Verified** byte-exact against captures |
| `brightness` | `0xD2` (Sol) | Documented opcode, envelope unproven |
| `temperature` | `0xE2` (Sol) | Documented opcode, envelope unproven |
| `rgb` | `0xF0` | Documented opcode, envelope unproven |
| `status` | `0xDB` | Documented opcode, envelope unproven |

"Unproven" means built from the protocol documentation and the confirmed
envelope shape, but not yet checked against real hardware. Compare the hex on
the wire with a capture before relying on them.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No connections at all | DNS redirect not applied, or lamp not power-cycled |
| Connection, then `TLS failure during read` | Certificate rejected / cipher mismatch |
| Connection, TLS fine, zero frames | Wrong port redirected, or different firmware protocol |
| `address already in use` | An earlier run still holds 23779 |
| Frames out, never `acknowledged` | Wrong `--device` id |

Look at packets alongside the log:

```powershell
tshark -i <iface> -f "tcp port 23779" -V
```

## Security notes

- The device protocol carries an account authorization token in its first
  message. Treat any capture as a credential and redact it before sharing.
- The self-signed certificate exists only to satisfy a device that does not
  verify the chain. Do not install it as a trusted root anywhere.
- The gateway binds `0.0.0.0` by default. On a shared network prefer
  `--host <your-lan-ip>` and keep the port firewalled.
- Reverting is just removing the DNS override and power-cycling the lamp; the
  device returns to the cloud on its own.
