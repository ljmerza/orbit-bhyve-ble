"""Sensor platform — battery percent, battery voltage, and BLE signal strength.

Both sensors are populated from the device's BLE info-ack response on
every connection: voltage in mV is read directly from payload bytes 4-5
(little-endian uint16), and percent is derived from it via a linear
discharge approximation (`devices.base._mv_to_pct`).
"""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices.base import _mv_to_pct
from .devices.protobuf import BHyveProtobufDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for coord in runtime.coordinators.values():
        device = coord.device
        # Every BLE device reports battery over RX; create both sensors so the
        # percent entity exists regardless of whether the cloud snapshot
        # happened to include a pct (the XD reports mv-only). Hubs / key-less
        # records have no BLE connection and no battery/signal to read.
        if device.connection is None:
            continue
        entities.append(BHyveBatterySensor(coord))
        entities.append(BHyveBatteryVoltageSensor(coord))
        entities.append(BHyveRssiSensor(coord))
        entities.append(BHyveLastSuccessfulPollSensor(coord))
        entities.append(BHyveConsecutiveTimeoutsSensor(coord))
        entities.append(BHyveWateringEndsSensor(coord))
        # Rain delay is a protobuf-family (HT34A/HT25G2) capability.
        if isinstance(device, BHyveProtobufDevice):
            entities.append(BHyveRainDelayEndsSensor(coord))
        # Flow-rate gauge only for models with a flow sensor (Gen2, not the XD).
        if getattr(device, "has_flow", False):
            entities.append(BHyveFlowRateSensor(coord))
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


class _RestoreLastValueSensor(_BHyveDeviceSensorBase, RestoreSensor):
    """Restores its last LIVE reading across a restart, so it shows the last real
    value instead of blipping to `unavailable` until the first poll. Right for
    slow-moving battery state (last-known is the sensible default) — and, since we
    no longer seed battery from the cloud, this is what fills the startup gap. A
    live device value always wins; `self._restored_value` is only the fallback."""

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            self._restored_value = last.native_value


class BHyveBatterySensor(_RestoreLastValueSensor):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_battery"
        self._attr_name = "Battery"

    @property
    def native_value(self) -> float | None:
        device = self.coordinator.device
        if device.battery_pct is not None:
            return device.battery_pct
        if device.battery_mv is not None:
            return _mv_to_pct(device.battery_mv)
        return self._restored_value


class BHyveBatteryVoltageSensor(_RestoreLastValueSensor):
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
    def native_value(self) -> float | None:
        mv = self.coordinator.device.battery_mv
        return mv if mv is not None else self._restored_value


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


class BHyveFlowRateSensor(_BHyveDeviceSensorBase):
    """Instantaneous flow rate (gpm) from the last flow spot-check (Gen2).

    Deliberately NOT a cumulative water meter: #59.#3's counter only advances
    while a #57 subscription is live, so HA never sees the whole run — a
    cumulative total would badly undercount. Instead `read_flow` samples the
    counter's slope over a few seconds and stores an instantaneous gpm here.
    Updated automatically on the watering poll (live during a run) and on demand
    (Check-flow button / automation).

    `state_class = MEASUREMENT`: each reading is a real ~4 s slope of actual flow,
    so long-term avg/min/max statistics are honest. For cumulative gallons, add
    HA's built-in Riemann-sum Integration helper on this entity (see the README)
    — that integrates the rate into a proper volume total; the raw counter can't
    be a passive meter here. See docs/ble_protocol.md.
    """

    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_native_unit_of_measurement = UnitOfVolumeFlowRate.GALLONS_PER_MINUTE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_flow_rate"
        self._attr_name = "Flow rate"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data or self.coordinator.device.state
        return state.flow_gpm


class BHyveRainDelayEndsSensor(_BHyveDeviceSensorBase):
    """Timestamp when the active rain delay expires; None when off."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:weather-rainy"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_rain_delay_ends"
        self._attr_name = "Rain delay ends"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.data or self.coordinator.device.state
        return state.rain_delay_ends


class BHyveLastSuccessfulPollSensor(_BHyveDeviceSensorBase):
    """Timestamp of the last successful BLE status poll (#15{})."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_last_successful_poll"
        self._attr_name = "Last successful poll"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.data or self.coordinator.device.state
        return state.last_successful_poll


class BHyveConsecutiveTimeoutsSensor(_BHyveDeviceSensorBase):
    """Number of consecutive failed BLE status polls."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_consecutive_timeouts"
        self._attr_name = "Consecutive timeouts"

    @property
    def native_value(self) -> int:
        state = self.coordinator.data or self.coordinator.device.state
        return state.consecutive_timeouts


class BHyveWateringEndsSensor(_BHyveDeviceSensorBase):
    """When the active run is expected to auto-close (state.expected_off_at).

    A single wall-clock timestamp (HA renders it as a live relative countdown),
    not a per-second integer — so it doesn't churn the recorder. Reads `unknown`
    when the valve is idle. The coordinator arms expected_off_at on start,
    re-anchors it via the drift-guard, and clears it on close."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:sprinkler"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_watering_ends"
        self._attr_name = "Watering ends"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.data or self.coordinator.device.state
        return state.expected_off_at
