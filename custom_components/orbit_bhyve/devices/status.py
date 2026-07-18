"""Protobuf-family RX status decode (HT34A / HT25G2).

Ported from our standalone CLI's proven decoder (`scripts/bhyve.py`,
`extract_status`) — hardware-validated against fw0107 (XD) and fw0111
(Gen2). The device→host notification is an inner message
`AA 77 5A 0F | payload_len | 00 | protobuf | CRC16-CCITT`; we parse the
protobuf for battery mV and run-state.

The CRC check is load-bearing here: a notification decrypted with a
desynced RX counter yields garbage that fails CRC, so consuming only
CRC-valid frames keeps a momentary counter desync from poisoning state.
"""
from __future__ import annotations

import logging
import struct
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from .base import FaultStatus, ProgramSummary

_LOGGER = logging.getLogger(__name__)

MSG_HEADER = bytes([0xAA, 0x77, 0x5A, 0x0F])

# RX message field numbers (see docs/ble_protocol.md).
RX_F_CLOCK = 7            # wrapper: device clock, Unix epoch seconds
RX_F_STATUS = 16          # device status submessage
RX_F_STATUS_MODE = 1      #   #16.#1: 1=idle, 3=rain-delay, 4=manual running
RX_F_STATUS_RUNECHO = 2   #   #16.#2: active-run echo { #1=mode, #2 { #3 { #1 stationId } } }
RX_F_RUNECHO_MODE = 1     #     #16.#2.#1: timerMode.mode (0=off, 1=auto, 2=manual)
RX_F_RUNECHO_PARAMS = 2   #     #16.#2.#2 manualParams
RX_F_RUNECHO_STATION = 3  #     #16.#2.#2.#3 stationInfo
RX_F_STATION_ID = 1       #     #16.#2.#2.#3.#1: stationId (0-indexed; zone = id + 1)
RX_F_STATUS_PROGRESS = 6  #   #16.#6: run progress (present only while watering)
# HW-verified 2026-07-05 on BOTH a Gen2 (fw0111) and the XD (fw0107): during a live 180s run
# #16.#6.#5 counted down 174->138->102 (once per second) while #16.#6.#7 stayed a constant 180.
# So #5 = remaining, #7 = total — matching knobunc's vendor names (currentTimeRemainingSec /
# totalRunTimeSec). Our earlier decode read #7 as "remaining" and thus reported a static total;
# that mislabel — not firmware — was the "static remaining" the auto-close drift-guard papered over.
RX_F_PROGRESS_REMAINING = 5  # #16.#6.#5: seconds remaining in the active run (counts down)
RX_F_PROGRESS_TOTAL = 7      # #16.#6.#7: total run-time seconds (constant during the run)
RX_F_PROGRESS_STATION = 4    # #16.#6.#4: currentStationId — the running station (HW-verified
                             #   2026-07-05: zone 3 -> 2). Shallow equivalent of #16.#2.#2.#3.#1.
RX_F_STATUS_RAINDELAY = 13  # #16.#13: rain-delay block { #1=min, #3=expiry, #4=on }
RX_F_RD_MINUTES = 1       #   #16.#13.#1: rain-delay minutes
RX_F_RD_EXPIRY = 3        #   #16.#13.#3: rain-delay expiry, Unix epoch seconds
RX_F_RD_ENABLED = 4       #   #16.#13.#4: rain-delay flag (knobunc: delayType enum, not a bool;
                          #     any nonzero value = a delay is set). Often absent on a bare clear.
RX_F_STATUS_BATT = 14     #   #16.#14: battery block { #3 = mV }
RX_F_BATT_MV = 3          #   battery millivolts (#16.#14.#3 or #46.#3)
RX_F_BATTERY_REPORT = 46  # standalone battery report { #3 = mV }
RX_F_WATERING = 59        # flow sensor data (knobunc: FlowSensorData). Gen2 only; XD emits none.
RX_F_WATERING_ACTIVE = 1  #   #59.#1: knobunc currentFlowRateFrequency_Hz — ~0 when no water moving,
                          #     so we use it as a "water currently flowing" signal (not "valve open").
RX_F_FLOW_TOTAL = 3       #   #59.#3: knobunc currentCycleVolumeTicks — CUMULATIVE per-run counter (gpm
                          #     = its slope).
