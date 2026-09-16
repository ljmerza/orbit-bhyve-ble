"""Entity write paths surface unconfirmed writes (issue #52).

Device writes (`set_rain_delay`, `clear_rain_delay`, `set_controller_mode`,
`set_program_enabled`) return False when the device does not echo the new state
or no write went out. The rain-delay number/select and the automatic-watering /
program switches used to discard that, so the entity silently snapped back on
the next refresh; they now go through `coordinator.async_confirm_write`, which
refreshes and then raises HomeAssistantError. Same HA-guard and direct-drive
style as test_refresh_service.py.
"""
from __future__ import annotations

import asyncio
from functools import partial
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from orbit_bhyve.coordinator import BHyveDeviceCoordinator  # noqa: E402
from orbit_bhyve.number import BHyveRainDelayNumber  # noqa: E402
from orbit_bhyve.select import BHyveRainDelaySelect  # noqa: E402
from orbit_bhyve.switch import (  # noqa: E402
    BHyveAutomaticWateringSwitch,
    BHyveProgramEnableSwitch,
)


def _coordinator(ok: bool):
    calls: list = []

    def _write(name):
        async def write(*args):
            calls.append((name, *args))
            return ok
        return write

    async def async_request_refresh():
        calls.append(("refresh",))

    device = SimpleNamespace(
        name="XD",
        unique_id="xd",
        cloud_id="cloud-xd",
        hardware="HT34A-0001",
        firmware="0107",
        mac="AA:BB:CC:DD:EE:FF",
        set_rain_delay=_write("set"),
        clear_rain_delay=_write("clear"),
        set_controller_mode=_write("mode"),
        set_program_enabled=_write("program"),
    )
    coord = SimpleNamespace(device=device, async_request_refresh=async_request_refresh)
    # Bind the real coordinator methods onto the stand-in (no hass needed).
    coord.async_confirm_write = partial(BHyveDeviceCoordinator.async_confirm_write, coord)
    coord.async_apply_rain_delay = partial(BHyveDeviceCoordinator.async_apply_rain_delay, coord)
    return coord, calls


def test_unconfirmed_set_raises_after_refresh():
    coord, calls = _coordinator(ok=False)
    with pytest.raises(HomeAssistantError, match="XD: rain delay write not confirmed"):
        asyncio.run(coord.async_apply_rain_delay(120))
    # The refresh still runs so the entity shows the device's real state.
    assert calls == [("set", 120), ("refresh",)]


def test_unconfirmed_clear_raises_after_refresh():
    coord, calls = _coordinator(ok=False)
    with pytest.raises(HomeAssistantError):
        asyncio.run(coord.async_apply_rain_delay(0))
    assert calls == [("clear",), ("refresh",)]


def test_confirmed_write_is_silent():
    coord, calls = _coordinator(ok=True)
    asyncio.run(coord.async_apply_rain_delay(120))
    assert calls == [("set", 120), ("refresh",)]


def test_number_routes_through_helper():
    coord, calls = _coordinator(ok=False)
    with pytest.raises(HomeAssistantError):
        asyncio.run(BHyveRainDelayNumber(coord).async_set_native_value(2))
    assert calls == [("set", 120), ("refresh",)]


def test_select_routes_through_helper():
    coord, calls = _coordinator(ok=False)
    with pytest.raises(HomeAssistantError):
        asyncio.run(BHyveRainDelaySelect(coord).async_select_option("Off"))
    assert calls == [("clear",), ("refresh",)]


@pytest.mark.parametrize("method, on", [("async_turn_on", True), ("async_turn_off", False)])
def test_automatic_watering_switch_raises_when_unconfirmed(method, on):
    coord, calls = _coordinator(ok=False)
    entity = BHyveAutomaticWateringSwitch(coord)
    with pytest.raises(HomeAssistantError, match="XD: automatic watering write not confirmed"):
        asyncio.run(getattr(entity, method)())
    assert calls == [("mode", on), ("refresh",)]


@pytest.mark.parametrize("method, on", [("async_turn_on", True), ("async_turn_off", False)])
def test_program_switch_raises_when_unconfirmed(method, on):
    coord, calls = _coordinator(ok=False)
    entity = BHyveProgramEnableSwitch(coord, "B")
    with pytest.raises(HomeAssistantError, match="XD: Program B write not confirmed"):
        asyncio.run(getattr(entity, method)())
    assert calls == [("program", entity._slot, on), ("refresh",)]


def test_confirmed_switch_write_is_silent():
    coord, calls = _coordinator(ok=True)
    asyncio.run(BHyveAutomaticWateringSwitch(coord).async_turn_on())
    assert calls == [("mode", True), ("refresh",)]
