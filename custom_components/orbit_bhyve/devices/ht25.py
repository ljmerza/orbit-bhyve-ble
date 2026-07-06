"""HT25-0000 (single-station hose-tap) device class.

d7-47 protocol family. Verified against fw0085 (Deck) 2026-05-03 and
fw0041 (Hill) 2026-05-05 — same protocol, the 2-byte frame prefix is the
device's own mesh_device_id in little-endian (Deck's old "D747_MAGIC"
constant was just Deck's mesh_id 18391 = 0x47D7 LE).

Firmware variants subclass this and override _rebind_sid_delta only.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from ..connection import BHyveBleConnection
from .base import BHyveBleDeviceBase

_LOGGER = logging.getLogger(__name__)

D747_ROUTING = 0x40
SEQ_BIND = 0x05
SEQ_STATUS = 0x02
SEQ_INFO = 0x03
SEQ_SUBSYSTEM = 0x01
SEQ_MAGIC_CHECK = 0x00
SEQ_HEARTBEAT = 0x09
SEQ_WATER_CTRL = 0x0D

INIT_INTER_STEP_SEC = 0.15

BIND_TAIL = bytes.fromhex("f66910ff")

# Empirical hub mesh_device_id per network key. The cloud /api/networks
# response doesn't surface ble_device_id for hubs in our wizard cache, so
# magic2 (which references the paired hub) needs this fallback. Values
# captured from phone-app BTSnoop logs:
#   Topology A (Deck, Hub Guest BR):    hub mesh_id 0xEB42 = 60226
#   Topology B (Hill, Corner, Hub Garage): hub mesh_id 0x233D = 9021
_HUB_MESH_BY_NETWORK_KEY = {
    "f0983e39083a335644614ffb3bd67ee4": 0xEB42,
    "bcd2ff1a23290e00482ee1d0d4376a95": 0x233D,
}


class BHyveHT25Device(BHyveBleDeviceBase):
    """HT25 single-station timer."""

    frame_magic = 0x10
    trailer_const = 0x10

    # Session-id increment between the `bind` and `rebind` init steps.
    # fw0041 (Hill) confirmed +2 via BTSnoop 2026-05-05. Firmware variants
    # override this attribute in a subclass rather than branching in code.
    _rebind_sid_delta: int = 2

    # Duration/zone of the most recent START we sent, stashed so the device's
    # async START-ack (f60d) can arm the off-timer even when the BLE
    # write-response timed out (e.g. through an ESPHome proxy). See
    # _observe_plaintext.
    _pending_start_duration: int | None = None
    _pending_start_zone: int | None = None

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
            hub_id = _HUB_MESH_BY_NETWORK_KEY.get(self.network_key.lower())
            if hub_id is None:
                _LOGGER.warning(
                    "%s: hub_mesh_device_id unresolved (network_key not in fallback "
                    "table); using 0x0000 in magic2. Init still completes with a "
                    "placeholder hub id, but if START is dropped this is a suspect.",
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
        """8-step init the phone runs after the AES handshake. Sending the
        watering command without it produces a silent drop. Only `bind`,
        `status`, and `info` are confirmed required; the others may be
        prunable but are kept for safety until empirically tested."""
        # Surface resolved addresses so a test run self-verifies the prefix is
        # the device's own id and NOT the legacy hardcoded d747.
        _LOGGER.debug(
            "%s: frames mesh_address=%s hub_mesh_address=%s",
            self.mac, self.mesh_address.hex(), self.hub_mesh_address.hex(),
        )

        sid = os.urandom(2)
        # Rebind reuses the session id incremented by a firmware-specific delta
        # (fw0041=+2; see _rebind_sid_delta / subclasses).
        sid2 = ((int.from_bytes(sid, "little") + self._rebind_sid_delta) & 0xFFFF).to_bytes(2, "little")

        # magic1 payload: 0x01 || self mesh_id LE || 4 zero bytes
        # magic2 payload: 0x00 || hub  mesh_id LE || 4 zero bytes
        magic1_payload = b"\x01" + self.mesh_address + b"\x00\x00\x00\x00"
        magic2_payload = b"\x00" + self.hub_mesh_address + b"\x00\x00\x00\x00"

        steps: list[tuple[bytes, str]] = [
            (self._build(0x81, SEQ_BIND, sid + BIND_TAIL), "bind"),
            (self._build(0x02, SEQ_STATUS, b"\x00"), "status"),
            (self._build(0x03, SEQ_INFO, b"\x00" * 7), "info"),
            (self._build(0x04, SEQ_SUBSYSTEM, b"\x00" * 3), "subsystem"),
            (self._build(0x85, SEQ_MAGIC_CHECK, magic1_payload), "magic1"),
            (self._build(0x85, SEQ_MAGIC_CHECK, magic2_payload), "magic2"),
            (self._build(0x85, SEQ_HEARTBEAT, b"\x00"), "heartbeat"),
            (self._build(0x86, SEQ_BIND, sid2 + BIND_TAIL), "rebind"),
        ]
        for plaintext, label in steps:
            _LOGGER.debug("%s: init → %s pt=%s", self.mac, label, plaintext.hex())
            # _write_locked, not send_raw — we're already inside conn's lock
            # (post-handshake hook runs from _open() called by send()).
            await conn._write_locked(plaintext)
            await asyncio.sleep(INIT_INTER_STEP_SEC)
        await asyncio.sleep(0.3)

    async def start_watering(self, station: int, duration_sec: int) -> bool:
        if self.connection is None:
            return False
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
            # The off-timer is armed by the device's START-ack (_observe_plaintext),
            # not by send()'s return — so this holds even when the write-response
            # times out. Report whatever state the ack has produced by now.
            return self.state.is_watering
        finally:
            if self.connection is not None:
                await self.connection.disconnect()

    async def stop_watering(self, station: int | None = None) -> bool:
        if self.connection is None:
            return False
        try:
            plaintext = self._build_stop(0xB7)
            _LOGGER.debug("%s: STOP tx pt=%s", self.mac, plaintext.hex())
            notifs = await self._send_command(plaintext, "STOP")
            self._stamp_command("stop", len(notifs))
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
        """Best-effort liveness refresh: update is_connected from the pooled
        connection without opening one. is_watering is driven by the device's
        START/STOP ack (_observe_plaintext) and the coordinator's wall-clock
        auto-close; the STATUS reply's mode byte is decoded too (base
        _observe_plaintext, 0x04=watering/0x01=idle), but an idle poll doesn't
        connect to elicit it, so live idle-poll status would need a connect +
        STATUS query (future enhancement, needs hardware verification)."""
        await super().refresh_state()
        return self.state