RX_F_FLOW_RATE_GPM = 4    #   #59.#4: knobunc currentFlowRateGpm — float32 (wire 5). Never yet observed
                          #     populated on fw0111, so it feeds only the disabled-by-default
                          #     "Flow rate (device)" sensor to gather field confirmation; the primary
                          #     gauge stays the #59.#3 slope (read_flow).
RX_F_WATERING_STATUS = 30  # WateringStatus notification { #1 = status enum } — the "stop ack"
RX_F_WSTATUS_CODE = 1      #   #30.#1: 1=complete, 2=inProgress, 3=pumpDelay, 4=stationComplete,
                           #   5=stationDelay, 6=programPreDelay, 7=programPostDelay
                           #   (enum per knobunc's PROTOCOL_SPEC; 1=complete corroborated by our
                           #   own stop reply #30{#1=1}). Terminal/absent => not watering.
RX_WSTATUS_ACTIVE = (2, 3, 5, 6, 7)  # in-progress + delay states = a run is still active
RX_F_STATUS_NEXTSTART_FLAGS = 9   # #16.#9: nextStartProgramFlags (slot bitmask, A=bit0)
RX_F_STATUS_NEXTSTART = 10        # #16.#10: nextStartTimeSecEpochUTC
RX_F_STATUS_FAULT = 7     # #16.#7: faultStatus block (OrbitPbApi_FaultStatus,
                          #   ble-messages.md "Nested: OrbitPbApi_FaultStatus"). A healthy
                          #   status carries it EMPTY (`3a 00`, per the captured idle
                          #   example) = "no faults"; protobuf omits false bools, so any
                          #   absent field inside the block is False. Fields:
_FLT_PUMP = 1             #   #16.#7.#1  pumpFault (bool)
_FLT_STATIONS_LO = 2      #   #16.#7.#2  stationFaultFlags_0_31
_FLT_STATIONS_HI = 3      #   #16.#7.#3  stationFaultFlags_32_63
_FLT_VBOOST = 4           #   #16.#7.#4  voltageBoostCircuitFail (bool)
_FLT_OFF_FLOW = 5         #   #16.#7.#5  valveOffFlowDetected (bool) — leak while closed
_FLT_NO_FLOW = 6          #   #16.#7.#6  valveOnNoFlowDetected (bool)
_FLT_LOW_FLOW = 7         #   #16.#7.#7  valveLowFlowDetected (bool)
_FLT_HIGH_FLOW = 8        #   #16.#7.#8  valveHighFlowDetected (bool)
_FLT_ACCESSORY = 9        #   #16.#7.#9  smartAccessoryFaultFlags
_FLT_BATTERY = 10         #   #16.#7.#10 batteryFault (bool)

# --- watering-program (#19 WateringProgram) decode ------------------------
# Field numbers inside a #19 body. Exactly one day-mode field is present.
RX_F_PROGRAM = 19          # #19 setProgramSchedule / connect-burst program body
_PRG_SLOT = 1              # #19.#1: slot (A=1 .. F=6)
_PRG_NOTSET = 2            # #19.#2: programTypeNotSet (present -> empty slot)
_DM_WEEKDAYS = 3          # #19.#3 weekdays { #1 dayFlags }  bit0=Sun .. bit6=Sat
_DM_INTERVAL = 4          # #19.#4 interval { #1 intervalDays, #2 anchorIso }
_DM_ODD = 5              # #19.#5 odd {} empty marker
_DM_EVEN = 6             # #19.#6 even {} empty marker
_DM_RUNONCE = 7          # #19.#7 runOnce { #1 programFlags }
_PRG_START = 8            # #19.#8 repeated start-times (mins-from-midnight; echoed PACKED)
_PRG_ZONES = 9           # #19.#9 repeated StationInfo { #1 stationId(0-idx), #2 runTimeSec }
_PRG_BUDGET = 10         # #19.#10 seasonal budget %
_PRG_NAME = 17           # #19.#17 name (UTF-8)
RX_F_ACTIVE_PROGRAMS = 20  # #20 setActivePrograms { #1 activeProgramFlags bitmask }
_AP_FLAGS = 1              # #20.#1 activeProgramFlags


def _crc16_ccitt(data: bytes, init: int = 0) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _read_varint(data: bytes, i: int):
    shift = 0
    result = 0
    while i < len(data):
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
    return None, i


