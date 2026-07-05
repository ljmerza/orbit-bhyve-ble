# APK reverse-engineering findings — official B-Hyve app v3.0.53

*Reverse-engineering notes; imported from the maintainer's research project and scrubbed of device-specific identifiers.*

## TL;DR

**The official app writes to a fourth GATT characteristic, `0x6c76`
(`network_char_uuid`), during its connect-and-prepare flow, BEFORE the AES
handshake.** At the time this was written, the working theory was that this write
*provisions* the device with the network key and that a missing provisioning step was
why valves don't actuate even though the AES handshake succeeds and encrypted command
writes are accepted.

> **Correction (later finding):** The "provision by writing the network key to
> `0x6c76`" conclusion in this document was **disproven**. Every device tested returns
> **"Write not permitted"** on `0x6c76`, so the app cannot be writing the key there over
> BLE. The network key is **per-account and fetched from the cloud** — it is never
> written to the device over BLE. Read the discovery of the 4th characteristic and the
> `provision → init_aes → subscribe` sequence below as accurate *observations of the
> decompiled call graph*, but treat the "you must write the key to `0x6c76` to actuate"
> claim as false. The characteristic exists and is referenced by the app, but it is not
> a client-writable key-provisioning channel.

The discovery of the 4th characteristic itself was the missing piece the BLE captures
could never show us — the phone uses RPAs and the single-channel sniffer couldn't follow
its connections, so we never observed this part of the sequence directly.

## How we got here

- APK: `com.orbit.orbitsmarthome` v3.0.53 (versionCode 1448), 70 MB single-ABI variant from APKMirror.
- App is **React Native + ClojureScript** (re-frame). All BLE protocol logic is in `assets/index.android.bundle` (16.4 MB Hermes bytecode). Java side has zero Orbit business logic — only stock `react-native-ble-manager` (`it.innove.BleManager`) and four boilerplate classes.
- Decompiled with `hbc-decompiler` (P1sec/hermes-dec, in a Python venv). Output: `bundle_decompiled.js` (98 MB, 2.77M lines of computed-goto JS pseudocode).

## The four GATT characteristics

Verbatim from the decompiled bundle, lines 1016769–1016865:

| Characteristic UUID | Source name | Our code |
|---|---|---|
| `00006c71-fe32-4f58-8b78-98e42b2c047f` | `aes_char_uuid` | `AES_CHAR` ✓ |
| `00006c72-fe32-4f58-8b78-98e42b2c047f` | `write_char_uuid` | `WRITE_CHAR` ✓ |
| `00006c73-fe32-4f58-8b78-98e42b2c047f` | `read_char_uuid` | `READ_CHAR` ✓ |
| `00006c76-fe32-4f58-8b78-98e42b2c047f` | **`network_char_uuid`** | **discovered here** |

Service UUID base is `fe32` → full service UUID is `0000fe32-0000-1000-8000-00805f9b34fb` (`SERVICE_UUID` ✓).

Other constants from the same module:
- `max_message_size` = 255
- (`default_tx_delay_ms`, `encryption_frame_size`, `our_protocol_overhead`, `ble_protocol_overhead` — values are register-relative; not yet decoded but secondary.)

## The connection sequence

The official app's connect-and-prepare-for-commands flow, mapped from decompiled functions in `lib.ble.service.common`:

```
provision_device!(peripheral, network_key_buf):
    BleManager.write(peripheral, service_uuid, network_char_uuid,
                     [01 00] ++ network_key_buf)
    # 2-byte LE prefix (default = 1) + 16-byte network key

init_aes(peripheral):
    BleManager.write(peripheral, service_uuid, aes_char_uuid, init_payload_20B)
    response = BleManager.read(peripheral, service_uuid, aes_char_uuid)
    iv, counter = derive_session_iv(init_payload, response)

subscribe_read_char(peripheral):
    BleManager.startNotification(peripheral, service_uuid, read_char_uuid)

init_network(args):
    if NOT app_db_marks_provisioned(peripheral):
        provision_device!(peripheral, base64_decode(stored_b64_key))
    init_aes(peripheral)
```

> **Correction (later finding):** the `provision_device!` write shown above targets
> `0x6c76`, which real devices reject with "Write not permitted." The call exists in the
> decompiled app, but it does not succeed as a key-provisioning write over BLE. The
> `init_aes` → `subscribe_read_char` ordering below is still a useful observation.

