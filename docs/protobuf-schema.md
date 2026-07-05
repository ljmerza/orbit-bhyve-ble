# Reconstructed Protobuf Schema

*Reverse-engineering notes; imported from the maintainer's research project.*

The Orbit B-Hyve XD messages on the BLE data channel are protobuf. This document records how that schema was **reconstructed by observation** — it is not a copy of any vendor source code. This integration does **not** ship a `.proto` file or depend on the `protobuf` runtime; it encodes/decodes the wire format by hand with small helpers in `custom_components/orbit_bhyve/devices/ht34a.py` (`_pb_varint`, `_pb_field_varint`, `_pb_field_bytes`, `_rd_varint`, `_pb_fields`). The full reconstructed field/enum tables live in [`ble-messages.md`](ble-messages.md).

## How It Was Reconstructed

The schema was assembled from the following observations:

1. **Wire-format inspection.** Decrypted plaintexts (see [`encryption.md`](encryption.md)) were fed to `protoc --decode_raw` to dump field numbers and wire types. This produces a structural skeleton (e.g. `1: 0, 2: 600, 14: { ... }`) without field names.
2. **Behavioral inference.** Field numbers were correlated with observed device behavior — sending a message with field 1 set to N caused station N to actuate, sending field 2 set to S caused the run time to be S seconds, etc.
3. **Cross-referencing observable strings.** Where the official mobile application exposes message-class names through public Android APIs (e.g. via `android.util.Log` traces or `getClass().getSimpleName()` calls visible in BLE-write contexts), those names were used to label the corresponding reconstructed messages. Names like `OrbitPbApi_Message`, `TimerMode`, `ManualModeParams`, and `StationInfo` were obtained this way.
4. **Empirical validation.** The reconstructed schema was used to *encode* messages and send them to the device. If the device responded as expected, the field interpretation was validated. If not, the schema was adjusted until behavior matched.

The schema was therefore built from a combination of:

- Wire-level traffic that any owner of the device can capture with standard Android developer tools (HCI snoop log).
- Public-API-observable class names from the mobile application running on the project authors' own hardware.
- Targeted experiments with the device the project authors lawfully own.

No proprietary firmware or vendor source code was redistributed.

## Coverage

The reconstructed schema covers what is needed to control a B-Hyve XD locally:

- `OrbitPbApi_Message` — the top-level message exchanged on the BLE data channel.
- `TimerMode` and `ManualModeParams` — for valve activation.
- `StationInfo` — to specify which valve and for how long.
- `DeviceControl` — for stop-watering and skip-current-station commands.
- `BleInitMsg` — used in the IPC top-level wrapper that surrounds device messages.

Many top-level fields exist in the protocol that this project did **not** reverse — anything related to scheduling, weather, sensors, programs, etc. — because they were not needed to satisfy the project's goal of HA-integrated manual zone control. If you extend this work, the schema will likely need extending too.

## Limitations of This Schema

- **Field names are approximations.** Where a name is not observable from public APIs, the schema uses descriptive English names (e.g. `runTimeSec` rather than the unknown internal token).
- **Field types may be looser than the vendor's.** The vendor likely uses `required` on many fields where this schema uses optional fields, because over-permissive types do not affect interoperability and are safer when names/constraints are uncertain.
- **No oneOf or oneof reconstruction.** Where the vendor likely uses `oneof` to group mutually-exclusive options, this schema uses optional fields. The device tolerates this.
- **Enum values were determined empirically** — `mode = 2` for manual mode was found by experiment, not extracted from any vendor source.

## Working With the Schema

This integration builds the protobuf wire-format **manually** and avoids the `protobuf` runtime dependency entirely: the `_pb_varint` / `_pb_field_varint` / `_pb_field_bytes` encoders and the `_rd_varint` / `_pb_fields` decoders in `custom_components/orbit_bhyve/devices/ht34a.py` emit and parse fields directly. That approach is simpler to vendor into a Home Assistant custom component and does not break across `protoc` versions.

If you would rather work from a generated `.proto` (as the original research project did), write one from the field/enum tables in [`ble-messages.md`](ble-messages.md) and generate bindings with:

```bash
pip install protobuf
protoc --python_out=. orbit_ble.proto   # produces orbit_ble_pb2.py
```