def pb_parse(data: bytes):
    """Parse protobuf to a list of (field, wire, value), or None if malformed."""
    fields = []
    i = 0
    while i < len(data):
        tag, i = _read_varint(data, i)
        if tag is None:
            return None
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, i = _read_varint(data, i)
            if val is None:
                return None
            fields.append((field, wire, val))
        elif wire == 2:
            ln, i = _read_varint(data, i)
            if ln is None or i + ln > len(data):
                return None
            fields.append((field, wire, data[i:i + ln]))
            i += ln
        elif wire == 5:
            if i + 4 > len(data):
                return None
            fields.append((field, wire, data[i:i + 4]))
            i += 4
        elif wire == 1:
            if i + 8 > len(data):
                return None
            fields.append((field, wire, data[i:i + 8]))
            i += 8
        else:
            return None  # groups / unknown wire types
    return fields


def decode_inner(pt: bytes):
    """Validate the inner message CRC and return its protobuf, or None."""
    if len(pt) < 6 or pt[:4] != MSG_HEADER:
        return None
    payload_len = pt[4]
    pb_end = 4 + payload_len
    if payload_len < 2 or pb_end + 2 > len(pt):
        return None
    protobuf = pt[6:pb_end]
    crc_rx = struct.unpack("<H", pt[pb_end:pb_end + 2])[0]
    if crc_rx != _crc16_ccitt(pt[:pb_end], 0):
        return None
    return protobuf


def _pb_field(fields, num):
    for field, _wire, val in fields or ():
        if field == num:
            return val
    return None


def _pb_path(fields, *nums):
    """Walk nested length-delimited submessages by field number, returning the
    final field's value (or None if any hop is missing / not a submessage)."""
    cur = fields
    for n in nums[:-1]:
        blob = _pb_field(cur, n)
        if not isinstance(blob, (bytes, bytearray)):
            return None
        cur = pb_parse(blob)
    return _pb_field(cur, nums[-1])


def _pb_subfield(fields, outer, inner):
    return _pb_path(fields, outer, inner)


class DeviceStatus(NamedTuple):
    run_state: int | None        # #16.#1: 1=idle, 3=rain-delay, 4=running
    is_watering: bool | None     # derived from #16.#1 / #59.#1
    battery_mv: int | None       # #16.#14.#3 or standalone #46.#3
    device_clock: int | None = None        # #7 device clock, Unix epoch seconds
    active_station: int | None = None      # #16.#6.#4 (fallback #16.#2.#2.#3.#1), 0-indexed (zone = +1)
    seconds_remaining: int | None = None   # #16.#6.#5 (counts down), present only while watering
    flow_total: int | None = None          # #59.#3 cumulative volume counter (Gen2)
    flow_rate_gpm: float | None = None     # #59.#4 device-reported gpm float32 (unconfirmed)
    rain_delay_minutes: int | None = None  # #16.#13.#1
    rain_delay_expiry: int | None = None   # #16.#13.#3, Unix epoch seconds
    rain_delay_active: bool | None = None  # #16.#13.#4
    controller_mode: int | None = None     # #16.#2.#1 timerMode.mode: 0=off, 1=auto, 2=manual
    next_start_flags: int | None = None    # #16.#9 nextStartProgramFlags (slot bitmask, A=bit0)
    next_start_epoch: int | None = None    # #16.#10 nextStartTimeSecEpochUTC
    faults: FaultStatus | None = None      # #16.#7 faultStatus; None = block absent


