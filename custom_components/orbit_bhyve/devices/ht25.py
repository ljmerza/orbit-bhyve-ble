"""HT25-0000 (single-station hose-tap) device class.

d7-47 protocol family. Verified against fw0085 (Deck) 2026-05-03 and
fw0041 (Hill) 2026-05-05 — same protocol, the 2-byte frame prefix is the
device's own mesh_device_id in little-endian (Deck's old "D747_MAGIC"
constant was just Deck's mesh_id 18391 = 0x47D7 LE).

The bind/rebind init steps are the app's utc-time write (mesh message 5); they
carry the real clock, not a session id.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
import struct
import time
from collections.abc import Callable

from bleak.exc import BleakError

from .mesh import (
    DEVICE_CONTROL_SET_RUN_MODE,
    MeshDeviceInfo,
    MeshLastRun,
    delay_end_body,
    manual_preset_body,
    parse_device_info,
    parse_health_faults,
    parse_last_water_cycle,
    run_mode_body,
)
from ..connection import BHyveBleConnection
from .base import BHyveBleDeviceBase, ha_local_now

_LOGGER = logging.getLogger(__name__)

D747_ROUTING = 0x40
SEQ_BIND = 0x05
SEQ_STATUS = 0x02
SEQ_INFO = 0x03
SEQ_SUBSYSTEM = 0x01
SEQ_MAGIC_CHECK = 0x00
SEQ_WATER_CTRL = 0x0D
SEQ_FLOW = 0x0B            # inbound flow sample
# Inbound run-end frame, run duration at pt[6:8] LE. NOT part of the flow
# subscription budget — observed arriving 46 s after a 10-sample subscription
# had expired — so it stays reliable as a release trigger even if sampling has
# lapsed mid-run.
SEQ_FLOW_END = 0x0C
SEQ_FLOW_SUBSCRIBE = 0x0E  # outbound subscribe request
TYPE_FLOW_SUBSCRIBE = 0x89

# Mesh settings/diagnostics, decoded from the B-hyve app's mesh spec. The type
# byte is really seq(6 bits) | command<<6 and the "seq" byte is the message id;
# each write below uses its own seq so its write-ack (command 3, seq echoed) can
# be matched. HW-verified on fw0085 (Deck, 2026-10-11): the delay-end, run-mode
# and button-run-time writes are acked. rain-delay (msg 10) is answered with
# invalid-message (msg id 0xFFF), even to a read, so it isn't sent.
MESH_CMD_READ = 0
MESH_CMD_WRITE_ACK = 3
SEQ_MANUAL_PRESET = 0x07   # button run time {run-time-secs u16}
SEQ_DELAY_END = 0x18       # watering-delay-end-time {delay-until-epoch-secs u32}
TYPE_MANUAL_PRESET_WRITE = 0x87
TYPE_MANUAL_PRESET_READ = 0x17
TYPE_LAST_RUN_READ = 0x1C
TYPE_DELAY_END = 0x98
TYPE_RUN_MODE = 0xB8       # device-control set-run-mode, on SEQ_WATER_CTRL
# device-status `status` (low nibble; the high nibble is padding for these).
STATUS_OFF = 0x00
STATUS_IDLE = 0x01
STATUS_RAIN_DELAY = 0x03
# device-status reflects a set-run-mode ~2-2.5 s after its ack (HW, Deck fw0085);
# status read sooner still shows the old mode.
MODE_STATUS_LAG_SEC = 5.0

# The device stops sampling ~300 s after a subscribe regardless of the sample
# count requested, so a long program needs the subscription renewed. 240 s
# leaves 60 s of margin and sustained a gapless 20.6-minute stream on hardware.
GEN1_RENEW_SEC = 240
# How long a renewal waits for _api_lock before skipping this cycle. Skipping is
# correct: queueing behind an actuation would fire the write late, possibly into
# a session STOP has already closed.
GEN1_RENEW_LOCK_TIMEOUT = 5
# Backstop margin on the session hold and the renewal loop, mirroring
# coordinator.EXPIRY_GRACE_SEC. Duplicated rather than imported: devices/ is
# deliberately free of coordinator and Home Assistant imports so the test suite
# can run without Home Assistant installed.
GEN1_HOLD_GRACE_SEC = 10

INIT_INTER_STEP_SEC = 0.15


def clock_payload(now: datetime) -> bytes:
    """utc-time body sent by bind/rebind: {utc_secs u32 LE, local_offset_mins i16 LE}.

    This is the app's set-date-time. The old "sid + f6 69 10 ff" was a captured
    clock write with the low 16 bits randomized, which set the timer ~5 months
    back with a fixed EDT offset (HW-verified on fw0041 and fw0085, 2026-10-11)."""
    off = now.utcoffset()
    offset_mins = int(off.total_seconds()) // 60 if off is not None else 0
    return struct.pack("<Ih", int(now.timestamp()), offset_mins)


def parse_hub_mesh_overrides(raw: str) -> dict[str, int]:
    """Parse the `hub_mesh_overrides` option into {MAC: hub_mesh_id}.

    Format is comma-separated `MAC=id` pairs, id decimal or 0x-hex:
        "AA:BB:CC:DD:EE:FF=0xEB42, 11:22:33:44:55:66=9021"

    MACs are upper-cased so lookup matches BHyveBleDeviceBase.mac. Malformed
    pairs are skipped with a warning rather than failing the whole entry — one
    typo shouldn't take the integration down at setup. Kept HA-free so it is
    unit-testable standalone (same rationale as accumulate_gallons)."""
    out: dict[str, int] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        mac, sep, value = chunk.partition("=")
        if not sep:
            _LOGGER.warning("hub_mesh_overrides: missing '=' in %r, skipped", chunk)
            continue
        try:
            hub_id = int(value.strip(), 0)
        except ValueError:
            _LOGGER.warning(
                "hub_mesh_overrides: %r is not a number in %r, skipped",
                value.strip(), chunk,
            )
            continue
        if not 0 <= hub_id <= 0xFFFF:
            _LOGGER.warning(
                "hub_mesh_overrides: %d out of range (0..65535) in %r, skipped",
                hub_id, chunk,
            )
            continue
        out[mac.strip().upper()] = hub_id
    return out


class BHyveHT25Device(BHyveBleDeviceBase):
    """HT25 single-station timer."""

    frame_magic = 0x10
    trailer_const = 0x10
    has_flow_gen1 = True

    # Duration/zone of the most recent START we sent, stashed so the device's
    # async START-ack (f60d) can arm the off-timer even when the BLE
    # write-response timed out (e.g. through an ESPHome proxy). See
    # _observe_plaintext.
    _pending_start_duration: int | None = None
    _pending_start_zone: int | None = None

    # User-supplied hub mesh id, from the `hub_mesh_overrides` option. Only
    # consulted when the cloud record has no bridge_device_id. Set in place by
    # _async_update_listener, so it tracks an options edit without a reload.
    hub_mesh_override: int | None = None

    # Decoded from the mesh replies in _observe_mesh_settings. None until the
    # device has sent one. manual_preset_sec is the physical button's run time.
    mesh_device_info: MeshDeviceInfo | None = None
    last_run: MeshLastRun | None = None
    manual_preset_sec: int | None = None
    # (device UTC secs, host UTC secs) from the last utc-time ack — the clock the
    # device itself runs, which is what watering-delay-end-time is judged by.
    _clock_ref: tuple[int, float] | None = None
    # time.monotonic() of the last acked set-run-mode; device-status lags it.
    _mode_written_at: float | None = None
    # (seq, msg_id) of every write-ack seen; immutable so instances never share it.
    _write_acks: frozenset[tuple[int, int]] = frozenset()

    @property
    def mesh_address(self) -> bytes:
        """The 2-byte device-address prefix on every command frame.
        It's the device's own mesh_device_id, little-endian."""
        if self.mesh_device_id is None:
            raise RuntimeError(
                f"{self.mac}: mesh_device_id missing from cloud record — "
                "cannot build HT25 frames"
            )
        return self.mesh_device_id.to_bytes(2, "little")

    @property
    def hub_mesh_address(self) -> bytes:
        """The 2-byte hub-address embedded in the magic2 init step."""
        hub_id = self.hub_mesh_device_id
        if hub_id is None:
            hub_id = self.hub_mesh_override
            if hub_id is None:
                _LOGGER.warning(
                    "%s: hub_mesh_device_id unresolved (cloud record has no "
                    "bridge_device_id and no hub_mesh_overrides entry for this MAC); "
                    "using 0x0000 in magic2. Init still completes with a placeholder "
                    "hub id, but if START is dropped this is a suspect.",
                    self.mac,
                )
                hub_id = 0
        return hub_id.to_bytes(2, "little")

    def _build(self, type_byte: int, seq: int, payload: bytes = b"") -> bytes:
        return self.mesh_address + bytes([type_byte & 0xFF, seq & 0xFF, D747_ROUTING]) + payload

    def _build_start(self, type_byte: int, duration_sec: int) -> bytes:
        if not 0 < duration_sec <= 0xFFFF:
            raise ValueError(f"duration_sec out of range (1..65535): {duration_sec}")
        payload = bytes([0x04]) + duration_sec.to_bytes(2, "little") + b"\x00\x00\x00\x00"
        return self._build(type_byte, SEQ_WATER_CTRL, payload)

    def _build_stop(self, type_byte: int) -> bytes:
        return self._build(type_byte, SEQ_WATER_CTRL, b"\x02\x00\x00\x00")

    def _observe_plaintext(self, pt: bytes) -> None:
        """Drive the local off-timer from the device's own command ack.

        The watering command keeps the entity honest even when the GATT
        write-response times out (e.g. an ESPHome proxy doesn't relay it): the
        device still receives the frame and replies with an ack notification,
        which arrives here independently of send(). Arming on the ack — not on
        send()'s return — is what makes the duration-based off-timer reliable.

        Frame: [mesh:2][type:1][seq:1][routing:1][payload:N]. A reply has the
        0x40 bit set on the type byte; START is 0xB6 → ack 0xF6, STOP 0xB7 →
        ack 0xF7, both on seq SEQ_WATER_CTRL. The START-ack echoes the accepted
        duration as [0x04][dur_LE:2][...]."""
        super()._observe_plaintext(pt)  # keep battery parsing
        self.apply_gen1_flow_frame(pt)
        self._observe_mesh_settings(pt)
        if (
            self.has_flow_gen1
            and len(pt) >= 8
            and pt[3] == SEQ_FLOW_END
            and pt[4] == D747_ROUTING
            and self.mesh_device_id is not None
            and pt[0:2] == self.mesh_address
        ):
            # The device's own run-end frame (run duration at pt[6:8] LE). This
            # is the earliest and most reliable signal that measuring is over —
            # the wall-clock auto-close is up to a poll interval behind it.
            # Both operations below are synchronous, so no add_job hop.
            _LOGGER.debug(
                "%s: flow run-end frame, duration=%ss",
                self.mac, int.from_bytes(pt[6:8], "little"),
            )
            self._end_gen1_flow()
        if len(pt) < 6 or pt[3] != SEQ_WATER_CTRL or pt[4] != D747_ROUTING:
            return
        if not (pt[2] & 0x40):
            # TX echo (reply bit clear), not a device ack.
            return
        cmd = pt[2] & ~0x40
        now = datetime.now(timezone.utc)
        if cmd == 0xB6:  # START ack — device accepted watering
            dur = None
            if len(pt) >= 8 and pt[5] == 0x04:
                dur = int.from_bytes(pt[6:8], "little")
            dur = dur or self._pending_start_duration
            if not dur:
                return
            self.state.is_watering = True
            self.state.active_zone = self._pending_start_zone
            self.state.started_at = now
            self.state.expected_off_at = now + timedelta(seconds=dur)
            self.state.seconds_remaining = dur
            _LOGGER.debug("%s: START ack — armed off-timer for %ds", self.mac, dur)
        elif cmd == 0xB7:  # STOP ack — device closed the valve
            self.state.is_watering = False
            self.state.active_zone = None
            self.state.started_at = None
            self.state.expected_off_at = None
            self.state.seconds_remaining = None
            _LOGGER.debug("%s: STOP ack — cleared off-timer", self.mac)
        else:
            return
        self._notify_state_changed()

    async def _post_handshake(self, conn: BHyveBleConnection) -> None:
        """Init the phone runs after the AES handshake. Sending the watering
        command without it produces a silent drop. Only `bind`, `status`, and
        `info` are confirmed required; the others may be prunable but are kept
        for safety until empirically tested.

        The phone's "heartbeat" step (`85/09 00`) is left out: per the app's
        mesh spec it writes active-programs = 0, which would disable every
        program stored on the timer on every connect."""
        # Surface resolved addresses so a test run self-verifies the prefix is
        # the device's own id and NOT the legacy hardcoded d747.
        _LOGGER.debug(
            "%s: frames mesh_address=%s hub_mesh_address=%s",
            self.mac, self.mesh_address.hex(), self.hub_mesh_address.hex(),
        )

        # magic1 payload: 0x01 || self mesh_id LE || 4 zero bytes
        # magic2 payload: 0x00 || hub  mesh_id LE || 4 zero bytes
        magic1_payload = b"\x01" + self.mesh_address + b"\x00\x00\x00\x00"
        magic2_payload = b"\x00" + self.hub_mesh_address + b"\x00\x00\x00\x00"

        # A None payload is a clock write (bind/rebind), stamped as it's sent.
        steps: list[tuple[int, int, bytes | None, str]] = [
            (0x81, SEQ_BIND, None, "bind"),
            (0x02, SEQ_STATUS, b"\x00", "status"),
            (0x03, SEQ_INFO, b"\x00" * 7, "info"),
            (0x04, SEQ_SUBSYSTEM, b"\x00" * 3, "subsystem"),
            (0x85, SEQ_MAGIC_CHECK, magic1_payload, "magic1"),
            (0x85, SEQ_MAGIC_CHECK, magic2_payload, "magic2"),
            (0x86, SEQ_BIND, None, "rebind"),
        ]
        for type_byte, seq, payload, label in steps:
            if payload is None:
                payload = clock_payload(ha_local_now())
            plaintext = self._build(type_byte, seq, payload)
            _LOGGER.debug("%s: init → %s pt=%s", self.mac, label, plaintext.hex())
            # _write_locked, not send_raw — we're already inside conn's lock
            # (post-handshake hook runs from _open() called by send()).
            await conn._write_locked(plaintext)
            await asyncio.sleep(INIT_INTER_STEP_SEC)
        await asyncio.sleep(0.3)

    async def start_watering(self, station: int, duration_sec: int) -> bool:
        if self.connection is None:
            return False
        # _api_lock: an active status poll (_connect_refresh) must never
        # interleave with an actuation on the device's single BLE session.
        async with self._api_lock:
            try:
                # HT25 is single-station; `station` is a no-op placeholder for API parity.
                # Stash before sending so the START-ack (which can arrive after a write
                # timeout) can arm the off-timer in _observe_plaintext.
                self._pending_start_duration = duration_sec
                self._pending_start_zone = station
                plaintext = self._build_start(0xB6, duration_sec)
                _LOGGER.debug("%s: START tx pt=%s", self.mac, plaintext.hex())
                notifs = await self._send_command(plaintext, "START")
                self._stamp_command(f"start s={station} d={duration_sec}", len(notifs))
                self._mark_reached()  # connected + sent → device reached this cycle
                if self.has_flow_gen1 and self.flow_gen1_enabled:
                    await self._begin_gen1_flow(duration_sec)
                # The off-timer is armed by the device's START-ack (_observe_plaintext),
                # not by send()'s return — so this holds even when the write-response
                # times out. Report whatever state the ack has produced by now.
                return self.state.is_watering
            finally:
                # A flow subscription needs this session for the whole program:
                # the device pushes samples and writes nothing, and the idle
                # timer only re-arms on writes. hold_open() disarms it, so
                # disconnecting here would end measurement a second after it
                # began — which is why the previous shape had to reconnect from
                # a background task and race the coordinator refresh.
                if self.connection is not None and not self.connection.is_held:
                    await self.connection.disconnect()

    async def stop_watering(self, station: int | None = None) -> bool:
        if self.connection is None:
            return False
        async with self._api_lock:
            try:
                plaintext = self._build_stop(0xB7)
                _LOGGER.debug("%s: STOP tx pt=%s", self.mac, plaintext.hex())
                notifs = await self._send_command(plaintext, "STOP")
                self._stamp_command("stop", len(notifs))
                self._mark_reached()  # connected + sent → device reached this cycle
                if self.has_flow_gen1:
                    # Not gated on flow_gen1_enabled: if the switch was turned
                    # off mid-run the hold still needs releasing. Release before
                    # the finally so the session actually closes, and zero the
                    # rate now rather than waiting for a zeroed sample that
                    # won't arrive once the link is down.
                    self._end_gen1_flow()
                    self.state.flow_lpm_gen1 = 0.0
                return not self.state.is_watering
            finally:
                if self.connection is not None:
                    await self.connection.disconnect()

    async def _send_command(self, plaintext: bytes, label: str) -> list[bytes]:
        """Send a command frame via send_actuation, which re-runs the bind/init
        first — this device acks but SILENTLY IGNORES a watering command on a
        stale pooled bind, so a fresh bind per actuation is what makes it take.
        Tolerates the ESPHome-proxy write-response timeout: the device still
        receives the frame and acks via notification (handled in
        _observe_plaintext), so a write timeout must not fail the service call."""
        try:
            notifs = await self.connection.send_actuation(plaintext, drain_ms=1500)
        except TimeoutError:
            _LOGGER.warning(
                "%s: %s write-response timed out; relying on the device ack to "
                "confirm and drive the off-timer",
                self.mac, label,
            )
            return []
        _LOGGER.debug("%s: %s got %d notifications", self.mac, label, len(notifs))
        return notifs

    async def refresh_state(self):
        """Poll. Default (option off): passive — return the last-known state
        without opening BLE. Connectivity is event-driven (stamped by
        _mark_reached on each actual connect: Sync / start / stop), so the
        passive poll deliberately does NOT touch state.is_connected — reading
        the torn-down live socket would pin the Connected sensor off.
        is_watering is driven by the device's START/STOP ack
        (_observe_plaintext) and the coordinator's wall-clock auto-close.

        With the "Mesh live status poll" option on (active_status_poll), an
        IDLE poll instead forces a connect: the init's STATUS (seq 0x02) and
        INFO (seq 0x03) replies refresh watering state, countdown and battery
        via _observe_plaintext — so app/hub/schedule runs surface within one
        idle tick. Never while watering: at the watering cadence a connect per
        tick would hammer the AA cells and race actuations, and the STATUS
        countdown + wall clock already track an active run."""
        if self.active_status_poll and not self.state.is_watering:
            recent = self.state.last_successful_poll
            # Skip if something else (Sync button, an actuation) reached the
            # device moments ago — no point paying for a second connect.
            if recent is None or (
                datetime.now(timezone.utc) - recent
            ).total_seconds() > 10:
                await self._connect_refresh()
        return self.state

    async def _connect_refresh(self) -> None:
        """Force a fresh ephemeral connect purely for its init replies: the
        8-step init's status (seq 0x02) and info (seq 0x03) responses refresh
        watering state and battery via _observe_plaintext. Disconnect first so
        the init actually re-runs (a pooled session would skip it), and
        disconnect afterwards so we don't hold the device's single session."""
        if self.connection is None:
            return
        async with self._api_lock:
            try:
                await self.connection.disconnect()
                await self.connection.ensure_connected()
            except (BleakError, asyncio.TimeoutError, OSError) as err:
                _LOGGER.warning("%s: status refresh connect failed: %s", self.mac, err)
                self._mark_unreachable()
            else:
                self._mark_reached()  # connect + init succeeded → device reached
                await self._read_mesh_extras()
            finally:
                await self.connection.disconnect()

    async def async_manual_sync(self) -> None:
        """Sync button: this mesh class's periodic refresh_state can be passive
        (it never opens BLE unless the live-status option is on), so force a
        fresh connect here — the on-demand refresh #24 dropped when it stopped
        forcing a connect on the button."""
        await self._connect_refresh()

    def _flow_subscribe_frame(self) -> bytes:
        # Captured reference frame from vendor app: 91eb 89 0e 40 e803 1e00
        #   type=0x89, seq=0x0E, payload = interval_ms(1000 LE) + sample_count(30 LE)
        # Measured on fw0085 across four units. Both fields are honoured, but
        # the subscription also carries a ~300 s ceiling: the device stops after
        # `count` samples or ~300 s, whichever comes first.
        #   1000 ms x 10  -> 10 samples, stopped 45 s into a 60 s run
        #   3000 ms x 700 -> 97 samples over ~300 s
        #   1000 ms x 700 -> 297 samples over ~300 s
        # 700 is deliberate: large enough that the ceiling always binds, so the
        # stream length is predictable whatever the interval. Coverage past
        # 300 s comes from re-subscribing — a 240 s renewal sustained a gapless
        # 20.6-minute stream on hardware. Stretching the interval instead would
        # be strictly worse: same 300 s, less resolution.
        # So there is nothing to size here, and stretching the interval for a
        # long program makes it strictly worse (same 300 s, less resolution).
        # Coverage past 300 s comes from re-subscribing instead — a 240 s
        # renewal sustained a gapless 20.6-minute stream on hardware.
        payload = struct.pack("<HH", 3000, 700)
        return self._build(TYPE_FLOW_SUBSCRIBE, SEQ_FLOW_SUBSCRIBE, payload)

    def reset_gen1_flow(self) -> None:
        self._gen1_last_elapsed = None
        self._gen1_last_cum = None
        self.state.flow_lpm_gen1 = None
        self.state.water_used_gen1_l = None

    def apply_gen1_flow_frame(self, pt: bytes) -> bool:
        """Decode one flow sample into rate + cumulative volume.

        Frame: [mesh:2][push_ctr:1][seq:1][routing:1][rate u16][elapsed u16]
        [cumulative u24], little-endian throughout. The volume is u24 per the
        app's instantaneous-flow spec ({flow-freq-hz u16, run-time-secs u16,
        water-volume-ticks u24}), so there is no 16-bit wrap to carry. Decoded
        from the spec: no hardware run has yet passed 65535 ticks to show it.

        pt[2] is a message counter cycling 0x80-0xBF, shared across flow, status
        and battery pushes — it is NOT a type byte, and the 0x40 reply bit is
        clear across that entire range, so neither a type check nor a TX-echo
        guard is possible on this frame.

        pt[7:9] is device-side seconds since START, advancing by exactly the
        subscribed interval on every sample (verified over 156 in-run samples on
        two units) and unchanged by a mid-run re-subscribe. Using it as dt keeps
        the host clock out of the calculation entirely.

        pt[5:7] is the device's own integer ticks/second and is deliberately
        ignored: at 118 counts/L one step is 0.51 L/min, roughly three times
        coarser than deriving the rate from the cumulative delta.
        """
        if self.mesh_device_id is None:
            # No mesh id in the cloud record means there is no address to match
            # against, so no frame can be attributed to this device. Bail before
            # touching mesh_address, which raises rather than returning None.
            return False
        if (
            len(pt) < 12
            or pt[0:2] != self.mesh_address
            or pt[3] != SEQ_FLOW
            or pt[4] != D747_ROUTING
        ):
            return False
        elapsed = struct.unpack("<H", pt[7:9])[0]
        cum = int.from_bytes(pt[9:12], "little")

        if elapsed == 0:
            # Run over. The device keeps sampling with every field zeroed for as
            # long as the subscription lives, so this is how flow-stopped
            # arrives — and it must never be read as a volume of zero, or the
            # run total is wiped the moment the valve shuts.
            if self.state.flow_lpm_gen1 not in (None, 0.0):
                self.state.flow_lpm_gen1 = 0.0
                self._notify_state_changed()
            return True

        prev_elapsed = getattr(self, "_gen1_last_elapsed", None)
        prev_cum = getattr(self, "_gen1_last_cum", None)
        dt = None if prev_elapsed is None else elapsed - prev_elapsed

        if dt is not None and dt > 0 and cum < prev_cum:
            # A u24 counter can't wrap inside any program this hardware runs
            # (16.7M ticks is ~150,000 L at 112 counts/L), so a decrease while
            # the device clock advances is a corrupt or desynced frame. Anchor
            # here rather than booking a rate off it.
            _LOGGER.debug(
                "%s: flow counter went backwards %s->%s over %ss; re-anchoring",
                self.mac, prev_cum, cum, dt,
            )
            dt = None

        # dt <= 0 is the first sample of a new run (elapsed counts from START
        # and survives a re-subscribe), so it only anchors.
        if dt is not None and dt > 0:
            # inc == 0 is a legitimate reading (valve open, meter static) and
            # must produce a rate of 0.0, not a held stale value.
            inc = cum - prev_cum
            self.state.flow_lpm_gen1 = round(
                inc / dt * 60.0 / self.flow_counts_per_litre, 1
            )
        self.state.water_used_gen1_l = round(cum / self.flow_counts_per_litre, 2)
        self._gen1_last_elapsed = elapsed
        self._gen1_last_cum = cum
        self._notify_state_changed()
        return True

    async def _begin_gen1_flow(self, duration_sec: int) -> None:
        """Subscribe to flow samples and hold the session for the run.

        Runs inside start_watering's _api_lock on the session START just used,
        so it neither races the coordinator refresh that valve.async_open_valve
        fires nor pays a second connect and 8-step init. A failure here costs
        this run's flow data, never the run itself.
        """
        self.reset_gen1_flow()
        self.connection.hold_open(duration_sec + GEN1_HOLD_GRACE_SEC)
        try:
            frame = self._flow_subscribe_frame()
            _LOGGER.debug("%s: flow subscribe tx pt=%s", self.mac, frame.hex())
            await self.connection.send(frame, drain_ms=1200)
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.warning("%s: flow subscribe failed: %s", self.mac, err)
            self._end_gen1_flow()
            return
        self._flow_sub_task = self.hass.async_create_task(
            self._gen1_renewal_loop(duration_sec)
        )

    async def _gen1_renewal_loop(self, duration_sec: int) -> None:
        """Re-subscribe every GEN1_RENEW_SEC for the length of the run."""
        deadline = duration_sec + GEN1_HOLD_GRACE_SEC
        waited = 0
        try:
            while waited < deadline:
                await asyncio.sleep(GEN1_RENEW_SEC)
                waited += GEN1_RENEW_SEC
                if not self.state.is_watering or self.connection is None:
                    return
                try:
                    await asyncio.wait_for(
                        self._api_lock.acquire(), timeout=GEN1_RENEW_LOCK_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    _LOGGER.debug("%s: flow renewal skipped, device busy", self.mac)
                    continue
                try:
                    if not self.state.is_watering:
                        return
                    frame = self._flow_subscribe_frame()
                    await self.connection.send(frame, drain_ms=1200)
                except (BleakError, asyncio.TimeoutError, OSError) as err:
                    _LOGGER.debug("%s: flow renewal failed: %s", self.mac, err)
                finally:
                    self._api_lock.release()
        finally:
            # Ran out, saw the run end, or was cancelled — hand the session
            # back either way. release() is idempotent, so overlapping with an
            # explicit release path is harmless.
            if self.connection is not None:
                self.connection.release()

    def _end_gen1_flow(self) -> None:
        """Stop measuring: cancel the renewal task and release the session hold.

        Called from every route a run can end by — the device's own run-end
        frame, an explicit STOP, the coordinator's wall-clock auto-close,
        stop_all, and config entry unload. Idempotent by construction, because
        a run can end by more than one of those at effectively the same moment.
        """
        task = getattr(self, "_flow_sub_task", None)
        self._flow_sub_task = None
        if task is not None and not task.done():
            task.cancel()
        if self.connection is not None:
            self.connection.release()

    def on_watering_finished(self) -> None:
        self._end_gen1_flow()

    async def async_unload(self) -> None:
        # hass.async_create_task is NOT tied to the config entry. An orphaned
        # renewal task survives unload and keeps reconnecting, re-running the
        # 8-step init and re-subscribing every 240 s against a device this
        # integration no longer owns — observed on hardware, where only a full
        # HA restart stopped it.
        self._end_gen1_flow()
        await super().async_unload()

    # ─── Mesh settings + diagnostics (app mesh spec; writes not HW-verified) ──

    def _observe_mesh_settings(self, pt: bytes) -> None:
        """Decode the mesh replies behind the settings and diagnostic entities.

        Header per the app's spec: pt[2] = seq | command<<6 (0 read, 1 read-ack,
        2 write, 3 write-ack), pt[3] = message id, pt[4] = 0x40 (destination
        gatt-device). A command-0 frame can only be our own read echoed back, so
        it never carries device data. Device pushes arrive as command 2, which a
        TX echo of our own write would share, so the messages we write (5, 7,
        10, 13) are only read from acks."""
        if (
            len(pt) < 6
            or pt[4] != D747_ROUTING
            or self.mesh_device_id is None
            or pt[0:2] != self.mesh_address
        ):
            return
        cmd = pt[2] >> 6
        if cmd == MESH_CMD_READ:
            return
        msg, body = pt[3], pt[5:]
        if cmd == MESH_CMD_WRITE_ACK:
            self._write_acks = self._write_acks | {(pt[2] & 0x3F, msg)}
        state = self.state
        changed = False
        if msg == SEQ_SUBSYSTEM:
            # device-info: the init's "subsystem" read, also pushed unprompted
            # (fw0041 sends `a0 01 40 0029 0001 ...` after magic1).
            info = parse_device_info(body)
            if info is not None and info != self.mesh_device_info:
                self.mesh_device_info = info
                changed = True
        elif msg == SEQ_INFO:
            # device-health: the init's "info" read. Battery is parsed in base.
            faults = parse_health_faults(body)
            if faults is not None and faults != state.faults:
                state.faults = faults
                changed = True
        elif msg == SEQ_STATUS:
            changed = self._apply_mesh_status_settings(body)
        elif msg == SEQ_FLOW_END:
            # last-water-cycle-info: pushed when a run ends on a live session,
            # or answered to _read_mesh_extras.
            run = parse_last_water_cycle(body)
            if run is not None and run != self.last_run:
                self.last_run = run
                changed = True
        elif not pt[2] & 0x40:
            return  # a write we could have sent ourselves; only acks count
        elif msg == SEQ_BIND and len(body) >= 4:
            # utc-time ack (bind/rebind): echoes the clock the device now runs.
            dev_utc = int.from_bytes(body[0:4], "little")
            self._clock_ref = (dev_utc, datetime.now(timezone.utc).timestamp())
            state.device_clock = dev_utc
        elif msg == SEQ_MANUAL_PRESET and len(body) >= 2:
            secs = int.from_bytes(body[0:2], "little")
            if secs != self.manual_preset_sec:
                self.manual_preset_sec = secs
                changed = True
        elif (
            msg == SEQ_WATER_CTRL
            and len(body) >= 2
            and body[0] & 0x0F == DEVICE_CONTROL_SET_RUN_MODE
        ):
            if body[1] != state.controller_mode:
                state.controller_mode = body[1]
                changed = True
        if changed:
            self._notify_state_changed()

    def _apply_mesh_status_settings(self, body: bytes) -> bool:
        """Rain delay and run mode from a device-status body. The watering
        variants stay with base._observe_plaintext. Status 0 is off and 1 (idle)
        is auto: HW-verified on the Deck (fw0085, 2026-10-11), with status lagging
        a set-run-mode by ~2.5 s, so for MODE_STATUS_LAG_SEC after an acked mode
        write the status doesn't override the mode just written."""
        if not body:
            return False
        state = self.state
        before = (state.rain_delay_minutes, state.controller_mode)
        status = body[0]
        mode_settled = (
            self._mode_written_at is None
            or time.monotonic() - self._mode_written_at >= MODE_STATUS_LAG_SEC
        )
        if status == STATUS_RAIN_DELAY and len(body) >= 4:
            # {_4, delay-remaining-mins u24}
            minutes = int.from_bytes(body[1:4], "little")
            if minutes != state.rain_delay_minutes:
                state.rain_delay_minutes = minutes
                state.rain_delay_ends = (
                    datetime.now(timezone.utc) + timedelta(minutes=minutes) if minutes else None
                )
        elif status == STATUS_IDLE:
            state.rain_delay_minutes = 0
            state.rain_delay_ends = None
            if mode_settled:
                state.controller_mode = 1
        elif status == STATUS_OFF and mode_settled:
            state.controller_mode = 0
        return (state.rain_delay_minutes, state.controller_mode) != before

    def _device_utc_now(self) -> int:
        """The device's own UTC clock, carried forward from the last utc-time
        ack. Host time until an ack has been seen."""
        host = datetime.now(timezone.utc).timestamp()
        if self._clock_ref is None:
            return int(host)
        dev_utc, at = self._clock_ref
        return int(dev_utc + (host - at))

    async def _send_mesh_writes(self, label: str, *builders: Callable[[], bytes]) -> bool:
        """Send setting writes in one session; True only if every one is
        write-acked. Each frame is built just before it goes out, so a frame
        that needs the device clock sees this session's utc-time ack. The first
        goes through send_actuation for a fresh bind, as START does."""
        if self.connection is None:
            return False
        async with self._api_lock:
            acked = True
            try:
                # Connect (running the init's utc-time write) before building
                # anything, so the first frame already sees this session's clock.
                await self.connection.ensure_connected()
                n = 0
                for i, build in enumerate(builders):
                    frame = build()
                    key = (frame[2] & 0x3F, frame[3])
                    self._write_acks = self._write_acks - {key}
                    send = self.connection.send_actuation if i == 0 else self.connection.send
                    _LOGGER.debug("%s: %s tx pt=%s", self.mac, label, frame.hex())
                    n += len(await send(frame, drain_ms=1500))
                    acked = acked and key in self._write_acks
                self._stamp_command(label, n)
                self._mark_reached()
            except (BleakError, asyncio.TimeoutError, OSError) as err:
                _LOGGER.warning("%s: %s failed on a BLE error: %s", self.mac, label, err)
                return False
            finally:
                # Don't cut a flow session that is holding the link for a run.
                if self.connection is not None and not self.connection.is_held:
                    await self.connection.disconnect()
        _LOGGER.log(
            logging.DEBUG if acked else logging.WARNING,
            "%s: %s %s", self.mac, label, "acked" if acked else "not acked",
        )
        return acked

    async def set_rain_delay(self, minutes: int) -> bool:
        """Rain delay via watering-delay-end-time {epoch}. The app also sends
        rain-delay {delay-secs} first, but fw0085 rejects that message as
        invalid; the end time alone sets the delay (device-status then reports
        rain delay with the minutes left; HW-verified on the Deck, 2026-10-11).
        The end time is anchored to the device's clock, not the host's, so a
        timer whose clock is off still gets the delay length asked for."""
        if minutes <= 0:
            return await self.clear_rain_delay()
        secs = minutes * 60
        ok = await self._send_mesh_writes(
            f"rain_delay set {minutes}m",
            lambda: self._build(
                TYPE_DELAY_END, SEQ_DELAY_END, delay_end_body(self._device_utc_now() + secs)
            ),
        )
        if ok:
            self.state.rain_delay_minutes = minutes
            self.state.rain_delay_ends = datetime.now(timezone.utc) + timedelta(seconds=secs)
        return ok

    async def clear_rain_delay(self) -> bool:
        """Clear the rain delay: an end time of 0, in the past on any clock.
        HW-verified on the Deck (device-status back to idle)."""
        ok = await self._send_mesh_writes(
            "rain_delay clear",
            lambda: self._build(TYPE_DELAY_END, SEQ_DELAY_END, delay_end_body(0)),
        )
        if ok:
            self.state.rain_delay_minutes = 0
            self.state.rain_delay_ends = None
        return ok

    async def set_controller_mode(self, on: bool) -> bool:
        """Automatic watering: device-control set-run-mode, 1 auto / 0 off.
        HW-verified on the Deck (2026-10-11): write-acked, and device-status
        follows (0 off / 1 idle) about 2.5 s later."""
        ok = await self._send_mesh_writes(
            f"controller mode {'auto' if on else 'off'}",
            lambda: self._build(TYPE_RUN_MODE, SEQ_WATER_CTRL, run_mode_body(on)),
        )
        if ok:
            self.state.controller_mode = 1 if on else 0
            self._mode_written_at = time.monotonic()
        return ok

    async def set_manual_preset_runtime(self, seconds: int) -> bool:
        """Run time the timer's physical button starts (manual-preset-run).
        HW-verified on the Deck: write-acked and read back (2026-10-11)."""
        body = manual_preset_body(seconds)
        ok = await self._send_mesh_writes(
            f"button run time {seconds}s",
            lambda: self._build(TYPE_MANUAL_PRESET_WRITE, SEQ_MANUAL_PRESET, body),
        )
        if ok:
            self.manual_preset_sec = seconds
        return ok

    async def _read_mesh_extras(self) -> None:
        """Reads on a refresh connect: the last run's water use (msg 12) and,
        until known, the button run time (msg 7). The app reads neither; the
        device pushes msg 12 at run end only while a session is open. Reads of
        other messages the app never reads were answered on hardware, but these
        two are not HW-verified. Replies land in _observe_mesh_settings."""
        frames: list[bytes] = []
        if not self.state.is_watering:
            # A msg-12 reply also reads as the run-end frame, which would end
            # a flow session mid-run.
            frames.append(self._build(TYPE_LAST_RUN_READ, SEQ_FLOW_END, b"\x00"))
        if self.manual_preset_sec is None:
            frames.append(self._build(TYPE_MANUAL_PRESET_READ, SEQ_MANUAL_PRESET, b"\x00"))
        for frame in frames:
            try:
                await self.connection.send(frame, drain_ms=800)
            except (BleakError, asyncio.TimeoutError, OSError) as err:
                _LOGGER.debug("%s: mesh read %s failed: %s", self.mac, frame.hex(), err)
                return
