"""Mesh (HT25 d7-47) settings + diagnostics: body codecs, reply decode, writes.

Layouts are the B-hyve app's mesh_body_spec (LSB-first, little-endian). Decoders
are pinned to replies captured from Deck (fw0085, mesh d747) and Hill (fw0041,
mesh c7cc) in the 2026-05-05 probe captures. The write frames are decoded from
the app's spec and are NOT hardware-verified, so these tests pin the bytes we
send, not the device's reaction. No hardware or Home Assistant required.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orbit_bhyve.devices.base import FaultStatus
from orbit_bhyve.devices.ht25 import BHyveHT25Device
from orbit_bhyve.devices.mesh import (
    MeshDeviceInfo,
    MeshLastRun,
    delay_end_body,
    manual_preset_body,
    parse_device_info,
    parse_health_faults,
    parse_last_water_cycle,
    rain_delay_body,
    run_mode_body,
)

DECK = 0x47D7
MESH = DECK.to_bytes(2, "little")  # d7 47
HILL = 0xCCC7

# Real replies to the init's reads (probe_status_captures_2026-05-05.md).
DECK_HEALTH = bytes.fromhex("d7474303409f5bf669980a00")
DECK_DEVICE_INFO = bytes.fromhex("d74744014000550001000000")
DECK_STATUS_IDLE = bytes.fromhex("d74742024001ffffffff0000")
DECK_BIND_ACK = bytes.fromhex("d747c10540de93f66910ff00")
HILL_DEVICE_INFO_PUSH = bytes.fromhex("c7cca0014000290001000000")


def _dev(mesh_id: int = DECK) -> BHyveHT25Device:
    # Key-less record -> no BLE connection, no hass needed.
    return BHyveHT25Device(None, {
        "cloud_id": "abc", "name": "Deck", "mac": "44:67:55:52:99:01",
        "hardware": "HT25-0000", "firmware": "0085", "stations": 1,
        "network_key": "", "mesh_device_id": mesh_id,
    })


class _AckConn:
    """Answers each write with its write-ack (command 3: 0x40 set on byte 2,
    body echoed), as the device does for bind/magic/heartbeat. send_actuation
    first plays the init's utc-time ack when `device_clock` is set."""

    def __init__(self, dev, *, ack: bool = True, device_clock: int | None = None):
        self.dev = dev
        self.ack = ack
        self.device_clock = device_clock
        self.sent: list[bytes] = []
        self.actuations = 0
        self.disconnects = 0
        self.is_held = False

    async def send_actuation(self, pt, *, drain_ms=1500):
        self.actuations += 1
        if self.device_clock is not None:
            self.dev._observe_plaintext(
                MESH + bytes([0xC1, 0x05, 0x40])
                + self.device_clock.to_bytes(4, "little") + b"\x10\xff\x00"
            )
        return await self.send(pt)

    async def send(self, pt, *, drain_ms=1500):
        self.sent.append(pt)
        if not self.ack:
            return []
        self.dev._observe_plaintext(pt[:2] + bytes([pt[2] | 0x40]) + pt[3:])
        return [b"notif"]

    async def disconnect(self):
        self.disconnects += 1


def _wired(**kw) -> tuple[BHyveHT25Device, _AckConn]:
    dev = _dev()
    conn = _AckConn(dev, **kw)
    dev.connection = conn
    return dev, conn


# --- body encoders (app spec, not HW-verified) ------------------------------

def test_body_encoders():
    assert rain_delay_body(7200).hex() == "201c0000"
    assert delay_end_body(0x69F693DE).hex() == "de93f669"
    assert run_mode_body(True).hex() == "0501"   # cmd 5 | _4, run-mode 1 = auto
    assert run_mode_body(False).hex() == "0500"
    assert manual_preset_body(600).hex() == "5802"


@pytest.mark.parametrize("secs", [0, 65536])
def test_manual_preset_body_range(secs):
    with pytest.raises(ValueError):
        manual_preset_body(secs)


def test_set_run_mode_shares_the_start_stop_command_layout():
    # device-control is {command u4, ...}: START's 04 and STOP's 02 (both
    # HW-confirmed) sit in the same low nibble set-run-mode's 5 does.
    dev = _dev()
    assert dev._build_start(0xB6, 600).hex() == "d747b60d40045802" + "00000000"
    assert dev._build_stop(0xB7).hex() == "d747b70d4002000000"


# --- decoders, pinned to captures -------------------------------------------

def test_device_info_capture_decodes():
    info = parse_device_info(DECK_DEVICE_INFO[5:])
    assert info == MeshDeviceInfo(device_type=0, hardware_version=0, firmware_version=85, stations=1)
    assert info.device_type_name == "hose_tap"


