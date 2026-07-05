# Orbit B-Hyve BLE: transport reliability & hardware-verified behavior (Gen2 + XD)

An **operational** companion to the existing protocol documentation: the transport-reliability
behavior and fine-grained, hardware-verified field semantics you need to make BLE control of the
Gen2 (HT25G2) and XD (HT34A) valves work reliably — especially over an ESPHome/BlueZ Bluetooth
proxy. It deliberately does **not** restate the GATT layout, cipher, handshake, framing, or the
message schema — those are already well covered (see *Prior art* below); this is the layer on top.

> **Verification.** Findings were validated on hardware the authors own — **Gen2 (HT25G2-0001,
> fw `0111`)** and the **4-station XD (HT34A-0001, fw `0107`)** — over ESPHome BT proxies and a
> direct adapter. Tagged **[HW-verified]** (driven end-to-end) or **[capture-decoded]** (decoded
> from an official-app capture + operator action log). No vendor firmware/app code is reproduced.
> **A firmware update may change any of this.**

## Prior art & credits

- **wxfield** — originator of this reverse-engineering effort (the original
  `Orbit_B-Hyve_4Port_Controller` project, MIT): the AES-ECB-as-CTR cipher, the frame trailer
  checksum, the `AA775A0F`/CRC-16 inner framing, the initial protobuf schema, and the first HA
  integration that actuated the XD.
