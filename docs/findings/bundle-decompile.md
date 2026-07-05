# Bundle decompile + Frida-gadget findings

*Reverse-engineering notes; imported from the maintainer's research project and scrubbed of device-specific identifiers.*

Written 2026-05-03. Updates the picture from the earlier handoff notes after a full
session of bundle decompilation and runtime instrumentation.

> **Identifier masking.** The worked hex examples below are per-session ephemeral
> (session IV and counters are derived from the BLE handshake, not from the network
> key) and are safe to publish. The network key itself is never reproduced. Any
> embedded device identifiers — the 2-byte mesh prefix at the start of each plaintext
> and any 4-byte mesh device IDs in the payload — are masked as `xx`.

## TL;DR — RESOLVED

The full cipher scheme is reverse-engineered. All 7 captured BTSnoop frames decrypt
cleanly and every trailer formula matches.

**Cipher**: AES-128-ECB used as a keystream generator (CTR-style XOR).
**Key**: `base64Decode(deviceData[":network-key"])` — a per-account 16-byte value.
The real key is never shown here; the worked examples below use the synthetic
placeholder `00112233445566778899aabbccddeeff` where a key value is needed.
**IV (12 bytes)**: `init_rx[0..4] || init_tx[4..12]` — built from the BLE AES-handshake
exchange.
**Counters**: TX and RX have **separate counters**. Both seeded from `init_tx[12..20]`:
- `:enc-ctr` (TX) initial = `uint32_LE(init_tx[12..16])`
- `:dec-ctr` (RX) initial = `uint32_LE(init_tx[16..20])`
- Each counter increments by 1 per frame (per direction).

**Block**: `IV (12B) || counter_LE_4B (4B)` → AES-ECB-encrypt → XOR with plaintext.
**Trailer**: `sum(plaintext) + 0x10 + len` (16-bit, little-endian).

For the captured session (these values are session-derived and ephemeral):
```
IV = 6345939f dd1a4ab3 3b4aad64
enc-ctr0 = 0xf9bbfffa     (TX side)
dec-ctr0 = 0xa0e8129a     (RX side)
```

Decrypted plaintexts confirm a custom binary protocol (NOT protobuf as the earlier
handoff guessed). All start with the same 2-byte mesh prefix (a device identifier,
masked as `xx xx` here). See "Decrypted plaintexts" section below.

## Frida-gadget toolchain — fully operational

The `frida_setup/01..05` scripts get you to a launching patched APK. Beyond what's
documented there:

