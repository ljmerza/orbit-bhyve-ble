*Contributed by @anahnymous in [issue #19](https://github.com/ljmerza/orbit-bhyve-ble/issues/19); code references adapted to this repository's layout.*

# B-hyve Hermes Decompilation Findings

The B-hyve Android app's BLE protocol logic lives in a Hermes-bytecode JavaScript
bundle (`index.android.bundle`), not in native code. This document records what that
bundle reveals about the BLE crypto, key architecture, and session handshake. Symbol
names below are the app's own (ClojureScript compiled to JS), so they are findable in
any decompile of the same bundle. Companion references: [`protocol.md`](protocol.md)
(wire format) and [`ble-messages.md`](ble-messages.md) (message catalog).

## App structure

- The app is React Native. The BLE protocol layer is ClojureScript (compiled
  CLJS -> JS -> Hermes; identifiers appear as `cljs$core$...`), in the namespace
  `lib.ble.service.common`.
- BLE transport is `react-native-ble-manager` (Java `it.innove`). It only moves
  bytes; all framing and crypto live in the ClojureScript.

## GATT characteristics

Custom characteristics use the base UUID `0000XXXX-fe32-4f58-8b78-98e42b2c047f`:

| App name | Short UUID | Role |
|----------|-----------|------|
| `service_uuid` | `fe32` (base) | service |
| `aes_char_uuid` | `6c71` | AES init / handshake (key setup + nonce) |
| `write_char_uuid` | `6c72` | command frames (host -> device) |
| `read_char_uuid` | `6c73` | notification frames (device -> host) |
| `network_char_uuid` | `6c76` | WiFi / network provisioning |

The bundle also defines `encryption_frame_size` (frame chunk size),
`max_message_size = 255`, and `default_tx_delay_ms`.

## Handshake and session nonce

`init_aes` generates `randomBytes(20)` (one byte set from a field) and writes it to
the AES characteristic. `read_aes_init_response` reads back a 20-byte device reply.
The per-session cipher state is composed from both blobs:

```
composed     = device_resp[0:4] ++ client_write[4:20]
nonce12      = composed[0:12]
counter_h2d  = readUInt32LE(composed, 12)
counter_d2h  = readUInt32LE(composed, 16)
```

Validation performed before composing:
- `device_resp[0:4]` must not be all-zero (the device supplies fresh entropy here).
- `device_resp[4:20]` must be all-zero.

So the nonce mixes 4 bytes of device randomness with 8 bytes of client randomness,
and both direction counters are purely client-generated (from the `randomBytes(20)`
in `init_aes`). This is an AES-CTR IV setup: a 12-byte nonce followed by a 4-byte
little-endian block counter, with separate counters per direction. The handshake
establishes only the per-session nonce and counters; the AES key is not exchanged.

## Cipher

- The cipher is `aes-js` `ModeOfOperation`. AES-CTR is realized manually over the
  nonce and counter above. There are no `createCipheriv` / `aes-128-ctr` algorithm
  strings, which is consistent with `aes-js` (it takes no algorithm string).
- The encrypt/decrypt path reads the AES key as a base64 field from the device
  record (`util.buffer.base64->buffer`), set on the `provision_device!` path. The key
  is provisioned, not embedded as a constant in the bundle.
- PBKDF2-SHA256 is present in the crypto stack (`pbkdf2`, `cachedPbkdf2`) via a
  crypto polyfill, but it is **not** used for the frame key: frames decrypt directly
  with the raw base64-decoded key. PBKDF2 is an unused polyfill or serves a different
  feature.

## Key architecture

- The AES key is a per-mesh `network_key`. `lib.ble.core.random_network_key` is:

  ```js
  function () { return randomBytes(16).toString('base64'); }
  ```

  a random 16-byte (AES-128) key, base64-encoded, generated at provisioning. It is
  not derived from the device serial, MAC, the handshake token, or any observable
  value; it is pure `crypto.randomBytes`.
- The key is shared by every timer in a `network_topology` (mesh) and is set once at
  provisioning. It is never sent over BLE.
- Because it is random, it cannot be reconstructed from a capture or from device
  identifiers. It can be obtained by:
  - retrieving it from the Orbit cloud account (see below),
  - reading it from a provisioned install's private storage (requires root), or
  - re-provisioning the timer, which generates a new key the provisioner then knows
    (this rewrites the timer's key and breaks the existing pairing).

## Retrieving the key from the Orbit cloud

At provisioning the app uploads the mesh record -- including the network key -- to
the Orbit cloud, and the server stores it. The provisioning code path is
`save_mesh_and_connect_BANG_` -> `save_mesh_to_server_BANG_` -> `save_mesh`, which
PUTs or POSTs the full mesh map (with the key) to the web service. The key survives
to the HTTP body because only the `:devices` sub-list is rewritten before the call.

Retrieve the key with two requests:

```
POST /v1/session
  orbit-app-id: Bhyve Dashboard
  body: {"session": {"email": "<account email>", "password": "<account password>"}}
  -> orbit_api_key: <jwt>

GET /v1/network_topologies
  orbit-app-id: Bhyve Dashboard
  orbit-api-key: <jwt>
  -> [{ ... "network_key": "<base64 AES-128 key>" ... }, ...]
```

Notes:
- The server stores the key under `network_key`. The CLJS keyword is
  `:ble-network-key`; the server normalises it on ingest.
- `/v1/network_topologies` is the correct endpoint. `/v1/meshes` returns an empty
  list, despite what the provisioning symbol names suggest.
- The response lists one entry per mesh, each with its associated devices. Pick the
  topology containing the target timer and base64-decode its `network_key` to the 16
  raw bytes used as the AES-128 key.

## Frame trailer

The outer BLE frame's 2-byte trailer is a 16-bit additive checksum over
`[0x11, len] + plaintext` (type and length header bytes included), little-endian:
`(0x11 + len + sum(plaintext)) & 0xFFFF`. It is not a CRC (a CRC-16 brute force over
the on-wire ciphertext bytes matches no common variant). The device drops any host
frame whose trailer does not match. The separate trailer inside the decrypted
`aa 77 5a 0f` application message is CRC-16/XMODEM; only the outer frame trailer is
the additive checksum. See [`protocol.md`](protocol.md) for the full wire format.
