"""Valve platform — one entity per physical station.

Valves are the HA-correct semantic for irrigation: voice assistants,
Lovelace cards, and automations all behave better with valve.deck_sprinkler
than switch.deck_sprinkler_zone.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEFAULT_DURATION, DEFAULT_DURATION, DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices import BHyveHubDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    default_duration = entry.options.get(CONF_DEFAULT_DURATION, DEFAULT_DURATION)
    entities: list[BHyveZoneValve] = []
    for coord in runtime.coordinators.values():
        # Hubs are filtered upstream in cloud.discover(); guard is defensive.
        if isinstance(coord.device, BHyveHubDevice):
            continue
        for station in range(1, max(coord.device.stations, 1) + 1):
            entities.append(BHyveZoneValve(coord, station, default_duration))
    async_add_entities(entities)


class BHyveZoneValve(CoordinatorEntity[BHyveDeviceCoordinator], ValveEntity):
    _attr_has_entity_name = True
    _attr_device_class = ValveDeviceClass.WATER
    _attr_supported_features = (
        ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    )
    _attr_reports_position = False

    def __init__(self, coordinator: BHyveDeviceCoordinator, station: int, default_duration: int):
        super().__init__(coordinator)
        self._station = station
        self._default_duration = default_duration
        device = coordinator.device

        suffix = f"_zone_{station}" if device.stations > 1 else ""
        self._attr_unique_id = f"{device.unique_id}{suffix or '_zone'}"
        self._attr_name = f"Zone {station}" if device.stations > 1 else "Zone"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }

    @property
    def is_closed(self) -> bool:
        state = self.coordinator.data or self.coordinator.device.state
        return not bool(state.is_watering and (state.active_zone in (None, self._station)))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data or self.coordinator.device.state
        return {
            "station": self._station,
            "seconds_remaining": state.seconds_remaining,
            "rain_delay_minutes": state.rain_delay_minutes,
            "rain_delay_ends": state.rain_delay_ends,
            "last_command": state.last_command_label,
            "last_command_at": state.last_command_at,
            "notifications_last_cmd": state.notifications_last_cmd,
        }

    async def async_open_valve(self, **kwargs: Any) -> None:
        duration = int(
            kwargs.get("duration")
            or self.coordinator.preferred_duration_sec
            or self._default_duration
        )
        if await self.coordinator.device.start_watering(self._station, duration):
            await self.coordinator.async_request_refresh()

    async def async_close_valve(self, **kwargs: Any) -> None:
        if await self.coordinator.device.stop_watering(self._station):
            await self.coordinator.async_request_refresh()
