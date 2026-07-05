# Ground-Truth Bytes From the Official B-Hyve App — Zone A timer (HT25-0000 / fw 0085)

*Reverse-engineering notes; imported from the maintainer's research project and scrubbed of device-specific identifiers.*

Captured from an Android phone's `btsnoop_hci.log` while doing the following:

- Phone running the official B-Hyve app.
- The hub (`AA:BB:CC:00:00:11`) physically unplugged (proving the phone goes direct, not via the hub).
- Tapped "Control via Bluetooth" on the Zone A timer (`AA:BB:CC:00:00:01`).
- Started zone 1 watering → stopped → exited.

This is the **gold standard** of bytes that the timer DOES accept and act on.

## Connection-level facts

- **No SMP/pairing/encryption events.** Plain unencrypted L2CAP throughout.
- The phone's NETWORK_CHAR (`0x6c76`, handle 0x10) write got an ATT **error response** (opcode 0x01) — the same rejection we get. The phone proceeded anyway.
- The phone's WRITE_CHAR (`0x6c72`, handle 0x0b) writes used **opcode 0x12 = WRITE_REQUEST** (with response). The device acknowledged each (opcode 0x13).
  - Important: when WE try `response=True` on the same characteristic, the BlueZ ESPHome proxy reports ATT error 0x06 "Request not supported" from the device. The phone gets clean acks. Reason unknown — same handle, same characteristic, different result.
- Notifications come back on READ_CHAR (`0x6c73`, handle 0x0d) opcode 0x1B.

## Raw bytes — first session (frames 1290–1319)

| Frame | t (s) | Op | Handle | Char | Bytes |
|-------|-------|----|--------|------|-------|
| 1290 | 3171.38 | 0x01 (ERROR) | 0x10 | NETWORK_CHAR | (rejected) |
| 1299 | 3171.70 | 0x12 (WRITE_REQ) | 0x09 | AES_CHAR | `d56ca2cddd1a4ab33b4aad64faffbbf99a12e8a0` (init_tx, 20B) |
| 1300 | 3171.77 | 0x13 (WRITE_RSP) | 0x09 | AES_CHAR | (ack) |
| 1301 | 3171.78 | 0x0a (READ_REQ) | 0x09 | AES_CHAR | (req) |
| 1303 | 3171.87 | 0x0b (READ_RSP) | 0x09 | AES_CHAR | `6345939f00000000000000000000000000000000` (init_rx, 20B) |
| 1308 | 3172.73 | 0x12 | 0x0b | WRITE_CHAR | `100b87219f1fdb1053fdfcf5b43605` (15B) |
| 1311 | 3172.92 | 0x1b (NOTIFY) | 0x0d | READ_CHAR | `100c9c51bb37c3a7757be8fd02ca7705` (16B) |
| 1312 | 3173.79 | 0x12 | 0x0b | WRITE_CHAR | `1006b6fdc18adb937801` (10B) |
| 1314 | 3173.87 | 0x1b | 0x0d | READ_CHAR | `100c058cbf2c897979b7582de612bb05` (16B) |
| 1316 | 3175.92 | 0x1b | 0x0d | READ_CHAR | `100c1b06737c70ae60e8179b35701906` (16B) |
| 1317 | 3176.40 | 0x12 | 0x0b | WRITE_CHAR | `100c7cde4784a126244380a996938001` (16B) |
| 1319 | 3176.47 | 0x1b | 0x0d | READ_CHAR | `100c11eb3603e5909f514b1da238b603` (16B) |

## What we learn from the bytes

**Frame format used by the phone:**
```
[0x10][length_byte][ciphertext_N_bytes][2_byte_trailer]
```
- Magic byte is **`0x10`** — NOT `0x11` as the upstream's HT34/0107 code assumes. Our code used `0x11`.
- `length_byte` = number of ciphertext bytes (NOT including trailer or header). Frame 1308 has len=`0x0b`=11, ciphertext is exactly 11 bytes (`87 21 9f 1f db 10 53 fd fc f5 b4`), trailer is the last 2 bytes (`36 05`).
- All notifications use the same format. Length is always `0x0c`=12 in this session.

**Cipher: NOT what the decompile naively suggests.**

I tried decrypting `87 21 9f 1f db 10 53 fd fc f5 b4` with the timer's network key (`<network-key>`) using:

- AES-CTR, IV = `init_rx[:12]` + counter `00000000` (this is what the decompile shows)
- Same with counter starting at 1
- Same with iv = `init_tx[:12]`, `init_rx[:4]+init_tx[4:12]`, etc. (~12 IV variants)
- AES-CBC with various IVs
- AES-ECB direct
- IV = MD5(tx+rx)[:12], MD5(tx)[:12], MD5(rx)[:12]
- IV = AES_ECB(key, rx[:16])[:12], AES_ECB(key, tx[:16])[:12]

