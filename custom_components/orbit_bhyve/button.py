"""Button platform — per-device sync button.

One ButtonEntity per non-hub device. Pressing it forces a fresh BLE
connect + 8-step init, then requests a coordinator refresh — which for
protobuf devices issues a solicited #15 status read (real run-state,
battery, rain-delay, seconds-remaining), the reliable way to pull live
state on demand (e.g. to see a program run the poll hasn't caught yet).
Mesh devices fall back to the connect-time push (battery).

Equivalent to a manual, on-demand version of the periodic status poll,
attached to the device card so a non-technical user can refresh without
going through Developer Tools.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices import BHyveHubDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []
    for coord in runtime.coordinators.values():
        if isinstance(coord.device, BHyveHubDevice):
            continue
        if coord.device.connection is None:
            continue
        entities.append(BHyveSyncButton(coord))
        # On-demand flow spot-check for models with a flow sensor (Gen2, not XD):
        # sample flow NOW (automation hook, or a leak check while idle) rather
        # than waiting for the watering poll's passive update.
        if getattr(coord.device, "has_flow", False):
            entities.append(BHyveCheckFlowButton(coord))
    async_add_entities(entities)


class BHyveSyncButton(CoordinatorEntity[BHyveDeviceCoordinator], ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Sync"
    _attr_icon = "mdi:sync"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_sync"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }

    async def async_press(self) -> None:
        device = self.coordinator.device
        if device.connection is None:
            return
        _LOGGER.info("%s: sync requested via button", device.mac)
        await self.coordinator.async_request_refresh()


class BHyveCheckFlowButton(CoordinatorEntity[BHyveDeviceCoordinator], ButtonEntity):
    """On-demand flow spot-check (Gen2). Samples the flow sensor now and updates
    the Flow rate gauge — for automations ('is water actually moving?') and for a
    leak check while idle (nonzero flow with the valve closed = a stuck valve)."""

    _attr_has_entity_name = True
    _attr_name = "Check flow"
    _attr_icon = "mdi:water-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_check_flow"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }

    async def async_press(self) -> None:
        device = self.coordinator.device
        if device.connection is None:
            return
        _LOGGER.info("%s: flow check requested via button", device.mac)
        await device.read_flow()
        # Push the freshly-sampled flow_gpm to entities without a full #15 poll.
        self.coordinator.async_set_updated_data(device.state)
