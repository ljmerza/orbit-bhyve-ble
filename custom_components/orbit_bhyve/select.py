"""Select platform — rain-delay presets.

A single SelectEntity per protobuf-family device (HT34A/HT25G2) that exposes
the rain delay as a coarse dropdown of hour/day presets. Each pick is exactly
ONE BLE write, which is the point: unlike a number stepper/slider (one write per
click/drag) there is no ambiguity about when the value is sent, and no queue of
unintended intermediate writes fighting the coordinator poll for the device's
single BLE session.

The finer-grained "Rain delay" NumberEntity (hours, arbitrary values) stays
alongside this for the rare odd value and for backwards compatibility with
existing dashboards/automations. Both drive the same device.set_rain_delay.
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices.protobuf import BHyveProtobufDevice

_LOGGER = logging.getLogger(__name__)

_OFF = "Off"

# Ordered preset label -> rain-delay minutes. Mirrors the B-Hyve app's coarse
# choices: short hour holds plus multi-day delays, capped at the app's 7-day
# ceiling. "Off" clears the delay.
RAIN_DELAY_PRESETS: dict[str, int] = {
    _OFF: 0,
    "1 hour": 60,
    "2 hours": 120,
    "4 hours": 240,
    "8 hours": 480,
    "12 hours": 720,
    "16 hours": 960,
    "1 day": 1440,
    "2 days": 2880,
    "3 days": 4320,
    "5 days": 7200,
    "7 days": 10080,
}
# Reverse map for reflecting the device's live state back to a preset label.
_MINUTES_TO_LABEL: dict[int, str] = {v: k for k, v in RAIN_DELAY_PRESETS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []
    for coord in runtime.coordinators.values():
        # Rain delay is a protobuf-family (HT34A/HT25G2) capability.
        if isinstance(coord.device, BHyveProtobufDevice):
            entities.append(BHyveRainDelaySelect(coord))
    async_add_entities(entities)


class BHyveRainDelaySelect(CoordinatorEntity[BHyveDeviceCoordinator], SelectEntity):
    """Rain delay as a preset dropdown. Reflects the device's live #16.#13 state,
    so it is not restored — the device is truth. One pick = one BLE write."""

    _attr_has_entity_name = True
    _attr_name = "Rain delay preset"
    _attr_icon = "mdi:weather-rainy"
    _attr_options = list(RAIN_DELAY_PRESETS)

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_rain_delay_preset"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }

    @property
    def current_option(self) -> str | None:
        state = self.coordinator.data or self.coordinator.device.state
        minutes = state.rain_delay_minutes or 0
        if not minutes:
            return _OFF
        # An active delay set to a non-preset value (e.g. via the app or the
        # exact-hours number) has no matching option — show nothing selected
        # rather than mislabel it; the "Rain delay" number/"ends" sensor carry
        # the exact value.
        return _MINUTES_TO_LABEL.get(minutes)

    async def async_select_option(self, option: str) -> None:
        minutes = RAIN_DELAY_PRESETS[option]
        device = self.coordinator.device
        if minutes <= 0:
            await device.clear_rain_delay()
        else:
            await device.set_rain_delay(minutes)
        await self.coordinator.async_request_refresh()
