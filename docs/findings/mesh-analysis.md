# BLE Sniffer Findings — Broadcast/Advertising Analysis

*Reverse-engineering notes; imported from the maintainer's research project and scrubbed of device-specific identifiers.*

> **Superseded hypothesis:** This document's headline conclusion — that commands
> are delivered **only** via a broadcast mesh, so a direct per-device BLE
> connection "can't work" — was **later disproven**. Direct, per-device BLE
> control does work: the timer accepts encrypted command frames on its GATT data
> channel. That protocol is implemented as the **d7-47 protocol family** in
> `custom_components/orbit_bhyve/devices/ht25.py`. Read the broadcast-only claims
> below as a dead end that was investigated and ruled out. The **advertising-
> analysis methodology** (extended-adv detection, manufacturer-data parsing under
> company ID `0x047f`, and the "this is not SIG Mesh" reasoning) is still valid
> and worth keeping.

## Original TL;DR (later disproven)

The working theory at the time was that the B-Hyve hub-to-timer protocol is **not
a regular BLE connection** with ATT/GATT writes, but a **mesh broadcast protocol**
where the hub puts encrypted commands into **BLE advertisement data** and the
timers receive them via passive scanning — no `CONNECT_IND`, no GATT, no L2CAP.

This was contrasted with the upstream integration, which BLE-connects and uses the
GATT data channel. (As it turned out, the GATT path is the correct one; see the
superseded-hypothesis banner above.)

## Evidence

After 5 captures totaling ~12 minutes and ~250,000 packets across various
conditions (with/without `--follow`, with/without hub power-cycle, with/without
watering trigger), we observed:

- **Zero `CONNECT_IND` for any Orbit-pattern device** — in our captures, neither timers nor hubs ever connected to each other.
- **Zero ATT writes / notifications / data-channel traffic** of any kind.
- **All Orbit-pattern devices broadcast frequently** in the 30-50 Hz range, with manufacturer-specific data fields containing variable-length encrypted-looking payloads.
- **At least 3 distinct payload sizes observed:**
  - 2 bytes (e.g. `0500`, `0600`) — likely heartbeat / status flags.
  - 13 bytes (`5c2e6337dee3a06a05377e8132`) — observed once on the Zone A timer at t=43s, possibly a state update tied to a watering action.
  - 20 bytes (`4acc1a97176357fbbffde44c8368aba9402fcc61`) — observed once on a Zone B timer MAC variant.
  - 46 bytes (`f547b7a7…0eed1a7a0cfb775176ea330abec4`) — observed once on a Hub 2 MAC variant; long enough to be a full encrypted command + envelope.

> **Note (correction):** The absence of `CONNECT_IND` / GATT traffic in these
> captures is now understood as a **capture-rig limitation**, not proof that no
> connection happens. The single-channel PCA10059 misses much of the extended-adv
> secondary-channel and connection setup. Direct BLE connections to the timer do
> occur and do carry the commands.

## Account topology (from the account topology config)

This deployment spans **two separate meshes**, each with its own hub and one
shared `ble_network_key`:

| Mesh | Hub | Network Key | Devices |
|---|---|---|---|
| Mesh 1 | Hub 1 (`AA:BB:CC:00:00:10`) | `<network-key>` | Zone B timer, Zone C timer |
| Mesh 2 | Hub 2 (`AA:BB:CC:00:00:11`) | `<network-key>` | Zone A timer, Zone D timer |

Both hubs are Orbit-branded Wi-Fi gateways, registered in the same Orbit cloud
account.

**Tooling bug noted:** the analyzer was defaulting to the first mesh's key, which
is wrong for any device on the second mesh. The right mesh key has to be selected
based on which timer's traffic is being decoded.

## Why the upstream integration connects but valves didn't move (original theory)

At the time, the integration was seen to succeed at:

1. BLE-connecting to the timer (timers DO accept connections).
2. Completing the AES init handshake on `0x6c71` (the per-session IV negotiation works because the network key IS correct).
3. Writing encrypted frames to `0x6c72`.

...yet the valve didn't actuate. The (incorrect) conclusion drawn was that the
timer's real command channel must be the broadcast scanner rather than the GATT
writes, and that the connection-based command form (legacy `IpcMsg.field_14`) was
a leftover from an older firmware that supported both paths — with HT25-0000 /
firmware 0041 acting only on the broadcast path for routine commands.

> **Correction:** The valve failure was a **frame-format problem, not a wrong
> channel**. The GATT write path is correct; the earlier frames used the wrong
> magic byte, envelope, and mesh_device_id shape. Once the d7-47 frame format was
> matched to what the firmware expects, direct GATT commands actuate the valve.

## Decryption attempts so far

Tried with both mesh keys, AES-ECB and AES-CTR with various IV constructions
(zero, MAC forward/reverse, MAC + counter): **none yielded plaintext containing
the `AA 77 5A 0F` MSG_HEADER**. For the broadcast payloads specifically, this is
unsurprising — a broadcast protocol would likely use:

