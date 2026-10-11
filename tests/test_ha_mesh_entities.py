"""Mesh (HT25) entities: which platforms create them, and what they read.

Rain delay (number + select + ends), Automatic watering, Button run time,
Firmware and Last run water used are created for a mesh HT25 with a BLE
session, not for a key-less one; the mesh Problem sensor folds the
device-health faults in with the bootloader check. Same HA-guard and stand-in
coordinator style as test_ha_rain_delay.py.
"""
from __future__ import annotations

import asyncio
from functools import partial
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from orbit_bhyve import binary_sensor, number, select, sensor, switch  # noqa: E402
from orbit_bhyve.const import DOMAIN  # noqa: E402
from orbit_bhyve.coordinator import BHyveDeviceCoordinator  # noqa: E402
from orbit_bhyve.devices.base import FaultStatus  # noqa: E402
from orbit_bhyve.devices.ht25 import BHyveHT25Device  # noqa: E402
from orbit_bhyve.devices.ht34a import BHyveHT34ADevice  # noqa: E402
from orbit_bhyve.devices.mesh import MeshDeviceInfo, MeshLastRun  # noqa: E402

MESH_ONLY = {
    "BHyveButtonRunTimeNumber",
    "BHyveMeshFirmwareSensor",
    "BHyveLastRunWaterSensor",
}
MESH_SETTINGS = {
    "BHyveRainDelayNumber",
    "BHyveRainDelaySelect",
    "BHyveRainDelayEndsSensor",
    "BHyveAutomaticWateringSwitch",
}


def _record(hardware: str, firmware: str, **extra) -> dict:
    return {
        "cloud_id": f"cloud-{hardware}", "name": hardware, "mac": "44:67:55:52:99:01",
        "hardware": hardware, "firmware": firmware, "stations": 1, "network_key": "",
        **extra,
    }


def _mesh(*, connected: bool = True) -> BHyveHT25Device:
    dev = BHyveHT25Device(None, _record("HT25-0000", "0085", mesh_device_id=0x47D7))
    if connected:
        dev.connection = SimpleNamespace(in_bootloader=False)
    return dev


def _entities(dev) -> set[str]:
    coord = SimpleNamespace(device=dev, data=None, preferred_duration_sec=None)
    hass = SimpleNamespace(data={DOMAIN: {"e1": SimpleNamespace(coordinators={"c": coord})}})
    entry = SimpleNamespace(entry_id="e1", options={})
    names: set[str] = set()
    for platform in (binary_sensor, number, select, sensor, switch):
        asyncio.run(platform.async_setup_entry(
            hass, entry, lambda ents: names.update(type(e).__name__ for e in ents)
        ))
    return names


def test_mesh_timer_gets_settings_and_diagnostics():
    names = _entities(_mesh())
    assert MESH_SETTINGS | MESH_ONLY <= names
    # The mesh Problem sensor stays the bootloader+health one.
    assert "BHyveBootloaderProblemBinarySensor" in names
    assert "BHyveProblemBinarySensor" not in names
    # Programs aren't exposed for mesh timers.
    assert "BHyveProgramEnableSwitch" not in names


def test_keyless_mesh_record_gets_none_of_them():
    names = _entities(_mesh(connected=False))
    assert not (MESH_SETTINGS | MESH_ONLY) & names


def test_protobuf_timer_gets_no_mesh_only_entities():
    names = _entities(BHyveHT34ADevice(None, _record("HT34A-0001", "0107")))
    assert not MESH_ONLY & names
    assert {"BHyveRainDelayNumber", "BHyveRainDelaySelect", "BHyveAutomaticWateringSwitch"} <= names


# --- Problem ---------------------------------------------------------------

def _problem(dev):
    return binary_sensor.BHyveBootloaderProblemBinarySensor(SimpleNamespace(device=dev, data=None))


def test_problem_off_with_healthy_report():
    dev = _mesh()
    dev.state.faults = FaultStatus()
    sensor_ = _problem(dev)
    assert sensor_.is_on is False
    assert sensor_.extra_state_attributes["reason"] == "No faults"


def test_problem_on_for_health_fault():
    dev = _mesh()
    dev.state.faults = FaultStatus(valve_off_flow=True)
    sensor_ = _problem(dev)
    assert sensor_.is_on is True
    attrs = sensor_.extra_state_attributes
    assert attrs["reason"] == "Flow while valve closed (leak)"
    assert attrs["valve_off_flow_detected"] is True
    assert attrs["voltage_boost_fail"] is False


def test_problem_bootloader_without_health_report():
    dev = _mesh()
    dev.connection.in_bootloader = True
    sensor_ = _problem(dev)
    assert sensor_.is_on is True
    assert sensor_.extra_state_attributes == {
        "reason": "Stuck in bootloader after a failed firmware update - press Recover firmware"
    }


# --- Firmware / Last run ---------------------------------------------------

def test_firmware_sensor_formats_like_the_cloud_record():
    dev = _mesh()
    fw = sensor.BHyveMeshFirmwareSensor(SimpleNamespace(device=dev, data=None))
    assert fw.native_value is None
    dev.mesh_device_info = MeshDeviceInfo(device_type=0, hardware_version=0, firmware_version=41, stations=1)
    assert fw.native_value == "0041"
    assert fw.extra_state_attributes == {
        "hardware_version": 0, "device_type": "hose_tap", "stations": 1,
    }


def test_last_run_sensor_uses_flow_calibration():
    dev = _mesh()
    dev.flow_counts_per_litre = 112
    last = sensor.BHyveLastRunWaterSensor(SimpleNamespace(device=dev, data=None))
    assert last.native_value is None
    dev.last_run = MeshLastRun(station=0, run_time_secs=600, program=1, water_volume_ticks=1120)
    assert last.native_value == 10.0
    assert last.extra_state_attributes == {
        "run_time_secs": 600, "program": "A", "water_volume_ticks": 1120,
    }
    dev.last_run = MeshLastRun(station=0, run_time_secs=60, program=0, water_volume_ticks=0)
    assert last.extra_state_attributes["program"] == "manual"


# --- Button run time -------------------------------------------------------

def _button_number(ok: bool, preset: int | None = None):
    calls: list = []

    async def set_manual_preset_runtime(secs):
        calls.append(secs)
        return ok

    async def async_request_refresh():
        calls.append("refresh")

    device = SimpleNamespace(
        name="Deck", unique_id="deck", cloud_id="cloud-deck", hardware="HT25-0000",
        firmware="0085", mac="44:67:55:52:99:01", manual_preset_sec=preset,
        set_manual_preset_runtime=set_manual_preset_runtime,
    )
    coord = SimpleNamespace(device=device, async_request_refresh=async_request_refresh)
    coord.async_confirm_write = partial(BHyveDeviceCoordinator.async_confirm_write, coord)
    return number.BHyveButtonRunTimeNumber(coord), calls


def test_button_run_time_reads_minutes():
    entity, _ = _button_number(True, preset=600)
    assert entity.native_value == 10.0
    entity, _ = _button_number(True)
    assert entity.native_value is None


def test_button_run_time_writes_seconds():
    entity, calls = _button_number(True)
    asyncio.run(entity.async_set_native_value(15))
    assert calls == [900, "refresh"]


def test_button_run_time_unconfirmed_raises():
    entity, calls = _button_number(False)
    with pytest.raises(HomeAssistantError, match="button run time write not confirmed"):
        asyncio.run(entity.async_set_native_value(5))
    assert calls == [300, "refresh"]