- **knobunc** — [`ha-orbit-bhyve-ble`](https://github.com/knobunc/ha-orbit-bhyve-ble) carries a
  comprehensive, hardware-verified **`PROTOCOL_SPEC.md`** (GATT, cipher, handshake, framing, message
  types incl. the HT25 mesh format) and a full **`protobuf_schema.json`**. Treat those as the
  structural reference; **this document is complementary** — it adds the reliability behavior and the
  verified status-decode semantics that spec does not cover.

Field notation `#N` = protobuf field number within the `AA775A0F` inner message.

---

## Part 1 — Transport reliability

These are the failure modes that make a naive client flaky (and that get *worse* over a BLE proxy),
with the mitigation that fixed each in practice.

### 1.1 One BLE session at a time
The battery valves accept a **single** connection. The most common source of the anomalies below is
the **vendor app holding the session** while a proxy also connects — close the app for clean
operation/captures. Because of this limit, an **ephemeral connect-per-operation** model (connect →
do one thing under a per-device lock → disconnect) is markedly more robust than pooling one
long-lived session across background polls, UI actions, and commands.

**This is the model we settled on and would recommend for any proxy-fronted deployment.** Moving from
a pooled long-lived session to ephemeral connect-per-op eliminated the cross-operation CTR-desync and
proxy-slot-starvation failures described in §1.3–1.4. The cost is an extra connect + handshake
(~2–5 s over a proxy) per poll/command; on the idle cadence that is a negligible battery hit, and it
trades cleanly for reliability. **[HW-verified]**

### 1.2 Solicited RX is reliable; the unsolicited connect-time push is not (when active)
After any TX the device answers with a full `#16` status burst. On connect, an **idle** device
pushes `#16` reliably — but an **active** device (**watering** `#16.#1=4` *or* **rain-delay**
`#16.#1=3`) often pushes only a minimal clock-bearing ack, or nothing. ⇒ For a dependable read in
**any** state, send a benign **`#15{}` status request** after the handshake and decode the elicited
`#16`, rather than waiting for a volunteered push. **[HW-verified]**

### 1.3 Duplicate RX notifications desync the CTR stream
A proxy/link can **re-deliver a byte-identical RX frame** (observed: a lone dup after a valid decode
on the XD; a burst of ~60 identical frames in ~30 ms on a Gen2 under app contention). Because RX is
one continuous CTR keystream, decrypting each delivery advances the per-direction counter, so **every
frame after the duplicate fails CRC** and no further state decodes. **Fix:** drop a byte-identical
re-delivery of the immediately-preceding frame *before* buffering/decrypting. This is safe — the same
plaintext at a new counter always yields *different* ciphertext, so identical consecutive ciphertext
is definitionally a re-delivery. Reset the marker on each new session. **[HW-verified]**

### 1.4 A `#57` flow subscription is persistent and starves `#16`
Subscribing to flow (`#57{#1=intervalMs,#2=2}`) makes the device stream `#59` ~1/s. The subscription
**survives reconnects** and, while active, **suppresses the `#16` status reply** — a `#15{}` returns
only `#59` (no run-state). A valve left subscribed answers every poll with `#59`, and unbounded
per-poll re-subscription can **wedge** it. **Fix:** always send **`#57{#1=0}`** when a flow read is
done (e.g. in a `finally`). Separately, a dropped `#59` on a long-lived connection desyncs the RX
counter; recover by **reconnecting** (a fresh handshake re-seeds IV + counter). **[HW-verified]**

### 1.5 Some firmware reports a *static* `remaining`
On a manual run, a valve may report the **same** `#16` remaining-seconds on **every** poll instead of
counting down. A client that re-arms a wall-clock auto-close from each reading will push the end time
forward forever and the entity appears to water indefinitely. **Fix:** drive the auto-close from the
**first** reading and re-anchor only when a later reading is *earlier*. **[HW-verified]**

### 1.6 CTR desync self-heal
Any of the above can leave the RX counter desynced (every frame decodes to garbage that fails CRC).
Detect it — a decrypted frame whose inner header is not `AA775A0F` on the **first** frame of a reply
— and **disconnect to force a fresh handshake** on the next operation. (Gate it to a reply's first
frame so a legitimately multi-frame CTR-streamed reply's continuation blocks don't false-trip it.)
**[HW-verified]**

---

## Part 2 — Hardware-verified status-decode semantics

Behavioral detail in the `#16` status block (and friends) that the structural specs don't pin down.
All confirmed on live hardware.

### 2.1 Run progress / seconds-remaining — and a Gen2↔XD layout flip
Run progress lives in **`#16.#6` `{#5 total sec, #7 remaining sec}`**, present only while watering.
**The nesting differs by model:** the **XD** reports remaining at **`#16.#6.#7`**, the **Gen2** at
**`#16.#7.#6`**. Decode both, and **validate the result is an integer** — a Gen2 *idle* frame emits
`#16.#7` as a short byte submessage that must not be mistaken for a countdown. **[HW-verified]**

### 2.2 Which zone is running (multi-station)
The active station is **`#16.#2.#2.#3.#1`** (`stationId`, 0-indexed → zone = id + 1). On the
4-station XD this is the **only** place the specific running station is reported; without it a poll-
or app-discovered run leaves the zone unknown and **every** zone renders as watering. **[HW-verified]**

### 2.3 A STOP reply is a bare `#30` ack — confirm with a follow-up `#15{}`
A stop answers with **only** a bare `#30 {#1=1}` ack (`f201 02 0801`) — **no `#16` status block** — so
a stop **cannot** be confirmed from its own reply. Confirm by issuing a follow-up `#15{}` and decoding
the resulting `#16` run-state (**idle `1` or rain-delay `3` both mean "not watering"**); treat the
`#30` as provisional and rely on the on-device auto-close as the safety net. A *start* reply, by
contrast, usually *does* carry `#16`. **[HW-verified]**

### 2.4 Rain delay — the device honors an absolute expiry, anchored to its own clock
`#17 {#1=minutes, #3=expiryUnixUTC, #4=1}` sets it; `#1=0` clears. The device **honors `#3` (absolute
expiry) literally and stores `#1` independently** — a skew probe sent `#1=360` with a deliberately
skewed `#3` and the device echoed *both* back unchanged. ⇒ **anchor `#3` to the *device* clock**
(`#7`, below), not the host clock, or a clock-skewed device ends the delay early/late. Run-state while
active is **`#16.#1=3`**. Status echoes in **`#16.#13 {#1 min, #3 expiry, #4 enabled}`**, but **`#4` is
often absent** (a fresh clear may echo a bare `{#1=0}`) — derive **`active = (minutes > 0)`** rather
than gating on `#4`. A full `#16` with run-state ≠ 3 and no `#13` block means it cleared out-of-band.
**[HW-verified]**

### 2.5 Flow (`#57`/`#59`) is Gen2-only and is a counter, not a rate
Subscribe `#57` → the device streams `#59 {#1 flow-active, #3 cumulative}` (~1/s). Two semantics that
bite if assumed wrong:
- **`#59.#3` is a CUMULATIVE per-run counter, not an instantaneous rate** — it climbs monotonically
  for one valve-open and resets on the next. The rate is its **slope** (Δcounts/Δt). One measured
  install calibrated to ≈ **433 counts/gallon** (a 44.5 s window logged +1443 counts while a bucket
  caught 3.33 gal); calibration is per-install.
- **`#59.#1` means "water currently flowing", not "valve open"** — valve open with supply throttled
  reads `#59.#1=0`. So `#59.#1` may only *assert* watering; **`#16.#1` is authoritative** for
  valve-open. (A useful side effect: nonzero flow while `#16.#1` is idle = a stuck valve / leak.)
- **Flow is Gen2-only** — the XD, sent the same `#57` directly over BLE, returns **zero `#59`**.
  **[HW-verified]**

### 2.6 `#7` device clock
The RX wrapper carries the device's own Unix-epoch clock at **`#7`**. Read it (via a `#15{}` elicit)
and use it to anchor the rain-delay `#3` expiry (§2.4). **[HW-verified]**

---

## Device / firmware coverage

| Device | Hardware | Firmware | Verified here |
|---|---|---|---|
| Gen2 1-station | `HT25G2-0001` | `0111` | reliability §1, progress `#16.#7.#6`, stop ack, rain delay, flow `#57`/`#59`, clock |
| XD 4-station | `HT34A-0001` | `0107` | reliability §1, progress `#16.#6.#7`, **active-zone** decode, stop ack, rain delay, **no flow** (confirmed) |

Other Gen2/XD SKUs in the same families are expected-compatible but were not exercised. See
knobunc's `PROTOCOL_SPEC.md` for the HT25 mesh (fw `0041`/`0085`) frame format, which this document
does not cover.