- AES-CCM (the standard for BLE Mesh and most mesh protocols).
- A nonce that incorporates a **broadcast sequence number** (which we don't yet know how to extract).
- Possibly the source MAC AND a per-message counter.
- Possibly authenticated encryption with a MIC trailing the ciphertext.

Without multiple samples of known-content commands (e.g., 10× "water on for 60s"
so we can spot the constant fields), the nonce construction can't be reversed.

## Take #6 — phone "Control via Bluetooth" also didn't surface a connection

`captures/phone_direct_*.pcap`, 5 min, 151,814 packets.

Triggered the official B-Hyve app's "Control via Bluetooth" button on the Zone A
timer. The app showed **find → connect → sync** — succeeded. Then triggered
watering on, then off.

**Capture findings:**

- 597 ADV/SCAN_RSP packets from the Zone A timer (`AA:BB:CC:00:00:01`).
- 6 SCAN_REQs targeting the Zone A timer (some scanner — probably the phone or hub — actively scanning).
- 188 total CONNECT_INDs across **147 unique advertiser addresses**.
- **ZERO of those CONNECT_INDs target any Orbit-pattern address** (no advertiser under Orbit's vendor OUI `44:67:55`, even allowing for bit-error variants).
- 720 ADV_EXT_IND packets (extended advertising, BLE 5.x) — but zero usable aux pointers in our captures.

At the time this read as a strong signal that no capturable BLE connection to the
timer was happening. Three explanations were on the table:

1. **Random/RPA addresses on both peers** — Orbit uses Resolvable Private Addresses for the connection peer, derived from an Identity Resolving Key we haven't extracted. Without the IRK we can't tell which CONNECT_IND is "to a B-Hyve device."
2. **Connection setup happens on extended-advertising secondary channels** that single-channel sniffers (PCA10059) can't reliably follow. Multi-channel sniffers (Ubertooth, HackRF) might.
3. **"Control via Bluetooth" actually goes phone→hub→broadcast.** The name suggests direct, but the flow might still route through the local hub's BLE peripheral mode.

> **Correction:** Explanations (1) and (2) were the real story — the connection
> and its GATT commands were happening but were **not captured** by the
> single-channel rig (RPA peers + extended-adv secondary channels). The
> broadcast-only reading in (3) was wrong.

## Tooling assessment — it is NOT BLE SIG Mesh, and the payloads are BLE5 extended adv

Two conclusions that still stand and redirect the effort:

**1. This is a proprietary broadcast, not standard Bluetooth SIG Mesh.** Across
all captures the encrypted advertisement payloads ride in **manufacturer-specific
data** under company ID `0x047f` ("Pro-Mark, Inc."). We saw **no** SIG-Mesh AD
types (Mesh Message `0x2A`, Mesh Beacon `0x2B`) and **no** mesh service UUIDs
(Provisioning `0x1827`, Proxy `0x1828`). So there is **no spec-based shortcut** —
the SIG Mesh crypto (AES-CCM with a documented `type‖pad‖seq‖src‖ivIndex` nonce,
crackable with the nRF Mesh app once the NetKey is known) does **not** apply. Any
broadcast decryption would mean reversing the *proprietary* nonce. (Confirmation
scan to keep on file: a passive scan should never surface `0x2A`/`0x2B`/
`0x1827`/`0x1828` for a device under Orbit's vendor OUI `44:67:55`.)

**2. The 46-byte hub broadcast must be BLE5 extended advertising.** Legacy
advertising AdvData is capped at **31 octets**; a 46-byte manufacturer-data
element cannot fit a legacy PDU. This matches the `720 ADV_EXT_IND` packets seen
in Take #6. The single-channel **PCA10059 caught a 46-byte payload only once** —
consistent with unreliable capture of the AUX (secondary-channel) extended-adv
packets. Any effort that needs **many** samples of that hub broadcast is bottle-
necked by the capture rig, not just the trigger log.

**Recommended capture upgrade:** a **Sniffle** BLE5 sniffer (TI CC1352/CC2652,
e.g. a reflashable Sonoff dongle), which reliably follows extended advertising.
Capture filtered to a hub MAC (`AA:BB:CC:00:00:10` / `AA:BB:CC:00:00:11`) with
`sniff_receiver.py -e`, then diff the extracted `0x047f` payloads to localize the
changing nonce/counter bytes across repeated identical actions. A BLE5 *host
adapter* + BlueZ extended scan is a cheaper thing to try first, but BlueZ
extended-adv scanning is known-flaky (bleak issue #1347), so treat it as a quick
experiment.

## Bugs noted along the way

1. The analyzer defaulted to the first mesh's `network_key`, which only matches one of the two meshes. It should pick the right mesh based on which timer's traffic is being decoded.
2. The original framework assumed a connection-based ATT-write protocol only. For broadcast decoding, a separate code path is needed that:
   - Walks ADV_IND packets per source MAC.
   - Extracts the manufacturer-specific data field (Type 0xff).
   - Strips the manufacturer ID (`0x047f`, "Pro-Mark, Inc.").
   - Treats the remaining bytes as the encrypted payload to decrypt.
