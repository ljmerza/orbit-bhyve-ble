"""Sensor platform — battery percent, battery voltage, and BLE signal strength.

Both sensors are populated from the device's BLE info-ack response on
every connection: voltage in mV is read directly from payload bytes 4-5
(little-endian uint16), and percent is derived from it via a linear
discharge approximation (`devices.base._mv_to_pct`).
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BHyveDeviceCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for coord in runtime.coordinators.values():
        device = coord.device
        if device.connection is None:
            # Hubs / key-less records have no BLE battery or signal to report.
            continue
        # Battery comes from the device's info-ack at runtime, so create the
        # sensors unconditionally rather than gating on the cloud-cached value
        # (which is None for BT-only devices the cloud never primed).
        entities.append(BHyveBatterySensor(coord))
        entities.append(BHyveBatteryVoltageSensor(coord))
        entities.append(BHyveRssiSensor(coord))
    async_add_entities(entities)


class _BHyveDeviceSensorBase(CoordinatorEntity[BHyveDeviceCoordinator], SensorEntity):
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


class BHyveBatterySensor(_BHyveDeviceSensorBase):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_battery"
        self._attr_name = "Battery"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.device.battery_pct


class BHyveBatteryVoltageSensor(_BHyveDeviceSensorBase):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_battery_mv"
        self._attr_name = "Battery voltage"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.device.battery_mv


class BHyveRssiSensor(_BHyveDeviceSensorBase):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_rssi"
        self._attr_name = "Signal strength"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.device.rssi