| Step | What we did differently from the original scripts |
|---|---|
| Path | Scripts hardcoded an absolute `/path/to/...` prefix — fixed to derive from `$0`'s parent. |
| Bundle is a **Play split base** (`requiredSplitTypes="base__abi,base__density"`). objection's `--ignore-nativelibs` is needed; **and** the gadget can't live in `base.apk` because the system only loads native libs from the ABI split. |
| Solution: pull all 5 splits via `adb pull`, patch `base.apk` smali (objection adds `loadLibrary("frida-gadget")` to MainActivity `<clinit>`), inject `libfrida-gadget.so` into `split_config.arm64_v8a.apk`, re-sign every split with the same objection debug keystore, `adb install-multiple`. |
| Repack details that bite: `lib/arm64-v8a/libfrida-gadget.so` must be **stored uncompressed**, `resources.arsc` must be stored, all libs page-aligned (`zipalign -p 4`). |
| `apktool b` chokes on a private-resource reference in `res/layout/number_picker_material.xml`. Strip the `android:textAppearance="@android:style/TextAppearance.SlidingTabActive"` attr before rebuilding. |
| **PairIP (Google's anti-tamper licensecheck)** ships in this APK. After resigning, `LicenseContentProvider.onCreate()` redirects to Play Store. Stub it to `return true` (`smali_classes2/com/pairip/licensecheck/LicenseContentProvider.smali`) before rebuilding base. No other PairIP entry points in the manifest, so this single stub is enough. |
| Gadget needs a JSON config in the arch split at `lib/arm64-v8a/libfrida-gadget.config.so` — content: `{"interaction":{"type":"listen","address":"127.0.0.1","port":27042,"on_load":"wait"}}`. |
| Connect from host with `adb forward tcp:27042 tcp:27042` then Python: `frida.get_device_manager().add_remote_device('localhost:27042').attach('Gadget')`. |

The re-signing keystore is objection's default debug keystore (its password is
objection's built-in default — not reproduced here).

## Bundle structure (decompiled)

`assets/index.android.bundle` — 16 MB Hermes bytecode, magic `c61fbc03`, version 96.

Decompiled with `hbc-decompiler` (from PyPI `hermes-dec`) → 102 MB JS at
`hermes_work/decomp.js`. Disassembly at `hermes_work/disasm.hasm` (196 MB).

### AES library

`module$aes_js` is the standard npm `aes-js` library.
- `Function #27951` = `ModeOfOperationECB` constructor
- `Function #27956` = `ModeOfOperationCTR` constructor
- The library defines all of ECB / CBC / CFB / OFB / CTR even though the user code only
  calls ECB.

### User code's cipher construction (the BLE messaging path)

In ClojureScript, function `init_aes` (decomp.js around line 1017950):

```js
// destructure device data
r6 = __destructure_map(a0)              // a0 = device record from cloud
const networkKeyString = get(r6, :network-key)   // base64 string
const keyBuffer = base64ToBuffer(networkKeyString)  // 16 bytes
// build BLE service path  [:lib :ble :service :data ... :aes]
r7 = service_path(r6, [..., :aes])

// construct cipher
r3 = aesjs.ModeOfOperation.ecb           // function ECB(key)
const cipher = new r3(keyBuffer)         // r12 in decomp is undefined; ECB takes 1 arg

// store cipher into app db at the ble path
db.core.write(ctx, r7, cipher)
```

Closure slots resolved by tracing keyword definitions:
- `_closure1_slot3313` = `:network-key`
- `_closure1_slot3777` = `:aes`
- `_closure1_slot5988` = `:iv`

### Encryption call site (BLE write path)

`perform_crypto(ctx, ctr, plaintext_seq, acc)` at decomp.js line ~1018347, ClojureScript
named `:lib/:ble/:service/:common/perform_crypto`.

For each 16-byte chunk of plaintext:
```js
buf = Buffer.alloc(encryption_frame_size)        // 16 bytes
ivBytes = (:iv ctx)                              // 12-byte IV from cipher state
ivBytes.copy(buf, 0, 0, 13)                      // copies bytes 0..12 (note: 13 bytes, but
                                                 // overwritten by next op at offset 12)
buf.writeUInt32LE(ctr, 12)                       // counter as little-endian uint32 at [12..16]

cipher = (:aes ctx)
keystream = cipher.encrypt(buf)                  // AES-ECB(key, buf)

ctr_next = inc_ctr(ctr)
remaining_pt = drop(16, plaintext_seq)
this_pt     = take(16, plaintext_seq)
xored = map(bit_xor, keystream, this_pt)         // XOR keystream with plaintext
accumulator = concat(acc, xored)
```

So the full block construction is:
- `block = ctx[:iv][0..12] || u32_LE(counter)`  (16 bytes)
- `keystream = AES_ECB_encrypt(key, block)`
- `ciphertext = plaintext XOR keystream`

Counter starts at *something* and increments per block via `inc_ctr`. (Resolved below —
see "What unlocked it".)

### The two crypto modules (don't confuse them)

There's also a SECOND, separate crypto module at decomp.js line 609571 — `make_cbc` /
`encrypt_string_BANG_` / `decrypt_string_BANG_` — uses **AES-CBC** with a key passed
through `trim_pad_encyption_key` (sic). This is an internal *string* encryption used by
the app's local DB, not the BLE messaging path. Don't confuse the two.

## What unlocked it

The breakthrough was finding the cipher-state construction by tracing where `:iv` is
PUT (not just READ). It's set in the `init_aes` `.then()` callback at decomp.js
~line 1017340-1017430:

```js
// init_aes (a0=ctx, a1=init_tx_bytes); _closure2_slot0 = a1
rn_ble_manager.read(peripheralId, service_uuid, aes_char_uuid).then(function(init_rx_bytes) {
  // validate handshake
  if (every?(zero?, take(4, init_rx_bytes))) error;          // init_rx[0..4] must be non-zero
  if (not_every?(zero?, drop(4, init_rx_bytes))) error;      // init_rx[4..end] must be all zero

  // build cipher state buffer = init_rx[0..4] + init_tx[4..end]
  const buf = Buffer.from(concat(take(4, init_rx_bytes), drop(4, _closure2_slot0)));

  // store three values into the cipher state map
  cipherState[:iv]      = buf.slice(0, 12);                  // 12-byte IV
  cipherState[:enc-ctr] = buf.readUInt32LE(12);              // TX counter seed
  cipherState[:dec-ctr] = buf.readUInt32LE(16);              // RX counter seed
});
```

Resolved closure-slot keywords:
- `_closure1_slot3313` = `:network-key`
- `_closure1_slot3777` = `:aes`
- `_closure1_slot4479` = `:enc-ctr`   ← TX counter
- `_closure1_slot1656` = `:dec-ctr`   ← RX counter
- `_closure1_slot5988` = `:iv`

## Decrypted plaintexts (verification)

The leading 2-byte mesh prefix and any 4-byte mesh device IDs are masked as `xx`.

```
1308 TX  cnt=0xf9bbfffa  pt=xx xx 81 05 40 xx xx xx xx 10 ff
1311 RX  cnt=0xa0e8129a  pt=xx xx c1 05 40 xx xx xx xx 10 ff 00
1312 TX  cnt=0xf9bbfffb  pt=xx xx 02 02 40 00
1314 RX  cnt=0xa0e8129b  pt=xx xx 42 02 40 01 ff ff ff ff 00 00
1316 RX  cnt=0xa0e8129c  pt=xx xx a0 02 40 01 ff ff ff ff 00 00
1317 TX  cnt=0xf9bbfffc  pt=xx xx 03 03 40 00 00 00 00 00 00 00
1319 RX  cnt=0xa0e8129d  pt=xx xx 43 03 40 xx xx xx xx 38 0b 00
```

All trailers match `sum(pt) + 0x10 + len` exactly (verified against the unmasked
plaintexts). ✓

Custom binary protocol observations:
- All plaintexts start with the same **2-byte mesh prefix** (a device identifier,
  masked as `xx xx`; NOT protobuf — the earlier handoff was wrong about this)
- Byte 2: looks like a type/direction flag (TX uses 0x81, 0x02, 0x03; RX uses 0xc1, 0x42, 0xa0, 0x43 — high bit set on RX seems intentional)
- Byte 3: sequence number — pairs match between TX/RX (1308↔1311 both have 0x05; 1312↔1314 both have 0x02; 1317↔1319 both have 0x03)
- Byte 4: constant `0x40`
- Bytes 5+: payload (a 4-byte mesh device id — masked `xx xx xx xx` — appears repeatedly in the early frames for the device being controlled)

## Key files this session

Paths are relative to the research toolchain; local filesystem prefixes have been dropped.

| Path | Notes |
|---|---|
| `apk/base.apk`, `apk/split_config.*.apk` | original 5 splits pulled from the device |
| `apk/base.objection.apk`, `apk/split_config.arm64_v8a.apk` | the **patched + signed** versions actually installed on the phone. base has the smali loadLibrary + PairIP-stub edits, arm64 split has the gadget + config |
| `hermes_work/index.android.bundle` | 16 MB original Hermes bundle |
| `hermes_work/decomp.js` | 102 MB decompiled JS |
| `hermes_work/disasm.hasm` | 196 MB disassembly |
| `heap_full.bin`, `heap_full.index` | 964 MB heap dump from the running gadget process |

## Inner mesh plaintext protocol — what we've found

The plaintext is built by a **mesh wire-format encoder**, not protobuf. Trace from the
decompile:

1. The TX driver looks up `(:stream input)` from a map (`_closure1_slot4960` = `:stream`)
   and feeds it to `perform_crypto`.
2. `:stream` is set via `(event->stream tx_subsys event)` (decomp.js ~line 1019944).
3. `event__GT_stream` (~line 1019877) calls
   `lib.serialize.mesh.core.transcode(svc, event)`.
4. `transcode` dispatches on the BLE service implementation. The relevant impl
   (decomp.js line 1042719) does:
   ```
   buf = util.buffer.seq->buffer(
           serialize.common.->le_byte_stream(
             serialize.mesh.core.encode(mesh_body_spec, event, "")))
   ```
5. `mesh_body_spec` (decomp.js ~line 1035967) is **procedurally constructed at runtime**
   as a deeply-nested PersistentArrayMap of field specs (`r195[27] = ...` etc.). It's
   not a static constant and isn't easily reconstructable from grep alone.

The **2-byte prefix does not appear anywhere as a static constant in the JS source** —
not as a byte-array literal, not as a packed 16-bit hex word, and not as either of its
decimal encodings. It is *computed* by the `encode` step from the spec + event. This
strongly implies:

- the prefix is a **fixed mesh-protocol preamble** the encoder emits unconditionally
  (perhaps a length/version byte plus a routing tag), or
- it is a **packed bitfield / device mesh identifier** (subsystem id, mesh layer flags)
  that always renders to the same two bytes for direct-BLE messages to a given device.

Either way, treat the prefix as opaque-but-stable for now. (Subsequent work identified
it as a per-device mesh identifier — hence the masking in this doc.)

### Empirical structure (from the 7 captured plaintexts)

```
byte 0..1  xx xx        2-byte device mesh prefix (masked; constant across a session)
byte 2     <type/flag>  request 0x02/0x03/0x81; response 0x42/0x43/0xc1; notification 0xa0
                        — bit 0x40 is set on RX→TX responses; bit 0x80 appears on
                        session-init-class messages
byte 3     <seq>        sequence id; pairs match between request/response
byte 4     0x40         constant (probably a routing/subsystem id)
byte 5..   <payload>    variable. First TX command after init carries a 4-byte mesh
                        device id (masked xx xx xx xx). Later commands omit the id once
                        the session is bound.
```

Without labeled samples (e.g. "tap Start zone N for D seconds" → known plaintext) we
can't confidently map type/payload → action. The 7 frames we have are all from one
short "Control via Bluetooth → start zone 1 → stop" session and don't contain enough
diversity to disambiguate.

## Next steps (work that remains)

1. **Capture more labeled traffic.**
   Reinstall the original (Play Store) B-Hyve on the phone — the patched APK has
   resource corruption from repeated apktool rebuilds and was crashing on the React
   Switch widget. The original app is unmodified and works. Steps:
   - On phone: Settings → Apps → B-Hyve → Uninstall (removes the broken patched build)
   - Install B-Hyve from the Play Store
   - Settings → Developer options → enable **Bluetooth HCI snoop log**
   - Reproduce a SCRIPTED set of actions, e.g.:
     - `[t=0]`  open app → tap "Control via Bluetooth"
     - `[t=10]` start zone 1 for 60 s
     - `[t=15]` stop
     - `[t=20]` start zone 2 for 60 s
     - `[t=25]` stop
     - `[t=30]` start zone 3 for 60 s
     - `[t=35]` stop
     - `[t=40]` exit
   - `adb bugreport` (or trigger via Settings → Send feedback) to pull a fresh
     `btsnoop_hci.log`
   - Decrypt with `decode_btsnoop_frames.py` (will need to update the constants to use
     the NEW session's `init_tx` / `init_rx` bytes — easy to extract from the new
     BTSnoop)
   - Diff plaintexts side-by-side: bytes that change with zone number / duration are
     clearly identifiable

2. **Implement `turn_on` / `turn_off`** for this device family using the inner binary
   protocol once the action↔payload mapping is known. In this repo the shared cipher
   already lives in `custom_components/orbit_bhyve/connection.py`
   (`encrypt` / `decrypt` / `_aes_xor` / `_handshake`) and is correct — outer frame
   magic `0x10`/`0x11`, IV from the handshake mix, dual counters, trailer
   `sum + 0x10 + len`. What needs writing is the inner mesh (`xx xx …`) payload builder
   for this family, which belongs in `custom_components/orbit_bhyve/devices/ht25.py`.
   (The protobuf-style `_pb_field_*` builders now live in
   `custom_components/orbit_bhyve/devices/ht34a.py` — that is the separate XD device
   family, not the mesh path described here.)

3. **Test end-to-end** with the research CLI (`bhyve.py on 1 60 --device 2`) and a
   Home Assistant entity toggle.

## Frida live-decrypt: tried, not viable

We attempted live-decrypt via Frida-gadget (see `live_decrypt.py`). It works in
principle (TX hook captures setValue calls fine) but consistently SIGSEGVs the patched
RN/Hermes process at fault address `0x367faa9026ffc` shortly after the inlined Java
bridge installs. The crash is in Frida 17.9.5's Java bridge interacting with this
specific Hermes build — same fault occurred with and without `getValue()` hooks active.
Pursuing this further would need a different Frida-gadget version or native-only hooks
(no Java bridge), neither cheap. The BTSnoop path above sidesteps all of this.
