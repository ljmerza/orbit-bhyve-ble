"""HT25G2 (Gen2 single-station hose timer) device class.

Protobuf protocol family — the SAME framing, cipher, and timerMode start
message as the HT34A XD timer (frame magic 0x11), NOT the d7-47 mesh
protocol the older HT25-0000 hose timers (fw0041/0085) speak. Sharing a
"HT25" hardware prefix with those mesh devices is the only thing they have
in common; the dispatcher in __init__.py disambiguates by hardware-suffix /
firmware so these land here instead of on BHyveHT25Device.

Sibling of BHyveHT34ADevice (not a subclass): both are single, independent
BHyveBleDeviceBase classes that reuse the shared protobuf builder functions
from ht34a. This keeps the device classes decoupled — matching the pattern
the rest of devices/ follows — so XD-specific changes can't silently alter
the Gen2 path.

Hardware-verified start AND stop on fw0111 valves (BTValve01-04) via the
standalone CLI, which drives byte-identical protobuf frames. Single station:
the device exposes one valve, addressed as wire station_id 0 (station 1 - 1).
"""
from __future__ import annotations

import logging

from .base import BHyveBleDeviceBase
from .ht34a import _STOP_PB, _build_message, _build_start_pb

_LOGGER = logging.getLogger(__name__)


class BHyveHT25G2Device(BHyveBleDeviceBase):
    """Gen2 single-station valve (fw0111), protobuf protocol family."""

    frame_magic = 0x11
    trailer_const = 0x11

    async def start_watering(self, station: int, duration_sec: int) -> bool:
        if self.connection is None:
            return False
        # Single-station device: wire station_id is 0-indexed (station 1 -> 0).
        plaintext = _build_message(_build_start_pb(station - 1, duration_sec))
        notifs = await self.connection.send(plaintext, drain_ms=2000)
        _LOGGER.debug("%s: HT25G2 START station=%d got %d notifications",
                      self.mac, station, len(notifs))
        self._stamp_command(f"start s={station} d={duration_sec}", len(notifs))
        if notifs:
            self.state.is_watering = True
            self.state.active_zone = station
            self.state.seconds_remaining = duration_sec
        return bool(notifs)

    async def stop_watering(self, station: int | None = None) -> bool:
        if self.connection is None:
            return False
        plaintext = _build_message(_STOP_PB)
        notifs = await self.connection.send(plaintext, drain_ms=2000)
        _LOGGER.debug("%s: HT25G2 STOP got %d notifications", self.mac, len(notifs))
        self._stamp_command("stop", len(notifs))
        if notifs:
            self.state.is_watering = False
            self.state.active_zone = None
            self.state.seconds_remaining = None
        return bool(notifs)
