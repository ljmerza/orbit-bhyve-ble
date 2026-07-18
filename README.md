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
| Hose-tap timer (Gen2) | `HT25G2-0001` | `0111`          | ✅ Actuated end-to-end (protobuf protocol) |
| 4-port XD         | `HT34A-0001`   | `0107`           | ✅ Battery/status decode verified on hardware; XD actuation proven via HT32A sibling |
| 4-port XD         | `HT34-0001`    | `0058`           | ⚠️ Shares the XD protobuf protocol; not tested here |
| 2-port XD         | `HT32A-0001`   | `0107`           | ✅ Actuated end-to-end (shares the HT34A XD protobuf protocol) |
| Hose-tap timer    | `HT31-0001`    | `0058`           | ✅ Actuated end-to-end (shares the HT34A XD protobuf protocol) |

> ⚠️ **Do NOT update your B-Hyve device firmware.** This integration was
> reverse-engineered against the firmware versions above. A firmware update
> may change the encryption protocol or trailer algorithm. If the official
> B-Hyve app prompts you to update, decline.

## Install via HACS (recommended)

1. **HACS → Integrations → ⋮ menu → Custom repositories**
2. URL: `https://github.com/ljmerza/orbit-bhyve-ble` — Category: **Integration**
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

- **Valve** per physical station (HT25 = 1, HT34A/HT34 = up to 4) — uses
  `valve.open_valve` / `valve.close_valve`. Open/closed state is
  **optimistic** (derived from the last command, not from a decoded
  device status).
- **Battery (%)** sensor — live, BLE-sourced. Decoded from the device's
  info-ack frame on every poll, no cloud round-trip after setup. The percent
  is a linear voltage gauge whose discharge curve is chosen by the **Battery
  chemistry** selector below.
- **Battery voltage (mV)** sensor — same source as the percent sensor;
  disabled by default, enable it from the entity's settings if you want
  the raw reading.
- **Battery chemistry** (`select`, config category) — the AA cell chemistry
  you installed (**Alkaline** / **Ni-MH rechargeable** / **Lithium primary**
  / **Lithium regulated 1.5 V**), which selects the voltage→percent curve.
  Chemistry can't be auto-detected from a single voltage reading (2550 mV
  could be alkaline at 25% or Ni-MH at 80%), so it defaults to Alkaline
  (Orbit's stock assumption) and you set it per device; changing it re-gauges
  the percent immediately, no device round-trip. **Note:** regulated 1.5 V
  Li-Ion cells output a constant voltage until they abruptly cut off, so no
  percentage gauge can track them — the percent reads full right up until the
  valve dies. Watch the voltage sensor instead.
- **Signal strength (RSSI)** sensor — the BLE advertisement RSSI from
  Home Assistant's bluetooth manager (works even while disconnected);
  disabled by default.
- **Connected** and **Watering** binary sensors — device connectivity
  (diagnostic) and whether a station is currently running, for automations
  and dashboards.
- **Problem** binary sensor (protobuf family) — the device's own fault
  report: pump, battery, voltage-boost, flow anomalies and per-station
  faults, with each flag as an attribute. On flow-capable Gen2 valves two
  dedicated sensors ride along: **Leak detected** (water moving while the
  valve is commanded closed) and **No flow** (valve open but nothing
  flowing; disabled by default until field-confirmed — you can test it by
  starting a run with the faucet closed).
- **Default watering duration** (`number` entity, minutes) — per device.
  The valve uses this when `start_watering` is called without an
  explicit duration. Restored across HA restarts.
- **Sync** button per device — forces a fresh BLE connect + init
  handshake. Useful after a long idle, or to refresh the battery
  reading on demand without waiting for the next poll.
- **Identify** button (protobuf family) — flashes the device LED (#47)
  so you can physically locate it. No-op on the XD, which ignores it.
- **Automatic watering** switch — the device-global controller mode
  (auto vs. off). When off, stored programs don't run.
- **Program A–D** enable switches + summary sensors — each stored
  program slot's on/off enable bit, plus a sensor showing its schedule
  (days, start times, per-zone durations). A slot with no stored program
  reads `unavailable`.
- **Next run** sensor — the device-computed next scheduled program start
  (timestamp), with the program letter(s) as an attribute.
- Manufacturer / model / firmware / MAC are exposed via the device's
  "Device info" panel.
- **Download diagnostics** — the integration and each device support HA's
  standard diagnostics download (Settings → Devices & Services → ⋮ →
  Download diagnostics): device records, live state and poll health, with
  credentials and BLE network keys redacted. Attach it to bug reports.

Hubs (`BH1-0001`) are filtered out at discovery — they don't actuate
anything, so they don't appear in the device picker or the device
registry.

## Services

- `orbit_bhyve.start_watering` — `entity_id` + optional `duration` (sec)
- `orbit_bhyve.stop_all` — stop everything on the targeted device
- `orbit_bhyve.refresh_devices` — re-query the cloud (for new devices, key
  rotation, or fw changes); manual, no background polling
- `orbit_bhyve.get_program` — read back a program slot (A–F) as structured
  data (`SupportsResponse.ONLY`): days, start times, per-zone durations
- `orbit_bhyve.set_program` — store/replace a program in a slot: watering
  days (weekdays / even / odd / every-N-days), start time(s), per-zone
  run durations. Optionally enable it in the same call
- `orbit_bhyve.delete_program` — clear a program slot

> **Watering programs are for non-Smart slots.** The device stores up to six
> program slots; this integration reads and writes their schedules over BLE.
> Slots are read back from the device's `#10` sync dump (a multi-frame reply
> reassembled over the connection), so `get_program` and the Program sensors
> reflect what's actually stored — including programs created in the B-Hyve
> app. For richer scheduling logic, drive `start_watering` from
> `irrigation_unlimited` or HA's Smart Irrigation instead.

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
3. **Reuse**: the connection stays open across commands until idle timeout.
   Watering commands re-run the model init/bind first — the device silently
   ignores a watering frame sent on a stale bind — while reads reuse the
   pooled session directly. Marginal proxy links get a bounded handshake with
   a few clean retries instead of a wedged connection.

The cipher (AES-128-ECB used as a CTR-style keystream, frame trailer =
`sum(plaintext) + magic + len`) was reverse-engineered against captured
phone-app traffic. Different hardware families (HT25 vs HT34A) use different
inner plaintext formats and different magic bytes; the per-model device
classes encode that. Adding a new model = drop a `devices/htXX.py` and
register it.

## Credits

The marginal-link connection hardening (bounded handshake + retry, capped
write-ack), the per-command re-bind for HT25 actuation, the RSSI and
connectivity/watering sensors, and the HT34/HT34A battery + watering-status
decode were ported from
[@stuartdenne](https://github.com/stuartdenne/ha-orbit-bhyve-ble-old)'s fork.

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
