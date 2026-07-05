# Orbit B-Hyve BLE — protocol & reverse-engineering docs

Reference documentation for the local BLE protocol this integration speaks, plus
the reverse-engineering notes behind it. Useful if you want to understand the
wire format, extend the integration, or reproduce the analysis on other Orbit
hardware.

There are **two device families** on this protocol:

- **Protobuf "XD" family** — frame magic `0x11`, protobuf messages wrapped in an
  `aa 77 5a 0f` header with a CRC-16. Covers HT34A, HT32A, and the Gen2 HT25G2.
- **HT25 "d7-47" family** — frame magic `0x10`, custom binary frames prefixed
  with a per-device mesh address (no protobuf, no CRC-16). Covers the older
  HT25 hose-tap timers (fw0041/fw0085).

The AES cipher, the 20-byte handshake, and the outer BLE frame are **shared** by
both families (see `custom_components/orbit_bhyve/connection.py`).

## Protocol reference (XD / protobuf family)

| Doc | What it covers |
|-----|----------------|
| [`protocol.md`](protocol.md) | **Authoritative** wire spec: GATT layout, handshake, AES-CTR cipher, `0x11` frame + checksum, `aa 77 5a 0f` app message, command/status catalog. |
| [`ble-messages.md`](ble-messages.md) | Full per-message field/enum tables with example hex (the message catalog). |
| [`hermes-findings.md`](hermes-findings.md) | Cipher, key architecture, and handshake as recovered from the vendor Android app's Hermes bundle. |
| [`encryption.md`](encryption.md) | Cipher + trailer deep-dive: the AES-ECB-as-CTR construction and why the byte-sum trailer matters. |
| [`ble-protocol.md`](ble-protocol.md) | GATT/connection-sequence walkthrough and behavioral notes (bonding, MAC enforcement, replay, ATT opcodes). Superseded by `protocol.md` where they differ. |
| [`protobuf-schema.md`](protobuf-schema.md) | How the protobuf schema was reconstructed by observation. |

## HT25 "d7-47" family

| Doc | What it covers |
|-----|----------------|
| [`findings/d7-47-protocol.md`](findings/d7-47-protocol.md) | The `0x10`/mesh-address binary protocol: frame layout, command catalog, init sequence, status/battery decode. |

## How it was reverse-engineered

| Doc | What it covers |
|-----|----------------|
| [`reverse-engineering-journey.md`](reverse-engineering-journey.md) | The full narrative — reconnaissance, cracking the cipher, the trailer-checksum breakthrough, and lessons for BLE RE. |
| [`network-key-extraction.md`](network-key-extraction.md) | How the per-account AES `network_key` is obtained (the integration does this for you at setup). |
| [`findings/bundle-decompile.md`](findings/bundle-decompile.md) | Hermes bytecode decompile + Frida toolchain; the cipher derivation. |
| [`findings/apk-findings.md`](findings/apk-findings.md) | APK decompile methodology and the GATT characteristic map. |
| [`findings/phone-ground-truth.md`](findings/phone-ground-truth.md) | Capturing btsnoop ground truth from the phone; the failed-decryption attempts. |
| [`findings/nrf-sniffer-setup.md`](findings/nrf-sniffer-setup.md) | Building an nRF52840 BLE sniffer capture rig. |
| [`findings/mesh-analysis.md`](findings/mesh-analysis.md) | BLE5 extended-advertising analysis (contains a **superseded** broadcast-only hypothesis). |
| [`findings/wrong-hypotheses.md`](findings/wrong-hypotheses.md) | Dead ends and falsified hypotheses — the most instructive part. |

## Provenance & attribution

- **`protocol.md`, `ble-messages.md`, `hermes-findings.md`** were contributed by
  **@anahnymous** in [issue #19](https://github.com/ljmerza/orbit-bhyve-ble/issues/19).
  Code references have been adapted to this repository's layout; this integration
  implements a subset of the documented catalog (run-zone, stop, status, battery).
- **Everything else** is imported from the maintainer's own reverse-engineering
  project and has been scrubbed of device-specific identifiers (MAC addresses,
  network keys, home device names).

## Scope

This is original research against devices lawfully owned by the authors,
conducted by observing the devices' own wire-level behavior and the publicly
distributed companion mobile application. No proprietary firmware or vendor
source code is reproduced here. AES `network_key`s, MAC addresses, and other
device-specific secrets have been removed from these notes — never commit yours.
