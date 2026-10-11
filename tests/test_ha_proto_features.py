"""Entities for the protobuf-family additions from the app-schema gap report.

Same HA-guard and stand-in coordinator style as test_ha_device_status.py: the
entities are built on a SimpleNamespace coordinator and read DeviceState
directly, so no running Home Assistant is needed.
"""
from __future__ import annotations

import asyncio
from functools import partial
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from orbit_bhyve.button import BHyveRunProgramButton  # noqa: E402
from orbit_bhyve.coordinator import BHyveDeviceCoordinator  # noqa: E402
from orbit_bhyve.devices.base import DeviceState, ProgramSummary  # noqa: E402
from orbit_bhyve.sensor import BHyveDeviceFlowRateSensor  # noqa: E402


def _device(**state_kwargs):
    return SimpleNamespace(
        name="Garden",
        unique_id="garden",
        cloud_id="cloud-garden",
        hardware="HT25G2-0001",
        firmware="0111",
        mac="44:67:55:00:00:01",
        stations=1,
        flow_counts_per_gallon=433,
        state=DeviceState(**state_kwargs),
    )


def _coord(device):
    return SimpleNamespace(device=device, data=None)


# --- Flow rate (device): app-style math --------------------------------------

def test_device_flow_sensor_prefers_reported_gpm():
    sensor = BHyveDeviceFlowRateSensor(_coord(_device(flow_gpm_device=2.5, flow_hz=30)))
    assert sensor.native_value == 2.5
    assert sensor.extra_state_attributes["source"] == "reported"


def test_device_flow_sensor_uses_hz_line_and_calibrated_volume():
    sensor = BHyveDeviceFlowRateSensor(_coord(_device(flow_hz=32, flow_total=866)))
    assert sensor.native_value == 4.58
    attrs = sensor.extra_state_attributes
    assert attrs["source"] == "frequency"
    assert attrs["frequency_hz"] == 32
    assert attrs["cycle_volume_gal"] == 2.0  # 866 ticks / 433 per gallon


def test_device_flow_sensor_unknown_without_flow_data():
    sensor = BHyveDeviceFlowRateSensor(_coord(_device()))
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["source"] is None


# --- Run program buttons -----------------------------------------------------

def _write_coord(device, ok: bool, calls: list):
    async def async_request_refresh():
        calls.append(("refresh",))

    coord = SimpleNamespace(
        device=device, data=None, last_update_success=True,
        async_request_refresh=async_request_refresh,
    )
    coord.async_confirm_write = partial(BHyveDeviceCoordinator.async_confirm_write, coord)
    return coord


def test_run_program_button_available_only_for_stored_slots():
    device = _device(programs={
        1: ProgramSummary(slot=1, empty=False, zones=((0, 60),)),
        2: ProgramSummary(slot=2, empty=True),
    })
    coord = _write_coord(device, True, [])
    assert BHyveRunProgramButton(coord, "A").available is True
    assert BHyveRunProgramButton(coord, "B").available is False   # empty slot
    assert BHyveRunProgramButton(coord, "C").available is False   # never read
    assert BHyveRunProgramButton(coord, "A").unique_id == "garden_run_program_a"


@pytest.mark.parametrize("ok", [True, False])
def test_run_program_button_press_runs_slot_and_surfaces_failure(ok):
    calls: list = []
    device = _device()

    async def run_program(slot):
        calls.append(("run", slot))
        return ok

    device.run_program = run_program
    button = BHyveRunProgramButton(_write_coord(device, ok, calls), "C")
    if ok:
        asyncio.run(button.async_press())
    else:
        with pytest.raises(HomeAssistantError):
            asyncio.run(button.async_press())
    assert calls == [("run", 3), ("refresh",)]


# --- button gating (async_setup_entry) ------------------------------------------

def _real_device(cls, hardware, stations):
    dev = object.__new__(cls)  # real class for the isinstance gates, no HA __init__
    dev.cloud_id, dev.name, dev.mac = f"cloud-{hardware}", hardware, "AA:BB:CC:DD:EE:10"
    dev.hardware, dev.firmware, dev.stations = hardware, "0107", stations
    dev.connection = object()  # any non-None BLE connection
    dev.state = DeviceState()
    return dev


def _setup_platform(module, *devices):
    from orbit_bhyve.const import DOMAIN

    coords = {
        d.cloud_id: SimpleNamespace(device=d, data=None, last_update_success=True)
        for d in devices
    }
    hass = SimpleNamespace(data={DOMAIN: {"e1": SimpleNamespace(coordinators=coords)}})
    added: list = []
    asyncio.run(module.async_setup_entry(hass, SimpleNamespace(entry_id="e1"), added.extend))
    return added


def test_skip_zone_button_only_on_multi_station_units():
    from orbit_bhyve import button
    from orbit_bhyve.devices import BHyveHT25G2Device, BHyveHT34ADevice

    xd = _real_device(BHyveHT34ADevice, "HT34A-0001", 4)
    g2 = _real_device(BHyveHT25G2Device, "HT25G2-0001", 1)
    names = {(e.coordinator.device.hardware, e.name) for e in _setup_platform(button, xd, g2)}
    assert ("HT34A-0001", "Skip zone") in names
    assert ("HT25G2-0001", "Skip zone") not in names
    for hw in ("HT34A-0001", "HT25G2-0001"):
        for letter in "ABCD":
            assert (hw, f"Run program {letter}") in names


def test_skip_zone_button_available_only_while_watering():
    from orbit_bhyve.button import BHyveSkipZoneButton

    device = _device(is_watering=False)
    coord = SimpleNamespace(device=device, data=None, last_update_success=True)
    button = BHyveSkipZoneButton(coord)
    assert button.available is False
    device.state.is_watering = True
    assert button.available is True


# --- Firmware version (#23 DeviceInfo) -----------------------------------------

def test_firmware_version_sensor():
    from datetime import datetime, timezone

    from orbit_bhyve.devices.base import DeviceInfoReport
    from orbit_bhyve.sensor import BHyveFirmwareVersionSensor

    device = _device()
    sensor = BHyveFirmwareVersionSensor(_coord(device))
    assert sensor.native_value is None and sensor.extra_state_attributes == {}
    device.state.device_info = DeviceInfoReport(
        fw_version="0111", hw_version="HT25G2-0001", num_stations=1,
        bootloader_version=5, ble_bootloader_version=3, ble_app_version=7,
    )
    device.state.device_info_at = datetime(2026, 10, 11, tzinfo=timezone.utc)
    assert sensor.native_value == "0111"
    assert sensor.extra_state_attributes == {
        "hardware_version": "HT25G2-0001",
        "bootloader_version": 5,
        "ble_app_version": 7,
        "ble_bootloader_version": 3,
        "stations": 1,
        "read_at": "2026-10-11T00:00:00+00:00",
    }