def extract_status(protobuf: bytes) -> DeviceStatus:
    top = pb_parse(protobuf)
    if top is None:
        return DeviceStatus(None, None, None)

    run_state = battery_mv = is_watering = seconds_remaining = None
    active_station = rd_minutes = rd_expiry = rd_active = None
    controller_mode = next_start_flags = next_start_epoch = faults = None

    device_clock = _pb_field(top, RX_F_CLOCK)     # #7 wrapper field
    status = _pb_field(top, RX_F_STATUS)          # #16 submessage
    if isinstance(status, (bytes, bytearray)):
        sfields = pb_parse(status)
        run_state = _pb_field(sfields, RX_F_STATUS_MODE)
        battery_mv = _pb_subfield(sfields, RX_F_STATUS_BATT, RX_F_BATT_MV)
        # Controller mode (#16.#2.#1) + the next scheduled program run (#16.#9/#10).
        controller_mode = _pb_path(sfields, RX_F_STATUS_RUNECHO, RX_F_RUNECHO_MODE)
        next_start_flags = _pb_field(sfields, RX_F_STATUS_NEXTSTART_FLAGS)
        next_start_epoch = _pb_field(sfields, RX_F_STATUS_NEXTSTART)
        # Which zone is running: prefer the shallow #16.#6.#4 (currentStationId,
        # HW-verified), fall back to the deep timerMode path #16.#2.#2.#3.#1.
        active_station = _pb_subfield(sfields, RX_F_STATUS_PROGRESS, RX_F_PROGRESS_STATION)
        if not isinstance(active_station, int):
            active_station = _pb_path(
                sfields, RX_F_STATUS_RUNECHO, RX_F_RUNECHO_PARAMS,
                RX_F_RUNECHO_STATION, RX_F_STATION_ID,
            )
        # Remaining is #16.#6.#5 on both Gen2 and XD (HW-verified 2026-07-05). #16.#6.#7
        # is the constant total, so it must NOT be used as a fallback here.
        seconds_remaining = _pb_subfield(
            sfields, RX_F_STATUS_PROGRESS, RX_F_PROGRESS_REMAINING
        )
        if not isinstance(seconds_remaining, int):
            seconds_remaining = None
        flt = _pb_field(sfields, RX_F_STATUS_FAULT)      # #16.#7
        if isinstance(flt, (bytes, bytearray)):
            # b"" (the healthy `3a 00` empty block) parses to [] → all-False.
            ff = pb_parse(flt)
            faults = FaultStatus(
                pump_fault=bool(_pb_field(ff, _FLT_PUMP)),
                voltage_boost_fail=bool(_pb_field(ff, _FLT_VBOOST)),
                valve_off_flow=bool(_pb_field(ff, _FLT_OFF_FLOW)),
                valve_on_no_flow=bool(_pb_field(ff, _FLT_NO_FLOW)),
                valve_low_flow=bool(_pb_field(ff, _FLT_LOW_FLOW)),
                valve_high_flow=bool(_pb_field(ff, _FLT_HIGH_FLOW)),
                battery_fault=bool(_pb_field(ff, _FLT_BATTERY)),
                station_fault_flags=(_pb_field(ff, _FLT_STATIONS_LO) or 0)
                | ((_pb_field(ff, _FLT_STATIONS_HI) or 0) << 32),
                accessory_fault_flags=_pb_field(ff, _FLT_ACCESSORY) or 0,
            )
        rd = _pb_field(sfields, RX_F_STATUS_RAINDELAY)   # #16.#13
        if isinstance(rd, (bytes, bytearray)):
            rdf = pb_parse(rd)
            rd_minutes = _pb_field(rdf, RX_F_RD_MINUTES)
            rd_expiry = _pb_field(rdf, RX_F_RD_EXPIRY)
            enabled = _pb_field(rdf, RX_F_RD_ENABLED)
            # A cleared delay echoes a bare #13{#1=0} (no #4), so don't leave
            # active=None there or the clear is dropped — derive it from minutes
            # when #4 is absent.
            if enabled is not None:
                rd_active = bool(enabled)
            elif rd_minutes is not None:
                rd_active = rd_minutes > 0
            else:
                rd_active = None
            # Run-state is authoritative. #16.#13 is NOT cleared when a delay expires —
            # it lingers stale as {#1:mins, #3:<past>, #4:1} (HW-verified 2026-07-05, both
            # families), so the block alone would report a phantom active delay. A
            # run-state other than 3 (rain-delay) means no delay is active, period.
            if run_state is not None and run_state != 3:
                rd_active = False

    if battery_mv is None:                         # standalone #46.#3
        battery_mv = _pb_subfield(top, RX_F_BATTERY_REPORT, RX_F_BATT_MV)

    # ONLY #16.#1 run_state drives is_watering — it is authoritative for whether
    # the valve is open. #59.#1 ("water currently flowing") deliberately does NOT
    # touch is_watering: letting a #59 assert "watering" latched HA on when the
    # #16 read was being starved by an active flow subscription, so the valve
    # stayed "open" long after the run ended (hardware, 2026-07-03). #59 now
    # contributes only the flow counter below.
    if run_state is not None:
        is_watering = run_state == 4
    elif _pb_field(top, RX_F_WATERING_STATUS) is not None:
        # A #30 WateringStatus notification (the "stop ack") arrives without a #16.
        # Decode its status enum instead of blindly idling on any #30: terminal
        # states (1=complete, 4=stationComplete) and a bare #30 mean not watering
        # — our observed stop reply is #30{#1=1}=complete — while in-progress/delay
        # states (2,3,5,6,7) mean a run is still active, so we must NOT idle the valve
        # on those. (Enum per knobunc's PROTOCOL_SPEC; the non-terminal codes aren't
        # yet observed here, so this only *refines* the old "any #30 => idle".)
        ws = _pb_subfield(top, RX_F_WATERING_STATUS, RX_F_WSTATUS_CODE)
        is_watering = ws in RX_WSTATUS_ACTIVE
    flow_total = _pb_subfield(top, RX_F_WATERING, RX_F_FLOW_TOTAL)   # #59.#3 cumulative
    # #59.#4 device-reported instantaneous gpm — a float32 (wire 5), which
    # pb_parse returns as the raw 4 bytes.
    flow_rate_gpm = None
    raw_gpm = _pb_subfield(top, RX_F_WATERING, RX_F_FLOW_RATE_GPM)
    if isinstance(raw_gpm, (bytes, bytearray)) and len(raw_gpm) == 4:
        flow_rate_gpm = round(struct.unpack("<f", bytes(raw_gpm))[0], 2)

    return DeviceStatus(
        run_state=run_state,
        is_watering=is_watering,
        battery_mv=battery_mv,
        device_clock=device_clock,
        active_station=active_station,
        seconds_remaining=seconds_remaining,
        flow_total=flow_total,
        flow_rate_gpm=flow_rate_gpm,
        rain_delay_minutes=rd_minutes,
        rain_delay_expiry=rd_expiry,
        rain_delay_active=rd_active,
        controller_mode=controller_mode,
        next_start_flags=next_start_flags,
        next_start_epoch=next_start_epoch,
        faults=faults,
    )


