# The Reverse-Engineering Journey

*Reverse-engineering notes; imported from the maintainer's research project.*

This document chronicles how the protocol used by the Orbit B-Hyve XD Bluetooth hose timer was reverse-engineered from scratch, what worked, what didn't, and the techniques that proved useful. It is intended as a technical record and a learning resource for anyone working on similar BLE-protocol problems.

> **Scope statement.** The work described here was performed against a device the authors lawfully purchased and own. No proprietary firmware was redistributed. No vendor application source code is reproduced in this repository. Where techniques applicable to compiled React Native applications are described, they are presented as *general approaches* to BLE protocol analysis, not as a step-by-step recipe applied to any specific application binary.

---

## Table of Contents

1. [Overview](#overview)
2. [Device Information](#device-information)
3. [Phase Chronicle](#phase-chronicle)
4. [The Trailer Checksum Breakthrough](#the-trailer-checksum-breakthrough)
5. [Lessons for BLE Reverse Engineering](#lessons-for-ble-reverse-engineering)

---

## Overview

The B-Hyve XD is a Bluetooth-only hose timer (no Wi-Fi, no LoRa, no proprietary radio). It pairs with the official Orbit B-Hyve mobile application over BLE GATT and is controlled directly by that app — no cloud round-trip is required to open or close a valve. Because the device has no Wi-Fi and the app must be physically near the device to send a command, integrating it into Home Assistant as a network-controllable sprinkler requires either:

1. Standing up a dedicated Bluetooth bridge that runs the official Android app (fragile, cloud-dependent), or
2. Speaking the BLE protocol directly from a Linux host with a working Bluetooth stack.

The second approach is what this project pursued. It required decoding:

- The BLE GATT service structure and which characteristics carry which traffic
- The connection initialization handshake (a 20-byte exchange establishing a per-session AES context)
- The custom AES encryption mode used for the data channel (an unusual ECB-as-CTR construction)
- The protobuf schema used by the application messages
- The frame format wrapping each encrypted message
- A 16-bit content-dependent **trailer checksum** validated by the device firmware (the longest hold-out)

By the end of the project, all four valve channels were controllable from Home Assistant via the included custom integration, with full per-zone duration support.

---

## Device Information

| Property | Value |
|----------|-------|
| Model | B-Hyve XD Bluetooth Hose Faucet Timer |
| Part Number | 24634 |
| FCC ID | ML6-HT34BT |
| Valves | 4 ports |
| Communication | Bluetooth Low Energy (BLE) |
| Firmware tested | 0107 |
| Hardware | HT34A-0001 |
| Manufacturer | Orbit Irrigation Products Inc. |

The device advertises a 16-bit BLE service UUID `0xFE32` (full UUID `0000fe32-0000-1000-8000-00805f9b34fb`) and uses three primary GATT characteristics for data exchange.

---

## Phase Chronicle

### Phase 1 — Reconnaissance

**Objective:** Understand what is on the wire and how the application talks to the device.

**What was done:**

- The device was paired with an Android tablet running the official mobile application. BLE traffic was captured using the OS-level **Bluetooth HCI snoop log** facility — a standard Android developer feature that records every HCI frame to `btsnoop_hci.log`. Enabling this is the textbook first step for any BLE protocol-analysis task and requires no instrumentation of the application itself.
- The captured `btsnoop` file was opened in Wireshark, which natively decodes ATT, GATT, and SMP layers. This made the GATT service discovery trivially observable: the device exposes a custom service with five characteristics, three of which carry traffic during normal valve operation.
- The BLE transport was characterized: MTU is negotiated to a higher value early in the connection, LE Data Length Extension is used, and the application transmits data using ATT Write Commands and Write Requests on different characteristics.
- The device's GATT handle map was reconstructed from the Wireshark dissection. See [`ble-protocol.md`](ble-protocol.md) for the resulting map.

**Result:** A clear picture of the wire format at the BLE layer, and the realization that the data on the data-channel characteristic was opaque encrypted bytes (high entropy, no recognizable structure).

### Phase 2 — Network Key Extraction

**Objective:** Obtain the symmetric key used to encrypt BLE data-channel messages.

**What was done:**

- The mobile application's data directory was inspected via ADB on a rooted Android device. Two locations were found to contain the per-account `networkKey`:
  1. The application's HTTP cache directory (a JSON response from the Orbit cloud API that the app caches locally).
  2. The MMKV (Tencent's persistent key-value store) used by the application for offline state.
- An automated extractor was written to pull this key over ADB without manual file-tree exploration.
- A second extractor retrieves the same key directly from the Orbit cloud REST API, given valid account credentials. This avoids needing a rooted device entirely and is the path this integration's config flow uses (see [`network-key-extraction.md`](network-key-extraction.md)).

> **Note on key handling:** The network key is account-specific, not device-specific. It is the secret derived for your Orbit account and transmitted to your devices when they are provisioned. Anyone in possession of the key can control all devices on your account; treat it like a password.

**Result:** A 16-byte network key, ready for use as a candidate AES key.

### Phase 3 — BLE Traffic Capture & Encryption Analysis

**Objective:** Crack the encryption used on the data-channel characteristic.

**What was done:**

- Multiple captured BLE write payloads were collected from `btsnoop` for a known plaintext (a "stop all watering" command, sent repeatedly with no other input between attempts).
- An attempt was made to decrypt the ciphertext directly with the network key under several common AES modes (CBC, CTR, GCM, OFB, CFB) using a brute-force harness that tried plausible IV/nonce derivations from the surrounding bytes. Roughly 500+ combinations were tried; **none produced plaintext that resembled a known protobuf structure.**
- A more invasive approach was needed. Frida (a dynamic instrumentation toolkit) was attached to the running mobile application and used to hook the relevant BluetoothGatt write methods. This made it possible to log the **plaintext** the application was about to encrypt before it disappeared into the cipher routine.

**The key crypto observation:** The application is built on React Native with Hermes (Facebook's JavaScript engine for React Native). Java-level Cipher hooks produced no hits during BLE writes, and native libcrypto hooks produced no hits either. This narrowed the AES implementation to **JavaScript** — almost certainly the `aes-js` pure-JS library, which is commonly used inside React Native apps.

**Determining the AES construction:**

With pre-encryption plaintext available via Frida hooks and post-encryption ciphertext available from the BLE captures, the encryption algorithm was discovered by inspection. The cipher does not match any standard mode. Specifically:

```
Key       = networkKey (16 bytes)
IV        = rx_header[:4] || init_tx[4:12]      (12 bytes, per-session)
Counter   = init_tx[12:16] interpreted as little-endian uint32
Block_in  = IV(12 bytes) || counter_LE(4 bytes)  (16 bytes total)
Keystream = AES-ECB-Encrypt(Key, Block_in)
Ciphertext_block = Plaintext_block XOR Keystream
Counter   = (Counter + 1) mod 2^32   per 16-byte block
```

This is a CTR-mode-style construction implemented manually using AES-ECB as the keystream generator, rather than using a library's CTR mode primitive directly. Once the `Block_in` layout was identified, all captured ciphertexts decrypted cleanly to valid protobuf-shaped plaintexts.

> **Aside on technique.** Compiled React Native applications package their JavaScript as Hermes bytecode, not as readable JS. Various decompilation tools exist that can convert Hermes bytecode back into approximate JavaScript. This is a *general technique* applicable to any React Native app, and is one approach that **may** be used in scenarios where dynamic instrumentation is insufficient. The crypto construction described above is fully derivable from the *observable behavior* of the cipher (matching plaintext/ciphertext pairs and the structure of the session-init handshake) and does not require static analysis of the application's bytecode.

### Phase 4 — Protobuf Schema Recovery

**Objective:** Decode the structure of the plaintext messages.

**What was done:**

- Decrypted plaintexts were fed to `protoc --decode_raw` to dump field numbers and wire types. Structures repeated consistently between captures.
- A schema was reconstructed by observation: which field number controls the timer mode, which controls the station ID, which controls the duration in seconds, etc. The schema was confirmed by encoding our own messages from the schema and observing the device respond.
- The reconstructed schema is documented in [`protobuf-schema.md`](protobuf-schema.md). It is described as a *reconstructed interface description* — it is sufficient to interoperate with the device, not a copy of any vendor source.

### Phase 5 — First Working Valve Command

**Objective:** Open Zone 1.

**What was done:**

- A Python script using `bleak` was written to (a) connect to the device, (b) perform the AES session-init handshake, (c) build a protobuf message requesting Zone 1 ON for 60 seconds, (d) encrypt it, (e) wrap it in the observed frame format `[0x11][length][ciphertext][2-byte trailer]`, and (f) write it to the device's data-channel characteristic.
- After several iterations to get framing details right, **Zone 1 actuated**. A physical click from the solenoid was audible, water flowed.

**Status at end of Phase 5:** Zone 1 fully working, all four zones encryption/decryption working, but **only Zone 1 actually triggers the device** when sent.

### Phase 6 — The Zone 2-4 Mystery

This is where the project nearly stalled for two sessions. The code that worked perfectly for Zone 1 was identical except for the `stationId` field in the protobuf — which should change from 0 to 1, 2, or 3 to address the other zones. But **only Zone 1 worked**. Zones 2-4 produced no device response, no error, no LCD blink — silent rejection.

Things that were ruled out (each verified by experiment, often after hours of work):

1. **Protobuf encoding errors.** The application's own Zone 3 ON command was captured via `btsnoop`, decrypted, and compared byte-for-byte with the project's Zone 3 plaintext. They were **100% identical**.
2. **BLE transport differences.** `btmon` traces of the working Zone 1 and the failing Zone 2 commands were captured. Both went out as single unfragmented ACL packets, both used the same ATT opcode, both were confirmed delivered by HCI Number-of-Completed-Packets events.
3. **ATT Write Command vs Write Request opcode.** The application uses Write Request (0x12) which expects a response. The project's code used Write Command (0x52, fire-and-forget). Switching to Write Request was attempted; the device returned ATT Error 0x80 ("Application Error") for any payload longer than 20 bytes, regardless of MTU/data-length negotiation. This is a known firmware quirk and proved to be a red herring.
4. **MAC address spoofing.** The Linux host's BLE adapter MAC was spoofed to match the tablet's, on the hypothesis that the device might be tied to the paired peer's address. The device responded identically — **MAC was not the gate**. This was a clean negative result: the device uses application-level validation, not BLE-link-layer identity.
5. **MTU and LE data length negotiation.** Both were re-negotiated to match the application's values. No effect.
6. **Sync prelude messages.** The project's code sent timestamp-sync, getDeviceStatusInfo, and getWateringStatus messages before the zone command, mimicking the application's full pre-command sequence. No effect on Zones 2-4.
7. **`stationId` indexing.** Tried 0-indexed and 1-indexed. The application uses 0-indexed; this was confirmed correct.
8. **Alternative protobuf fields.** Various other top-level fields were tried (`deviceControl runAllStationsManually`, `autoMode` instead of `manualMode`, etc.). None worked for Zones 2-4.

**The critical observation that broke the case:** Timestamp-sync messages **did** work — meaning the device was reading our messages, decrypting them, and acting on them. The B-Hyve LCD display visibly updated to match the timestamp we set. So the encryption was right, the connection was right, the framing was right. *Something* was specifically rejecting valve commands when `stationId != 0`.

### Phase 7 — The Bit-Flip Diagnostic

**Concept:** AES-CTR (and CTR-style) encryption is fundamentally an XOR with a deterministic keystream. Any bit flipped in the ciphertext flips the same bit in the plaintext after decryption. This means it is possible to *modify* an encrypted message **without knowing the key** as long as you know the plaintext byte at the position you want to change.

**The experiment:** Use Frida to intercept the application's *valid* Zone 1 ON command, XOR specific bytes of the ciphertext to transform it into a Zone 2 ON command, and let the application's authenticated, ATT-Write-Request connection deliver it. If the bit-flipped command works, then there is no integrity-protected portion outside the ciphertext we modified — the device is just being silly about something else. If it fails, then there *is* something else being validated.

**XOR values needed for the Zone-1-to-Zone-2 transformation:**
- `byte[17] XOR 0x01` — flips `stationId` from 0 to 1
- `byte[21] XOR 0xB4` — fixes CRC16 byte 1 (the inner protobuf checksum changes when stationId changes)
- `byte[22] XOR 0x76` — fixes CRC16 byte 2

These were precomputed by encoding both Zone 1 and Zone 2 plaintexts ourselves and XORing them.

**Result:** The bit-flipped Zone 2 command was delivered through the application's own authenticated BLE connection. **Zone 2 did not activate.** The application reported "Your timer didn't respond within 30 seconds."

This was the breakthrough. There **must** be content-dependent integrity protection somewhere outside the AES-encrypted region. The only thing outside the encrypted region was the **2-byte trailer**.

### Phase 8 — The Trailer Algorithm

The frame format is `[0x11][length][ciphertext][trailer_lo][trailer_hi]`. The trailer had previously been treated as either a fixed protocol marker or a transport-layer detail — the project's code hardcoded `b"\x80\x04"` for all valve commands and it had worked for Zone 1.

**The hypothesis:** The trailer is a content-dependent checksum, and `0x0480` is the correct value for Zone 1 *by coincidence*.

**Verification approach:** Captures of the application's own Zone 2, Zone 3, and Zone 4 commands were extracted from `btsnoop`. Their trailers were:

| Command | Captured Trailer |
|---------|------------------|
| Zone 1 ON | `0x8004` (LE bytes `80 04`) |
| Zone 2 ON | `0xa304` |
| Zone 3 ON | `0xa203` |
| Zone 4 ON | `0xc403` |

Different for every zone. Hypothesis confirmed: the project's hardcoded `0x8004` was right for Zone 1 only, and Zones 2-4 had been silently rejected because their trailers were wrong.

**Deriving the formula:** With plaintext, ciphertext, and correct trailers known for seven different commands (4 zones plus three sync messages), the trailer formula was determined empirically by trying simple combinations of byte sums and structural elements:

```python
def compute_trailer(plaintext: bytes) -> bytes:
    """The 2-byte content-dependent trailer for a B-Hyve BLE frame.

    Args:
        plaintext: the unencrypted inner message (protobuf + inner CRC16)

    Returns:
        2 bytes, little-endian uint16
    """
    total = sum(plaintext) + 0x11 + len(plaintext)
    return struct.pack("<H", total & 0xFFFF)
```

In words: sum every byte of the plaintext, add the magic byte `0x11`, add the length byte, take the low 16 bits, store little-endian. This formula matched **all 7** of the captured commands exactly. The project's hardcoded `0x8004` was, by sheer numerical coincidence, the correct sum-mod-65536 for Zone 1's specific plaintext.

### Phase 9 — All Zones Working

With the trailer fix in place:

- **Zone 2 ON:** confirmed, valve actuated, LCD changed.
- **Zone 3 ON:** confirmed, valve actuated.
- **Zone 4 ON:** confirmed, the last unsolved zone, screen lit up with all icons and segments as the relay engaged.

### Phase 10 — Home Assistant Integration

The trailer computation was added to the custom HA integration (in this repo it is computed inline in `BHyveBleConnection.encrypt()`, `custom_components/orbit_bhyve/connection.py`). All four zones became controllable from Home Assistant immediately. The integration uses HA's built-in Bluetooth manager for device access (the same pattern used by upstream HA Bluetooth integrations like SensorPush), which means it does not require a dedicated Bluetooth proxy and works on any HA installation with Bluetooth enabled.

---

## The Trailer Checksum Breakthrough

The trailer turns out to be a textbook example of a defense that looks like decoration. Most BLE protocol layers expect that the cipher provides integrity (CTR mode by itself does not — it is malleable). When a vendor uses CTR-style encryption *without* an authenticated mode (no GCM, no Encrypt-then-MAC), they often add an ad-hoc checksum outside the encryption envelope to detect tampering. Because the checksum is plaintext and looks like a fixed marker for any single captured command, it can easily be overlooked as a constant — until you try a second command and see the trailer change.

**Why "sum of bytes" rather than CRC?** A simple byte sum is not a strong checksum (it cannot reliably detect bit transpositions or many forms of structured tampering), but it is cheap on a small embedded MCU and is sufficient to defeat naive bit-flip attacks: if you flip a bit, you must also adjust the trailer to match, and if you don't know the plaintext you can't compute the new sum. Since the bit-flip attack relies on knowing the *change* between two plaintexts, and the trailer depends on the absolute sum, the trailer effectively acts as a poor-man's MAC.

**Why Zone 1 worked by coincidence:** The trailer is `(sum + 0x11 + len) mod 65536`. For Zone 1's specific plaintext, that happens to equal `0x0480` (little-endian `0x8004`). There is no deeper meaning — it is just where the modular arithmetic landed.

---

## Lessons for BLE Reverse Engineering

These are the lessons from this project, applicable to similar work on other BLE-only consumer devices.

### 1. When some commands work but others don't, check the frame checksum first

Two full sessions were spent investigating BLE transport, ATT opcodes, MTU negotiation, MAC spoofing, device provisioning, and dozens of protobuf variations. The answer was a 2-byte checksum in the unencrypted trailer that happened to be correct for one command by coincidence. **Lesson:** when a single command works and structurally identical commands don't, suspect content-dependent integrity protection outside the cipher envelope.

### 2. The bit-flip attack on CTR-style cipher is a powerful diagnostic, not a real attack

When the cipher is CTR-style, you can freely modify the ciphertext to produce any plaintext you want at any byte position you know. Using this to test *whether the protocol depends on the cipher's integrity* is a clean experiment: if a bit-flipped command is rejected even when delivered through the application's authenticated connection, you know there is integrity protection somewhere outside the cipher.

### 3. Use `btmon` and Wireshark to verify BLE transport before investigating higher layers

Confirming that working and non-working commands were delivered identically at the BLE layer (same opcode, same fragmentation, same Number-of-Completed-Packets) ruled out an entire class of problems immediately and saved enormous time.

### 4. Keep your encryption analysis decoupled from your interaction

Once the cipher was understood, decrypting captures became a one-liner. This was used dozens of times during the Zone-2-4 investigation to compare the application's plaintext against the project's plaintext. **If your encryption is solid, your debugging gets faster every iteration.**

### 5. Set up visual feedback for rapid iteration

A USB webcam pointed at the device's LCD allowed instant confirmation of whether commands took effect. This is a small thing but it cuts the iteration loop from "walk to the device, look, walk back" to "watch a window on your monitor."

### 6. Do not assume trailers are decorative

Bytes after an encrypted payload often carry checksums that the firmware validates. They may look like padding, sequence numbers, or framing markers. Test the hypothesis early — capture multiple distinct commands and compare the trailer values.

### 7. MAC spoofing and BLE address-type changes are rarely the answer

For application-level rejections (which most BLE device rejections are), the problem is almost never the BLE MAC address. Devices that care about identity typically use application-level keys or provisioning, not link-layer address matching.

### 8. Document every failed attempt

Keeping a detailed log of what was tried and why it failed prevents re-testing the same hypotheses and helps identify the correct direction by process of elimination. The list of "things ruled out" in Phase 6 above came directly from such a log.

---

## Acknowledgements

This protocol description is the result of original research against the device, conducted entirely by observation of its own wire-level behavior and analysis of the publicly distributed companion mobile application. No proprietary firmware or vendor source code is reproduced. Where techniques applicable to React Native applications are discussed, they are presented as general approaches to BLE protocol analysis.
