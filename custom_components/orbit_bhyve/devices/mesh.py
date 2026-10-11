"""Body codecs for the d7-47 mesh messages behind the HT25 settings/diagnostics.

Layouts come from the B-hyve app's own `mesh_body_spec` (bit-packed LSB-first,
little-endian), recovered from the app bundle on 2026-10-11. Each helper
handles only a message BODY — the bytes after the 5-byte
[mesh:2][seq|cmd<<6][msg_id|dest<<14 : 2] header that ht25._build adds.

Decoders are pinned to captured replies in tests/test_mesh_settings.py where a
capture exists. The write encoders were decoded from the app's spec and are NOT
hardware-verified. Kept HA-free so the test suite runs without Home Assistant.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .base import FaultStatus

# device-info `device-type` (u5) values.
MESH_DEVICE_TYPES = {
    0: "hose_tap",
    1: "underground",
    2: "rain_sensor",
    3: "moisture_sensor",
    4: "flood_sensor",
}

# device-control `command` (u4) for set-run-mode.
DEVICE_CONTROL_SET_RUN_MODE = 5


@dataclass(frozen=True)
class MeshDeviceInfo:
    """device-info (msg 1): {device-type u5, hardware-version u3,
    firmware-version u16, stations u6, _26}."""

    device_type: int
    hardware_version: int
    firmware_version: int
    stations: int | None = None

    @property
    def device_type_name(self) -> str | None:
        return MESH_DEVICE_TYPES.get(self.device_type)


@dataclass(frozen=True)
class MeshLastRun:
    """last-water-cycle-info (msg 12): {station u6, _2, run-time-secs u16,
    program u3, _5, water-volume-ticks u24}. program 0 = manual, 1-7 = A-G."""

    station: int
    run_time_secs: int
    program: int
    water_volume_ticks: int


def parse_device_info(body: bytes) -> MeshDeviceInfo | None:
    """Decode a device-info body. Capture: `00 55 00 01` = hose tap, hw 0,
    fw 85, 1 station (Deck, fw0085)."""
    if len(body) < 3:
        return None
    return MeshDeviceInfo(
        device_type=body[0] & 0x1F,
        hardware_version=body[0] >> 5,
        firmware_version=int.from_bytes(body[1:3], "little"),
        stations=(body[3] & 0x3F) if len(body) >= 4 else None,
    )


def parse_health_faults(body: bytes) -> FaultStatus | None:
    """Fault bits of a device-health body: {local-time-secs u32,
    battery-level-mv u16, boost-circuit u1, valve-on-no-flow u1,
    valve-off-flow u1, _5}. Mapped onto the protobuf family's FaultStatus so
    the Problem sensor's reasons read the same on both families."""
    if len(body) < 7:
        return None
    flags = body[6]
    return FaultStatus(
        voltage_boost_fail=bool(flags & 0x01),
        valve_on_no_flow=bool(flags & 0x02),
        valve_off_flow=bool(flags & 0x04),
    )


def parse_last_water_cycle(body: bytes) -> MeshLastRun | None:
    """Decode a last-water-cycle-info body (7 bytes)."""
    if len(body) < 7:
        return None
    return MeshLastRun(
        station=body[0] & 0x3F,
        run_time_secs=int.from_bytes(body[1:3], "little"),
        program=body[3] & 0x07,
        water_volume_ticks=int.from_bytes(body[4:7], "little"),
    )


def rain_delay_body(delay_secs: int) -> bytes:
    """rain-delay (msg 10): {delay-secs u32}. The app sends hours x 3600."""
    return struct.pack("<I", delay_secs)


def delay_end_body(epoch_secs: int) -> bytes:
    """watering-delay-end-time (msg 24): {delay-until-epoch-secs u32}."""
    return struct.pack("<I", epoch_secs)


def run_mode_body(on: bool) -> bytes:
    """device-control set-run-mode: {command u4 = 5, _4, run-mode u8}
    (0 off, 1 auto)."""
    return bytes([DEVICE_CONTROL_SET_RUN_MODE, 1 if on else 0])


def manual_preset_body(run_time_secs: int) -> bytes:
    """manual-preset-run (msg 7): {run-time-secs u16}, the run length the
    timer's physical button starts."""
    if not 0 < run_time_secs <= 0xFFFF:
        raise ValueError(f"run_time_secs out of range (1..65535): {run_time_secs}")
    return struct.pack("<H", run_time_secs)