# --- watering-program decode (multi-frame reads) --------------------------

def _decode_packed_varints(data: bytes) -> list[int]:
    """Decode a packed-repeated-varint byte run into a list of ints."""
    out: list[int] = []
    i = 0
    while i < len(data):
        v, i = _read_varint(data, i)
        if v is None:
            break
        out.append(v)
    return out


def parse_program_body(pb: bytes) -> ProgramSummary | None:
    """Decode a #19 WateringProgram body -> ProgramSummary (or None if malformed).

    Mirrors the CLI reference (`scripts/bhyve.py::parse_program_body`) byte-for-byte,
    including the read-back quirk that #8 start-times come back as a PACKED repeated
    varint (wire 2) even though we WRITE them as individual varints (HW-confirmed on
    fw0111)."""
    f = pb_parse(pb)
    if f is None:
        return None
    slot = _pb_field(f, _PRG_SLOT)
    empty = isinstance(_pb_field(f, _PRG_NOTSET), (bytes, bytearray))  # #2 present

    day_mode = weekday_mask = interval_days = interval_anchor = None
    wk = _pb_field(f, _DM_WEEKDAYS)
    if isinstance(wk, (bytes, bytearray)):
        day_mode, weekday_mask = "weekdays", _pb_field(pb_parse(wk), 1)
    iv = _pb_field(f, _DM_INTERVAL)
    if isinstance(iv, (bytes, bytearray)):
        ivf = pb_parse(iv)
        day_mode, interval_days = "interval", _pb_field(ivf, 1)
        anchor = _pb_field(ivf, 2)
        if isinstance(anchor, (bytes, bytearray)):
            interval_anchor = anchor.decode(errors="replace")
    if _pb_field(f, _DM_ODD) is not None:
        day_mode = "odd"
    if _pb_field(f, _DM_EVEN) is not None:
        day_mode = "even"
    if _pb_field(f, _DM_RUNONCE) is not None:
        day_mode = "once"

    start_mins: list[int] = []
    for num, wire, v in f:
        if num != _PRG_START:
            continue
        if wire == 0:
            start_mins.append(v)
        elif isinstance(v, (bytes, bytearray)):
            start_mins.extend(_decode_packed_varints(v))

    zones: list[tuple[int, int]] = []
    for num, _wire, v in f:
        if num == _PRG_ZONES and isinstance(v, (bytes, bytearray)):
            zf = pb_parse(v)
            zones.append((_pb_field(zf, 1), _pb_field(zf, 2)))

    name = _pb_field(f, _PRG_NAME)
    if isinstance(name, (bytes, bytearray)):
        name = name.decode(errors="replace")
    return ProgramSummary(
        slot=slot, empty=empty, day_mode=day_mode, weekday_mask=weekday_mask,
        interval_days=interval_days, interval_anchor=interval_anchor,
        start_mins=tuple(start_mins), zones=tuple(zones), name=name,
        budget=_pb_field(f, _PRG_BUDGET),
    )


