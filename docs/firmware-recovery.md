# HT25 firmware recovery (bootloader mode)

An HT25 hose-tap timer whose firmware update failed blinks **white every ~2
seconds** and stops responding to the app, the hub, and this integration. It is
stuck in its **bootloader**, not dead: re-sending the official firmware over BLE
brings it back. This procedure and protocol were worked out by @CoderBananna in
[issue #61](https://github.com/ljmerza/orbit-bhyve-ble/issues/61), who
recovered two units this way.

Code: [`custom_components/orbit_bhyve/firmware.py`](../custom_components/orbit_bhyve/firmware.py)
(protocol) and [`scripts/recover_firmware.py`](../scripts/recover_firmware.py)
(CLI).

## Scope — read this first

- **Verified on:** two `HT25-0000` units, image `fw0085` (52,874 bytes). After
  recovery, both kept their mesh credentials (no re-pairing needed) and watered
  when tested.
- **Not verified:** other hardware (HT25A/G2, XD), other image sizes, other
  bootloader revisions. The image-validation-failed status (2) has never been
  seen on hardware.
- **Only for stuck timers.** The CLI refuses to flash a device that isn't in
  bootloader mode. Do not use this to update a working timer.
- Disconnect the timer from water while recovering.
- A recovered unit can still have a separate hardware fault (one unit in #61
  had a failed valve actuator). Confirm water actually flows before you trust
  it. A green LED or an "open" state is not proof.

## Using the CLI

Needs a machine with a BLE adapter near the timer, plus `bleak` and `aiohttp`.
No Home Assistant needed.
Pass `--adapter hci1` (before the subcommand) to use a specific BlueZ adapter
instead of the system default. `hciconfig` lists them.

```bash
# 1. Find it. A stuck unit still advertises the 0xfe32 service.
python3 scripts/recover_firmware.py scan

# 2. Confirm it's in the bootloader (no 6c71 AES characteristic).
python3 scripts/recover_firmware.py probe AA:BB:CC:DD:EE:FF

# 3. Download the official image with your B-hyve account.
#    Password comes from $BHYVE_PASSWORD or a prompt.
python3 scripts/recover_firmware.py fetch --email you@example.com --out ht25-fw0085.bin

# 4. Send it. Use --no-install first if you want to stop after validation.
python3 scripts/recover_firmware.py flash AA:BB:CC:DD:EE:FF --image ht25-fw0085.bin --version 85
```

`flash` checks the image against the known-good SHA-256 before sending it. It
refuses unknown images unless you pass `--allow-unknown-image`. If a transfer is
interrupted, rerun the same command: it resumes from the offset the device
reports. It only installs once the device reports that the full image was
received and validated. Afterwards it reconnects and checks the timer is back
in normal (AES) mode.

## Protocol

The bootloader advertises the same service `0000fe32-…` with write
characteristic `6c72` and read/notify characteristic `6c73`. **`6c71` (AES) and
`6c76` are absent and nothing is encrypted.** That missing `6c71` is how to
recognise a stuck timer. Subscribe to `6c73` before writing. All writes are
Write Requests (with response). The device reports a rejected command as an
ATT error.

```
frame    = flag:u8 | len:u8 | payload | checksum:u16LE
checksum = sum(flag, len, payload) & 0xffff
```

| Flag | Payload | Meaning |
|------|---------|---------|
| `0x12` | `u16LE(0)` | Read progress |
| `0x12` | `u16LE(1) + u16LE(version) + u32LE(size)` | Start update |
| `0x12` | `u16LE(2)` | Install validated image |
| `0x13` | up to 16 raw image bytes | Image data, sent in order, no address, last block not padded |

### Progress reply

An 18-byte frame: header `12 0e`, then a 14-byte payload and the checksum.

| Payload offset | Field | Encoding |
|---|---|---|
| 0 | command | u16LE (0) |
| 2 | status | u16LE: 0 idle, 1 receiving, 2 validation failed, 3 validation passed |
| 4 | version | u16LE |
| 6 | size | **two u16LE words, high word first** |
| 10 | received offset | ordinary u32LE |

⚠️ The size and offset fields use different word orders. For 52,874 bytes, size
arrives as `00 00 8a ce` but the final offset arrives as `8a ce 00 00`. Do **not**
word-swap the size in the *start* command: the device rejects that with ATT
Write Not Permitted.

```
idle                       12 0e 0000 0000 0000 00000000 00000000 2000
receiving v85, offset 0    12 0e 0000 0100 5500 00008ace 00000000 ce01
after first 16 bytes       12 0e 0000 0100 5500 00008ace 10000000 de01
complete, validation ok    12 0e 0000 0300 5500 00008ace 8ace0000 2803
```

### Sequence

1. Connect, subscribe to `6c73`, read progress.
2. Start update with the image's version and size. Expect status 1, offset 0.
3. Send 16-byte data frames in order. Check progress after the first block and
   periodically, and abort if the offset is wrong.
4. Require status 3 with offset = size, then send install. The link drops as
   the device reboots.
5. Reconnect. The normal GATT layout (`6c71` present) should be back.

A reconnect in mid-transfer kept the receiving state and offset (observed
once). A battery pull reset it to idle. After a write fails with no clear
result, read the device's offset before resending anything.

### Where the image comes from

`GET https://api.orbitbhyve.com/v1/device_updates/hardware_version/ht25-0001`
(authenticated) returns metadata plus a temporary download URL. The app maps
**`HT25-0000` devices to `ht25-0001`** on this path. Only that mapping is
verified. No firmware binaries live in this repository.

| Image | Size | SHA-256 |
|---|---|---|
| `ht25-hw0001-fw0085` | 52,874 | `adc4002dc1830f27aa02001c47490cf0ecd6a09ccaeee95ea1d015b7f126abb3` |
