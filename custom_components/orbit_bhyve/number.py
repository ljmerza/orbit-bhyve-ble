"""Number platform — per-device watering duration in minutes.

One NumberEntity per HT25 sprinkler. The user sets a duration in minutes;
valve.async_open_valve reads coordinator.preferred_duration_sec when the
valve is opened. State is restored across HA restarts.
"""
from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEFAULT_DURATION, DEFAULT_DURATION, DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices import BHyveHT25Device, BHyveHubDevice
from .devices.protobuf import BHyveProtobufDevice

_LOGGER = logging.getLogger(__name__)

MIN_MINUTES = 1
MAX_MINUTES = 1440  # 24h

# Gen1 flow calibration bounds. Four fw0085 timers measured 113-118 counts/L
# against the vendor app; the range is wide enough for a differently-plumbed
# install without admitting a value that could only be a typo.
MIN_COUNTS_PER_LITRE = 50
MAX_COUNTS_PER_LITRE = 500

MAX_RAIN_DELAY_HOURS = 168  # 7 days — matches the B-Hyve app's ceiling

# Mesh button run time (manual-preset-run {run-time-secs u16}), in minutes.
MIN_BUTTON_MINUTES = 1
MAX_BUTTON_MINUTES = 0xFFFF // 60  # 1092: the u16 seconds field's ceiling


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    default_duration_sec = entry.options.get(CONF_DEFAULT_DURATION, DEFAULT_DURATION)
    entities: list[NumberEntity] = []
    for coord in runtime.coordinators.values():
        if isinstance(coord.device, BHyveHubDevice):
            continue
        entities.append(BHyveDurationNumber(coord, default_duration_sec))
        # Protobuf-family (HT34A/HT25G2) rain delay.
        if isinstance(coord.device, BHyveProtobufDevice):
            entities.append(BHyveRainDelayNumber(coord))
        # Mesh (HT25) rain delay and button run time, written over the d7-47
        # mesh messages. A key-less record has no session to write over.
        if isinstance(coord.device, BHyveHT25Device) and coord.device.connection is not None:
            entities.append(BHyveRainDelayNumber(coord))
            entities.append(BHyveButtonRunTimeNumber(coord))
        # Flow calibration — Gen1 (HT25) devices with an inline flow sensor.
        # A key-less record has no BLE connection and therefore no flow sensors,
        # so there would be nothing to calibrate.
        if coord.device.has_flow_gen1 and coord.device.connection is not None:
            entities.append(BHyveFlowCalibrationNumber(coord))
    async_add_entities(entities)


class BHyveFlowCalibrationNumber(CoordinatorEntity[BHyveDeviceCoordinator], RestoreNumber):
    """Per-device flow calibration in sensor counts per litre.

    The HT25's flow meter reports raw tick counts; counts-per-litre converts
    them to volume. Varies between units, so it's per-device and user-tunable:
    run a known volume, compare Water used against reality, scale accordingly.
    """

    _attr_has_entity_name = True
    _attr_name = "Flow calibration"
    _attr_icon = "mdi:water-percent"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = MIN_COUNTS_PER_LITRE
    _attr_native_max_value = MAX_COUNTS_PER_LITRE
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "counts/L"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_flow_calibration"
        self._attr_native_value = float(device.flow_counts_per_litre)
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
        last = await self.async_get_last_number_data()
        if last is None or last.native_value is None:
            return
        value = max(MIN_COUNTS_PER_LITRE, min(MAX_COUNTS_PER_LITRE, int(last.native_value)))
        self._attr_native_value = float(value)
        self.coordinator.device.flow_counts_per_litre = value

    async def async_set_native_value(self, value: float) -> None:
        value = max(MIN_COUNTS_PER_LITRE, min(MAX_COUNTS_PER_LITRE, int(value)))
        self._attr_native_value = float(value)
        self.coordinator.device.flow_counts_per_litre = value
        self.async_write_ha_state()


class BHyveDurationNumber(CoordinatorEntity[BHyveDeviceCoordinator], RestoreNumber):
    _attr_has_entity_name = True
    _attr_name = "Watering duration"
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = MIN_MINUTES
    _attr_native_max_value = MAX_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: BHyveDeviceCoordinator, default_duration_sec: int):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_duration"
        initial_minutes = max(MIN_MINUTES, default_duration_sec // 60)
        self._attr_native_value = float(initial_minutes)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }
        # Seed coordinator immediately; async_added_to_hass may overwrite from
        # restored state, but until then valve.async_open_valve has a value.
        coordinator.preferred_duration_sec = initial_minutes * 60

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is None or last.native_value is None:
            return
        minutes = max(MIN_MINUTES, min(MAX_MINUTES, int(last.native_value)))
        self._attr_native_value = float(minutes)
        self.coordinator.preferred_duration_sec = minutes * 60

    async def async_set_native_value(self, value: float) -> None:
        minutes = max(MIN_MINUTES, min(MAX_MINUTES, int(value)))
        self._attr_native_value = float(minutes)
        self.coordinator.preferred_duration_sec = minutes * 60
        self.async_write_ha_state()


class BHyveRainDelayNumber(CoordinatorEntity[BHyveDeviceCoordinator], NumberEntity):
    """Rain delay in hours; 0 clears it. Reflects the device's live state
    (the #16.#13 echo), so it is not a RestoreNumber — the device is truth."""

    _attr_has_entity_name = True
    _attr_name = "Rain delay"
    _attr_icon = "mdi:weather-rainy"
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_RAIN_DELAY_HOURS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_rain_delay"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data or self.coordinator.device.state
        minutes = state.rain_delay_minutes
        if not minutes:
            return 0.0
        return round(minutes / 60, 1)

    async def async_set_native_value(self, value: float) -> None:
        hours = max(0.0, min(MAX_RAIN_DELAY_HOURS, value))
        await self.coordinator.async_apply_rain_delay(int(round(hours * 60)))


class BHyveButtonRunTimeNumber(CoordinatorEntity[BHyveDeviceCoordinator], NumberEntity):
    """Mesh HT25: how long a press of the timer's physical button waters, in
    minutes (sent as seconds in manual-preset-run). Reflects the device, so it
    is not a RestoreNumber: unknown until a Sync reads it or a write is acked.
    The write is decoded from the app's mesh spec, not HW-verified."""

    _attr_has_entity_name = True
    _attr_name = "Button run time"
    _attr_translation_key = "button_run_time"
    _attr_icon = "mdi:gesture-tap-button"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = MIN_BUTTON_MINUTES
    _attr_native_max_value = MAX_BUTTON_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_button_run_time"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }

    @property
    def native_value(self) -> float | None:
        secs = self.coordinator.device.manual_preset_sec
        if secs is None:
            return None
        return round(secs / 60, 1)

    async def async_set_native_value(self, value: float) -> None:
        minutes = max(MIN_BUTTON_MINUTES, min(MAX_BUTTON_MINUTES, int(value)))
        ok = await self.coordinator.device.set_manual_preset_runtime(minutes * 60)
        await self.coordinator.async_confirm_write(ok, "button run time")
