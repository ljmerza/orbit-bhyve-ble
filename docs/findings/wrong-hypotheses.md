# Dead Ends and Falsified Hypotheses

*Reverse-engineering notes; imported from the maintainer's research project and scrubbed of device-specific identifiers.*

Most write-ups of a reverse-engineering effort only show the path that worked, which makes the result look inevitable and teaches almost nothing. The reality is that progress came from a series of confident, well-argued hypotheses that turned out to be wrong. Each one cost days, and each one was falsified by a single decisive test. Documenting those dead ends is the most useful thing this project can leave behind: it shows *why* a wrong idea looked right, and it stops the next person from spending the same days re-deriving the same mistake.

Three hypotheses stand out. Each was internally consistent with the evidence available at the time. Each was wrong. Here is what we believed, why it seemed right, how it fell, and what the actual answer turned out to be.

---

## Hypothesis 1 — "Provisioning writes the network key to GATT char `0x6c76`"

### What we believed

The integration completed the AES handshake and its encrypted command writes were accepted with no GATT error, yet valves refused to actuate. Decompilation of the official app's bytecode bundle revealed a **fourth** GATT characteristic we had never used, `0x6c76` (`network_char_uuid`), alongside the three we already spoke (`aes`, `write`, `read` on the `0xfe32` service). The app appeared to write an 18-byte payload — a 2-byte little-endian prefix (`01 00`) followed by the 16-byte network key — to `0x6c76` *before* the AES init. The conclusion seemed unavoidable: this "provisioning" write is the device's signal that the central is authorized to issue commands, and because our code never performed it, the command-handling layer silently ignored everything we sent.

### Why it seemed right

