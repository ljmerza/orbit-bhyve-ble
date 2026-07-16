"""Protobuf-protocol device family (frame magic 0x11): HT34A XD + HT25G2 Gen2.

These devices share one wire protocol end to end — the same framing, AES-CTR
cipher, `timerMode` start/stop messages, and protobuf RX status decode. The
only per-model differences are the human-readable log label and the station
count (already carried as `self.stations`), so the actuation logic lives once
here and the per-model modules (`ht34a.py`, `ht25g2.py`) are trivial subclasses.

Per-*protocol* device modules are justified (mesh vs protobuf vs hub); per-
*model* modules within a protocol are not — collapsing the two identical Gen2/XD
classes removes a confirm-and-retry implementation that had been written twice.

TX frame builders live here; the RX decode + CRC live in `status.py`. The CRC
and inner-message header are shared with the RX side, imported rather than
re-declared, so there is a single source for both directions.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import struct
import time
from datetime import datetime, timedelta, timezone

from bleak.exc import BleakError

from .base import (
    SLOT_LETTERS,
    BHyveBleDeviceBase,
    ProgramSpec,
)
from .status import MSG_HEADER, _crc16_ccitt, apply_status_plaintext, parse_sync_dump

_LOGGER = logging.getLogger(__name__)


def _ble_write_guard(method):
    """Make a bool-returning BLE write method fail gracefully on a marginal link.

    A CTR-desync self-heal tears the session down mid-sequence (its disconnect is
    scheduled on the loop), so a following write raises BleakError("Not connected")
    — over an ESPHome proxy this is a routine, transient event. Catch it (and a
    proxy write-response timeout) and return False instead of surfacing a raw
    exception to the service/switch call: the coordinator refresh that follows
    re-reads the device's actual state, and the user/automation can retry. Mirrors
    the read path's best-effort behavior (get_programs falls back to last-known).
    The method's own try/finally has already run its disconnect by the time we
    catch here."""
    @functools.wraps(method)
    async def _wrapped(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except (BleakError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "%s: %s %s failed on a BLE error (%s) — marginal link, not confirmed",
                self.mac, self.log_label, method.__name__, err,
            )
            return False
    return _wrapped


def _pb_varint(val: int) -> bytes:
    r = bytearray()
    while val > 0x7F:
        r.append((val & 0x7F) | 0x80)
        val >>= 7
    r.append(val & 0x7F)
    return bytes(r)


def _pb_field_varint(f: int, v: int) -> bytes:
    return _pb_varint((f << 3) | 0) + _pb_varint(v)


def _pb_field_bytes(f: int, d: bytes) -> bytes:
    return _pb_varint((f << 3) | 2) + _pb_varint(len(d)) + d


def _pb_field_varint_signed(f: int, v: int) -> bytes:
    """int32/int64 field (non-zigzag). Negatives sign-extend to 64 bits (10-byte
    varint), matching protobuf's wire format — the device reads #2 tz offset as a
    signed int32, so e.g. -14400 (EDT) must encode with the low 32 bits 0xFFFFC7C0."""
    return _pb_varint((f << 3) | 0) + _pb_varint(v & 0xFFFFFFFFFFFFFFFF if v < 0 else v)


def _build_message(protobuf: bytes) -> bytes:
    payload_len = len(protobuf) + 2
    msg = MSG_HEADER + bytes([payload_len, 0x00]) + protobuf
    crc = struct.pack("<H", _crc16_ccitt(msg, 0))
    return msg + crc


def _build_start_pb(station_id: int, duration_sec: int) -> bytes:
    station_info = _pb_field_varint(1, station_id) + _pb_field_varint(2, duration_sec)
    manual_params = _pb_field_bytes(3, station_info)
    timer_mode = _pb_field_varint(1, 2) + _pb_field_bytes(2, manual_params)
    return _pb_field_bytes(14, timer_mode)


_STOP_PB = bytes.fromhex("720408021200")

# #15 {} — empty getDeviceStatus request. Elicits a full #16 status burst even
# mid-run (solicited RX is reliable; the unsolicited connect-time push is
# suppressed while the device is active — watering or rain-delay). This is how
# we read the REAL run-state after a command: the device
# answers a start with a #16 status but answers a stop with only a bare #30 ack
# (no #16), so without this poll a healthy stop can never be confirmed.
_REQUEST_STATUS_PB = bytes.fromhex("7a00")

# #57 { #1=1000 (intervalMs); #2=2 } — flow-sensor subscribe. The device then
# streams periodic #59 { #1=flow-active, #3=cumulative } frames that
# apply_status_plaintext folds into state.flow_total. Byte-identical to the app's
# flow-screen subscribe (Gen2 capture). Gen2-only in the captures; gated by
# has_flow so we don't poke a flow-less XD.
_FLOW_SUBSCRIBE_PB = bytes.fromhex("ca030508e8071002")

# #57 { #1=0 (intervalMs=0); #2=2 } — flow UNSUBSCRIBE. A subscribe leaves the
# device streaming #59 continuously (it persists across reconnects), which starves
# the #16 status read on later polls and, left unbounded, wedged a valve
# (hardware, 2026-07-03). Interval 0 cancels the stream. read_flow always sends
# this after sampling so we never leave a persistent subscription behind.
_FLOW_UNSUBSCRIBE_PB = bytes.fromhex("ca030408001002")

# read_flow re-subscribes this many times (~1 s each) to sample the cumulative
# #59.#3 counter over a few seconds and derive an instantaneous rate from its
# slope. 4 → ~4 s window: enough counts for a clean slope without holding the
# radio long. The counter only advances while subscribed, so this is inherently
# a spot check, not a continuous meter.
_FLOW_SAMPLE_CYCLES = 4


def _gpm_from_samples(samples: list[tuple[float, int]], counts_per_gallon: int) -> float:
    """Instantaneous gpm from (monotonic_time, cumulative_counts) samples:
    slope in counts/s × 60 / counts-per-gallon. Returns 0.0 if the counter
    didn't advance (no flow) or there aren't two usable samples."""
    if len(samples) >= 2 and counts_per_gallon > 0:
        dt = samples[-1][0] - samples[0][0]
        dcounts = samples[-1][1] - samples[0][1]
        if dt > 0 and dcounts > 0:
            return round(dcounts / dt * 60 / counts_per_gallon, 2)
    return 0.0


def _build_rain_delay_pb(minutes: int, expiry: int | None) -> bytes:
    """Rain delay: #17 { #1=minutes; #3=expiryUnixUTC; #4=1 }.

    `minutes=0` clears the delay (bare #17{#1=0}). The device echoes its own
    authoritative expiry back in #16.#13, which apply_status_plaintext stores.
    """
    body = _pb_field_varint(1, minutes)
    if minutes > 0 and expiry is not None:
        body += _pb_field_varint(3, expiry) + _pb_field_varint(4, 1)
    return _pb_field_bytes(17, body)


# ─── Watering programs (#19 / #20 / #14 / #10) + clock (#75) ──────────────────
#
# Mirrors the CLI reference (scripts/bhyve.py) byte-for-byte — the CLI is the
# proven encode/decode contract, and tests assert HA bytes == CLI bytes. Reads use
# #10 syncRequest (a one-shot dump of every #19 slot + the #20 enable bitmask + a
# #16 status), reassembled across a multi-frame RX burst. Writes/runs use the
# 3-write handshake: store #19 -> enable #20 -> autoMode #14{1} -> re-send
# store+enable, confirmed via #16.#9/#10 next-start.

# Day-mode field numbers inside a #19 body (exactly one is present).
_DM_WEEKDAYS = 3   # { #1 dayFlags }  bit0=Sun .. bit6=Sat
_DM_INTERVAL = 4   # { #1 intervalDays, #2 anchorIso }
_DM_ODD = 5        # {} empty marker
_DM_EVEN = 6       # {} empty marker
_DM_RUNONCE = 7    # { #1 programFlags }


def _build_set_epoch_time_pb(epoch_utc: int | None = None, tz_offset_sec: int | None = None) -> bytes:
    """Clock-sync: #75 setEpochTime { #1 timeSecEpochUTC, #2 timezoneOffsetSec }.

    This — NOT #18 setDateTime (a no-op over BLE, HW-verified) — sets the device
    clock, and the app also uses it to trigger the first status burst on connect.
    Its reply is content-identical to a #15 status (HW-verified 2026-07-06: same
    #16 run-state, battery, clock, controller-mode, next-start), so it doubles as
    the coordinator-poll status elicitor while keeping the device clock honest
    (program start-times, rain-delay #3, and auto-close all key off this clock).
    Defaults to HA's configured local time / UTC offset — NOT the container OS
    timezone, which can diverge from the user's HA setting and would skew the
    device's schedule next-start computation."""
    if epoch_utc is None or tz_offset_sec is None:
        try:
            # HA's configured local time, not the container OS timezone. Local
            # import so the module still loads without Home Assistant (the
            # standalone protocol tests, which pass explicit epoch/offset).
            from homeassistant.util import dt as dt_util
            now = dt_util.now()
        except ImportError:
            now = datetime.now().astimezone()
        if epoch_utc is None:
            epoch_utc = int(now.timestamp())
        if tz_offset_sec is None:
            off = now.utcoffset()
            tz_offset_sec = int(off.total_seconds()) if off is not None else 0
    body = _pb_field_varint(1, epoch_utc) + _pb_field_varint_signed(2, tz_offset_sec)
    return _pb_field_bytes(75, body)


def _build_sync_request_pb() -> bytes:
    """#10 syncRequest {} — empty; the device replies with a full state dump
    (every #19 program body + the #20 enable bitmask + a #16 status)."""
    return _pb_field_bytes(10, b"")


def _build_set_active_programs_pb(flags: int) -> bytes:
    """#20 setActivePrograms { #1 activeProgramFlags } — the enable BITMASK
    (A=bit0: 1<<(slot-1)). The device fills #2/#3 lastChange* itself."""
    return _pb_field_bytes(20, _pb_field_varint(1, flags))


def _build_set_timer_mode_pb(mode: int) -> bytes:
    """#14 timerMode { #1 mode, #2 {} EMPTY } — DEVICE-GLOBAL controller mode.

    mode 0=offMode ("controller off / automatic watering disabled"), 1=autoMode
    ("Enable Watering", the normal resting state — scheduled programs run),
    2=manualMode (empty #2 => stop the current run). The empty #2 marker (`12 00`)
    is REQUIRED — a #14 that omits it is silently ignored (W0-verified)."""
    return _pb_field_bytes(14, _pb_field_varint(1, mode) + _pb_field_bytes(2, b""))


def _build_program_pb(spec: ProgramSpec) -> bytes:
    """#19 setProgramSchedule from a ProgramSpec (a full, runnable program)."""
    body = _pb_field_varint(1, spec.slot)
    if spec.day_mode == "weekdays":
        body += _pb_field_bytes(_DM_WEEKDAYS, _pb_field_varint(1, spec.weekday_mask or 0))
    elif spec.day_mode == "interval":
        iv = _pb_field_varint(1, spec.interval_days or 1)
        if spec.interval_anchor:
            iv += _pb_field_bytes(2, spec.interval_anchor.encode())
        body += _pb_field_bytes(_DM_INTERVAL, iv)
    elif spec.day_mode == "odd":
        body += _pb_field_bytes(_DM_ODD, b"")
    elif spec.day_mode == "even":
        body += _pb_field_bytes(_DM_EVEN, b"")
    elif spec.day_mode == "once":
        body += _pb_field_bytes(_DM_RUNONCE, _pb_field_varint(1, 1 << (spec.slot - 1)))
    for m in spec.start_mins:
        body += _pb_field_varint(8, m)
    for sid, sec in spec.zones:
        body += _pb_field_bytes(9, _pb_field_varint(1, sid) + _pb_field_varint(2, sec))
    body += _pb_field_varint(10, spec.budget)
    if spec.name:
        body += _pb_field_bytes(17, spec.name.encode())
    return _pb_field_bytes(19, body)


def _build_program_delete_pb(slot: int) -> bytes:
    """Clear a slot to empty: #19 { #1 slot, #2 {} } (programTypeNotSet, no #8/#9)."""
    return _pb_field_bytes(19, _pb_field_varint(1, slot) + _pb_field_bytes(2, b""))


def _build_identify_pb(seconds: int) -> bytes:
    """#47 identifyDevice { #1 identifyTimeSec } — LED locate. #1>0 starts the
    flash (it LATCHES — identifyTimeSec is not a reliable auto-off, HW 2026-07-06
    on Gen2 fw0111), #1=0 stops it. XD (HT34A) treats it as a no-op."""
    return _pb_field_bytes(47, _pb_field_varint(1, seconds))


class BHyveProtobufDevice(BHyveBleDeviceBase):
    """Shared base for protobuf-protocol valves (frame magic 0x11).

    Subclasses set `log_label` for human-readable logging; station count comes
    from `self.stations` (1 for Gen2, 4 for the XD), so no other override is
    needed for single- vs multi-station addressing.
    """

    frame_magic = 0x11
    trailer_const = 0x11
    # Protobuf replies start with the inner-message header; enables the
    # connection's CTR-desync self-heal (mesh classes leave this None).
    reply_header = b"\xaa\x77\x5a\x0f"
    log_label = "protobuf"

    def _observe_plaintext(self, pt: bytes) -> None:
        # Protobuf-family status decode (live battery + real watering state),
        # not the d7-47 mesh battery parse the base class does.
        apply_status_plaintext(self, pt)

    async def refresh_status(self, drain_ms: int = 1500, *, sync_clock: bool = False) -> None:
        """Elicit a full #16 status burst; the decoded run-state, battery,
        seconds-remaining, controller-mode, next-start, and rain-delay fold into
        self.state via _observe_plaintext. This is the canonical mid-run /
        post-command read — solicited RX is reliable; the unsolicited push is
        suppressed while the device is active (watering or rain-delay).

        `sync_clock` (used by the coordinator poll) sends #75 setEpochTime(host-now)
        instead of the bare #15{}: its reply is content-identical to #15's, so it
        reads status AND keeps the device clock honest in one message (like the
        app). Post-command confirm reads keep the pure #15{} (no side effects
        mid-command). See _build_set_epoch_time_pb."""
        if self.connection is None:
            return
        self._status_parsed = False
        # For flow-capable devices (Gen2), proactively send #57{#1=0} unsubscribe
        # before querying status. Otherwise, a lingering flow subscription from
        # an earlier session/test will stream #59 and completely suppress #16.
        if self.has_flow:
            try:
                await self.connection.send(_build_message(_FLOW_UNSUBSCRIBE_PB), drain_ms=500)
            except Exception:  # noqa: BLE001
                pass
        elicitor = _build_set_epoch_time_pb() if sync_clock else _REQUEST_STATUS_PB
        await self.connection.send(_build_message(elicitor), drain_ms=drain_ms)
        if self._status_parsed:
            return
        # Connected, but the device returned no #16. Two hardware-observed causes,
        # both of which leave the BLE link healthy (so the poll otherwise looks
        # "successful") yet update nothing — battery, run-state, everything stays
        # frozen on whatever was last known (e.g. the initial cloud snapshot):
        #   1. A persistent #57 flow subscription (left in device flash by an
        #      interrupted flow read) streams #59 on every connect, starving the
        #      #16 reply AND desyncing the pooled RX counter. A same-session retry
        #      can't recover — only a fresh handshake re-bases the counter.
        #   2. Some units simply don't answer the passive #15 query at all, but DO
        #      echo a full #16 in reply to a benign setRainDelay write.
        # Recover both over the air so a REMOTE operator never needs a physical
        # battery pull (HW-verified un-wedging BTValve01 (cause 1) and BTValve04
        # (cause 2) with no site visit).
        _LOGGER.debug("%s: refresh_status got no status block — recovering", self.mac)
        await self._recover_status(drain_ms)

    async def _recover_status(self, drain_ms: int) -> None:
        """Best-effort recovery for a poll that connected but decoded no #16.
        Never raises — a failed recovery leaves the prior state untouched."""
        if self.connection is None:
            return
        for attempt in range(2):
            try:
                # Fresh handshake re-bases the RX counter, clearing a connect-time
                # #59-stream desync from a persistent subscription.
                await self.connection.disconnect()
                if self.has_flow:
                    # Subscribe to catch the stream while it's synced, THEN
                    # unsubscribe to both stop it and clear the flash subscription.
                    # A cold unsubscribe on a desynced session doesn't take;
                    # subscribing first (which resyncs) is what makes the
                    # unsubscribe land — HW-proven on a stuck BTValve01.
                    await self.connection.send(_build_message(_FLOW_SUBSCRIBE_PB), drain_ms=1500)
                    await self.connection.send(_build_message(_FLOW_UNSUBSCRIBE_PB), drain_ms=800)
                await self.connection.send(_build_message(_REQUEST_STATUS_PB), drain_ms=drain_ms)
                if self._status_parsed:
                    return
                # #15 still silent: this unit ignores the passive query. A
                # setRainDelay write echoes the full #16 status. Re-assert the
                # CURRENTLY known rain-delay state so the write is a no-op that can
                # never wipe an active delay (bare clear only when none is set).
                await self.connection.send(
                    _build_message(self._noop_rain_delay_pb()), drain_ms=drain_ms
                )
                if self._status_parsed:
                    return
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "%s: status recovery attempt %d failed: %s", self.mac, attempt + 1, err
                )

    def _noop_rain_delay_pb(self) -> bytes:
        """A setRainDelay (#17) payload that re-asserts the currently known
        rain-delay state, so sending it is a no-op whose only effect is to elicit
        the #16 status echo — used to read status from units that ignore #15."""
        mins = self.state.rain_delay_minutes or 0
        if mins > 0 and self.state.rain_delay_ends is not None:
            return _build_rain_delay_pb(mins, int(self.state.rain_delay_ends.timestamp()))
        return _build_rain_delay_pb(0, None)

    async def read_flow(self) -> None:
        """Spot-check the flow sensor and set state.flow_gpm (instantaneous rate).

        #59.#3 is a cumulative counter that only advances while subscribed, so we
        subscribe (#57) once and collect subsequent streamed notifications in the
        background over ~4 s, sample the counter after each tick, and take its slope:
        gpm = Δcounts/Δt · 60 / self.flow_counts_per_gallon (the configurable
        "Flow calibration" option). No flow (or valve closed) → the counter doesn't
        move → 0 gpm. Flow-capable models only (Gen2). Called on the watering poll
        (live gauge) and by the Check-flow button / automation (on-demand). ALWAYS
        unsubscribes (#57{#1=0}) afterwards so it never leaves a persistent #59 stream
        that would starve the next poll's #16 read."""
        if self.connection is None or not self.has_flow:
            return
        async with self._api_lock:
            await self._read_flow_locked()

    async def _read_flow_locked(self) -> None:
        if self.connection is None or not self.has_flow:
            return
        try:
            # Two attempts: a POOLED session whose RX counter has desynced (a dropped
            # #59 during earlier streaming) decodes every #59 to garbage → no samples.
            # That's distinct from a genuinely idle valve, which still returns a
            # decodable #59 (flow 0). So "no decodable #59 at all" means resync: drop
            # the connection and retry once. A healthy session succeeds on the first
            # pass with no extra latency (the poll's normal case).
            for attempt in range(2):
                self.state.flow_total = None  # ignore any stale counter from before
                samples: list[tuple[float, int]] = []
                # Subscribe once
                await self.connection.send(_build_message(_FLOW_SUBSCRIBE_PB), drain_ms=1200)
                if self.state.flow_total is not None:
                    samples.append((time.monotonic(), self.state.flow_total))
                # Wait and collect subsequent streamed notifications in the background
                for _ in range(_FLOW_SAMPLE_CYCLES - 1):
                    await asyncio.sleep(1.2)
                    if self.state.flow_total is not None:
                        samples.append((time.monotonic(), self.state.flow_total))
                if samples or attempt:
                    self.state.flow_gpm = _gpm_from_samples(samples, self.flow_counts_per_gallon)
                    return
                _LOGGER.debug(
                    "%s: flow read got no decodable #59 — reconnecting to resync", self.mac
                )
                await self.connection.disconnect()
        finally:
            # Cancel the stream so the next poll's #15 gets a clean #16. This
            # finally also runs under task CANCELLATION (an HA restart/unload mid
            # read), so it must be cancellation-safe:
            #  - never reconnect from here — on a teardown a reconnect races the
            #    unload and fights the device's single-BLE-session limit (and can
            #    hang), so only unsubscribe if the session is STILL up;
            #  - shield the write so a cancellation delivers it instead of dropping
            #    it mid-flight (CancelledError is a BaseException — it would slip
            #    past the BleakError/Exception guard otherwise).
            # A leaked subscription is no longer sticky regardless: _recover_status
            # self-heals it on a later poll.
            conn = self.connection
            if conn is not None and conn.is_connected:
                try:
                    await asyncio.shield(
                        conn.send(_build_message(_FLOW_UNSUBSCRIBE_PB), drain_ms=500)
                    )
                except asyncio.CancelledError:
                    _LOGGER.debug("%s: flow unsubscribe shielded through cancellation", self.mac)
                except (BleakError, Exception) as err:  # noqa: BLE001
                    _LOGGER.debug("%s: flow unsubscribe ignored loss: %s", self.mac, err)

    async def refresh_state(self):
        """Coordinator poll: actually read the device over BLE (#15{}) so HA
        tracks state the device changed on its own — a scheduled PROGRAM run,
        an app/button run, an on-device auto-close, or a rain delay expiring —
        not just HA-issued commands. Runs on the 'Poll idle'/'Poll watering'
        cadence. Best-effort: a failed poll leaves the last-known state rather
        than raising, so one out-of-range moment doesn't mark the device
        unavailable."""
        async with self._api_lock:
            if self.connection is not None:
                try:
                    # Poll with #75 setEpochTime: reads the full #16 status AND
                    # syncs the device clock to HA-now (schedules/rain-delay/auto-
                    # close all key off the device clock), mirroring the app.
                    await self.refresh_status(sync_clock=True)
                    # "Connected" tracks whether the last poll REACHED the device,
                    # not the momentary BLE link — under the ephemeral model we
                    # disconnect immediately below, so the live link is always down
                    # between polls. Poll-reachability is the meaningful signal.
                    self.state.is_connected = True
                    if getattr(self, "_status_parsed", False):
                        # A real #16 was decoded this cycle: this is a genuinely
                        # successful poll.
                        self.state.last_successful_poll = datetime.now(timezone.utc)
                        self.state.consecutive_timeouts = 0
                        # Flow only means anything mid-run; poll it (Gen2 only)
                        # once a run is confirmed so the reading tracks watering.
                        if self.has_flow and self.state.is_watering:
                            await self._read_flow_locked()
                        # Refresh the A–D program schedules on IDLE polls only —
                        # they don't change mid-run, and this keeps the watering
                        # cadence lean. Best-effort: a failed #10 sync read leaves
                        # the last-known programs rather than failing the poll.
                        if not self.state.is_watering:
                            try:
                                await self._read_programs()
                            except Exception as err:  # noqa: BLE001
                                _LOGGER.debug(
                                    "%s: program refresh skipped: %s", self.mac, err
                                )
                    else:
                        # Reached the device but it returned NO status (a wedge
                        # recovery couldn't clear this cycle). Do NOT stamp a
                        # phantom "successful poll" — that false success is exactly
                        # what masked frozen battery for a whole deploy. Count it so
                        # the diagnostic sensors (Last successful poll / Consecutive
                        # timeouts) surface the problem instead of hiding it.
                        self.state.consecutive_timeouts += 1
                        _LOGGER.debug(
                            "%s: %s poll reached device but got no status (%d consecutive)",
                            self.mac, self.log_label, self.state.consecutive_timeouts,
                        )
                except Exception as err:  # noqa: BLE001
                    self.state.consecutive_timeouts += 1
                    self.state.is_connected = False
                    _LOGGER.debug(
                        "%s: %s status poll failed (%d consecutive): %s",
                        self.mac, self.log_label, self.state.consecutive_timeouts, err
                    )
                finally:
                    await self.connection.disconnect()
            return self.state

    async def start_watering(self, station: int, duration_sec: int) -> bool:
        async with self._api_lock:
            if self.connection is None:
                return False
            try:
                # Stations are 0-indexed on the wire (station 1 -> 0).
                plaintext = _build_message(_build_start_pb(station - 1, duration_sec))
                # The start reply usually carries a #16 status that _observe_plaintext
                # decodes into self.state.is_watering; if this one didn't, poll #15{} to
                # read the real run-state before deciding. Retry once with a fresh
                # session if still unconfirmed.
                for attempt in range(2):
                    notifs = await self.connection.send(plaintext, drain_ms=2000)
                    self._stamp_command(f"start s={station} d={duration_sec}", len(notifs))
                    if not self.state.is_watering:
                        # Give the physical valve solenoid time to actuate and settle
                        # before querying the status echo.
                        await asyncio.sleep(1.0)
                        await self.refresh_status()
                    if self.state.is_watering:
                        now = datetime.now(timezone.utc)
                        self.state.active_zone = station
                        self.state.started_at = now
                        # Arm the wall-clock auto-close: the coordinator flips the valve
                        # closed at expected_off_at even if a later BLE read/stop fails,
                        # so it can't sit stuck-open on the device's own timer.
                        self.state.expected_off_at = now + timedelta(seconds=duration_sec)
                        if not self.state.seconds_remaining:
                            self.state.seconds_remaining = duration_sec
                        _LOGGER.debug("%s: %s START confirmed watering", self.mac, self.log_label)
                        return True
                    _LOGGER.warning(
                        "%s: %s START not confirmed (attempt %d/2) — fresh session",
                        self.mac, self.log_label, attempt + 1,
                    )
                    if attempt < 1:
                        await self.connection.disconnect()
                _LOGGER.error(
                    "%s: %s START failed to actuate after retries", self.mac, self.log_label
                )
                return False
            finally:
                await self.connection.disconnect()

    async def stop_watering(self, station: int | None = None) -> bool:
        async with self._api_lock:
            if self.connection is None:
                return False
            try:
                plaintext = _build_message(_STOP_PB)
                for attempt in range(2):
                    notifs = await self.connection.send(plaintext, drain_ms=2000)
                    self._stamp_command("stop", len(notifs))
                    # Give the physical valve solenoid time to actuate and settle
                    # before querying the status echo.
                    await asyncio.sleep(1.0)
                    # The device answers a stop with a bare #30 ack (no #16 status), so
                    # the send alone never updates is_watering. Poll #15{} to read the
                    # real run-state (idle, or run-state 3 if a rain delay is active —
                    # both are "not watering") before deciding.
                    await self.refresh_status()
                    if not self.state.is_watering:
                        self.state.active_zone = None
                        self.state.seconds_remaining = None
                        self.state.started_at = None
                        self.state.expected_off_at = None
                        _LOGGER.debug("%s: %s STOP confirmed idle", self.mac, self.log_label)
                        return True
                    _LOGGER.warning(
                        "%s: %s STOP not confirmed (attempt %d/2) — fresh session",
                        self.mac, self.log_label, attempt + 1,
                    )
                    if attempt < 1:
                        await self.connection.disconnect()
                _LOGGER.error(
                    "%s: %s STOP failed to close after retries", self.mac, self.log_label
                )
                return False
            finally:
                await self.connection.disconnect()

    async def set_rain_delay(self, minutes: int) -> bool:
        """Set the rain delay to `minutes` (0 clears). Returns True once the
        device's #16.#13 echo confirms the new state."""
        async with self._api_lock:
            if self.connection is None:
                return False
            try:
                if minutes <= 0:
                    return await self._clear_rain_delay_unlocked()
                # Absolute expiry the device enforces. A skew probe (2026-06-30) showed
                # the device honors #3 LITERALLY (it does not recompute it from #1
                # minutes), so #3 must be anchored to the *device* clock, not the host
                # clock — otherwise a clock-skewed device ends the delay early/late by
                # the skew. Read the device clock first via #15{} (stored on
                # DeviceState.device_clock by apply_status_plaintext), and fall back to
                # host UTC only if the device didn't report one this session.
                await self.refresh_status()
                base = self.state.device_clock or int(time.time())
                expiry = base + minutes * 60
                plaintext = _build_message(_build_rain_delay_pb(minutes, expiry))
                notifs = await self.connection.send(plaintext, drain_ms=2000)
                self._stamp_command(f"rain_delay set {minutes}m", len(notifs))
                # Seed the INTENDED delay onto state before the confirm read. If that
                # read falls into _recover_status (a unit that ignores the passive
                # #15), its status elicitor is a setRainDelay re-assert of self.state
                # (_noop_rain_delay_pb). Left at the pre-set value it would re-send
                # #17{#1=0} and CLEAR the delay we just set (the reported bug). Seeding
                # the new value makes that re-assert idempotent with the set, so the
                # device echoes the real #16.#13 back and confirmation stays honest.
                self.state.rain_delay_minutes = minutes
                self.state.rain_delay_ends = datetime.fromtimestamp(expiry, tz=timezone.utc)
                # Read back the authoritative #16.#13 echo via #15{} rather than trusting
                # the set reply's push (which the device suppresses while active). Gate
                # "confirmed" on a real #16 decoding this cycle (_status_parsed) so the
                # seeded value alone can't report a false confirm when no status came back.
                await self.refresh_status()
                ok = self._status_parsed and bool(self.state.rain_delay_minutes)
                _LOGGER.log(
                    logging.DEBUG if ok else logging.WARNING,
                    "%s: %s rain-delay set %dm %s",
                    self.mac, self.log_label, minutes, "confirmed" if ok else "unconfirmed",
                )
                return ok
            finally:
                await self.connection.disconnect()

    async def clear_rain_delay(self) -> bool:
        """Clear the rain delay (#17{#1=0}). Returns True once #16.#13 reads off."""
        async with self._api_lock:
            if self.connection is None:
                return False
            return await self._clear_rain_delay_unlocked()

    async def _clear_rain_delay_unlocked(self) -> bool:
        try:
            plaintext = _build_message(_build_rain_delay_pb(0, None))
            notifs = await self.connection.send(plaintext, drain_ms=2000)
            self._stamp_command("rain_delay clear", len(notifs))
            # Seed the cleared state before the confirm read for the same reason as
            # set_rain_delay: if _recover_status runs, its _noop_rain_delay_pb elicitor
            # re-asserts self.state — left at the stale pre-clear minutes it would
            # re-send the delay and UNDO the clear. Seeding 0 makes it re-send #17{#1=0}
            # (idempotent with the clear) and the device echoes the real cleared #16.#13.
            self.state.rain_delay_minutes = 0
            self.state.rain_delay_ends = None
            # Confirm the cleared #16.#13 echo via a #15{} read-back.
            await self.refresh_status()
            return self._status_parsed and not self.state.rain_delay_minutes
        finally:
            if self.connection is not None:
                await self.connection.disconnect()

    # ─── Watering programs (#19 / #20 / #14 / #10) ────────────────────────────
    #
    # These mirror the CLI reference (scripts/bhyve.py) op-for-op. A pooled BLE
    # session's RX counter continues across sequential send()/send_stream() calls,
    # so the store->enable->autoMode->re-send handshake and the multi-frame sync
    # read all run on one connection with correct counter accounting; the wrapper
    # holds _api_lock and disconnects in finally (ephemeral-session model).

    async def _read_programs(self) -> tuple[dict, int | None]:
        """Read every program slot in one shot via #10 syncRequest, reassembled
        across the multi-frame RX burst, and update self.state.programs. Returns
        (programs {slot:ProgramSummary}, active_mask). The #16 status in the dump
        is applied to state by the per-frame observer; parse_sync_dump here pulls
        the multi-frame #19 program bodies + the #20 enable bitmask.

        Only overwrite state when program BODIES decode: a truncated / CTR-desynced
        burst (the header self-heal already disconnects on a bad first frame) yields
        nothing and must NOT blank the last-known schedules. If the bodies decode
        but the small #20 mask frame dropped (mask=None), carry the known enabled
        flags forward so a partial burst doesn't spuriously de-enable a slot.

        Do NOT retry with an immediate reconnect here. Orbit valves allow only ONE
        BLE session, so a fast disconnect+reconnect races the device's session
        release and can wedge the link into a desync cascade (hardware-observed);
        the next scheduled poll retries cleanly instead, and get_programs / the
        sensors read through the retained last-known state.programs meanwhile."""
        stream = await self.connection.send_stream(
            _build_message(_build_sync_request_pb()), drain_ms=6000
        )
        programs, mask, status = parse_sync_dump(stream)
        if programs:
            if mask is None:
                # Mask frame dropped on this burst — keep each slot's known enable
                # state rather than reporting it as unknown/off.
                for sid, sch in programs.items():
                    prev = self.state.programs.get(sid)
                    if sch.enabled is None and prev is not None:
                        sch.enabled = prev.enabled
            self.state.programs = programs
        elif mask is not None or status is not None:
            # ZERO #19 bodies but the burst DID decode a coherent frame (the #20 enable
            # mask and/or the #16 status): the device answered fully and stores no
            # programs — e.g. every slot deleted via the app on hardware that omits
            # deleted slots rather than echoing NotSet bodies. Clear last-known so a
            # stale schedule doesn't linger forever. A truncated / CTR-desynced burst
            # decodes NEITHER (mask and status both None), so it still preserves state
            # — the presence of a decoded frame is the distinguishing signal.
            self.state.programs = {}
        return programs, mask

    async def get_programs(self) -> dict:
        """Public read: connect, dump all slots, disconnect. Returns
        {slot(1-6): ProgramSummary} — the freshly-read schedules, or the last-known
        state.programs if this read came back partial, so a marginal-link miss never
        returns empty or de-enabled."""
        async with self._api_lock:
            if self.connection is None:
                return {}
            try:
                await self._read_programs()
                return dict(self.state.programs)
            finally:
                await self.connection.disconnect()

    def _enable_mask(self, mask: int | None) -> int:
        """The current #20 enable bitmask to base a write on. A program read can
        decode the #19 bodies but drop the trailing #20 mask frame (mask=None) on
        a marginal link; falling back to 0 there would clear every OTHER slot's
        enable bit on the next write. _read_programs already carries each slot's
        enabled flag forward into state.programs, so reconstruct the mask from it
        instead of collapsing to 0."""
        if mask is not None:
            return mask
        return sum(1 << (sid - 1) for sid, s in self.state.programs.items() if s.enabled)

    @_ble_write_guard
    async def set_program(self, spec: ProgramSpec) -> bool:
        """Store a program (#19) and, if spec.enabled, drive the 3-write run
        handshake (store -> enable #20 -> autoMode #14{1} -> re-send store+enable).
        Returns True once the store read-back lands (store-only) or the device
        reports a next-start (enabled). Never leaves the controller in offMode."""
        async with self._api_lock:
            if self.connection is None:
                return False
            try:
                letter = SLOT_LETTERS.get(spec.slot, str(spec.slot))
                _programs, mask = await self._read_programs()
                mask = self._enable_mask(mask)
                pb = _build_program_pb(spec)
                await self.connection.send(_build_message(pb), drain_ms=2000)
                self._stamp_command(f"program set {letter}", 0)

                programs, _ = await self._read_programs()
                stored = programs.get(spec.slot)
                stored_ok = stored is not None and not stored.empty

                bit = 1 << (spec.slot - 1)
                if not spec.enabled:
                    # Store only: clear this slot's enable bit, keep the controller
                    # in autoMode (never drop it to offMode). Other slots unchanged.
                    await self.connection.send(
                        _build_message(_build_set_active_programs_pb(mask & ~bit))
                    )
                    await self.connection.send(_build_message(_build_set_timer_mode_pb(1)))
                    return stored_ok

                newmask = mask | bit
                # The device computes a next-start only when store+enable arrive
                # while already in autoMode: enable -> autoMode -> re-send store+enable.
                await self.connection.send(
                    _build_message(_build_set_active_programs_pb(newmask))
                )
                await self.connection.send(_build_message(_build_set_timer_mode_pb(1)))
                await self.connection.send(_build_message(pb), drain_ms=2000)
                await self.connection.send(
                    _build_message(_build_set_active_programs_pb(newmask))
                )
                # Confirm via #16.#9/#10 next-start (folded into state by the
                # observer). next_start_flags is a bitmask of whichever slot(s)
                # start NEXT, so anchor the confirm to THIS slot's bit — otherwise
                # an already-enabled program with an earlier start would confirm a
                # slot that didn't actually arm. (A slot that armed but isn't the
                # soonest reads as unconfirmed; the poll then re-reads real state.)
                await self.refresh_status()
                ok = bool(self.state.next_start_flags and self.state.next_start_flags & bit)
                _LOGGER.log(
                    logging.DEBUG if ok else logging.WARNING,
                    "%s: %s program set %s enabled %s",
                    self.mac, self.log_label, letter, "confirmed" if ok else "no next-start",
                )
                return ok
            finally:
                await self.connection.disconnect()

    @_ble_write_guard
    async def delete_program(self, slot: int) -> bool:
        """Clear a slot: drop its enable bit, write the #19 NotSet body, keep the
        controller in autoMode. Returns True once the read-back shows it empty."""
        async with self._api_lock:
            if self.connection is None:
                return False
            try:
                _programs, mask = await self._read_programs()
                bit = 1 << (slot - 1)
                await self.connection.send(
                    _build_message(_build_set_active_programs_pb(self._enable_mask(mask) & ~bit))
                )
                await self.connection.send(_build_message(_build_program_delete_pb(slot)))
                await self.connection.send(_build_message(_build_set_timer_mode_pb(1)))
                self._stamp_command(f"program delete {SLOT_LETTERS.get(slot, slot)}", 0)
                programs, _ = await self._read_programs()
                sch = programs.get(slot)
                return sch is None or sch.empty
            finally:
                await self.connection.disconnect()

    @_ble_write_guard
    async def set_program_enabled(self, slot: int, on: bool) -> bool:
        """Toggle a stored program's enable bit (#20 bitmask) and keep the
        controller in autoMode. On enable, re-send the bitmask while in autoMode so
        the device computes a next-start. Returns True once the read-back mask
        matches. (The stored #19 body is untouched — enabling an existing program.)"""
        async with self._api_lock:
            if self.connection is None:
                return False
            try:
                _programs, mask = await self._read_programs()
                mask = self._enable_mask(mask)
                bit = 1 << (slot - 1)
                newmask = (mask | bit) if on else (mask & ~bit)
                await self.connection.send(
                    _build_message(_build_set_active_programs_pb(newmask))
                )
                await self.connection.send(_build_message(_build_set_timer_mode_pb(1)))
                if on:
                    await self.connection.send(
                        _build_message(_build_set_active_programs_pb(newmask))
                    )
                self._stamp_command(
                    f"program {'enable' if on else 'disable'} {SLOT_LETTERS.get(slot, slot)}", 0
                )
                await self.refresh_status()
                _programs2, mask2 = await self._read_programs()
                # Route the confirm read through _enable_mask so a dropped #20 frame
                # (mask2=None) fails safe in BOTH directions: `(mask2 or 0)` would let a
                # DISABLE confirm True without verifying anything (0 & bit == 0 == "off"),
                # asymmetric with enable. Reconstructing from last-known flags reports the
                # unverified toggle as unconfirmed instead — the safe direction.
                return bool(self._enable_mask(mask2) & bit) == on
            finally:
                await self.connection.disconnect()

    @_ble_write_guard
    async def set_controller_mode(self, on: bool) -> bool:
        """Device-global controller mode via #14 timerMode: on -> autoMode(1)
        ('Enable Watering', scheduled programs run), off -> offMode(0) (automatic
        watering disabled). Confirms via #16.#2.#1. offMode is only ever reached
        through an explicit off here — run/stop/cleanup paths keep autoMode."""
        async with self._api_lock:
            if self.connection is None:
                return False
            try:
                mode = 1 if on else 0
                notifs = await self.connection.send(_build_message(_build_set_timer_mode_pb(mode)))
                self._stamp_command(f"controller mode {'auto' if on else 'off'}", len(notifs))
                await self.refresh_status()
                return self.state.controller_mode == mode
            finally:
                await self.connection.disconnect()

    @_ble_write_guard
    async def identify(self, seconds: int = 6) -> bool:
        """Flash the device's LED to locate it (#47 identifyDevice). The flash
        LATCHES on Gen2 (identifyTimeSec isn't a reliable auto-off), so we start it,
        hold the session for `seconds`, then send the explicit stop (#47{#1=0}).
        A no-op on the XD (HT34A). Returns True if the start write went out."""
        async with self._api_lock:
            if self.connection is None:
                return False
            try:
                await self.connection.send(_build_message(_build_identify_pb(seconds)))
                self._stamp_command("identify", 0)
                await asyncio.sleep(seconds)
                await self.connection.send(_build_message(_build_identify_pb(0)))
                return True
            finally:
                await self.connection.disconnect()
