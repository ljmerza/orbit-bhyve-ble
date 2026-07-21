*Contributed by @anahnymous in [issue #19](https://github.com/ljmerza/orbit-bhyve-ble/issues/19); code references adapted to this repository's layout.*

# B-hyve BLE Message Catalog

This document is the authoritative reference for all known BLE application messages in the B-hyve timer protocol.
For the outer BLE framing and encryption details -- including the `0x11` frame format, the 16-bit additive checksum trailer, the AES-128 CTR cipher, and the GATT handshake -- see [`protocol.md`](protocol.md).

> **Scope:** This catalog covers the protobuf **"XD" family** (BLE frame magic
> `0x11`: HT34A / HT32A / HT25G2). The older HT25 **"d7-47" family** (frame magic
> `0x10`) is documented separately in
> [`findings/d7-47-protocol.md`](findings/d7-47-protocol.md). This integration
> implements a subset of the catalog (run-zone, stop, status request, battery
> request, and status/battery decode); the other commands below are documented for
> completeness.

Each decrypted `0x11` frame stream reassembles into application messages with the structure:

```
aa 77 5a 0f  <type:LE16 LE>  <OrbitPbApi_Message protobuf>  <crc16_xmodem:LE16>
```

**Key rule (CORRECTED 2026-06-28):** the 16-bit field after the `aa775a0f` magic -- labeled "Type code" in the tables below -- is the frame **LENGTH** = `len(payload) + 2` (payload + the 2-byte crc16), verified **51/51** frames across captures. It is **NOT** a message type, opcode, channel, or protobuf field number. The device **dispatches purely on the protobuf field** inside the `OrbitPbApi_Message` body (exactly one field set). The "Type code" column values below are therefore just `len(payload)+2` for each captured example -- identify a message by its **protobuf field**, not by that number. (Older revisions of this doc called it a "category/channel"; that model is superseded. Messages with the same payload length collide on this value purely by coincidence -- e.g. several short get requests share `0x05` because their bodies are all 3 bytes.)

> WARNING **Probing hazard:** sending a message with a **malformed or unrecognized protobuf field** makes the device stop answering for the rest of the connection -- an AES-CTR counter desync (it does not advance its RX counter on a rejected frame). When probing unknown messages, use **one fresh connection per probe attempt**. (A wrong length field alone does not desync -- the device simply ignores the message silently, which is what masked the schedule bug for so long.)

---

## Section L -- Live-verified GET requests (Timer-NW, 2026-06-27)

Confirmed against live hardware (Timer-NW `44:67:55:D7:4B:F9`), not just the capture. All queries below are sent on channel **`0x05`** with an empty body except `getDeviceStatusInfo` (channel `0x04`). The "get" returns the matching data/`set*` message -- often `request_field + 1`. Of these, only `getDeviceStatusInfo` and `getBatteryStatus` are implemented in this integration (as the `_GET_STATUS_PB` and `_GET_BATTERY_PB` constants in `devices/ht34a.py`); the other `get*` request forms below are documented for completeness and are not implemented.

| get (request field) | req channel | request frame | response | response field |
|---|---|---|---|---|
| `getDeviceStatusInfo` (f15) | `0x04` | `aa775a0f04007a00bc34` | `0x3E` | f16 `deviceStatusInfo` |
| `getBatteryStatus` (f45) | `0x05` | `aa775a0f0500ea0200dd7e` | `0x16` | f46 `batteryStatus` |
| `getDeviceInfo` (f22) | `0x05` | `aa775a0f0500b20100e1dc` | `0x2F` | f23 `deviceInfo` |
| `getActivePrograms` (f77) | `0x05` | `aa775a0f0500ea04007bd4` | `0x19` | f20 `SetActivePrograms` |
| `getSettings` (f28) | `0x05` | `aa775a0f0500e201002f82` | `0x13` | f29 `SetSettings` |
| `getManualPresetRunTime` (f48) | `0x05` | `aa775a0f0500820300267f` | `0x16` | f49 `manualPresetRunTime` (600 s) |
| `getNextStartTime` (f26) | `0x05` | `aa775a0f0500d201008a47` | `0x3E` | f16 `deviceStatusInfo` |

**Real negatives (empty body, isolated connections, no reply under `0x05`/`0x04`/`0x07`):**
- `getWateringStatus` (f110) -- not a standalone request; `wateringStatus` (0x15/f30) is emitted inside the `timerMode`-triggered status burst.
- `getNetworkStatus` (f36) -- no BLE reply; network status is a cloud/aggregator concern, excluded from the direct-BLE surface.
- `getStationCfg` (f97) -- no reply with an empty body; very likely requires a station-id parameter (untested).
- `getProgramSchedule` (f69) -- **no schedule read exists over BLE.** Probed live 2026-06-27 with a valid `programId` on channels `0x05`/`0x04`/`0x07` (two clean runs): the device returned **nothing** on any of them; an earlier empty-body probe also got no reply. `getActivePrograms` does **not** fill the gap -- `SetActivePrograms` is only the `activeProgramFlags` bitmask + change metadata (schema-confirmed); schedules live in `SetProgramSchedule`/`ProgramSchedules`. The app reads schedules from the cloud. The `getProgramSchedule` request form (`0x05`/f69 + `programId` f1) is recorded here only for the encoding + the negative result; it is not implemented in this integration. **To read a schedule back, WRITE it (`0x5F`) and parse the `0x0067` echo** -- capture-verified (frames 44->59): the echo is a full `OrbitPbApi_Message` (`id` + `timestamp` + f19 `setProgramSchedule`); parsing that echo back into a schedule is not implemented in this integration.

**Note on the catalog below:** the capture-derived "Section A" lists 13 messages but the real h2d count is >=14 -- the capture also contains a `getDeviceInfo` request (h2d `0x05`/f22, capture frame 38) that the original inventory collapsed against the `0x05`/f45 battery request. The request forms above are the authoritative encoding.

---

## Section A -- Captured BLE messages (authoritative, 13)

These 13 messages were observed in `bluetooth_logs/btsnoop_hci.log.last` (Timer_SE session) and in live hardware tests.
Type codes are known from wire traffic; all field names and enum values are from the reconstructed schema (see [`protobuf-schema.md`](protobuf-schema.md)).

### Master table

| Type code | Dir | OrbitPbApi_Message field | Message class | Captured |
|-----------|-----|--------------------------|---------------|----------|
| `0x0004` | h2d | f15 `getDeviceStatusInfo` | `OrbitPbApi_GetDeviceStatusInfo` | yes |
| `0x0005` | h2d | f45 `getBatteryStatus` | `OrbitPbApi_GetBatteryStatus` | yes |
| `0x0007` | h2d | f20 `setActivePrograms` | `OrbitPbApi_SetActivePrograms` | yes |
| `0x000E` | h2d | f14 `timerMode` | `OrbitPbApi_TimerMode` | yes |
| `0x0016` | h2d | f75 `setEpochTime` | `OrbitPbApi_SetEpochTime` | yes |
| `0x0020` | h2d | f18 `setDateTime` | `OrbitPbApi_SetDateTime` | yes |
| `0x005F` | h2d | f19 `setProgramSchedule` | `OrbitPbApi_SetProgramSchedule` | yes |
| `0x0015` | d2h | f30 `wateringStatus` | `OrbitPbApi_WateringStatus` | yes |
| `0x0016` | d2h | f46 `batteryStatus` | `OrbitPbApi_BatteryStatus` | yes |
| `0x002F` | d2h | f23 `deviceInfo` | `OrbitPbApi_DeviceInfo` | yes |
| `0x003E` | d2h | f16 `deviceStatusInfo` | `OrbitPbApi_DeviceStatusInfo` | yes |
| `0x005E` | d2h | f16 `deviceStatusInfo` | `OrbitPbApi_DeviceStatusInfo` | yes |
| `0x0067` | d2h | f19 `setProgramSchedule` | `OrbitPbApi_SetProgramSchedule` | yes |

Note: `0x003E` and `0x005E` carry the same protobuf class (`deviceStatusInfo`); `0x005E` appears to be a watering-status-summary variant (it consistently carries an `OrbitPbApi_WateringStatusSummary` in `f18` of the `DeviceStatusInfo`).
Note: `0x0016` is used for **both** `h2d setEpochTime` and `d2h batteryStatus` -- direction disambiguates.
Note: `0x0067` is d2h only in the capture (device echoes the program schedule); `0x005F` is the h2d set direction.

---

### A.1 -- `0x0004` h2d `getDeviceStatusInfo`

**Direction:** host -> device  
**Frame type:** `0x0004`  
**OrbitPbApi_Message field:** f15 `getDeviceStatusInfo`  
**Class:** `OrbitPbApi_GetDeviceStatusInfo`  
**Encoder:** the `_GET_STATUS_PB` constant in `devices/ht34a.py`

#### Fields

_(no fields -- empty request message)_

#### Captured example

```
aa 77 5a 0f 04 00 7a 00 bc 34
```

Decoded: empty `getDeviceStatusInfo` request -- triggers a `deviceStatusInfo` response burst from the device.

---

### A.2 -- `0x0005` h2d `getBatteryStatus`

**Direction:** host -> device  
**Frame type:** `0x0005`  
**OrbitPbApi_Message field:** f45 `getBatteryStatus`  
**Class:** `OrbitPbApi_GetBatteryStatus`

#### Fields

_(no fields -- empty request message)_

#### Captured example

```
aa 77 5a 0f 05 00 ea 02 00 dd 7e
```

Decoded: empty `getBatteryStatus` request -- triggers a `batteryStatus` response.

---

### A.3 -- `0x0007` h2d `setActivePrograms`

**Direction:** host -> device  
**Frame type:** `0x0007`  
**OrbitPbApi_Message field:** f20 `setActivePrograms`  
**Class:** `OrbitPbApi_SetActivePrograms`

#### Fields

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `activeProgramFlags` | uint32 | required; bitmask of active programs |
| f2 | `lastChangeDateSecEpochUtc` | uint32 | |
| f3 | `lastChangeId` | `OrbitPbApi_InterfaceId` | see enum below |

**OrbitPbApi_InterfaceId enum:**

| Value | Name |
|-------|------|
| 0 | `localInterface` |
| 1 | `bleInterface` |
| 2 | `wifiInterface` |
| 3 | `cellularInterface` |
| 4 | `ethernetInterface` |
| 5 | `lorawanInterface` |

#### Captured example

```
aa 77 5a 0f 07 00 a2 01 02 08 00 35 3e
```

Decoded: `setActivePrograms { activeProgramFlags: 0 }` -- disables all program schedules (sets device to manual-only mode).

---

### A.4 -- `0x000E` h2d `timerMode`

**Direction:** host -> device  
**Frame type:** `0x000E`  
**OrbitPbApi_Message field:** f14 `timerMode`  
**Class:** `OrbitPbApi_TimerMode`  
**Encoders:** `_build_start_pb(station_id, duration_sec)` (run-zone) and the `_STOP_PB` constant (stop) in `devices/ht34a.py`

#### Fields

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `mode` | `Mode` | required |
| f2 | `manualModeParams` | `OrbitPbApi_ManualModeParams` | populated for manualMode; **must still be present as an EMPTY message (`12 00`) for offMode and autoMode** -- the device silently ignores a timerMode write that omits f2 (live-verified, both modes) |

**Mode enum:**

| Value | Name |
|-------|------|
| 0 | `offMode` |
| 1 | `autoMode` |
| 2 | `manualMode` |

**References: OrbitPbApi_ManualModeParams** (top-level schema class, referenced by `timerMode` f2)

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `startTimeIso8601` | string | |
| f2 | `activeProgramFlags` | uint32 | |
| f3 | `stationInfo` | `OrbitPbApi_StationInfo` | repeated |
| f4 | `startTimeSecEpochUtc` | uint32 | |
| f5 | `groupWateringPreDelaySec` | uint32 | |
| f6 | `groupWateringPostDelaySec` | uint32 | |

**Nested: OrbitPbApi_StationInfo**

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `stationId` | uint32 | required; 0-indexed |
| f2 | `runTimeSec` | uint32 | required |
| f3 | `groupId` | uint32 | |
| f4 | `bridgeId` | bytes | |
| f5 | `meshDeviceId` | uint32 | |
| f10 | `type` | `Type` | |
| f11 | `sequenceId` | uint32 | |

**OrbitPbApi_StationInfo.Type enum:**

| Value | Name |
|-------|------|
| 0 | `station` |
| 1 | `soak` |
| 2 | `scheduleSyncDelay` |

#### Captured example (run-zone)

```
aa 77 5a 0f 0e 00 72 0a 08 02 12 06 1a 04 08 00 10 1e e6 05
```

Decoded: `timerMode { mode: manualMode, manualModeParams { stationInfo[0] { stationId: 0, runTimeSec: 30 } } }` -- run station 0 for 30 seconds. **Byte-verified against captured frame 96 and live-verified on Timer-NW.**

---

### A.5 -- `0x0016` h2d `setEpochTime`

**Direction:** host -> device  
**Frame type:** `0x0016`  
**OrbitPbApi_Message field:** f75 `setEpochTime`  
**Class:** `OrbitPbApi_SetEpochTime`  
**Encoder:** (not implemented in this integration)

#### Fields

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `timeSecEpochUTC` | uint32 | |
| f2 | `timezoneOffsetSec` | int32 | |

#### Captured example

```
aa 77 5a 0f 16 00 da 04 11 08 ce d6 f0 d0 06 10 c0 8f ff ff ff ff ff ff 01 25 dc
```

Decoded: `setEpochTime { timeSecEpochUTC: <timestamp>, timezoneOffsetSec: -14400 }` -- sets epoch time with UTC-4 offset. **Required to trigger the first status burst from the device.**

---

### A.6 -- `0x0020` h2d `setDateTime`

**Direction:** host -> device  
**Frame type:** `0x0020`  
**OrbitPbApi_Message field:** f18 `setDateTime`  
**Class:** `OrbitPbApi_SetDateTime`  
**Encoder:** (not implemented in this integration)

#### Fields

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `currentTimeIso8601` | string | required; ISO-8601 with UTC offset |

#### Captured example

```
aa 77 5a 0f 20 00 92 01 1b 0a 19 32 30 32 36 2d 30 35 2d 33 31 54 30 38 3a 33 36 3a 33 30 2d 30 34 3a 30 30 00 eb
```

Decoded: `setDateTime { currentTimeIso8601: "2026-05-31T08:36:30-04:00" }` -- syncs the device clock to local time with UTC offset.

---

### A.7 -- `0x005F` h2d `setProgramSchedule`

**Direction:** host -> device  
**Frame type:** `0x005F`  
**OrbitPbApi_Message field:** f19 `setProgramSchedule`  
**Class:** `OrbitPbApi_SetProgramSchedule`

#### Fields

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `programId` | `OrbitPbApi_ProgramId` | required |
| f2 | `programTypeNotSet` | `OrbitPbApi_ProgramType_NotSet` | oneof program type |
| f3 | `programTypeDayOfWeek` | `OrbitPbApi_ProgramType_DayOfWeek` | oneof program type |
| f4 | `programTypeInterval` | `OrbitPbApi_ProgramType_Interval` | oneof program type |
| f5 | `programTypeOdd` | `OrbitPbApi_ProgramType_Odd` | oneof program type |
| f6 | `programTypeEven` | `OrbitPbApi_ProgramType_Even` | oneof program type |
| f7 | `programTypeRunOnce` | `OrbitPbApi_ProgramType_RunOnce` | oneof program type |
| f8 | `startTimesMinsFromMidnight` | uint32 | repeated |
| f9 | `stationInfo` | `OrbitPbApi_StationInfo` | repeated |
| f10 | `budgetPercent` | uint32 | |
| f11 | `startDateSecEpochUtc` | uint32 | |
| f12 | `stopDateSecEpochUtc` | uint32 | |
| f13 | `lastChangeDateSecEpochUtc` | uint32 | |
| f14 | `lastChangeId` | `OrbitPbApi_InterfaceId` | |
| f15 | `groupWateringPreDelaySec` | uint32 | |
| f16 | `groupWateringPostDelaySec` | uint32 | |
| f17 | `programName` | string | Max **32 bytes** (UTF-8). A longer name makes the device reject the whole `setProgramSchedule` while the separate `#20` enable still lands — the slot then reports enabled but keeps its old schedule. Verified HT34A fw0107 + HT25G2 fw0111. |
| f18 | `intervalHours` | uint32 | |
| f19 | `basicProgramMode` | bool | |
| f20 | `databaseId` | uint32 | |
| f21 | `originDateSecEpochUtc` | uint32 | |
| f22 | `endDateSecEpochUtc` | uint32 | |

**OrbitPbApi_ProgramId enum:**

| Value | Name |
|-------|------|
| 0 | `manual` |
| 1 | `a` |
| 2 | `b` |
| 3 | `c` |
| 4 | `d` |
| 5 | `e` |
| 6 | `f` |

**Nested: OrbitPbApi_ProgramType_DayOfWeek**

| Field | Name | Type |
|-------|------|------|
| f1 | `dayFlags` | uint32 (required) |

**Nested: OrbitPbApi_ProgramType_Interval**

| Field | Name | Type |
|-------|------|------|
| f1 | `intervalDays` | uint32 (required) |
| f2 | `intervalStartTimeIso8601` | string |

**Nested: OrbitPbApi_ProgramType_RunOnce**

| Field | Name | Type |
|-------|------|------|
| f1 | `programFlags` | uint32 |

_(OrbitPbApi_ProgramType_NotSet, _Odd, _Even have no fields in schema)_

`OrbitPbApi_StationInfo` fields: see [A.4](#a4--0x000e-h2d-timermode).

#### Captured example

```
aa 77 5a 0f 5f 00 9a 01 5a 08 01 22 1d 08 01 12 19 32 30 32 36 2d 30 34 2d 31 37 54 30 30 3a 30 30 3a 30 30 2d 30 34 3a ...
```

Decoded (partial): `setProgramSchedule { programId: a, programTypeInterval { ... }, stationInfo [...] }` -- programs schedule A with interval-based watering.

#### Encoder (not implemented in this integration)

Encoding `setProgramSchedule` is not implemented in this integration. For protocol
reference, the frame is built by emitting every present field in ascending protobuf
field-number order, which reproduces the on-wire byte order exactly. A `program_type`
discriminator key is always present. A representative field set (matching the structure
recovered from a `0x0067` echo):

```python
{
    "program_id": 1,                         # OrbitPbApi_ProgramId enum (0=manual,1=a,...)
    "program_name": "Program A",             # optional string
    "program_type": {                        # oneof discriminator
        "kind": "interval",                  # "not_set"|"day_of_week"|"interval"|"odd"|"even"|"run_once"
        "interval_days": 3,                  # interval only
        "interval_start_time_iso8601": "...",  # interval only
        # "day_flags": int                   # day_of_week only
        # "program_flags": int               # run_once only
    },
    "start_times_mins_from_midnight": [360], # list[int]
    "station_info": [                        # list[dict]
        {"station_id": 0, "run_time_sec": 300, "group_id": 0}
    ],
    "budget_percent": 100,
    # ... other scalar schedule fields
}
```

**Verification status:** in the contributor's reference library the `setProgramSchedule` encoder
byte-reproduced the captured h2d `setProgramSchedule` frame, and the full schedule flow is
**LIVE-VERIFIED on Timer-NW (2026-06-28)** (schedule setting is not implemented in this integration).
Setting a schedule that actually runs requires **three writes** plus the app's ordering:

1. **store** -- `setProgramSchedule` (f19) -- defines the program and its zone (`stationInfo.stationId`; Timer-NW = `0`).
2. **enable** -- `setActivePrograms` (f20) -- `activeProgramFlags` uint32 bitmask, program A = bit 0 (`1<<(programId-1)`, a=1 -> **1**); sole field.
3. **run-mode / "enable watering"** -- `timerMode{mode=autoMode(1), manualModeParams={} EMPTY}` (f14) -- autoMode encoder not implemented in this integration; byte-exact to `btsnoop_enable_program.log` idx94 = `aa775a0f0800720408011200972f`. The **empty `manualModeParams` (f2) is REQUIRED**; sending only `mode` is silently ignored.

The device computes a next start only when it receives store+enable **while in autoMode**, so the app
does store -> enable -> autoMode -> **re-send store+enable**,
then reads `deviceStatusInfo` (f9 `nextStartProgramFlags`, f10 `nextStartTimeSecEpochUTC`). No BLE
schedule READ exists (section Section L). Live result: device flipped `deviceOff/off` -> `deviceIdle/on` and
computed next watering 2026-06-28T06:00 for program A.

---

### A.7b -- `0x0011`-length h2d `setRainDelay` (f17)

Delay all watering for N minutes -- the local "skip schedules" control. Saved on the device
(`btsnoop_rain_delay.log` idx94). `OrbitPbApi_SetRainDelay`: f1 `rainDelayTimeMins` (required),
f2 `delayStartTimeSecEpochUtc`, f3 `delayEndTimeSecEpochUtc`, f4 `delayType`. The app sends f1+f3+f4.

Captured 3-hour delay (rain-delay encoder not implemented in this integration):
`aa775a0f10008a010b08b40118c88f83d2062001b959` -> `{rainDelayTimeMins=180, delayEndTimeSecEpochUtc, delayType=1}`.
`rain_delay_mins=0` cancels. The active delay reads back in `deviceStatusInfo` (`rain_delay`:
mins/end_epoch/type); `device_status` becomes `rainDelayEnabled`.
**LIVE-VERIFIED on Timer-NW (2026-06-28)** -- set 3h and cancel both confirmed. Rain delay is not
implemented in this integration; its `deviceStatusInfo` decoder here,
`BHyveHT34ADevice._parse_status()` in `devices/ht34a.py`, reads run-state + time-remaining only and
does not decode the rain-delay fields.

### A.7c -- `setScheduledMode` (f120) and cloud-only settings

`setScheduledMode` (f120, `OrbitPbApi_SetScheduledMode{scheduledMode{turnOn/turnOffDaySecEpochUtc, repeatAnnually}}`)
is the **system on/off date** (seasonal); sent over BLE, empty (`0a00`) when no dates are set.
**Cloud-only (no BLE writes -- confirmed from `btsnoop_rain_delay.log`):** Weather Adjustments
(weather station, rain/wind/freeze-delay thresholds) and Smart Watering Restrictions. The app's
"saving" toast on those screens is a cloud sync; the cloud computes them and pushes resulting
schedules/rain-delays down to the device.

---

### A.8 -- `0x0015` d2h `wateringStatus`

**Direction:** device -> host  
**Frame type:** `0x0015`  
**OrbitPbApi_Message field:** f30 `wateringStatus`  
**Class:** `OrbitPbApi_WateringStatus`  
**Parser:** folded into `BHyveHT34ADevice._parse_status()` in `devices/ht34a.py` (no separate function; it reads run-state + time-remaining only)

#### Fields

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `status` | `Status` | required |
| f2 | `rainSensorHold` | bool | |
| f3 | `currentProgramId` | `OrbitPbApi_ProgramId` | |
| f4 | `currentStationId` | uint32 | |
| f5 | `currentTimeRemainingSec` | uint32 | |
| f6 | `waterEventQueue` | `OrbitPbApi_WateringEvent` | repeated |
| f7 | `totalRunTimeSec` | uint32 | |
| f9 | `databaseId` | uint32 | |
| f10 | `runtimeIndex` | uint32 | |
| f11 | `sessionId` | uint32 | |
| f12 | `waterInstanceId` | uint32 | |
| f13 | `waterEventIndex` | uint32 | |
| f14 | `waterInstanceStartSecEpochUtc` | uint32 | |

**Status enum:**

| Value | Name |
|-------|------|
| 1 | `wateringComplete` |
| 2 | `wateringInProgress` |
| 3 | `pumpDelay` |
| 4 | `stationComplete` |
| 5 | `stationDelay` |
| 6 | `programPreDelay` |
| 7 | `programPostDelay` |

**OrbitPbApi_ProgramId enum:** see [A.7](#a7--0x005f-h2d-setprogramschedule).

**Nested: OrbitPbApi_WateringEvent**

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `programId` | `OrbitPbApi_ProgramId` | required |
| f2 | `stationId` | uint32 | required |
| f3 | `runTimeSec` | uint32 | required |
| f4 | `databaseId` | uint32 | |
| f5 | `eventType` | `EventType` | |

**WateringEvent.EventType enum:**

| Value | Name |
|-------|------|
| 1 | `wateringInProgress` |
| 2 | `programPreDelay` |
| 3 | `programPostDelay` |
| 4 | `soak` |
| 5 | `scheduleSyncDelay` |

#### Captured example

```
aa 77 5a 0f 15 00 0a 06 44 67 55 d8 28 ac 38 97 d7 f0 d0 06 f2 01 02 08 01 2b 72
```

Decoded: `wateringStatus { status: wateringInProgress, currentStationId: ..., currentTimeRemainingSec: ... }` -- active watering notification.

---

### A.9 -- `0x0016` d2h `batteryStatus`

**Direction:** device -> host  
**Frame type:** `0x0016`  
**OrbitPbApi_Message field:** f46 `batteryStatus`  
**Class:** `OrbitPbApi_BatteryStatus`  
**Parser:** inline in `BHyveHT34ADevice._observe_plaintext()` in `devices/ht34a.py`

Note: type code `0x0016` is shared with [A.5](#a5--0x0016-h2d-setepochtime); direction distinguishes them.

#### Fields

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `state` | `BatteryState` | |
| f2 | `batteryLevelPercent` | uint32 | |
| f3 | `batteryLevelMV` | uint32 | millivolts |
| f4 | `externallyPowered` | bool | |

**BatteryState enum:**

| Value | Name |
|-------|------|
| 0 | `charging` |

#### Captured example

```
aa 77 5a 0f 16 00 0a 06 44 67 55 d8 28 ac 38 cf d6 f0 d0 06 f2 02 03 18 96 16 10 37
```

Decoded: `batteryStatus { batteryLevelMV: 2838, batteryLevelPercent: ... }` -- Timer_SE battery reading (2838 mV observed in capture).

---

### A.10 -- `0x002F` d2h `deviceInfo`

**Direction:** device -> host  
**Frame type:** `0x002F`  
**OrbitPbApi_Message field:** f23 `deviceInfo`  
**Class:** `OrbitPbApi_DeviceInfo`

#### Fields

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `numStations` | int32 | |
| f2 | `hwVersion` | string | |
| f3 | `fwVersion` | string | |
| f4 | `wifiVersion` | uint32 | |
| f5 | `powerBoardId` | `PowerBoardId` | |
| f6 | `testState` | uint32 | |
| f7 | `pumpEnabled` | bool | |
| f8 | `stationsEnabledFlags_0_31` | uint32 | bitmask of enabled stations 0-31 |
| f9 | `stationsEnabledFlags_32_63` | uint32 | bitmask of enabled stations 32-63 |
| f10 | `deviceType` | `DeviceType` | |
| f11 | `bootloaderVersion` | uint32 | |
| f12 | `hasMfiSetupCode` | bool | |
| f13 | `bleStatus` | `BleStatus` | |
| f14 | `bleBootloaderVersion` | uint32 | |
| f15 | `bleAppVersion` | uint32 | |
| f16 | `bleSdkVersion` | uint32 | |
| f17 | `rl78Version` | uint32 | |
| f18 | `mfrTestStatus` | `OrbitPbApi_MfrTest_DutTestStatus` | |
| f19 | `pumpEnabledFlags` | uint32 | |

**PowerBoardId enum:**

| Value | Name |
|-------|------|
| 0 | `orbit6Station` |
| 1 | `orbit12Station` |
| 2 | `pro8Station` |
| 3 | `pro16Station` |
| 4 | `international6Station` |
| 5 | `international12Station` |
| 6 | `unknown` |

**DeviceType enum:**

| Value | Name |
|-------|------|
| 0 | `hosetapTimer` |
| 1 | `undergroundTimer` |
| 2 | `rainSensor` |
| 3 | `moistureSensor` |

**BleStatus enum:**

| Value | Name |
|-------|------|
| 0 | `bleUninitialized` |
| 1 | `bleUpdating` |
| 2 | `bleInitialized` |
| 3 | `bleError` |

**Nested: OrbitPbApi_MfrTest_DutTestStatus**

| Field | Name | Type |
|-------|------|------|
| f1 | `buttonTestState` | `ButtonTestState` |
| f2 | `rl78CommTestPassed` | bool |
| f3 | `zcdTestPassed` | bool |
| f4 | `scdSigDetected` | bool |
| f5 | `rainSenseSigDetected` | bool |
| f6 | `battSenseGood` | bool |
| f7 | `testDioFlags` | uint32 |
| f64 | `hscTestStatus` | `OrbitPbApi_MfrTest_Hsc` |
| f65 | `gcTestStatus` | `OrbitPbApi_MfrTest_GC` |

**ButtonTestState enum** (partial, from schema):

| Value | Name |
|-------|------|
| 0 | `buttonTestState_none` |
| 1 | `buttonTestState_clear` |
| 2 | `buttonTestState_back` |
| 3 | `buttonTestState_program` |
| 4 | `buttonTestState_rainDelay` |
| 5 | `buttonTestState_dialCw` |
| 6 | `buttonTestState_select` |
| 7 | `buttonTestState_dialCcw` |
| 8 | `buttonTestState_reset` |
| 9 | `buttonTestState_finished` |

#### Captured example

```
aa 77 5a 0f 2f 00 0a 06 44 67 55 d8 28 ac 38 cf d6 f0 d0 06 ba 01 1c 08 04 12 0a 48 54 33 34 41 2d 30 30 30 31 1a 04 30 ...
```

Decoded (partial): `deviceInfo { numStations: 4, hwVersion: "HT34A-0001", fwVersion: "0..." }` -- hardware model and firmware version of the captured device (Timer_SE).

---

### A.11 -- `0x003E` and `0x005E` d2h `deviceStatusInfo`

**Direction:** device -> host  
**Frame types:** `0x003E` (standard status), `0x005E` (watering-summary variant -- carries `f18 wateringStatusSummary`)  
**OrbitPbApi_Message field:** f16 `deviceStatusInfo`  
**Class:** `OrbitPbApi_DeviceStatusInfo`  
**Parser:** `extract_status()` in `devices/status.py` (decodes run-state (f1), run progress (f6), faultStatus (f7), next-start (f9/f10), rain delay (f13), battery (f14); the remaining fields below are protocol reference, not all parsed)

Both type codes carry the same protobuf class. `0x003E` is the typical device status update;
`0x005E` is seen when the device sends a watering status summary (its `OrbitPbApi_DeviceStatusInfo`
includes a populated `f18 wateringStatusSummary` containing one or more `WateringStatus` sessions).

Note: [`protocol.md`](protocol.md) also references type code `0x003D` for `deviceStatusInfo` -- that is the
**same message** observed live on Timer-NW (`44:67:55:D7:4B:F9`); the btsnoop capture (Timer-SE)
shows `0x003E` and `0x005E`. The frame type code for this `f16` message varies by device/firmware.

#### Fields

| Field | Name | Type | Notes |
|-------|------|------|-------|
| f1 | `deviceStatus` | `OrbitPbApi_DeviceStatus` | required |
| f2 | `timerMode` | `OrbitPbApi_TimerMode` | |
| f3 | `batteryLevelPercent` | uint32 | |
| f4 | `nextStartTime` | `OrbitPbApi_NextStartTime` | |
| f5 | `rainDelayTimeRemainingMins` | uint32 | |
| f6 | `wateringStatus` | `OrbitPbApi_WateringStatus` | |
| f7 | `faultStatus` | `OrbitPbApi_FaultStatus` | |
| f8 | `wacModeEnabled` | bool | |
| f9 | `nextStartProgramFlags` | uint32 | |
| f10 | `nextStartTimeSecEpochUTC` | uint32 | |
| f11 | `batteryLevelMV` | uint32 | |
| f12 | `programDelayType` | `OrbitPbApi_ProgramDelayType` | |
| f13 | `rainDelay` | `OrbitPbApi_SetRainDelay` | |
| f14 | `batteryStatus` | `OrbitPbApi_BatteryStatus` | |
| f15 | `lastLogEntryTime` | `OrbitPbApi_LogEntry` | |
| f16 | `deviceSettingsHash` | bytes | |
| f17 | `sensors` | `OrbitPbApi_Sensor` | |
| f18 | `wateringStatusSummary` | `OrbitPbApi_WateringStatusSummary` | populated in `0x005E` variant |

**OrbitPbApi_DeviceStatus enum:**

| Value | Name |
|-------|------|
| 0 | `deviceOff` |
| 1 | `deviceIdle` |
| 2 | `lowBattery` |
| 3 | `rainDelayEnabled` |
| 4 | `wateringInProgress` |
| 5 | `meshDeviceOffline` |

**OrbitPbApi_ProgramDelayType enum:**

| Value | Name |
|-------|------|
| 0 | `programDelayType_none` |
| 1 | `programDelayType_user` |
| 2 | `programDelayType_rain` |
| 3 | `programDelayType_wind` |
| 4 | `programDelayType_freeze` |

**Nested: OrbitPbApi_NextStartTime**

| Field | Name | Type |
|-------|------|------|
| f1 | `nextStartTimeIso8601` | string (required) |
| f2 | `programFlags` | uint32 (required) |

**Nested: OrbitPbApi_FaultStatus**

| Field | Name | Type |
|-------|------|------|
| f1 | `pumpFault` | bool |
| f2 | `stationFaultFlags_0_31` | uint32 |
| f3 | `stationFaultFlags_32_63` | uint32 |
| f4 | `voltageBoostCircuitFail` | bool |
| f5 | `valveOffFlowDetected` | bool |
| f6 | `valveOnNoFlowDetected` | bool |
| f7 | `valveLowFlowDetected` | bool |
| f8 | `valveHighFlowDetected` | bool |
| f9 | `smartAccessoryFaultFlags` | uint32 |
| f10 | `batteryFault` | bool |
| f15 | `pumpFaults` | `OrbitPbApi_PumpFault` (repeated) |
| f16 | `stationFaults` | `OrbitPbApi_StationFault` (repeated) |
| f17 | `mainlineFaults` | `OrbitPbApi_MainlineFault` (repeated) |

**Nested: OrbitPbApi_SetRainDelay**

| Field | Name | Type |
|-------|------|------|
| f1 | `rainDelayTimeMins` | uint32 (required) |
| f2 | `delayStartTimeSecEpochUtc` | uint32 |
| f3 | `delayEndTimeSecEpochUtc` | uint32 |
| f4 | `delayType` | `OrbitPbApi_ProgramDelayType` |
| f5 | `sensorStatus` | `SensorStatus` (repeated) |

**SensorStatus enum:**

| Value | Name |
|-------|------|
| 0 | `sensorDisabled` |
| 1 | `sensorOpen` |
| 2 | `sensorClosed` |

**Nested: OrbitPbApi_LogEntry**

| Field | Name | Type |
|-------|------|------|
| f1 | `logTimeSecEpochUTC` | uint32 |
| f2 | `logIndex` | uint32 |
| f10 | `waterEventLogEntry` | `OrbitPbApi_LogEntry_WaterEvent` |

**Nested: OrbitPbApi_WateringStatusSummary** (present in `0x005E` variant)

| Field | Name | Type |
|-------|------|------|
| f1 | `sessions` | `OrbitPbApi_WateringStatus` (repeated) |

`OrbitPbApi_TimerMode` fields: see [A.4](#a4--0x000e-h2d-timermode).  
`OrbitPbApi_WateringStatus` fields: see [A.8](#a8--0x0015-d2h-wateringstatus).  
`OrbitPbApi_BatteryStatus` fields: see [A.9](#a9--0x0016-d2h-batterystatus).  
`OrbitPbApi_Sensor` / `OrbitPbApi_SensorInfo` are present in the schema but nested details are not extracted to this doc level (no example values observed).

#### Captured example (`0x003E`)

```
aa 77 5a 0f 3e 00 0a 06 44 67 55 d8 28 ac 38 ce d6 f0 d0 06 82 01 2b 08 00 12 02 08 00 3a 00 48 00 50 00 6a 0b 08 a0 0b ...
```

Decoded (partial): `deviceStatusInfo { deviceStatus: deviceOff, timerMode { mode: offMode }, faultStatus {}, batteryStatus { batteryLevelMV: 2838 }, ... }` -- idle device status for Timer_SE.

#### Captured example (`0x005E`)

```
aa 77 5a 0f 5e 00 0a 06 44 67 55 d8 28 ac 38 f9 d6 f0 d0 06 82 01 4b 08 04 12 0e 08 02 12 0a 10 00 1a 04 08 00 10 1e 20 ...
```

Decoded (partial): `deviceStatusInfo { deviceStatus: wateringInProgress, timerMode { mode: manualMode, stationInfo [...] }, wateringStatusSummary { sessions [...] } }` -- active watering with session summary.

---

### A.12 -- `0x0067` d2h `setProgramSchedule`

**Direction:** device -> host  
**Frame type:** `0x0067`  
**OrbitPbApi_Message field:** f19 `setProgramSchedule`  
**Class:** `OrbitPbApi_SetProgramSchedule`

The device echoes the program schedule in this direction (same protobuf class as the h2d `0x005F` request).

#### Fields

Same as [A.7 `0x005F` h2d `setProgramSchedule`](#a7--0x005f-h2d-setprogramschedule).

#### Captured example

```
aa 77 5a 0f 67 00 0a 06 44 67 55 d8 28 ac 38 d3 d6 f0 d0 06 9a 01 54 08 01 22 1d 08 01 12 19 32 30 32 36 2d 30 34 2d 31 ...
```

Decoded (partial): `setProgramSchedule { programId: a, programTypeInterval { ... }, stationInfo [...] }` -- device echoing back schedule A. This is the only d2h occurrence; the h2d direction uses type `0x005F`.

---

## Section B -- Un-captured BLE messages (schema-only, from APK)

These messages are referenced in the APK's BLE layer (`lib.ble.*` in `apk/hbc-out/decompiled.js`) but have **no example in the capture**.
**The outer BLE frame type code is unknown for all of these** -- type codes are only recoverable from live wire traffic.
Field ids and names are from the reconstructed schema (see [`protobuf-schema.md`](protobuf-schema.md)). Direction is inferred from naming conventions (get* = h2d request, response = d2h) or from the APK writer/transformer evidence; direction marked `?` where ambiguous.

Confidence levels reflect the strength of the APK evidence:

- **high**: explicit `socket.writer.X_BANG_` BLE-write call site, or appears in `get_device_settings`/`send_device_settings` BLE connection sequence
- **medium**: `socket.util.X` builder exists, no direct BLE dispatch call site confirmed, or pairing is implied from a confirmed counterpart
- **low/keyword**: only a ClojureScript keyword in the global symbol table, no confirmed BLE dispatch call site

| Field ID | Field Name | Direction | Type code | APK evidence | Confidence |
|----------|------------|-----------|-----------|--------------|------------|
| f10 | `syncRequest` | h2d | unknown | `sync_request` builder + `sync_request_BANG_` writer | high |
| f17 | `setRainDelay` | h2d | unknown | `rain_delay_BANG_` writer; BLE-connect send sequence | high |
| f21 | `skipCurrentStation` | h2d | unknown | `skip_station_BANG_` writer; keyword confirmed | high |
| f22 | `getDeviceInfo` | h2d | unknown | `get_device_info_BANG_` writer; BLE-connect read sequence; keyword confirmed | high |
| f26 | `getNextStartTime` | h2d? | unknown | transformer field name `nextStartTime` -- no direct BLE call site found | low |
| f27 | `nextStartTime` | d2h | unknown | transformer for `.OrbitPbApi_NextStartTime.nextStartTimeIso8601` | medium |
| f28 | `getSettings` | h2d | unknown | `get_settings` builder + `get_settings_BANG_` writer; keyword confirmed | high |
| f29 | `setSettings` (`SetSettings`) | d2h resp | `0x13` | **live-verified 2026-06-28**: `getSettings`->`0x13`/f29. Device returned **EMPTY** `SetSettings` over BLE (payload `0a06...ea0100`). Parser: not implemented in this integration. | high |
| f36 | `getNetworkStatus` | h2d | unknown | `get_network_status_BANG_` writer; BLE-connect read sequence | high |
| f37 | `networkStatus` | d2h | unknown | implied counterpart to `getNetworkStatus` | medium |
| f47 | `identifyDevice` | h2d | unknown | keyword in global table; no BLE call site confirmed | low |
| f48 | `getManualPresetRunTime` | h2d | unknown | `set_manual_preset_runtime_BANG_` + `get_pump_config_BANG_` patterns; BLE-connect send | medium |
| f49 | `manualPresetRunTime` | d2h | `0x16` | **live-verified 2026-06-28**: `getManualPresetRunTime`->`0x16`/f49 = `08d804` (`presetRunTimeSec=600`). Parser: not implemented in this integration. | high |
| f55 | `getFlowSensorParams` | h2d | unknown | `flow_sensor_params_BANG_` writer; keyword confirmed | high |
| f56 | `flowSensorParams` | d2h | unknown | implied counterpart to `getFlowSensorParams` | medium |
| f57 | `enableFlowSensorData` | h2d | unknown | **HW-verified 2026-07-03 (Gen2 fw0111)**: subscribe `ca030508e8071002` = `#57{#1=1000ms, #2=2}`, unsubscribe `ca030408001002` = `#57{#1=0, #2=2}`. A live subscription persists across reconnects and starves the `#16` status read, so `read_flow` always unsubscribes after its ~4 s sampling window. Implemented: `devices/protobuf.py` (`_FLOW_SUBSCRIBE_PB` / `_FLOW_UNSUBSCRIBE_PB`). | high |
| f58 | `getFlowSensorData` | h2d | unknown | `get_instantaneous_flow_BANG_` writer; keyword confirmed. Not used by this integration — the f57 subscription stream serves the same data. | high |
| f59 | `flowSensorData` | d2h | unknown | **HW-verified 2026-07-03 (Gen2 fw0111)**: streamed ~1/s after an f57 subscribe. `#59.#1` flow-rate frequency Hz (~0 when no water moving), `#59.#3` `currentCycleVolumeTicks` cumulative per-run counter (~433 counts/gal, bucket-calibrated), `#59.#4` `currentFlowRateGpm` float32 (decoded; not yet observed populated). Parser: `devices/status.py::extract_status`. | high |
| f69 | `getProgramSchedule` | h2d | unknown | `get_program_schedule` keyword confirmed; `set_program_BANG_` write patterns | high |
| f76 | `programSchedule` | d2h | unknown | implied counterpart to `getProgramSchedule` | medium |
| f77 | `getActivePrograms` | h2d | unknown | keyword confirmed; implied writer | medium |
| f78 | `activePrograms` | d2h | unknown | implied counterpart to `getActivePrograms` | medium |
| f94 | `setStationCfg` | h2d | unknown | `set_station_configs_BANG_` writer; BLE-connect send; keyword confirmed | high |
| f97 | `getStationCfg` | h2d | unknown | `get_station_configs_BANG_` writer; BLE-connect read; keyword confirmed | high |
| f98 | `stationCfg` | d2h | unknown | implied counterpart to `getStationCfg` | medium |
| f100 | `ack` | d2h | unknown | schema-only -- standard protobuf acknowledgement | low |
| f101 | `resetDevice` | h2d | unknown | `reset_device_BANG_` writer; confirmed | high |
| f105 | `pumpCfg` | d2h | unknown | implied counterpart to `get_pump_config_BANG_` | medium |
| f107 | `getPumpCfg` | h2d | unknown | `get_pump_config_BANG_` writer | high |
| f110 | `getWateringStatus` | h2d | unknown | `get_watering_status_BANG_` writer; keyword confirmed | high |
| f111 | `wateringStatusSummary` | d2h | unknown | implied counterpart to `getWateringStatus` | medium |
| f112 | `skipWaterInstance` | h2d | unknown | `skip_water_instance_BANG_` writer; keyword confirmed | high |
| f113 | `skipWaterEvent` | h2d | unknown | `skip_water_event_BANG_` writer; keyword confirmed | high |
| f116 | `setConcurrentProgramGroups` | h2d | unknown | `set_concurrent_programs_BANG_` writer | high |
| f119 | `scheduledMode` | d2h | unknown | implied counterpart to `getScheduledMode` | medium |
| f120 | `setScheduledMode` | h2d | unknown | `set_system_scheduled_modes_BANG_` writer; BLE-connect send; keyword confirmed | high |
| f121 | `getScheduledMode` | h2d | unknown | implied by `set_system_scheduled_modes_BANG_` sequence | medium |
| f200 | `getSmartAccessories` | h2d | unknown | `get_smart_accessories_BANG_` writer; BLE-connect read; keyword confirmed | high |
| f201 | `setSmartAccessories` | h2d | unknown | `set_smart_accessories_BANG_` writer; keyword confirmed | high |
| f214 | `getSmartAccessoryOtaStatus` | h2d | unknown | `get_smart_accessory_ota_status_BANG_` writer | high |
| f215 | `smartAccessoryOtaStatus` | d2h | unknown | implied counterpart to `getSmartAccessoryOtaStatus` | medium |

### Excluded fields (not BLE-layer)

The following `OrbitPbApi_Message` fields appeared in the APK keyword table but are excluded from this catalog because evidence places them in cloud/hub/provisioning paths, not direct BLE:

| Field IDs | Reason |
|-----------|--------|
| f32/f33 `getApList`/`apList` | WiFi AP provisioning -- cloud/hub path |
| f34 `networkConnect` | WiFi provisioning -- cloud/hub path |
| f39/f40 `updateBlockRequest`/`updateBlockResponse` | OTA block protocol -- `ota-block-request` is dropped at the cloud socket event handler (line 1135850); not forwarded over direct BLE |
| f60/f61 `getMfiSaltVerifier`/`mfiSaltVerifier` | HomeKit MFi -- no BLE call site found |
| f63-f68 `agGetApList`, `radioConfigure`, etc. | `BhyveAgApi_*` = cloud aggregator/hub paths |
| f72-f74 `getHomeKitParams`/`setHomeKitParams`/`homeKitParams` | HomeKit -- no BLE call site |
| f80-f87 `cpaLite*`, `bleBridged*` | Cloud bridging / AG paths |
| f200+ `cellular*`, `lorawan*` | Explicitly cellular/LoRa transport (field IDs above f200 in this category; distinct from f200 `getSmartAccessories`) |

**OTA note:** The `OTAV1Service` class in `bhyve-app.lib.ble.service.ota-v1-service` uses the BLE `write_char_uuid` characteristic, suggesting firmware OTA may use direct BLE. However, the protobuf-level framing relationship to `OrbitPbApi_Message` is unclear, and the field mappings above are marked "dropped at cloud socket handler." OTA over BLE is out of scope for this catalog.

---

## Section C -- Scope and method

**Transport scope:** This catalog covers the direct BLE channel only -- frames on GATT characteristics `0x0014` (h2d) and `0x0016` (d2h). Cloud is used exclusively for initial login and AES-128 `network_key` retrieval (`GET /v1/network_topologies`); all device control and status is local BLE. `OrbitPbApi_Message` has approximately 173 used fields across its oneof, the majority of which are cloud-only and excluded here.

**Capture reconciliation:** 13 distinct message types are present in `bluetooth_logs/btsnoop_hci.log.last` (Timer_SE session, `44:67:55:D8:28:AC`) and cross-verified on Timer-NW hardware (`44:67:55:D7:4B:F9`). These are ground truth -- both type code and protobuf field mapping are confirmed from wire traffic.

**APK reconciliation:** An additional ~41 field references were identified in the APK BLE layer (`lib.ble.*` in `apk/hbc-out/decompiled.js`) via `socket.util.*` builders, `socket.writer.*_BANG_` dispatch, and the `get_device_settings`/`send_device_settings` BLE connect sequences. Outer type codes for these are unknown -- they are confirmed BLE messages but were not present in the capture session.

**Schema source:** All field ids, names, types, and enum values are from the reconstructed schema in [`protobuf-schema.md`](protobuf-schema.md) (originally extracted from the APK's embedded protobuf schema JSON at `apk/hbc-out/decompiled.js` line 977559). That reconstructed schema is the source for the 13-row inventory and the nested field tables used in Section A.
