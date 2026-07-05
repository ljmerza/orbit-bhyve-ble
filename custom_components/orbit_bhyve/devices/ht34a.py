"""HT34A / HT34 (XD timer) device class.

Protobuf-over-CRC16 protocol (the `OrbitPbApi_Message` schema from the APK),
shared across the XD family. Covers HT34A-0001 (fw0107, originally ported from
upstream `wxfield/Orbit_B-Hyve_4Port_Controller`) and HT34-0001 (fw0058). Also
serves the 2-port HT32A-0001 (fw0107), routed here as the XD sibling of the
4-port HT34A — station count comes from the cloud record, so the same class
drives a 2-port unit unchanged. Verified end-to-end on 3x HT32A units:
open/close actuation confirmed (issue #13). The cipher/handshake is shared
with HT25; only the inner plaintext (protobuf) and magic byte (0x11) differ.

Battery/status decode and watering-state confirmation were ported from the
stuartdenne fork (PRs #4/#5). The HT34A path is hardware-verified: battery +
status decode confirmed on a real HT34A-0001/fw0107 (issue #16), and the shared
XD actuation path is proven via the HT32A sibling (issue #13). The older
HT34-0001/fw0058 remains unverified — no such unit tested in this repo.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from datetime import datetime, timedelta, timezone

from .base import BHyveBleDeviceBase, _mv_to_pct

_LOGGER = logging.getLogger(__name__)

MSG_HEADER = bytes([0xAA, 0x77, 0x5A, 0x0F])


def _crc16_ccitt(data: bytes, init: int = 0) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


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
# Read-only request messages (empty sub-messages): getDeviceStatusInfo (field
# 15) and getBatteryStatus (field 45). The device answers both with the cached
# key, which is how HT34-0001 compatibility was reported by the fork.
_GET_STATUS_PB = bytes.fromhex("7a00")
_GET_BATTERY_PB = bytes.fromhex("ea0200")


def _rd_varint(b: bytes, i: int) -> tuple[int, int]:
    v = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << s
        if not x & 0x80:
            return v, i
        s += 7


def _pb_fields(data: bytes):
    """Yield (field_num, wire_type, value) for one protobuf message level.
    value is an int for varints, bytes for length-delimited fields."""
    i = 0
    n = len(data)
    while i < n:
        try:
            tag, i = _rd_varint(data, i)
        except IndexError:
            return
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _rd_varint(data, i)
            yield fn, 0, v
        elif wt == 2:
            ln, i = _rd_varint(data, i)
            yield fn, 2, data[i:i + ln]
            i += ln
        elif wt == 5:
            yield fn, 5, data[i:i + 4]
            i += 4
        elif wt == 1:
            yield fn, 1, data[i:i + 8]
            i += 8
        else:
            return


class BHyveHT34ADevice(BHyveBleDeviceBase):
    """4-port XD timer (HT34A / HT34), protobuf protocol."""

    frame_magic = 0x11
    trailer_const = 0x11

    async def _post_handshake(self, conn) -> None:
        """After the handshake, query battery + status so every connect (sync,
        command) refreshes them. Replies are parsed in _observe_plaintext."""
        for pb in (_GET_BATTERY_PB, _GET_STATUS_PB):
            try:
                await conn._write_locked(_build_message(pb))
                await asyncio.sleep(0.4)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: HT34A post-handshake query failed: %s", self.mac, err)

    def _observe_plaintext(self, pt: bytes) -> None:
        """Parse protobuf notifications (OrbitPbApi_Message): battery mV from
        batteryStatus (field 46 -> field 3) and watering state from
        deviceStatusInfo (field 16)."""
        if len(pt) < 8 or pt[:4] != MSG_HEADER:
            return
        body = pt[6:-2]  # strip AA775A0F + len + pad, and trailing CRC16
        for fn, wt, val in _pb_fields(body):
            if fn == 46 and wt == 2:  # batteryStatus
                for sfn, swt, sval in _pb_fields(val):
                    if sfn == 3 and swt == 0 and 1500 <= sval <= 4000:
                        self.battery_mv = sval
                        self.battery_pct = _mv_to_pct(sval)
            elif fn == 16 and wt == 2:  # deviceStatusInfo
                self._parse_status(val)

    def _parse_status(self, val: bytes) -> None:
        """deviceStatusInfo: field 1 is the run state (1=idle, 4=watering),
        field 6 is the active-watering block (present only while watering) with
        field 5 = seconds remaining (counts down) and field 7 = total run time
        (constant). Hardware-verified on a timed run (HT34A fw0107 + HT25G2 fw0111):
        6.5 counted 174->138->102 over a 180s run while 6.7 held at 180. These
        match the vendor names currentTimeRemainingSec / totalRunTimeSec."""
        status = None
        remaining = None
        for fn, wt, v in _pb_fields(val):
            if fn == 1 and wt == 0:
                status = v
            elif fn == 6 and wt == 2:  # active watering block
                for sfn, swt, sv in _pb_fields(v):
                    if sfn == 5 and swt == 0:  # field 5 = seconds remaining
                        remaining = sv
        if status is not None:
            self.state.is_watering = status == 4
            if status != 4:
                self.state.active_zone = None
                self.state.seconds_remaining = None
        if remaining is not None:
            self.state.seconds_remaining = remaining

    async def _refresh_status(self) -> None:
        """Query device status; the reply updates state via _observe_plaintext."""
        if self.connection is not None:
            await self.connection.send(_build_message(_GET_STATUS_PB), drain_ms=1200)

    async def start_watering(self, station: int, duration_sec: int) -> bool:
        if self.connection is None:
            return False
        # Upstream uses 0-indexed stations on the wire.
        plaintext = _build_message(_build_start_pb(station - 1, duration_sec))
        for attempt in range(2):
            notifs = await self.connection.send(plaintext, drain_ms=2000)
            self._stamp_command(f"start s={station} d={duration_sec}", len(notifs))
            await self._refresh_status()
            if self.state.is_watering:
                now = datetime.now(timezone.utc)
                self.state.active_zone = station
                self.state.started_at = now
                self.state.expected_off_at = now + timedelta(seconds=duration_sec)
                if not self.state.seconds_remaining:
                    self.state.seconds_remaining = duration_sec
                _LOGGER.debug("%s: HT34A START confirmed watering", self.mac)
                return True
            _LOGGER.warning("%s: HT34A START not confirmed (attempt %d/2)", self.mac, attempt + 1)
        _LOGGER.error("%s: HT34A START failed to confirm watering", self.mac)
        return False

    async def stop_watering(self, station: int | None = None) -> bool:
        if self.connection is None:
            return False
        plaintext = _build_message(_STOP_PB)
        for attempt in range(2):
            notifs = await self.connection.send(plaintext, drain_ms=2000)
            self._stamp_command("stop", len(notifs))
            await self._refresh_status()
            if not self.state.is_watering:
                self.state.active_zone = None
                self.state.seconds_remaining = None
                self.state.started_at = None
                self.state.expected_off_at = None
                _LOGGER.debug("%s: HT34A STOP confirmed idle", self.mac)
                return True
            _LOGGER.warning("%s: HT34A STOP not confirmed (attempt %d/2)", self.mac, attempt + 1)
        _LOGGER.error("%s: HT34A STOP failed to confirm idle", self.mac)
        return False
