# Encryption: Custom AES-ECB-as-CTR Construction

*Reverse-engineering notes; imported from the maintainer's research project.*

This document describes the AES construction used by the Orbit B-Hyve XD on its data-channel BLE characteristic, plus the content-dependent trailer checksum that protects each frame's integrity. It focuses on the *cipher and framing*, which are shared by every device family; see [`hermes-findings.md`](hermes-findings.md) for the same construction as recovered from the vendor app, and [`protocol.md`](protocol.md) for the wire-verified message layout.

In this integration the cipher, handshake and outer-frame trailer live in `custom_components/orbit_bhyve/connection.py` (`BHyveBleConnection._handshake`, `_aes_keystream` / `_aes_xor`, and the trailer computed inline in `encrypt()`).

## Cipher Construction

The B-Hyve uses **AES-128 in a custom CTR-style mode**, implemented manually using AES-ECB to generate keystream blocks. It is not a library's AES-CTR primitive — it is a hand-rolled construction.

### Parameters

| | |
|---|---|
| Cipher | AES-128 (ECB primitive used as keystream generator) |
| Key | The account-specific 16-byte `networkKey` |
| Block size | 16 bytes (standard AES) |
| IV length | 12 bytes, per session |
| Counter | 32-bit, little-endian, increments per 16-byte block |

### Algorithm

```
# Per-session setup (after the 20-byte 6c71 init exchange):
IV       = rx_response[:4] || init_tx[4:12]      # 12 bytes
Counter  = uint32_LE(init_tx[12:16])             # initial counter

# Per-block keystream:
Block_in   = IV || uint32_LE(Counter)            # 16 bytes
Keystream  = AES-ECB-Encrypt(networkKey, Block_in)
Ciphertext = Plaintext XOR Keystream
Counter    = (Counter + 1) mod 2^32

# Continue for each subsequent 16-byte block of the message.
```

Decryption is identical (XOR is its own inverse). The cipher is symmetric in both encrypt and decrypt directions, as expected of CTR-style modes.

### Why "ECB used as CTR" rather than just AES-CTR?

The construction is functionally equivalent to AES-CTR with a specific nonce/counter layout (12-byte nonce, 4-byte counter), but the implementation uses the AES-ECB primitive with a manually-assembled `IV || counter` block as input. This is because the application's encryption library (a pure JavaScript AES library bundled inside the React Native app) expects an ECB primitive and applies the counter increment in JavaScript code, rather than calling a higher-level CTR API.

### Session Init Handshake (`0x6c71`)

The 20-byte write to characteristic `0x6c71` establishes the session IV and counter. The bytes are:

```
init_tx = [ rx_seed_bytes (4) | iv_seed (8) | counter_LE (4) | reserved (4) ]
```

- The first 4 bytes are not used to derive the session IV directly; they are echoed-and-modified by the device in its response.
- Bytes 4–11 (8 bytes) are used as the second half of the 12-byte session IV.
- Bytes 12–15 (4 bytes, little-endian uint32) become the initial counter.
- Byte 11 (the last byte of the IV-seed range) is **always written as `0x00`** by the application. The device may reject otherwise.

The device's 20-byte response to the read of `0x6c71` contains:

```
rx_response = [ session_seed (4) | zero_bytes (16) ]
```

The session IV is then assembled as:

```
session_IV = rx_response[:4] || init_tx[4:12]
```

This is 12 bytes total, used as the high-order portion of every keystream block.

## Inner Message Integrity (CRC-16 CCITT)

Inside the encrypted payload, after the 4-byte inner header `AA 77 5A 0F` and a little-endian uint16 length (see [`protocol.md`](protocol.md) — earlier notes described this as a length byte plus reserved bytes), the protobuf data is followed by a **CRC-16 CCITT** checksum:

- Polynomial: `0x1021`
- Initial value: `0`
- No reflection, no XOR-out
- Standard 256-entry lookup table

This is the textbook CCITT CRC-16 used in many BLE and embedded protocols. It validates the protobuf bytes **before** encryption.

## Outer Frame Integrity (the Trailer)

Outside the encrypted payload, every frame carries a 2-byte trailer:

```python
def compute_trailer(plaintext: bytes, length: int) -> bytes:
    """Compute the 2-byte content-dependent trailer for a B-Hyve BLE frame.

    Args:
        plaintext: the unencrypted inner message bytes (header + protobuf + CRC16)
        length: the value of the frame length byte (= len(plaintext))

    Returns:
        2 bytes, little-endian uint16
    """
    total = sum(plaintext) + 0x11 + length
    return struct.pack("<H", total & 0xFFFF)
```

In this codebase the trailer is computed inline in `BHyveBleConnection.encrypt()` (`custom_components/orbit_bhyve/connection.py`) as `(sum(plaintext) + trailer_const + len(plaintext)) & 0xFFFF`, where `trailer_const` is `0x11` for the protobuf/XD family and `0x10` for the older HT25 "d7-47" family.

### Why this trailer matters

The CTR-style cipher is **malleable**: any bit in the ciphertext can be flipped to flip the corresponding plaintext bit. Without an outer integrity check, a captured ciphertext could be mutated into a different valid command without knowing the key. The byte-sum trailer plugs this hole — modifying any byte changes the sum, breaking the trailer.

It is not a cryptographic MAC (it has none of the unforgeability guarantees of HMAC or AES-GCM), but it is sufficient to defeat the trivial bit-flip attack and to detect transmission corruption.

### Trailer values for known commands (firmware 0107, sample plaintexts)

| Command | Trailer (uint16 LE) |
|---|---|
| Zone 1 ON, 60s | `0x8004` |
| Zone 2 ON, 60s | `0xa304` |
| Zone 3 ON, 60s | `0xa203` |
| Zone 4 ON, 60s | `0xc403` |

These are illustrative — your exact values will depend on the duration, timestamps, and any additional protobuf fields you include.

## Putting It All Together — Send Path

```
1. Build protobuf message (e.g. timerMode { mode=manual, manualModeParams { stationInfo { stationId=0, runTimeSec=300 } } })
2. Compute CRC-16 CCITT over the protobuf bytes; append.
3. Prepend [AA 77 5A 0F] + [length:LE16] => "inner message" / plaintext.
4. AES-encrypt the plaintext using the per-session IV/counter from the connection sequence.
5. Compute the outer trailer = uint16_LE((sum(plaintext) + 0x11 + length) mod 65536).
6. Assemble the on-wire frame: [0x11] + [length] + [ciphertext] + [trailer].
7. Write to characteristic 0x6c72 (handle 0x0014).
```

In this integration the send path is split across `custom_components/orbit_bhyve/devices/ht34a.py` (protobuf build → `_build_message` → CRC-16) and `custom_components/orbit_bhyve/connection.py` (`encrypt()` for AES + trailer, then the GATT write). The equivalent for the older HT25 "d7-47" family is in `custom_components/orbit_bhyve/devices/ht25.py`.