Source line refs in the decompiled bundle (`bundle_decompiled.js`):
- `provision_device!` — defined L1017723–1017942, written at L1017814
- `init_aes` — defined L1017277–1017711, write at L1017620, read at L1017309
- `subscribe_read_char` — defined L1017265
- `init_network` — defined L1017954–1018156, calls provision at L1018106, then init_aes at L1018120

## What's written to `network_char_uuid`

Decompiler output for the data payload assembly (L1017766–1017778):

```
short_LE = (slot2547(args) if truthy else 1)              # 2-byte little-endian short
key_vec  = buffer_to_vec(network_key_buffer)              # 16 bytes
data     = clj_to_js(concat([short_LE], key_vec))         # final 18-byte payload
BleManager.write(peripheral, service_uuid, network_char_uuid, data)
```

Payload shape: `01 00 || <16-byte network_key>` → 18 bytes total. The `slot2547` value
defaults to `1` when the args map doesn't carry an alternate (a versioning/key-id hint).

> **Correction (later finding):** this 18-byte write is what the app *constructs*, but
> the device does not accept it on `0x6c76` ("Write not permitted"). Do not implement a
> key-write step against this characteristic — it will fail. The network key is obtained
> from the cloud account, not written to the device.

## Provisioning is per-app-session (as coded in the app)

`init_network` checks `db.read([slot2617, address, slot6158])` — re-frame app-db keyed
by device address. App-db is in-memory (not persistent across app launches). So, as the
app is written:

- Phone app: would provision on first connection per app launch, then skip on subsequent connections within the same launch.
- (This whole branch is moot given the correction above — the write is rejected by the device regardless.)

## What this section originally claimed (superseded)

> **Correction (later finding):** the reasoning in this subsection is built on the
> disproven "provisioning gates commands" theory. It is retained for the record but is
> not correct.

The original theory was:

- Direct BLE connection works (we observed connection success in HA logs).
- AES handshake succeeds (the AES init exchange uses the encryption key correctly).
- WRITE_CHAR writes are *accepted* (no GATT error).
- But the valve never actuates → the guess was that the device's command-handling layer
  ignores commands until "provisioned" via `0x6c76`.

The later finding — that `0x6c76` is not writable and the key comes from the cloud —
means the actual cause of non-actuation lies elsewhere (inner payload format for the
device family), not in a missing provisioning write.

The capture-side observation still holds: an earlier phone "Control via Bluetooth" mode
capture showed no capturable Orbit `CONNECT_IND` because the phone uses Bluetooth
privacy (RPAs) and the single-channel sniffer couldn't follow its connection.

## Other notes worth keeping

- The crypto used is what the integration already implements: AES-CTR-style keystream with 12-byte IV (`response[:4] || init_payload[4:12]`) and 4-byte LE counter (`init_payload[12:16]`). No change needed there — the bundle's `read_aes_init_response` matches the integration's `derive_session_iv`. In this repo the cipher lives in `custom_components/orbit_bhyve/connection.py` (`_aes_xor` / `_handshake`).
- `subscribe_read_char` is called *after* `init_aes` in the official app. The integration currently subscribes *before* (in `custom_components/orbit_bhyve/connection.py`). Worth keeping in mind if command delivery still misbehaves.
- The decompiled bundle preserves no protobuf field names. If we ever need to refine the watering protobuf shapes, we'd need to find them in the same `bundle_decompiled.js` (search around `runZone`, `quickRun`, watering keywords).
- The `AA 77 5A 0F` message header, CRC-16-CCITT trailer, frame magic `0x11`, and 2-byte trailer all match what the integration implements in `custom_components/orbit_bhyve/devices/ht34a.py` — no change needed.

## Action items

> **Correction (later finding):** action items 1 and 2 below were premised on the
> disproven `0x6c76` key-write. `NETWORK_CHAR` can stay defined as a known UUID
> (`custom_components/orbit_bhyve/const.py`), but do **not** add a provisioning key-write
> step — the device rejects it. Left here for historical accuracy.

1. Add `NETWORK_CHAR = "00006c76-fe32-4f58-8b78-98e42b2c047f"` to `custom_components/orbit_bhyve/const.py`.
2. ~~Add a `provision_device(client, network_key)` step and call it BEFORE the AES init.~~ **Disproven** — `0x6c76` returns "Write not permitted"; the key is fetched from the cloud, not written over BLE.
3. Test on HT25-0000 / firmware 0041 hardware.
4. If behavior changes: post a follow-up comment on the upstream PR with the finding.
