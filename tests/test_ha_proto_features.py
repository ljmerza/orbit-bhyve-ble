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