def test_device_info_reply_and_push_are_observed():
    dev = _dev()
    dev._observe_plaintext(DECK_DEVICE_INFO)
    assert dev.mesh_device_info.firmware_version == 85
    # fw0041 also pushes device-info unprompted as a write (byte 2 = 0xa0).
    hill = _dev(HILL)
    hill._observe_plaintext(HILL_DEVICE_INFO_PUSH)
    assert hill.mesh_device_info.firmware_version == 41


def test_our_own_read_echo_is_not_device_data():
    # The init's subsystem read (command 0) carries zeros; never decode it.
    dev = _dev()
    dev._observe_plaintext(bytes.fromhex("d74704014000000000"))
    assert dev.mesh_device_info is None


def test_foreign_mesh_address_is_ignored():
    dev = _dev()
    dev._observe_plaintext(HILL_DEVICE_INFO_PUSH)
    assert dev.mesh_device_info is None


def test_health_capture_has_no_faults_and_keeps_battery():
    dev = _dev()
    dev._observe_plaintext(DECK_HEALTH)
    assert dev.battery_mv == 0x0A98  # 2712 mV, base parser untouched
    assert dev.state.faults == FaultStatus()
    assert not dev.state.faults.any_fault


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (0x01, FaultStatus(voltage_boost_fail=True)),
        (0x02, FaultStatus(valve_on_no_flow=True)),
        (0x04, FaultStatus(valve_off_flow=True)),
        (0x07, FaultStatus(voltage_boost_fail=True, valve_on_no_flow=True, valve_off_flow=True)),
    ],
)
def test_health_fault_bits(flags, expected):
    body = DECK_HEALTH[5:11] + bytes([flags])
    assert parse_health_faults(body) == expected


def test_health_faults_reach_state_and_reasons():
    dev = _dev()
    pokes: list[int] = []
    dev.set_state_changed_callback(lambda: pokes.append(1))
    dev._observe_plaintext(DECK_HEALTH[:11] + b"\x05")
    assert dev.state.faults.reasons == (
        "Voltage boost circuit failure",
        "Flow while valve closed (leak)",
    )
    assert pokes == [1]


def test_bind_ack_sets_the_device_clock_reference():
    # The utc-time ack echoes the clock the device now runs: 0x69f693de.
    dev = _dev()
    dev._observe_plaintext(DECK_BIND_ACK)
    assert dev.state.device_clock == 0x69F693DE
    assert abs(dev._device_utc_now() - 0x69F693DE) <= 1


def test_device_clock_falls_back_to_host_time():
    dev = _dev()
    now = datetime.now(timezone.utc).timestamp()
    assert abs(dev._device_utc_now() - now) <= 1


def test_status_idle_capture_means_auto_and_no_rain_delay():
    dev = _dev()
    dev.state.rain_delay_minutes = 60
    dev._observe_plaintext(DECK_STATUS_IDLE)
    assert dev.state.controller_mode == 1
    assert dev.state.rain_delay_minutes == 0
    assert dev.state.rain_delay_ends is None


def test_status_off_means_run_mode_off():
    dev = _dev()
    dev._observe_plaintext(MESH + bytes.fromhex("420240") + bytes.fromhex("00ffffffff0000"))
    assert dev.state.controller_mode == 0


def test_status_rain_delay_reads_minutes_left():
    # {status 3, _4, delay-remaining-mins u24}: 0xb4 = 180 minutes.
    dev = _dev()
    before = datetime.now(timezone.utc)
    dev._observe_plaintext(MESH + bytes.fromhex("420240") + bytes.fromhex("03b40000000000"))
    assert dev.state.rain_delay_minutes == 180
    left = (dev.state.rain_delay_ends - before).total_seconds()
    assert 180 * 60 - 2 < left < 180 * 60 + 2
    assert dev.state.is_watering is False


def test_set_run_mode_ack_sets_mode_but_echo_does_not():
    dev = _dev()
    dev._observe_plaintext(MESH + bytes.fromhex("b80d40") + bytes.fromhex("0500"))
    assert dev.state.controller_mode is None  # write (no ack bit): could be our own
    dev._observe_plaintext(MESH + bytes.fromhex("f80d40") + bytes.fromhex("0500"))
    assert dev.state.controller_mode == 0


def test_manual_preset_read_ack():
    dev = _dev()
    dev._observe_plaintext(MESH + bytes.fromhex("570740") + bytes.fromhex("5802"))
    assert dev.manual_preset_sec == 600


