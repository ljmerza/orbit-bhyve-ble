*Contributed by @anahnymous in [issue #19](https://github.com/ljmerza/orbit-bhyve-ble/issues/19); code references adapted to this repository's layout.*

# B-hyve BLE Protocol Reference

This document specifies the Bluetooth Low Energy protocol used to control an Orbit
B-hyve water-valve timer locally, without the Orbit cloud hub. It is the canonical
protocol reference. In this integration the implementation lives in
`custom_components/orbit_bhyve/`: the cipher, handshake, and frame I/O in
`connection.py` (`BHyveBleConnection`), and the protobuf message building and
parsing in `devices/ht34a.py` (`BHyveHT34ADevice`). The full message catalog with
per-field tables and example hex is in [`ble-messages.md`](ble-messages.md); cipher
and key internals are in [`hermes-findings.md`](hermes-findings.md).

> **Scope:** This document covers the protobuf **"XD" family** (BLE frame magic
> `0x11`: HT34A / HT32A / HT25G2). The older HT25 **"d7-47" family** (frame magic
> `0x10`) is documented separately in
> [`findings/d7-47-protocol.md`](findings/d7-47-protocol.md). This integration
> implements a subset of the catalog (run-zone, stop, status request, battery
> request, and status/battery decode); the other commands below are documented for
> completeness.

## Protocol stack

A single logical command travels down four layers on the way out and back up the
same four on the way in:

```
+-- BLE GATT --------------------------------------------------------+
|  Write / notify on fixed characteristics (auth, tx, rx).           |
+-- BLE frame -------------------------------------------------------+
|  0x11 <len:1> <ciphertext> <additive-checksum:2>                   |
|  A large app message is split across several frames.               |
+-- Crypto ----------------------------------------------------------+
|  AES-128-CTR. Each frame payload is XORed with the keystream.      |
|  Nonce and counters come from the session handshake.               |
+-- App message -----------------------------------------------------+
|  aa 77 5a 0f <len:LE16> <protobuf> <crc16_xmodem:LE16>             |
|  This is the unit the device dispatches on. May span >1 frame.     |
+-- Protobuf --------------------------------------------------------+
   An OrbitPbApi_Message. The command = which field is set.
```

## GATT layout

Custom characteristics use the base UUID `0000XXXX-fe32-4f58-8b78-98e42b2c047f`:

| UUID (`XXXX`) | Role | Properties | Notes |
|---------------|------|------------|-------|
| `6c71` | Auth / key setup | write + read | 20-byte write, 20-byte read (see Handshake) |
| `6c72` | Data TX (host -> device) | write-without-response | encrypted `0x11` frames |
| `6c73` | Data RX (device -> host) | notify | encrypted `0x11` frames |
| `6c76` | Write-only | write | unused by local control (OTA firmware path) |

Handle numbers vary per device, so address by UUID rather than handle. Notifications
on the RX characteristic must be enabled by writing `01 00` to its CCCD before the
device will send status frames. A larger-than-default ATT MTU is negotiated so that
frames up to ~100 bytes arrive as a single notification.

## Session handshake and key

### Key

Encryption uses a 16-byte AES-128 `network_key`. One key is generated per mesh
(`network_topology`) at provisioning and shared by every timer in that mesh. It is
**not** derived from the handshake and is not exchanged over BLE. Obtain it from the
Orbit cloud API:

```
GET https://api.orbitbhyve.com/v1/network_topologies    ->    field "network_key" (base64)
```

The response lists one entry per mesh; pick the `network_topology` whose devices
include the target timer and decode its base64 `network_key` to the 16 raw bytes used
as the AES-128 key.

### Handshake

At session start, exchange 20-byte blobs on the auth characteristic (`6c71`):

1. Host writes 20 random bytes (`client_write`).
2. Host reads 20 bytes back (`device_resp`); only the first 4 bytes are significant.

The per-session cipher state is composed from both blobs:

```
composed     = device_resp[0:4] + client_write[4:20]     # 20 bytes
nonce12      = composed[0:12]
counter_h2d  = LE32(composed[12:16])
counter_d2h  = LE32(composed[16:20])
```

After the handshake, enable notifications (CCCD `01 00`); encrypted frames then flow
in both directions.

## Encryption

AES-128 in counter (CTR) mode with a manual little-endian counter block:

```
keystream_block = AES-ECB(key, nonce12 || LE32(counter))
out             = data XOR keystream
counter         = (counter + 1) mod 0xFFFFFFFF
```

The counter advances once per 16-byte block. Host-to-device and device-to-host
traffic use independent counters (`counter_h2d`, `counter_d2h`), each seeded from the
handshake. Because CTR mode XORs a keystream, encrypt and decrypt are the same
operation. Each `0x11` frame payload is encrypted independently.

## BLE frame format

Every write to the TX characteristic and every RX notification is one frame:

```
0x11  <len>  <ciphertext (len bytes)>  <checksum (2 bytes)>
```

- Byte 0 is `0x11`, a constant frame-type marker.
- Byte 1 is the payload length in bytes. Total frame size is `len + 4`.
- The 2-byte trailer is a little-endian 16-bit **additive checksum** over
  `[0x11, len] + plaintext` (the type and length header bytes are included):

  ```
  checksum = (0x11 + len + sum(plaintext_bytes)) & 0xFFFF     # little-endian
  ```

  It is not a CRC. It is computed over the **plaintext** payload, but the frame
  carries the **ciphertext** payload followed by this trailer. The device silently
  drops any host frame whose checksum does not match. Computed inline in
  `BHyveBleConnection.encrypt()` (`connection.py`); there is no standalone checksum
  function.

A single application message may span several frames; concatenate the decrypted
payloads and parse the running buffer.

## Application message

Decrypted frame payloads reassemble into an application message:

```
aa 77 5a 0f  <length:LE16>  <protobuf payload>  <crc16_xmodem:LE16>
```

- `aa 77 5a 0f` is a constant magic preamble.
- `length` equals `len(protobuf) + 2` (the protobuf plus its 2-byte CRC), stored
  little-endian. This is a **length, not a message type**. The device routes purely
  on the protobuf field that is set; the length field is not used for dispatch.
  `_build_message()` (with `MSG_HEADER`) in `devices/ht34a.py` computes this length
  from the payload.
- The inner 2-byte trailer is **CRC-16/XMODEM** (poly `0x1021`, init `0`, no
  reflection) over the protobuf, stored little-endian. This is distinct from the
  outer frame's additive checksum. Implemented by `_crc16_ccitt()` in
  `devices/ht34a.py` (the "CCITT" name notwithstanding, poly `0x1021` + init `0` is
  CRC-16/XMODEM).

The payload is an `OrbitPbApi_Message` protobuf with exactly one field set; that
field selects the command. Field numbers and enum values come from the reconstructed
schema in [`protobuf-schema.md`](protobuf-schema.md). A malformed or unknown protobuf
field gets no reply and desyncs the AES-CTR counter for the remainder of the
connection; reconnect or power-cycle to recover.

### Command and status messages

Identify a message by its `OrbitPbApi_Message` field number:

| Field | Dir | Meaning | Encoder / decoder (this integration) |
|-------|-----|---------|-------------------|
| f18 `setDateTime` | h2d | set clock (ISO-8601 local + offset) | (not implemented in this integration) |
| f75 `setEpochTime` | h2d | set epoch + tz offset (triggers first status burst) | (not implemented in this integration) |
| f15 `getDeviceStatusInfo` (empty) | h2d | request status | `_GET_STATUS_PB` constant (`devices/ht34a.py`) |
| f45 `getBatteryStatus` (empty) | h2d | request battery status | `_GET_BATTERY_PB` constant (`devices/ht34a.py`) |
| f19 `setProgramSchedule` | h2d | store a watering program (zone via `stationInfo`) | (not implemented in this integration) |
| f20 `setActivePrograms` | h2d | enable programs (uint32 bitmask, program A = bit 0) | (not implemented in this integration) |
| f14 `timerMode {mode=autoMode, manualModeParams={}}` | h2d | enable watering / run schedules (empty f2 required) | (not implemented in this integration) |
| f14 `timerMode {mode=manualMode, manualModeParams{stationInfo}}` | h2d | manual run-zone | `_build_start_pb()` (`devices/ht34a.py`) |
| f14 `timerMode {mode=offMode, manualModeParams={}}` | h2d | stop watering (empty f2 required) | `_STOP_PB` constant (`devices/ht34a.py`) |
| f17 `setRainDelay {rainDelayTimeMins, delayEndTimeSecEpochUtc, delayType}` | h2d | rain delay (skip schedules); `mins=0` cancels | (not implemented in this integration) |
| f120 `setScheduledMode` | h2d | seasonal system on/off dates (empty = none) | -- |
| f16 `deviceStatusInfo` | d2h | device status (state, schedule, rain delay) | `BHyveHT34ADevice._parse_status()` (`devices/ht34a.py`) |

The `timerMode` `manualModeParams` submessage (f2) must be present and empty for
`autoMode` and `offMode`; sending only the `mode` field is ignored. Zone / station
ids are 0-indexed (`station_id 0` is zone 1). See [`ble-messages.md`](ble-messages.md)
for the full field tables, example hex, the read-only GET requests, and the
device-to-host messages (`wateringStatus`, `batteryStatus`, `deviceInfo`, and the
`setProgramSchedule` echo).

### deviceStatusInfo decode

`deviceStatusInfo` (`OrbitPbApi_Message` field f16) is the device-to-host status
message. Its length varies with content (a status with no rain-delay or schedule
block is shorter), so detect it by decoding the protobuf, never by the frame length
field. The full `OrbitPbApi_DeviceStatusInfo` submessage is documented below as
protocol reference; this integration's decoder, `extract_status()`
(`devices/status.py`), reads the run state (f1), run progress (f6), fault status
(f7), next-start (f9/f10), rain delay (f13) and battery (f14) — it does not
extract every field in the table:

| Field | Name | Notes |
|-------|------|-------|
| f1 | `deviceStatus` (enum) | `deviceOff`, `deviceIdle`, `lowBattery`, `rainDelayEnabled`, `wateringInProgress` (4), `meshDeviceOffline` |
| f2 | `timerMode.mode` | off / on / program |
| f6 | `wateringStatus` | running station and `time_remaining_sec` |
| f7 | `faultStatus` | empty message means no faults; parsed into the Problem / Leak / No-flow binary sensors |
| f9 | `nextStartProgramFlags` | program bitmask for the next scheduled start |
| f10 | `nextStartTimeSecEpochUTC` | epoch time of the next scheduled start |
| f13 | `rainDelay` | `{mins, end_epoch, type}` |
| f14 | `batteryStatus.batteryLevelMV` | battery level in millivolts |

The outer `OrbitPbApi_Message` also carries f1 `id` (device MAC) and f7
`timestampSecEpochUTC`.

## Setting a schedule

A schedule runs only when three things are in place:

1. **Store** the program: `setProgramSchedule` (f19).
2. **Enable** it: `setActivePrograms` (f20), a uint32 bitmask where program A is bit 0
   (`1 << (programId - 1)`; program A = `1`).
3. **Run mode**: put the timer in `autoMode` via `timerMode {mode=autoMode,
   manualModeParams={}}` (f14). Without autoMode, `deviceStatusInfo.timerMode` stays
   `off` and the schedule does not run.

Saving is fire-and-forget, matching the app: send `setProgramSchedule` then
`setActivePrograms` without blocking on a reply. The device computes a next start
time only when it receives the store and enable while already in autoMode, so the
sequence is: store, enable, autoMode, then re-send store and enable. The device then
pushes an updated `deviceStatusInfo` from which the next start is read (f9/f10). The
optional unsolicited `0x0067`-length schedule frame is a cache-refresh push, not a
required acknowledgement.

There is no schedule read over BLE; `getProgramSchedule` returns nothing and the app
reads schedules from the cloud.

## Rain delay

`setRainDelay` (f17) sets or cancels a rain delay that suppresses scheduled starts.
`rainDelayTimeMins = 0` cancels an active delay. Current delay state is reflected in
`deviceStatusInfo` f13.

## Implementation notes

- The Android app is React Native. The native layer is the generic
  `react-native-ble-manager` bridge and contains no protocol logic; all framing,
  encryption, and message building live in the app's Hermes-bytecode JavaScript
  bundle.
- The BLE service layer is ClojureScript. Encryption uses `aes-js` in CTR mode. The
  key is a random per-mesh `network_key` generated at provisioning.
- The zone-run mode enum is `manual = 0`, `soak = 1`, `rain = 5`.
- An OTA firmware path chunks blocks over the same BLE channel
  (`ota-block-request`, `ota-block-chunk`); it is unused by local control.
- Cloud-only features (weather adjustments, smart-watering restrictions, seasonal
  adjust) produce no BLE writes; the cloud computes them and pushes results down in
  `deviceStatusInfo`.
