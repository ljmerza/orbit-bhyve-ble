# Orbit B-Hyve BLE — Home Assistant integration

**Local BLE control for Orbit B-Hyve hose-tap and XD timers.** Cloud is
contacted only at setup to discover devices and fetch network keys. After
setup, every command and state poll is BLE-only — your timers keep working
when the WAN goes down.

## Supported hardware

| Family            | Hardware       | Firmware tested | Status                                  |
|-------------------|----------------|------------------|------------------------------------------|
| Hose-tap timer    | `HT25-0000`    | `0085`           | ✅ Actuated end-to-end                   |
| Hose-tap timer    | `HT25-0000`    | `0041`           | ✅ Actuated end-to-end (per-device mesh-ID addressing) |
| 4-port XD         | `HT34A-0001`   | `0107`           | ⚠️ Ported from upstream; not tested here|

> ⚠️ **Do NOT update your B-Hyve device firmware.** This integration was
> reverse-engineered against the firmware versions above. A firmware update
> may change the encryption protocol or trailer algorithm. If the official
> B-Hyve app prompts you to update, decline.

## Install via HACS (recommended)

1. **HACS → Integrations → ⋮ menu → Custom repositories**
2. URL: `https://github.com/ljmerza/bhyve-hack` — Category: **Integration**
3. Click **Install** on **Orbit B-Hyve BLE**
4. Restart Home Assistant
5. **Settings → Devices & Services → Add Integration → "Orbit B-Hyve BLE"**
6. Enter your Orbit cloud email + password — the integration discovers all
   devices on the account and fetches each one's BLE network key

## Manual install

1. Copy `custom_components/orbit_bhyve/` into `<config>/custom_components/`
2. Restart HA
3. **Settings → Devices & Services → Add Integration → "Orbit B-Hyve BLE"**

## What you get

Per discovered sprinkler device:

- **Valve** per physical station (HT25 = 1, HT34A = up to 4) — uses
  `valve.open_valve` / `valve.close_valve`. Open/closed state is
  **optimistic** (derived from the last command, not from a decoded
  device status).
- **Battery (%)** sensor — live, BLE-sourced. Decoded from the device's
  info-ack frame on every poll, no cloud round-trip after setup.
- **Battery voltage (mV)** sensor — same source as the percent sensor;
  disabled by default, enable it from the entity's settings if you want
  the raw reading.
- **Default watering duration** (`number` entity, minutes) — per device.
  The valve uses this when `start_watering` is called without an
  explicit duration. Restored across HA restarts.
- **Sync** button per device — forces a fresh BLE connect + init
  handshake. Useful after a long idle, or to refresh the battery
  reading on demand without waiting for the next poll.
- Manufacturer / model / firmware / MAC are exposed via the device's
  "Device info" panel.

Hubs (`BH1-0001`) are filtered out at discovery — they don't actuate
anything, so they don't appear in the device picker or the device
registry.

## Services

- `orbit_bhyve.start_watering` — `entity_id` + optional `duration` (sec)
- `orbit_bhyve.stop_all` — stop everything on the targeted device
- `orbit_bhyve.refresh_devices` — re-query the cloud (for new devices, key
  rotation, or fw changes); manual, no background polling

## Options flow

- **Default watering duration** (sec) — used when `start_watering` is called
  without an explicit duration
- **Disconnect after idle** (sec) — pooled BLE connection closes after this
  many seconds idle to free the proxy slot
- **Polling interval — idle** (sec) — how often to refresh state when no
  station is watering
- **Polling interval — watering** (sec) — faster polling while a station is
  active

## How it works

1. **Setup**: log into Orbit cloud once → fetch device list → fetch one AES
   network key per mesh → cache everything in the config entry
2. **Per command**: the integration's pooled BLE connection (one per device)
   does an AES handshake, runs the model-specific init sequence on first
   connect, then sends one encrypted frame per command and reads back
   notifications
3. **Reuse**: the connection stays open across commands until idle timeout;
   subsequent commands skip the handshake and init, going directly to the
   command frame

The cipher (AES-128-ECB used as a CTR-style keystream, frame trailer =
`sum(plaintext) + magic + len`) was reverse-engineered against captured
phone-app traffic. Different hardware families (HT25 vs HT34A) use different
inner plaintext formats and different magic bytes; the per-model device
classes encode that. Adding a new model = drop a `devices/htXX.py` and
register it.

## Brand icons

Source artwork lives in `assets/` and can be regenerated with
`python3 assets/generate_icons.py`. The variants HACS and the HA UI
actually display come from
[home-assistant/brands](https://github.com/home-assistant/brands) under
`custom_integrations/orbit_bhyve/` — until that PR is merged, HACS
shows the default integration icon.

## Legal & ethical notice

This project documents the protocol of a device the project authors
lawfully purchased and own. Reverse engineering for the purpose of
interoperability with hardware you own is protected in the United States
under 17 U.S.C. §1201(f). The protocol descriptions in this repository
were reconstructed from observation of the device's wire-level BLE traffic
and from analysis techniques applied to the publicly distributed companion
mobile application. The authors are not affiliated with Orbit Irrigation
Products Inc.

[MIT](LICENSE).
