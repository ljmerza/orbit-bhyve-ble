"""Switch platform — automatic watering + per-program enable (protobuf family).

Two switch types, both protobuf-family (HT34A / HT25G2) capabilities:

- **Automatic watering** — the device-global controller mode (#14 timerMode):
  on = autoMode (scheduled programs run), off = offMode (all automatic watering
  disabled). Reads the live #16.#2.#1 mode from the status poll.
- **Program A–D enable** — each stored program's enable bit in the #20
  activeProgramFlags bitmask. On/off drives set_program_enabled; the switch is
  only available once a program is stored in that slot (an empty slot has nothing
  to enable). Program state comes from the idle-poll #10 sync read.

One mesh-family switch alongside them:

- **Flow measurement** (HT25, config category) — per-device opt-in for Gen1
  flow measurement, default off. Pure HA-side config: it sends nothing to the
  device, it only decides whether the next run subscribes to flow samples.
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices.base import PROGRAM_SLOTS, UI_SLOTS
from .devices.protobuf import BHyveProtobufDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []
    for coord in runtime.coordinators.values():
        # Gen1 (HT25) flow measurement opt-in. Gated on the capability flag
        # rather than an isinstance check, and on a live connection — a key-less
        # record has no BLE session to measure over.
        if coord.device.has_flow_gen1 and coord.device.connection is not None:
            entities.append(BHyveGen1FlowSwitch(coord))
        if not isinstance(coord.device, BHyveProtobufDevice):
            continue
        entities.append(BHyveAutomaticWateringSwitch(coord))
        for letter in UI_SLOTS:
            entities.append(BHyveProgramEnableSwitch(coord, letter))
    async_add_entities(entities)


class _BHyveSwitchBase(CoordinatorEntity[BHyveDeviceCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }

    @property
    def _state(self):
        return self.coordinator.data or self.coordinator.device.state


class BHyveGen1FlowSwitch(_BHyveSwitchBase, RestoreEntity):
    """Opt-in for Gen1 (HT25) flow measurement.

    Off by default, deliberately. Measuring holds the timer's single BLE session
    open for the whole program and re-subscribes every 240 s, because the device
    stops sampling ~300 s after each subscribe regardless of the sample count
    asked for. That is more radio-on time than the mesh status poll this
    integration already defaults off, and while the session is held the B-hyve
    app and any status poll are locked out of the device.

    Pure HA-side config: flipping this sends nothing over BLE, it only decides
    whether the next run subscribes. Persisted via restore-state, since the
    device has no notion of it.
    """

    _attr_name = "Flow measurement"
    _attr_icon = "mdi:water-check"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_flow_measurement"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self.coordinator.device.flow_gen1_enabled = last.state == "on"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.device.flow_gen1_enabled)

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.device.flow_gen1_enabled = True
        self.async_write_ha_state()
        # Re-evaluate the flow sensors' availability now rather than waiting for
        # the next poll.
        self.coordinator.async_update_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        device = self.coordinator.device
        device.flow_gen1_enabled = False
        # Off mid-run has to stop measuring now, not at the end of the program:
        # the user has withdrawn consent for the session cost, so the hold and
        # the renewal task go with it.
        device.on_watering_finished()
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()


class BHyveAutomaticWateringSwitch(_BHyveSwitchBase):
    """Device-global automatic watering (autoMode vs offMode)."""

    _attr_name = "Automatic watering"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_automatic_watering"

    @property
    def is_on(self) -> bool | None:
        mode = self._state.controller_mode
        if mode is None:
            return None
        return mode != 0  # 1=auto (on), 2=manual also counts as "not off"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.device.set_controller_mode(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.device.set_controller_mode(False)
        await self.coordinator.async_request_refresh()


class BHyveProgramEnableSwitch(_BHyveSwitchBase):
    """Enable/disable a single stored program (A–D) via the #20 bitmask."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-check"

    def __init__(self, coordinator: BHyveDeviceCoordinator, letter: str):
        super().__init__(coordinator)
        self._letter = letter
        self._slot = PROGRAM_SLOTS[letter]
        self._attr_unique_id = f"{coordinator.device.unique_id}_program_{letter.lower()}_enable"
        self._attr_name = f"Program {letter}"

    @property
    def _summary(self):
        return self._state.programs.get(self._slot)

    @property
    def available(self) -> bool:
        # Only meaningful once a program is stored in this slot; an empty slot has
        # nothing to enable. Programs populate from the idle-poll #10 sync read.
        summary = self._summary
        return super().available and summary is not None and not summary.empty

    @property
    def is_on(self) -> bool | None:
        summary = self._summary
        if summary is None or summary.empty:
            return None
        return bool(summary.enabled)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.device.set_program_enabled(self._slot, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.device.set_program_enabled(self._slot, False)
        await self.coordinator.async_request_refresh()
