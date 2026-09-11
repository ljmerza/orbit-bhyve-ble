"""Rain-delay entity write path (issue #52).

`device.set_rain_delay` / `clear_rain_delay` return False when the device does
not echo the new state. The number and preset select used to discard that and
the entity silently snapped back on the next refresh; `async_apply_rain_delay` now
raises HomeAssistantError so the user sees a service error. Same HA-guard and
direct-drive style as test_refresh_service.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from orbit_bhyve.number import async_apply_rain_delay  # noqa: E402


def _coordinator(ok: bool):
    calls: list = []

    async def set_rain_delay(minutes):
        calls.append(("set", minutes))
        return ok

    async def clear_rain_delay():
        calls.append(("clear",))
        return ok

    async def async_request_refresh():
        calls.append(("refresh",))

    device = SimpleNamespace(
        name="XD", set_rain_delay=set_rain_delay, clear_rain_delay=clear_rain_delay
    )
    return SimpleNamespace(device=device, async_request_refresh=async_request_refresh), calls


def test_unconfirmed_set_raises_after_refresh():
    coord, calls = _coordinator(ok=False)
    with pytest.raises(HomeAssistantError):
        asyncio.run(async_apply_rain_delay(coord, 120))
    # The refresh still runs so the entity shows the device's real state.
    assert calls == [("set", 120), ("refresh",)]


def test_unconfirmed_clear_raises_after_refresh():
    coord, calls = _coordinator(ok=False)
    with pytest.raises(HomeAssistantError):
        asyncio.run(async_apply_rain_delay(coord, 0))
    assert calls == [("clear",), ("refresh",)]


def test_confirmed_write_is_silent():
    coord, calls = _coordinator(ok=True)
    asyncio.run(async_apply_rain_delay(coord, 120))
    assert calls == [("set", 120), ("refresh",)]