def split_inner_messages(stream: bytes) -> list[bytes]:
    """Scan a reassembled decrypted byte stream for every CRC-valid inner message,
    returning each one's protobuf. The device packs back-to-back messages
    contiguously (NOT block-padded), so one outer frame is not necessarily one
    message and a message may span frames — this rejoins them from the stream."""
    out: list[bytes] = []
    i = 0
    while i + 6 <= len(stream):
        hdr = stream.find(MSG_HEADER, i)
        if hdr < 0 or hdr + 6 > len(stream):
            break
        payload_len = stream[hdr + 4]
        total = payload_len + 6
        if payload_len < 2 or hdr + total > len(stream):
            break  # incomplete trailing message
        pb = decode_inner(stream[hdr:hdr + total])
        if pb is not None:
            out.append(pb)
        i = hdr + total
    return out


def parse_sync_dump(stream: bytes):
    """From a reassembled #10 sync dump (concatenated plaintext), return
    (programs {slot:ProgramSummary}, active_mask int|None, status DeviceStatus|None).
    `enabled` is filled on each program from the #20 bitmask (A=bit0)."""
    programs: dict[int, ProgramSummary] = {}
    active_mask: int | None = None
    status: DeviceStatus | None = None
    for pb in split_inner_messages(stream):
        top = pb_parse(pb)
        if top is None:
            continue
        p19 = _pb_field(top, RX_F_PROGRAM)
        if isinstance(p19, (bytes, bytearray)):
            sch = parse_program_body(p19)
            if sch and sch.slot is not None:
                programs[sch.slot] = sch
        p20 = _pb_field(top, RX_F_ACTIVE_PROGRAMS)
        if isinstance(p20, (bytes, bytearray)):
            active_mask = _pb_field(pb_parse(p20), _AP_FLAGS)
        if isinstance(_pb_field(top, RX_F_STATUS), (bytes, bytearray)):
            status = extract_status(pb)
    if active_mask is not None:
        for sid, sch in programs.items():
            sch.enabled = bool(active_mask & (1 << (sid - 1)))
    return programs, active_mask, status


