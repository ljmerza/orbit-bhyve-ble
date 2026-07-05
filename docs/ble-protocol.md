# BLE Protocol Reference

*Reverse-engineering notes; imported from the maintainer's research project.*

> **See also:** [`protocol.md`](protocol.md) is the wire-verified protocol reference for the protobuf "XD" family and supersedes this document where they differ (notably the inner-message length field — see the note below). This document is kept for the extra behavioral notes (bonding, MAC enforcement, replay, ATT opcode quirks) and the connection-sequence walkthrough.

Technical reference for the Orbit B-Hyve XD BLE protocol as observed and reconstructed during the project. For the narrative of how this was figured out, see [`reverse-engineering-journey.md`](reverse-engineering-journey.md).

## GATT Service & Characteristics

The device advertises one custom GATT service:

| Service UUID | Notes |
|---|---|
| `0000fe32-0000-1000-8000-00805f9b34fb` | Used for HA's BLE auto-discovery |

The service exposes five characteristics. The three used in normal operation are:

| Handle | UUID | Properties | Purpose |
|---|---|---|---|
| 0x0012 | `00006c71-fe32-4f58-8b78-98e42b2c047f` | read, write | AES session initialization (always 20-byte writes) |
| 0x0014 | `00006c72-fe32-4f58-8b78-98e42b2c047f` | write-without-response, write | Encrypted data channel — outgoing (TX) |
| 0x0016 | `00006c73-fe32-4f58-8b78-98e42b2c047f` | notify | Encrypted data channel — incoming (RX, via notifications) |
| 0x0017 | (CCCD for 0x0016) | write | Enable notifications on RX (write `0x0100`) |
| 0x0018 | `00006c76-fe32-4f58-8b78-98e42b2c047f` | write | Unknown — ATT 0x80 (Application Error) for any write without proper auth context |

## Connection Sequence

A working session looks like this:

1. **Connect** to the device's BLE address (no BLE bonding required — the device does not maintain a paired-peer table).
2. **Service discovery**.
3. **MTU negotiation.** The application requests an MTU around 262; the device accepts up to about 672 bytes. In practice 247 is plenty.
4. **AES session init.** Write a 20-byte buffer to characteristic `0x6c71` (handle 0x0012). The structure of the buffer is described in the Encryption section below. Read `0x6c71` back; the device returns a 20-byte response whose first 4 bytes are a session-specific value used to derive the session IV.
5. **Enable notifications on `0x6c73`** by writing `0x0100` to its CCCD (handle 0x0017).
6. **Exchange encrypted frames.** Outgoing on `0x6c72`; incoming on `0x6c73` notifications. Each frame uses the framing described next.

## Frame Format (data channel)

```
+------+--------+--------------------------+----------+----------+
| 0x11 | length | encrypted_payload (length bytes)    | trailer  |
+------+--------+--------------------------+----------+----------+
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ 2 bytes LE
```

- **`0x11`** — fixed magic header byte (decimal 17).
- **`length`** — single byte. Length of the encrypted payload **only**; does not include the trailer.
- **encrypted payload** — `length` bytes of AES-encrypted data (see [`encryption.md`](encryption.md) for the cipher construction).
- **trailer** — 2 bytes, little-endian, content-dependent checksum. Algorithm:
  ```
  trailer_uint16 = (sum(plaintext_bytes) + 0x11 + length) mod 65536
  ```
  Where `plaintext_bytes` is the unencrypted inner message (the bytes that were encrypted to produce the encrypted payload).

Total frame size on the wire = `2 + length + 2` = `length + 4` bytes.

## Inner Message (plaintext) Format

After decryption, the inner message is itself wrapped:

```
+----+----+----+----+--------+------+------+----------------+--------------+
| AA | 77 | 5A | 0F | length |  00  |  00  | protobuf bytes | CRC16-CCITT  |
+----+----+----+----+--------+------+------+----------------+--------------+
```

> **Correction:** later wire analysis (51/51 frames) showed the byte after the `aa 77 5a 0f` magic is a **little-endian uint16 length** (`= len(protobuf) + 2`), not a single length byte followed by two reserved bytes. Read the field as `aa 77 5a 0f <length:LE16> <protobuf> <crc16>`. See [`protocol.md`](protocol.md) for the authoritative layout. The description below reflects the earlier decode.

- **`AA 77 5A 0F`** — 4-byte fixed inner-frame header.
- **`length`** — payload length including the 2 trailing CRC bytes.
- **`00 00`** — reserved.
- **protobuf bytes** — encoded `OrbitPbApi_Message` (or `OrbitPbApi_IpcMsg`); see [`protobuf-schema.md`](protobuf-schema.md).
- **CRC-16 CCITT** — checksum over the protobuf bytes only, using the standard CCITT polynomial `0x1021` and lookup table. In this repo: `_crc16_ccitt()` in `custom_components/orbit_bhyve/devices/ht34a.py`.

## Notes on Behavior

- **No BLE bonding.** The device does not write to the host's `bt_config.conf` paired-devices table. It does not enforce link-layer pairing or LE Secure Connections.
- **No MAC enforcement.** The device does not validate the BLE link-layer source MAC of incoming writes. Confirmed by experiment with a spoofed adapter MAC.
- **ATT Write Command vs Write Request.** The device accepts both for short payloads. For longer payloads (≈ 25+ bytes), the device returns ATT Error 0x80 (Application Error) on Write Request but accepts Write Command. The custom integration uses Write Command for compatibility.
- **Replay protection.** The session-init handshake establishes a per-session IV/counter. Replaying a captured init message from a previous session does not work — the device tracks something across sessions (likely a counter in flash).

## Verifying Your Connection

The simplest live test, after establishing a session:

- Send a timestamp-sync message setting the device clock to a recognizable value (e.g. `2000-01-01T00:00:00Z`). The B-Hyve LCD should immediately update to show that date/time.
- This confirms encryption, framing, trailer, and protobuf encoding are all correct, even if no valve actuates.