- It explained the exact symptom precisely: connect succeeds, handshake succeeds, writes are accepted, but nothing actuates. A "you are not provisioned, so I will accept and ignore your commands" state fit perfectly.
- The characteristic was real and named `network_char_uuid` in the app's own code — not something we invented.
- The provisioning step lived in the app's connect-and-prepare flow, gated by an in-memory "already provisioned?" check, exactly where a one-time-per-session authorization write would go.
- We had never *observed* the phone doing this over the air (it uses resolvable private addresses and our single-channel sniffer couldn't follow its connections), so an unobserved-but-plausible step was easy to believe.

### How it was falsified

We implemented the write: `[01 00 || <network-key>]` to `0x6c76`, before the AES handshake. Every device rejected it with **GATT error 3, "Write not permitted"** — including the one reference device that actuates correctly with our existing code. The characteristic is firmware-locked. Whatever the app does to that characteristic (if anything, at runtime), it is not a plain client write we can replicate, and it is emphatically not the missing step for the broken devices — because the *working* device also refuses the write yet works fine without it.

### The actual answer

The network key is **per-account, fetched from the vendor cloud, and never written over BLE** by a third-party central. There is no provisioning-write handshake to reproduce. The provisioning code was reverted; the `0x6c76` constant was kept in the source for documentation only. The real cause of the broken devices lay elsewhere entirely (see Hypothesis 3).

### Lesson

A characteristic existing in the app's symbol table does not mean a client is *allowed* to write it, and "we never captured it, but it's plausible" is a hypothesis, not evidence. The single cheapest test — just attempt the write and read the GATT status — falsified in one shot what a plausibility argument had propped up for days. When a theory predicts a specific observable ("this write unlocks commands"), run the one-line experiment that can kill it *before* building on top of it.

---

## Hypothesis 2 — "Control is broadcast-mesh only; direct point-to-point BLE is impossible"

### What we believed

After several passive sniffer captures, we concluded the hub-to-timer protocol was **not** a GATT connection at all but a **broadcast mesh**: the hub places encrypted commands into BLE advertisement data and timers receive them by passive scanning. On that theory, the whole idea of connecting to a timer and driving it over the GATT data channel was a dead end — the connection existed only for setup-class operations (pairing, firmware, key acceptance), and routine watering commands were delivered exclusively over the broadcast path. The recommended conclusion was that direct BLE control was fundamentally incompatible with this hardware and that the only viable Home Assistant path was a cloud-based integration.

### Why it seemed right

- Across ~12 minutes and ~250,000 packets we saw **zero `CONNECT_IND`** to any timer- or hub-pattern address, and **zero ATT writes/notifications** — only frequent advertisements (30–50 Hz) carrying variable-length encrypted-looking payloads under company ID `0x047f`.
- Multiple distinct payload sizes (2, 13, 20, and 46 bytes) looked like a real message family — heartbeat, status, and a long-enough-to-be-a-command hub broadcast.
- Even with the official app in its "Control via Bluetooth" mode, we captured **no** `CONNECT_IND` to any timer-pattern address — 188 connection requests across 147 advertisers, none matching the vendor OUI.
- The GATT writes we *did* make were accepted but produced no actuation, which the broadcast theory explained neatly: we were writing to a channel that wasn't the real command channel.

### How it was falsified

The broadcast picture was an artifact of the capture rig, not the protocol. The devices use resolvable private addresses for their connections, and the long 46-byte hub payloads were BLE5 extended advertising — legacy AdvData caps at 31 octets, so a 46-byte element *cannot* be a legacy PDU. A single-channel PCA10059 sniffer cannot reliably follow either RPAs or the secondary-channel extended-advertising packets, so it saw advertisements and missed every connection. The absence of `CONNECT_IND` in our logs meant "our sniffer couldn't follow the connection," not "no connection happened." Direct per-device GATT control does work — **the entire integration is built on it**, connecting to each timer and driving its valve over the data channel.

### The actual answer

Direct, point-to-point BLE control of each timer is exactly how the device is driven: connect, run the AES handshake, and write encrypted command frames on the data channel. The broadcast advertisements are real but are status/beacon traffic, not the control path a central must use. The "broadcast-mesh only" conclusion inverted cause and effect: an under-powered sniffer produced an absence of evidence that we read as evidence of absence.

### Lesson

Absence of evidence is not evidence of absence — *especially* when your instrument is known to be blind to the thing you're looking for. Before concluding "X never happens," establish that your tooling *could have seen X if it did happen*. A single-channel sniffer against RPAs and BLE5 extended advertising was structurally incapable of observing the connections, so its silence proved nothing. Had this hypothesis won, the project would have been abandoned in favor of a cloud integration — the most expensive dead end of all: the one that stops you from trying the thing that actually works.

---

## Hypothesis 3 — "`d7 47` is a fixed protocol magic constant"

### What we believed

Frames on the working reference device began with the bytes `d7 47`, and the integration hardcoded them as a protocol magic constant (`D747_MAGIC = bytes([0xD7, 0x47])`), prepended to every command frame for every device. On the reference unit this worked end-to-end, so `d7 47` looked like a fixed envelope marker shared by the whole device family — the same kind of constant as the message header or the frame-type byte.

### Why it seemed right

- It was empirically load-bearing on the one device we developed against: frames starting `d7 47` actuated the valve, and the acknowledgements came back with the matching prefix.
- Two bytes at the very start of every frame is exactly what a protocol magic *looks* like, and we already had other genuine constants (message header, frame-type, trailer formula) in the same frames, so one more fit the mental model.
- All development and regression testing happened against that single reference device, so the hardcoded value was never contradicted — every frame it built was correct *for that device*.

### How it was falsified

Other devices of the same hardware family exhibited a sharp, distinctive failure: the AES handshake completed, all init writes succeeded with no GATT error, and **the device returned zero notifications at any stage**. The working reference device returned an acknowledgement per init step; the others returned none. A phone-app BTSnoop capture against one of the broken devices settled it: same magic byte, same trailer formula (`sum(pt) + 0x10 + len`), same AES cipher, same 8-step init — **only the 2-byte prefix differed**. The captured frames for that device started with *its* prefix, not `d7 47`.

`d7 47` was not a constant at all. It was the reference device's own `mesh_device_id`, `18391` (`0x47D7`), written little-endian → `d7 47`. By hardcoding it, the integration addressed *every* device's frames to the reference unit's mesh address. The reference unit answered because the frames were addressed to it; every other device silently dropped frames addressed to someone else — which is exactly why they produced zero notifications while erroring nowhere.

### The actual answer

Each device's frame is prefixed with **its own** 2-byte mesh address — its `mesh_device_id` in little-endian — not a shared constant. In this repo the fix lives in `custom_components/orbit_bhyve/devices/ht25.py`: the frame builders are instance methods that use a per-device `mesh_address` property (`self.mesh_device_id.to_bytes(2, "little")`) instead of the hardcoded `d7 47`. A companion address, the paired hub's mesh address (`hub_mesh_address`), is used for the second init frame and is resolved from the cloud record (with an empirical per-network-key fallback). The change is backward-compatible: for the original reference device, the dynamically built prefix is byte-identical to the old hardcoded `d7 47`, so it keeps working as a regression target while the previously broken devices now actuate.

### Lesson

Beware constants derived from a sample size of one. A value that is genuinely device-specific is indistinguishable from a global constant when you only ever test against one device — the hardcode is *correct* for that device, so nothing pushes back. The tell was there in plain sight (`d7 47` reversed is `0x47D7` = `18391`, a value that also appeared as that device's `mesh_device_id`), but it took a second device to force the question. When something works on exactly one unit, treat every literal in the working frame as a suspect until a *different* unit confirms it's actually shared.

---

## Common thread

All three failures share a shape: a hypothesis that perfectly explained the evidence *available at the time*, defeated by one decisive test that the evidence had made easy to skip.

- H1 was killed by attempting the write and reading one GATT status code.
- H2 was killed by recognizing the instrument was blind to the thing it "didn't see."
- H3 was killed by testing against a second device.

The recurring discipline: identify the single cheapest experiment that could falsify the theory, and run it *before* building on the theory — not after the scaffolding is already load-bearing.
