"""Binary sensor platform — BLE connectivity, watering state and fault report.

One Connected (diagnostic) and one Watering binary sensor per non-hub device.
Both read DeviceState, which the coordinator refreshes on each poll and which
the device also updates out of band from its notification acks. Protobuf-family
devices additionally expose the #16.#7 faultStatus block as a Problem sensor
(plus Leak / No-flow sensors on flow-capable Gen2 hardware).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices import BHyveHubDevice
from .devices.base import FaultStatus
from .devices.protobuf import BHyveProtobufDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []
    for coord in runtime.coordinators.values():
        if isinstance(coord.device, BHyveHubDevice):
            continue
        if coord.device.connection is None:
            continue
        entities.append(BHyveConnectedBinarySensor(coord))
        entities.append(BHyveWateringBinarySensor(coord))
        if isinstance(coord.device, BHyveProtobufDevice):
            entities.append(BHyveProblemBinarySensor(coord))
            # The flow faults (#7.#5-#8) need the inline flow sensor, which only
            # the Gen2 (has_flow) hardware carries — the XD never populates them.
            # The Problem sensor above still surfaces the XD's pump/battery/
            # voltage-boost faults.
            if getattr(coord.device, "has_flow", False):
                entities.append(BHyveLeakBinarySensor(coord))
                entities.append(BHyveNoFlowBinarySensor(coord))
    async_add_entities(entities)


class _BHyveBinarySensorBase(CoordinatorEntity[BHyveDeviceCoordinator], BinarySensorEntity):
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


class BHyveConnectedBinarySensor(_BHyveBinarySensorBase):
    """Connectivity: True when the last status poll REACHED the device. Under the
    ephemeral connect-on-demand model the BLE link is torn down between polls, so
    this reflects poll-reachability (coherent with the Consecutive timeouts sensor)
    rather than a live socket."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_connected"
        self._attr_name = "Connected"

    @property
    def is_on(self) -> bool:
        return self.coordinator.device.state.is_connected


class BHyveWateringBinarySensor(_BHyveBinarySensorBase):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:sprinkler-variant"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_watering"
        self._attr_name = "Watering"

    @property
    def is_on(self) -> bool:
        return self.coordinator.device.state.is_watering


class _BHyveFaultSensorBase(_BHyveBinarySensorBase):
    """Base for sensors reading the #16.#7 fault report. Unknown (None) until
    the first decoded status carries the block; a status without the block
    keeps the last-known report (see FaultStatus in devices/base.py)."""

    @property
    def _faults(self) -> FaultStatus | None:
        return self.coordinator.device.state.faults


class BHyveProblemBinarySensor(_BHyveFaultSensorBase):
    """Aggregate device fault: on when the device reports ANY fault — pump,
    battery, voltage-boost, flow anomalies or a per-station fault. The
    individual flags ride along as attributes for automations; the
    leak-specific flag also gets its own alerting entity on Gen2."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_problem"
        self._attr_name = "Problem"

    @property
    def is_on(self) -> bool | None:
        faults = self._faults
        return None if faults is None else faults.any_fault

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        faults = self._faults
        if faults is None:
            return None
        return {
            "pump_fault": faults.pump_fault,
            "battery_fault": faults.battery_fault,
            "voltage_boost_fail": faults.voltage_boost_fail,
            "valve_off_flow_detected": faults.valve_off_flow,
            "valve_on_no_flow_detected": faults.valve_on_no_flow,
            "valve_low_flow_detected": faults.valve_low_flow,
            "valve_high_flow_detected": faults.valve_high_flow,
            "station_faults": list(faults.station_faults),
            "accessory_fault_flags": faults.accessory_fault_flags,
        }


class BHyveLeakBinarySensor(_BHyveFaultSensorBase):
    """Leak: valveOffFlowDetected (#7.#5) — the inline flow sensor saw water
    moving while the valve was commanded CLOSED (seeping/stuck valve or a
    downstream break). MOISTURE class so HA dashboards/alerts treat it as a
    leak detector. Primary alerting entity, so not diagnostic-category."""

    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_leak_detected"
        self._attr_name = "Leak detected"

    @property
    def is_on(self) -> bool | None:
        faults = self._faults
        return None if faults is None else faults.valve_off_flow


class BHyveNoFlowBinarySensor(_BHyveFaultSensorBase):
    """No flow during a run: valveOnNoFlowDetected (#7.#6) — valve open but the
    flow sensor sees nothing (supply tap closed, blocked line, broken head).
    Disabled by default until the signal is field-confirmed — deliberately
    inducible by starting a run with the upstream faucet closed."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_no_flow"
        self._attr_name = "No flow"

    @property
    def is_on(self) -> bool | None:
        faults = self._faults
        return None if faults is None else faults.valve_on_no_flow
