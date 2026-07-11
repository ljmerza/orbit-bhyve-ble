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
import logging
import struct
import time
from datetime import datetime, timedelta, timezone

from bleak.exc import BleakError

from .base import BHyveBleDeviceBase
from .status import MSG_HEADER, _crc16_ccitt, apply_status_plaintext

_LOGGER = logging.getLogger(__name__)


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

    async def refresh_status(self, drain_ms: int = 1500) -> None:
        """Send #15{} to elicit a full #16 status burst; the decoded run-state,
        battery, seconds-remaining, and rain-delay fold into self.state via
        _observe_plaintext. This is the canonical mid-run / post-command read —
        solicited RX is reliable; the unsolicited push is suppressed while the
        device is active (watering or rain-delay)."""
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
        await self.connection.send(_build_message(_REQUEST_STATUS_PB), drain_ms=drain_ms)
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
                    await self.refresh_status()
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