def apply_status_plaintext(device, pt: bytes) -> None:
    """Plaintext observer for protobuf-family devices: decode a CRC-valid
    status notification and update the device's live battery + watering state.
    Non-status / desynced frames fail CRC and are ignored."""
    protobuf = decode_inner(pt)
    if protobuf is None:
        return
    st = extract_status(protobuf)

    if st.device_clock is not None:
        # Device's own Unix clock — used to anchor the rain-delay absolute expiry
        # (#3) to the device rather than the host, since the device honors #3
        # literally (a host/device skew would otherwise end the delay early/late).
        device.state.device_clock = st.device_clock

    if st.battery_mv is not None and 1500 <= st.battery_mv <= 4000:
        device.battery_mv = st.battery_mv  # battery_pct is a chemistry-aware property

    # Raw cumulative flow counter (#59.#3) — record whenever a #59 carries it,
    # regardless of the flow-active flag, so read_flow can sample its slope even
    # across a momentary #59.#1=0. It's transient; the gauge value is flow_gpm.
    if st.flow_total is not None:
        device.state.flow_total = st.flow_total

    # Device-reported instantaneous gpm (#59.#4) — feeds the diagnostic
    # "Flow rate (device)" sensor; not yet observed populated on hardware.
    if st.flow_rate_gpm is not None:
        device.state.flow_gpm_device = st.flow_rate_gpm

    if st.is_watering is not None:
        device.state.is_watering = st.is_watering
        if hasattr(device, "_status_parsed"):
            device._status_parsed = True
        if st.is_watering:
            # Which zone is running (#16.#2.#2.#3.#1). Without this a poll- or
            # app-discovered run leaves active_zone=None, and every zone entity
            # on a multi-station device (the XD) renders open — not just the one
            # actually watering.
            if st.active_station is not None:
                device.state.active_zone = st.active_station + 1
            if st.seconds_remaining is not None:
                device.state.seconds_remaining = st.seconds_remaining
                # Re-anchor the wall-clock auto-close to the device's own
                # remaining, so a poll-discovered run (program/app/button) gets
                # a live countdown and closes cleanly even between polls.
                # Guard: only ever move expected_off_at EARLIER, never later, so
                # a re-discovered / re-reported run can't keep postponing the
                # close — a wall-clock backstop if the device stops answering
                # mid-run. (#16.#6.#5 counts down correctly on both families, so
                # in steady state new_off_at holds ~constant; the guard just
                # bounds anomalies, not the old #6.#7 static-total mis-read.)
                new_off_at = datetime.now(timezone.utc) + timedelta(seconds=st.seconds_remaining)
                if (
                    device.state.expected_off_at is None
                    or new_off_at < device.state.expected_off_at - timedelta(seconds=15)
                ):
                    device.state.expected_off_at = new_off_at
        else:
            device.state.active_zone = None
            device.state.seconds_remaining = None
            device.state.flow_total = None
            device.state.flow_gpm = 0.0  # valve closed → no flow
            device.state.flow_gpm_device = None
            device.state.started_at = None
            device.state.expected_off_at = None

    if st.rain_delay_active is not None:
        if st.rain_delay_active and st.rain_delay_minutes:
            device.state.rain_delay_minutes = st.rain_delay_minutes
            device.state.rain_delay_ends = (
                datetime.fromtimestamp(st.rain_delay_expiry, tz=timezone.utc)
                if st.rain_delay_expiry
                else None
            )
        else:
            device.state.rain_delay_minutes = 0
            device.state.rain_delay_ends = None
    elif st.run_state is not None and st.run_state != 3:
        # Run-state says not rain-delayed and this status carried no #16.#13 block.
        # (When a block IS present, extract_status already forces rain_delay_active
        # False for run-state != 3 — the device does NOT clear #16.#13 on expiry, it
        # lingers stale.) Clear any stale value so the number / "ends" don't linger
        # after expiry (the "7 hours ago" bug).
        device.state.rain_delay_minutes = 0
        device.state.rain_delay_ends = None

    # Fault report (#16.#7) — the Problem / Leak binary sensors read this.
    # Present-but-empty decodes to the all-False report (all-clear); a status
    # WITHOUT the block keeps the last-known report rather than clearing it —
    # absence means "no fault report in this frame", not "faults gone" (unlike
    # #16.#13, there is no evidence the device ever drops #7 to signal a clear;
    # the captured healthy idle status still carries the empty block).
    if st.faults is not None:
        device.state.faults = st.faults

    # Controller mode (#16.#2.#1: 0=off, 1=auto, 2=manual) — the "Automatic
    # watering" switch reads this. Present whenever a #16 status block is decoded.
    if st.controller_mode is not None:
        device.state.controller_mode = st.controller_mode

    # Next scheduled program run (#16.#9/#10). The device reports it once a
    # program is stored+enabled while in autoMode; feeds the "Next run" sensor and
    # confirms the enable handshake. Absent (flags 0/None) => no next start armed.
    if st.next_start_flags is not None:
        device.state.next_start_flags = st.next_start_flags or None
        device.state.next_start_at = (
            datetime.fromtimestamp(st.next_start_epoch, tz=timezone.utc)
            if st.next_start_flags and st.next_start_epoch
            else None
        )
    elif st.run_state is not None:
        # A #16 status block was decoded but carried no #16.#9 nextStartProgramFlags.
        # Protobuf omits zero-valued scalars, so when the last enabled program is
        # disabled/deleted the device stops emitting #9 entirely (rather than #9=0),
        # which would otherwise freeze the "Next run" sensor at the old timestamp and
        # leave stale flags that let set_program falsely confirm. Clear it — symmetric
        # to the rain-delay clear above (the "7 hours ago" bug).
        device.state.next_start_flags = None
        device.state.next_start_at = None

    if (
        st.battery_mv is not None
        or st.is_watering is not None
        or st.rain_delay_active is not None
    ):
        _LOGGER.debug(
            "%s: live status battery=%smv watering=%s run_state=%s remaining=%ss rain_delay=%s",
            device.mac, st.battery_mv, st.is_watering, st.run_state,
            st.seconds_remaining, st.rain_delay_minutes,
        )
