"""Select platform — rain-delay presets and battery chemistry.

The rain-delay select is a single SelectEntity per protobuf-family device
(HT34A/HT25G2) that exposes the rain delay as a coarse dropdown of hour/day
presets. Each pick is exactly ONE BLE write, which is the point: unlike a number
stepper/slider (one write per click/drag) there is no ambiguity about when the
value is sent, and no queue of unintended intermediate writes fighting the
coordinator poll for the device's single BLE session.

The finer-grained "Rain delay" NumberEntity (hours, arbitrary values) stays
alongside this for the rare odd value and for backwards compatibility with
existing dashboards/automations. Both drive the same device.set_rain_delay.

The battery-chemistry select is a CONFIG entity on every BLE device: the AA cell
chemistry the user installed determines the voltage->percent discharge curve, and
it can't be auto-detected from a stateless voltage reading (a 2550 mV cell could
be alkaline at 25% or Ni-MH at 80%). It persists via restore-state (no device
traffic) and re-derives the battery-percent sensor on change.
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices.base import BATTERY_CHEMISTRIES
from .devices.protobuf import BHyveProtobufDevice

_LOGGER = logging.getLogger(__name__)

# Internal chemistry key -> user-facing dropdown label. Keys match
# devices.base.BATTERY_CHEMISTRIES; labels are what the user picks.
BATTERY_CHEMISTRY_LABELS: dict[str, str] = {
    "alkaline": "Alkaline",
    "nimh": "Ni-MH rechargeable",
    "lithium_primary": "Lithium (primary)",
    "lithium_regulated": "Lithium (regulated 1.5V)",
}
_LABEL_TO_CHEMISTRY: dict[str, str] = {v: k for k, v in BATTERY_CHEMISTRY_LABELS.items()}
# Every gauge chemistry must have a user-facing label (and vice versa) or a valid
# selection would have no curve / a curve would be unreachable.
assert set(BATTERY_CHEMISTRY_LABELS) == set(BATTERY_CHEMISTRIES)

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
        # Battery chemistry applies to every BLE device (mesh + protobuf); hubs /
        # key-less records have no battery to gauge.
        if coord.device.connection is not None:
            entities.append(BHyveBatteryChemistrySelect(coord))
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


class BHyveBatteryChemistrySelect(
    CoordinatorEntity[BHyveDeviceCoordinator], SelectEntity, RestoreEntity
):
    """Which AA cell chemistry the user installed, driving the battery-percent
    fuel gauge. Pure HA-side config: no BLE traffic — it just selects the
    voltage->percent curve applied to the live battery_mv. Persisted across
    restarts via restore-state (the device can't report its own chemistry)."""

    _attr_has_entity_name = True
    _attr_name = "Battery chemistry"
    _attr_icon = "mdi:battery-sync"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(BATTERY_CHEMISTRY_LABELS.values())

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_battery_chemistry"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore the user's last pick onto the device so the battery gauge uses
        # it from the first reading, before any user interaction. A stored label
        # that no longer maps (renamed option) falls through to the default.
        last = await self.async_get_last_state()
        if last is not None and last.state in _LABEL_TO_CHEMISTRY:
            self.coordinator.device.battery_chemistry = _LABEL_TO_CHEMISTRY[last.state]

    @property
    def current_option(self) -> str | None:
        return BATTERY_CHEMISTRY_LABELS.get(self.coordinator.device.battery_chemistry)

    async def async_select_option(self, option: str) -> None:
        self.coordinator.device.battery_chemistry = _LABEL_TO_CHEMISTRY[option]
        # No device round-trip; recompute the battery-percent sensor (and this
        # entity) from the already-known voltage right away.
        self.coordinator.async_update_listeners()