def test_last_water_cycle_push():
    # {station u6, _2, run-time-secs u16, program u3, _5, water-volume-ticks u24}
    body = bytes.fromhex("00" "5802" "01" "701101")
    assert parse_last_water_cycle(body) == MeshLastRun(
        station=0, run_time_secs=600, program=1, water_volume_ticks=70_000
    )
    dev = _dev()
    dev._observe_plaintext(MESH + bytes.fromhex("8c0c40") + body)
    assert dev.last_run.water_volume_ticks == 70_000
    assert dev.last_run.run_time_secs == 600


def test_short_last_water_cycle_is_ignored():
    dev = _dev()
    dev._observe_plaintext(MESH + bytes.fromhex("8c0c40") + bytes.fromhex("005802"))
    assert dev.last_run is None


# --- writes (frames decoded from the app's spec; not HW-verified) -----------

def test_set_rain_delay_sends_both_messages_anchored_to_device_clock():
    # Device clock deliberately ~5 months behind the host, as on main before
    # the clock fix: the end time must follow the device's clock.
    dev, conn = _wired(device_clock=0x69F693DE)
    before = datetime.now(timezone.utc)
    assert asyncio.run(dev.set_rain_delay(120)) is True
    assert conn.actuations == 1  # first write gets the fresh bind
    assert conn.sent[0].hex() == "d7478a0a40201c0000"
    end = conn.sent[1]
    assert end[:5].hex() == "d747981840"
    assert abs(int.from_bytes(end[5:9], "little") - (0x69F693DE + 7200)) <= 1
    assert dev.state.rain_delay_minutes == 120
    left = (dev.state.rain_delay_ends - before).total_seconds()
    assert 7200 - 2 < left < 7200 + 2
    assert conn.disconnects == 1


def test_rain_delay_unacked_is_not_confirmed():
    dev, conn = _wired(ack=False)
    assert asyncio.run(dev.set_rain_delay(60)) is False
    assert len(conn.sent) == 2
    assert dev.state.rain_delay_minutes is None


def test_clear_rain_delay_writes_zeros():
    dev, conn = _wired()
    dev.state.rain_delay_minutes = 60
    dev.state.rain_delay_ends = datetime.now(timezone.utc) + timedelta(hours=1)
    assert asyncio.run(dev.set_rain_delay(0)) is True
    assert [f.hex() for f in conn.sent] == ["d7478a0a4000000000", "d747981840" "00000000"]
    assert dev.state.rain_delay_minutes == 0
    assert dev.state.rain_delay_ends is None


@pytest.mark.parametrize(("on", "frame", "mode"), [
    (True, "d747b80d400501", 1),
    (False, "d747b80d400500", 0),
])
def test_set_controller_mode(on, frame, mode):
    dev, conn = _wired()
    assert asyncio.run(dev.set_controller_mode(on)) is True
    assert [f.hex() for f in conn.sent] == [frame]
    assert dev.state.controller_mode == mode


def test_set_controller_mode_unacked():
    dev, _ = _wired(ack=False)
    assert asyncio.run(dev.set_controller_mode(False)) is False
    assert dev.state.controller_mode is None


def test_set_manual_preset_runtime():
    dev, conn = _wired()
    assert asyncio.run(dev.set_manual_preset_runtime(600)) is True
    assert [f.hex() for f in conn.sent] == ["d7478707405802"]
    assert dev.manual_preset_sec == 600


def test_an_older_ack_does_not_confirm_a_new_write():
    dev, conn = _wired()
    asyncio.run(dev.set_controller_mode(True))
    conn.ack = False
    assert asyncio.run(dev.set_controller_mode(True)) is False


def test_writes_leave_a_held_flow_session_open():
    dev, conn = _wired()
    conn.is_held = True
    asyncio.run(dev.set_controller_mode(True))
    assert conn.disconnects == 0


def test_writes_without_a_connection_fail():
    dev = _dev()
    assert asyncio.run(dev.set_rain_delay(60)) is False
    assert asyncio.run(dev.set_controller_mode(True)) is False
    assert asyncio.run(dev.set_manual_preset_runtime(60)) is False


# --- refresh reads ----------------------------------------------------------

def test_refresh_reads_last_run_and_unknown_preset():
    dev, conn = _wired(ack=False)
    asyncio.run(dev._read_mesh_extras())
    assert [f.hex() for f in conn.sent] == ["d7471c0c4000", "d747170740" "00"]


def test_refresh_skips_known_preset_and_last_run_mid_run():
    # A msg-12 reply doubles as the run-end frame, which would end a flow
    # session mid-run, so it isn't read while watering.
    dev, conn = _wired(ack=False)
    dev.manual_preset_sec = 600
    dev.state.is_watering = True
    asyncio.run(dev._read_mesh_extras())
    assert conn.sent == []