**None decrypt to a plaintext whose byte-sum + flag + len equals the observed trailer.** That trailer formula was the upstream's; if it's also wrong on this firmware, our chances of identifying the cipher by trailer-cross-check are low.

The decompile is unambiguous that the cipher is `aes-js`'s `ModeOfOperation.ecb` used in a CTR pattern (manual block construction), with the IV stored at db key `:iv` (12 bytes) and counters at `:enc-ctr` and `:dec-ctr` (uint32 LE each, derived from `read_aes_init_response[:12]`, `[12:16]`, `[16:20]`). Yet the math doesn't work.

Possibilities I haven't ruled out:

- The KEY used by the cipher is a **derived value**, not the raw network key from the cloud API. The decompile's `r5` register at the cipher constructor (`new ecb(r5, r12)`) traces back to a chain involving `base64_to_buffer` of `:network-key-b64`, but maybe a hash or KDF is applied somewhere in between that I didn't catch.
- The IV used in encryption blocks may NOT be `:iv` directly — `state.iv.copy(block, 0, 0, 13)` copies "up to 13 bytes" but we only stored 12. The 13th byte might come from somewhere we missed (initial value of the allocated buffer? a different state slot?). However, `Buffer.alloc(N)` zeros memory, so byte 12 of the block before the counter is written would be 0 — same as our model.
- The "trailer" might not be a checksum. It could be a sequence number, or part of an authenticated-encryption tag.
- The cipher object might be created with a `Counter` object (not plain ECB), making `cipher.encrypt(block)` advance internal state per call. That would change every encryption result.

## Comparison to our integration's frames

Our HA component sent, for "zone 1 ON 600s" with our then-current best guess:
```
0x11  0x19  <25-byte ciphertext>  <2-byte trailer>
```
where the plaintext was `aa 77 5a 0f 0f 00 30 xx xx xx 72 0b 08 02 12 07 1a 05 08 01 10 d8 04 e7 8e` (the `xx` bytes are the device's mesh_device_id), AES-CTR-encrypted with the same key, IV from `read_gatt_char(AES_CHAR)[:12]`, counter 0.

Differences vs what the phone actually sends:

- We used magic `0x11`; the phone uses `0x10`.
- Our plaintext length is 25; the phone's first command was 11. So the protobuf shape we send is ~2x larger than what the device expects on this firmware.
- We embed an upstream-defined inner envelope (MSG_HEADER `aa 77 5a 0f` + length + reserved + protobuf + CRC-16-CCITT). The phone's plaintexts (whatever they are) decrypt to 11 / 6 / 12 bytes — too short to contain that envelope plus a meaningful protobuf message.

That suggests the inner-envelope concept is an upstream HT34-specific layer that doesn't exist on HT25/0085. The plaintext on the wire might be JUST the protobuf (or just the protobuf with a tiny prefix).

## What we need to do to crack this

1. **Resolve the cipher's actual key.** This means resolving `r5` in the decompiled bundle to its actual byte value at that moment. Hard because of the register-based decompiled output, but doable with patient line-by-line tracing through `init_aes`. Alternatively, instrument the `aes-js` calls at runtime by injecting JS into the running app (frida on a real device).

2. **Confirm the trailer formula.** We have 7 known frames (3 TX, 4 RX). The trailer is consistent within a session but doesn't match `sum(pt) + flag + len` for any decryption we tried. Likely candidates left to try once we have correct decryption:
   - sum(plaintext bytes) only (no flag/len)
   - CRC-16-CCITT of plaintext
   - CRC-16-CCITT of the full unencrypted prefix
   - HMAC truncated to 2 bytes

3. **Decode the resulting plaintext as protobuf** — should be an `OrbitPbApi_Message` with fields like `meshDeviceId`, `messageId`, `getDeviceStatusInfo` (the first message after connect is usually a status request).

## Practical next steps

- **Easiest:** use a cloud-based HA integration (e.g. `sebr/hass-bhyve`) which routes through Orbit's cloud → hub → BLE the same way the official app does. Works today on hub-paired devices.
- **Medium:** repeat this snoop-log capture but **with packet timestamps preserved** so we can correlate exactly which "Control via Bluetooth" UI action produced which write. Currently we know the byte order but not the semantic intent of each write.
- **Hard:** extract the actual cipher key at runtime via frida on a rooted Android device, OR finish reverse-engineering the cipher key derivation in the Hermes bundle by hand.

## Files

| Path | Contents |
|---|---|
| `btsnoop_hci.log` | Raw BTSnoop log extracted from the Android phone's bug report (`bugreport-*.zip`) |
| `orbit_schema.json` | Pretty-printed protobuf schema extracted from the Hermes bundle |
| `apk_decompiled/bundle_decompiled.js` | Decompiled JS (~98 MB) |

The cipher/frame reference implementation now lives in `custom_components/orbit_bhyve/connection.py`.
