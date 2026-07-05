# d7-47 inner protocol (HT25 hose-tap family)

*Reverse-engineering notes; imported from the maintainer's research project and scrubbed of device-specific identifiers.*

Protocol reference for the older HT25 single-zone hose-tap timers. These devices
speak a **custom binary framing** over a proprietary GATT service — **no
protobuf, no CRC16**. The outer BLE frame is identified by magic byte `0x10` and
protected only by a simple additive checksum. Reversed from 257 decoded frames
across eight capture sessions plus one labelled "zone on for 10 minutes"
session that pinned the start-command shape exactly.

The name "d7-47" is historical. As the [mesh-address prefix](#the-prefix-is-a-mesh-address-not-a-magic)
section explains, `d7 47` is not a protocol constant — it was one device's mesh
address that early code mistook for a fixed header.

---

## Two framing layers

There are two nested frames. Keep them straight — the magic byte `0x10` and the
mesh-address prefix live at different layers.

### Outer BLE frame (on the wire, command characteristic)

```
0x10   [len:1]   [ciphertext: len bytes]   [trailer: uint16_LE]
```

- `0x10` — outer frame magic. Constant for this family.
- `len` — ciphertext length in bytes.
- `ciphertext` — the inner frame (below), encrypted.
- `trailer` — `(sum(plaintext_bytes) + 0x10 + len) & 0xFFFF`, little-endian. A
  plain additive checksum, **not** a CRC16. All decoded captures verify against
  this formula.

The cipher itself (AES-128-ECB used as a CTR-style keystream, IV mixed from the
handshake, independent TX/RX counters) is out of scope here — see the cipher
notes. In this repo the outer wrap/unwrap and checksum live in
`custom_components/orbit_bhyve/connection.py` (`encrypt` / `decrypt`), whose
`frame_magic` and `trailer_const` default to `0x10`.

### Inner plaintext frame

After decryption the payload is:

```
[mesh-addr:2]   [type:1]   [seq:1]   0x40   [payload:N]
```

- `mesh-addr` — the device's own 2-byte mesh address, little-endian. **See the
  next section — this is the field that used to be hard-coded.**
- `type` — 1 byte. Bit `0x40` is **set on responses** (reply/echo). Bit `0x80`
  appears on init-class messages. The remaining bits behave like a
  session-relative message counter that advances monotonically within a single
  BLE connection — it is **not** the message *kind*. Do not switch on it.
- `seq` — 1 byte. **This is effectively the command code.** The same value pairs
  a request to its response (TX `seq=0d` → RX `seq=0d`). Different values mean
  different operations.
- `0x40` — routing/subsystem byte. Constant; no other value has ever been
  observed.
- `payload` — variable, depends on `seq`.

---

## The prefix is a mesh address, not a magic

**Correction to earlier notes.** The leading `d7 47` bytes are **not** a fixed
protocol magic. They are one specific device's `mesh_device_id` written
little-endian:

```
mesh id 18391 = 0x47D7  →  bytes  d7 47
```

Early code hard-coded that one device's value as a "D747 magic" constant. That
worked for the one device it was captured from and **silently failed for every
other device** — a timer whose real mesh address is (for example) `0x1234`
receives frames addressed to `0x47D7`, does not recognise them as its own, and
drops them without error. It acks the handshake but ignores the watering
command.

The correct frame uses **each device's own 2-byte mesh address** as the prefix.
The mesh address comes from the cloud device record (`mesh_device_id`). Treat
`d7 47` / `18391` throughout this document as **one illustrative example only**;
substitute your device's address (shown generically as `<mesh-addr>`) in every
frame.

### In this repo

Implemented per-device in `custom_components/orbit_bhyve/devices/ht25.py`:

- `BHyveHT25Device.mesh_address` returns `mesh_device_id.to_bytes(2, "little")`,
  and raises if the cloud record has no mesh id (rather than falling back to a
  wrong constant).
- `BHyveHT25Device._build(type_byte, seq, payload)` assembles
  `mesh_address + bytes([type, seq, 0x40]) + payload` — the inner frame above.
- `_build_start` / `_build_stop` build the two watering commands on top of
  `_build`.

The device also carries a **hub** mesh address, used only inside the "magic
check" init payloads (below). It resolves per network key; two topologies were
observed with distinct hub mesh ids (shown here generically as
`<hub-mesh-addr>`).

---

## Command / seq catalog

`seq` is the command code. `type` carries the `0x40` response bit and the
session counter, so the same command shows different `type` bytes across a
session; match requests to responses by `seq`.

| seq  | name          | dir | payload (hex)                     | meaning                                                    |
|------|---------------|-----|-----------------------------------|------------------------------------------------------------|
| `05` | BIND          | TX  | `<sid:2> f6 69 10 ff`             | Bind session to the timer                                  |
| `05` | BIND          | RX  | `<sid:2> f6 69 10 ff <status>`    | Bind ack; echoes the session id, trailing byte varies by fw |
| `02` | STATUS        | TX  | `00`                              | Status request                                             |
| `02` | STATUS        | RX  | `01 ffffffff 0000` (idle)         | See [status / is_watering](#status--is_watering-decode)    |
| `03` | INFO          | TX  | `00 00 00 00 00 00 00`            | Device-info request                                        |
| `03` | INFO          | RX  | `<sid:2> f6 69 <mv_LE:2> 00`      | Device info — **payload bytes 4-5 = battery_mV (LE)**      |
| `01` | SUBSYSTEM     | TX  | `00 00 00`                        | Subsystem init                                             |
| `01` | SUBSYSTEM     | RX  | `00 <fw> 00 01 00 00 00`          | byte 1 = **firmware version as a decimal byte**            |
| `00` | MAGIC_CHECK   | TX  | `01 <mesh-addr> 00 00 00 00`      | Self mesh-address ping                                     |
| `00` | MAGIC_CHECK   | TX  | `00 <hub-mesh-addr> 00 00 00 00`  | Hub mesh-address ping                                      |
| `00` | MAGIC_CHECK   | RX  | (echoes both of the above)        |                                                            |
| `09` | HEARTBEAT     | TX  | `00`                              | Heartbeat                                                  |
| `09` | HEARTBEAT     | RX  | `00 00 00 00 7f <b5> 7c`          | byte 5 tracks firmware (see notes); last byte `7c` constant |
| `0d` | WATER_CTRL    | TX  | `04 <dur_LE:2> 00 00 00 00`       | **Start watering — confirmed by labelled capture**        |
| `0d` | WATER_CTRL    | TX  | `02 00 00 00`                     | Stop / cancel                                              |
| `0d` | WATER_CTRL    | RX  | echo with `0x40` reply bit set    | Ack; start-ack echoes the accepted `04 <dur_LE> …`        |
| `0b` | (keep-alive)  | T/R | `00 00 00 00 00 00 00`            | Connection ack / keep-alive                               |
| `0e` | (timer)       | T/R | `e8 03 02 00 [7f fb 7c]`          | Periodic timer echo (`0x03e8` = 1000)                     |
| `0c` | (state push)  | RX  | `02 03 00 00 00 00 00`            | Older watering-state update observation                    |

### Watering command detail

- **Start:** payload `04 <dur_LE_u16> 00 00 00 00`. The leading `04` is a
  constant flag, **not** a zone selector — the HT25 is single-zone, so there is
  nothing to select. The same `04 58 02 00 00 00 00` payload appeared identically
  across five sessions on different days, the strongest "stable command shape"
  signal in the data.
- **Stop:** payload `02 00 00 00`.
- This repo sends START with inner `type = 0xB6` (ack `0xF6`) and STOP with
  `type = 0xB7` (ack `0xF7`); the `0x40` reply bit is what distinguishes an ack
  from the outgoing frame's echo. See `_build_start` / `_build_stop` /
  `_observe_plaintext` in `devices/ht25.py`.

### Duration encoding

Little-endian uint16 seconds:

```
58 02  →  0x0258  =  600 seconds  =  10 minutes  (the app's default manual run)
3c 00  →  0x003c  =   60 seconds
1e 00  →  0x001e  =   30 seconds
```

The same `<dur_LE:2>` field appears in both the START payload and the active
STATUS response, so a started run reports back the duration it accepted.

---

## Init-response sequence (8 steps)

The device is driven through a fixed 8-step handshake before it will accept a
watering command. Each step is a TX frame; the device replies with a matching
frame carrying the `0x40` reply bit. In this repo the sequence is
`BHyveHT25Device._post_handshake` in `devices/ht25.py`, written via
`connection._write_locked` (never `send()` from inside the hook — the connection
lock is already held).

The reply payloads, after the `<mesh-addr> [type] [seq] 40` header. `<sid>` is a
per-session id echoed back; it is random-ish and increments across the two bind
steps.

| Step        | seq  | reply payload (fw0085 example)   | notes                                                                 |
|-------------|------|----------------------------------|----------------------------------------------------------------------|
| bind        | `05` | `<sid> f6 69 10 ff 00`           | echoes session id; trailing byte differs by firmware (`00` vs `ff`)  |
| status      | `02` | `01 ffffffff 0000`               | idle baseline — identical across all devices, no per-device data      |
| info        | `03` | `<sid> f6 69 <mv_LE> 00`         | **battery_mV in payload bytes 4-5**; bytes 0-1 vary per session       |
| subsystem   | `01` | `00 55 00 01 00 00 00`           | **byte 1 = firmware version** (`0x55`=85 → fw0085)                    |
| magic1      | `00` | echoes `01 <mesh-addr> 00000000` | self-address ping                                                    |
| magic2      | `00` | echoes `00 <hub-mesh-addr> 0…`   | hub-address ping; hub mesh id varies by network topology             |
| heartbeat   | `09` | `0000 00 7f fb 7c`               | byte 5 (`fb`) tracks firmware; last byte `7c` constant                |
| rebind      | `05` | `<sid+n> f6 69 10 ff 00`         | re-issues bind with an incremented sid (increment differs by fw)      |

After the rebind ack the device emits **spontaneous pushes** unprompted (count
varies per connection). On fw0041 devices these pushes are byte-for-byte
identical to the subsystem ack (`00 29 00 01 00 00 00`) — a "current
device-state snapshot" broadcast rather than new information.

Firmware differences observed between an fw0085 device and two fw0041 devices:
the bind trailing byte (`00` vs `ff`), the sid increment on rebind, the
subsystem firmware byte (`0x55` vs `0x29`), and heartbeat byte 5 (`0xfb` vs
`0x95`). All are firmware-stable, none are battery.

---

## Status / is_watering decode

The STATUS command (`seq=0x02`) returns a fixed-shape payload. The first byte is
a state flag; the rest is meaningful only while active.

| State    | payload (after routing `0x40`)      |
|----------|-------------------------------------|
| Idle     | `01 ff ff ff ff 00 00`              |
| Watering | `04 c0 95 40 58 02 00`              |
| Watering | `04 80 95 40 58 02 00` (counting down) |

Working byte layout of the payload:

- **byte 0** — state flag: `0x01` idle, `0x04` active.
- **bytes 1-3** — remaining-time counter (24-bit LE; possibly 32-bit including
  byte 4). Idle fills these with `ff ff ff ff` (no active run).
- **byte 4** — `0x40` while active (alignment / part of the counter; unresolved).
- **bytes 5-6** — requested duration uint16_LE. `58 02` = 600 s, matching the
  labelled 10-minute run and the START payload's duration field.
- **byte 7** — `0x00`.

Idle is unambiguous (`01 ffffffff 0000`); active is `04 <countdown> … <dur> 00`.
There is **no standard GATT Battery Service (0x180F)** and no separate watering
service — status must be parsed from this proprietary response.

In this repo `is_watering` is driven optimistically from command stamping plus
the START-ack: `_observe_plaintext` in `devices/ht25.py` watches for the
`seq=0x0D` reply (`0x40` bit set), reads the echoed `04 <dur_LE> …`, and arms an
off-timer for that duration. That makes the auto-off reliable even when the
BLE write-response times out.

---

## Battery decode

The battery voltage rides in the **INFO** response (`seq=0x03`), which is already
part of the init handshake — no separate battery command is needed. (An earlier
investigation wrongly concluded battery was absent from the init responses,
because it read the two voltage bytes as a "session counter" instead of a
little-endian uint16.)

INFO reply payload (7 bytes):

```
<bytes 0-1: session counter>  <bytes 2-3: f6 69>  <bytes 4-5: battery_mV LE u16>  <byte 6: 00>
```

- bytes 0-1 vary session-to-session (counter / random).
- bytes 2-3 are always `f6 69`.
- bytes 4-5 are `battery_mV` as **little-endian uint16** — e.g. `58 02` … no,
  for battery: `38 0b` → `0x0b38` = **2872 mV**.
- byte 6 is always `0x00`.

### Evidence

- One fw0041 device read **2601 mV** over BLE vs **2602 mV** from the cloud
  snapshot (off by 1 mV).
- A second fw0041 device read **2606 mV** vs **2606 mV** (exact).
- An fw0085 device captured across nine sessions over about a week produced a
  **monotonically decreasing** trace — `2872 → 2872 → 2867 → 2861 → 2845 → 2840
  → 2840 → 2840 → 2835 mV` — consistent with battery discharge. Random session
  counters would not behave this way; this is the decisive proof that bytes 4-5
  are voltage.

### In this repo

`custom_components/orbit_bhyve/devices/base.py`:

- `connection.set_plaintext_observer` feeds every decrypted notification to
  `BHyveBleDeviceBase._observe_plaintext`.
- `_observe_plaintext` filters for the info reply (`pt[3] == 0x03`,
  `pt[4] == 0x40`, and the `0x40` reply bit set on `pt[2]`), reads
  `mv = int.from_bytes(pt[9:11], "little")` (payload bytes 4-5), and range-checks
  `1500 ≤ mv ≤ 4000` before accepting it.
- `_mv_to_pct(mv)` converts to a percentage with a linear approximation of the
  cloud's discharge curve: **0 % at 2400 mV, 100 % at 3000 mV**, clamped to
  0-100. Tuned against three live devices (e.g. 2602 mV ≈ 33 %, 2771 mV ≈ 65 %).

Confirmed on both firmwares (fw0085 and fw0041).

---

## Firmware version is a decimal byte

The SUBSYSTEM ack (`seq=0x01`) carries the firmware version in **payload byte 1,
as a plain decimal byte** (not hex-coded, not ASCII):

```
0x55 = 85  →  fw0085
0x29 = 41  →  fw0041
```

Confirmed stable across repeat probes of the same device and consistent across
three devices. Two other bytes track firmware and are explicitly **not** battery
or any per-device varying property:

- **Subsystem byte 1** — firmware version (above).
- **Heartbeat byte 5** — `0xfb` (=251) on fw0085, `0x95` (=149) on fw0041. Exact
  meaning unknown, but constant within a firmware.

Everything that genuinely varies session-to-session (bind/rebind sid bytes, INFO
bytes 0-1) is a session-derived counter, not a device property. The only
per-device fields that are both stable across probes and meaningful are the
firmware byte and the battery voltage.

---

## Open questions

- The active-STATUS counter (bytes 1-4) is not fully decoded — whether it is a
  24-bit or 32-bit remaining-time value, and the role of byte 4 (`0x40` while
  active), needs more captures at different durations.
- Whether any of the 8 init steps are optional (a minimal "connect → start →
  stop" capture would show which acks the device actually requires).
- The write-only characteristic in the custom service is unused; its purpose
  (settings / OTA / query trigger) is unknown and it should not be written
  without deliberate testing.
