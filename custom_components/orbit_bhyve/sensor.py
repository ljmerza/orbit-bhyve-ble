"""Sensor platform — battery percent, battery voltage, and BLE signal strength.

Both sensors are populated from the device's BLE info-ack response on
every connection: voltage in mV is read directly from payload bytes 4-5
(little-endian uint16), and percent is derived from it via a linear
discharge approximation (`devices.base._mv_to_pct`).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

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
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices.base import (
    PROGRAM_SLOTS,
    SLOT_LETTERS,
    UI_SLOTS,
    ProgramSummary,
    accumulate_gallons,
)
from .devices.protobuf import BHyveProtobufDevice

_WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _fmt_days(summary: ProgramSummary) -> str:
    """Human day-mode summary for a program's extra attributes."""
    if summary.day_mode == "weekdays":
        mask = summary.weekday_mask or 0
        if mask == 0x7F:
            return "Every day"
        return ", ".join(_WEEKDAY_NAMES[b] for b in range(7) if mask & (1 << b)) or "(none)"
    if summary.day_mode == "interval":
        base = f"Every {summary.interval_days} days"
        if summary.interval_anchor:
            base += f" from {summary.interval_anchor[:10]}"
        return base
    return {"odd": "Odd days", "even": "Even days", "once": "Once"}.get(
        summary.day_mode, str(summary.day_mode)
    )


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
        # Rain delay + watering programs are protobuf-family (HT34A/HT25G2)
        # capabilities.
        if isinstance(device, BHyveProtobufDevice):
            entities.append(BHyveRainDelayEndsSensor(coord))
            entities.append(BHyveNextRunSensor(coord))
            for letter in UI_SLOTS:
                entities.append(BHyveProgramSummarySensor(coord, letter))
        # Flow-rate gauge only for models with a flow sensor (Gen2, not the XD).
        if getattr(device, "has_flow", False):
            entities.append(BHyveFlowRateSensor(coord))
            entities.append(BHyveWaterUsedSensor(coord))
            entities.append(BHyveDeviceFlowRateSensor(coord))
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
        # battery_pct is a chemistry-aware property derived from battery_mv, so
        # it already covers the mv-only case; fall back to the restored value only
        # when no live voltage has been read yet.
        pct = self.coordinator.device.battery_pct
        return pct if pct is not None else self._restored_value


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
    so long-term avg/min/max statistics are honest. For cumulative gallons see
    BHyveWaterUsedSensor below, which integrates this rate over wall time — the
    raw counter can't be a passive meter here. See docs/ble_protocol.md.
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


class BHyveWaterUsedSensor(_RestoreLastValueSensor):
    """Cumulative water used (gallons), integrated from the flow gauge (Gen2).

    The raw #59.#3 counter only advances while a #57 subscription is live — a
    few-second sampling window per watering poll — so exposing it directly
    would undercount most of the run (see BHyveFlowRateSensor). Instead this
    integrates the slope-derived `flow_gpm` over wall time between coordinator
    updates: a left-rectangle sum (each interval booked at the rate live at
    its start), the same rule as HA's Integration helper with `method: left`,
    which this sensor replaces.

    Gating and bounds:
    - an interval accrues only if the PREVIOUS update was watering, so a
      nonzero gpm left by an idle-time Check-flow press (a leak spot-check)
      never silently accumulates for a whole idle period;
    - each interval is capped at max(120 s, 2x the watering cadence) so a
      restart/outage gap can't book phantom gallons;
    - the run's final stretch is booked at the last live rate: the idle #16
      forces flow_gpm to 0.0, which lands here one update later.

    TOTAL_INCREASING + RestoreSensor keeps the total monotonic across
    restarts, so HA's long-term statistics / Water dashboard see clean deltas.
    Counter resets on the device are irrelevant — the raw counter is never
    consumed here.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:water"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_water_used"
        self._attr_name = "Water used"
        self._total = 0.0
        self._last_update: float | None = None  # time.monotonic()
        self._prev_gpm: float | None = None
        self._prev_watering = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._restored_value is not None:
            try:
                self._total = float(self._restored_value)
            except (TypeError, ValueError):
                self._total = 0.0

    @callback
    def _handle_coordinator_update(self) -> None:
        now = time.monotonic()
        if self._last_update is not None and self._prev_watering:
            cap = max(120.0, 2.0 * self.coordinator.poll_watering)
            self._total = accumulate_gallons(
                self._total, self._prev_gpm, now - self._last_update, cap
            )
        state = self.coordinator.data or self.coordinator.device.state
        self._last_update = now
        self._prev_gpm = state.flow_gpm
        self._prev_watering = bool(state.is_watering)
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        return round(self._total, 3)


class BHyveDeviceFlowRateSensor(_BHyveDeviceSensorBase):
    """The device's own #59.#4 currentFlowRateGpm float (Gen2).

    Named in the vendor schema but never yet observed populated on fw0111 —
    disabled by default purely to gather field confirmation. If reports show
    it live, a later release can prefer it over the slope-derived Flow rate.
    """

    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_native_unit_of_measurement = UnitOfVolumeFlowRate.GALLONS_PER_MINUTE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:water"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_flow_rate_device"
        self._attr_name = "Flow rate (device)"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data or self.coordinator.device.state
        return state.flow_gpm_device


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


class BHyveNextRunSensor(_BHyveDeviceSensorBase):
    """When the next scheduled program run starts (#16.#10), None when none is
    armed. The `programs` attribute lists which slot(s) start then (#16.#9)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_next_run"
        self._attr_name = "Next run"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.data or self.coordinator.device.state
        return state.next_start_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data or self.coordinator.device.state
        flags = state.next_start_flags or 0
        slots = [SLOT_LETTERS[b + 1] for b in range(6) if flags & (1 << b) and (b + 1) in SLOT_LETTERS]
        return {"programs": slots}


class BHyveProgramSummarySensor(_BHyveDeviceSensorBase):
    """Read-only summary of one program slot (A–D). State is enabled/disabled/empty;
    the schedule detail (days, start times, per-zone minutes, name) is in the
    attributes. Populated from the idle-poll #10 sync read."""

    _attr_icon = "mdi:calendar-text"

    def __init__(self, coordinator: BHyveDeviceCoordinator, letter: str):
        super().__init__(coordinator)
        device = coordinator.device
        self._letter = letter
        self._slot = PROGRAM_SLOTS[letter]
        self._attr_unique_id = f"{device.unique_id}_program_{letter.lower()}"
        self._attr_name = f"Program {letter}"

    @property
    def _summary(self) -> ProgramSummary | None:
        state = self.coordinator.data or self.coordinator.device.state
        return state.programs.get(self._slot)

    @property
    def native_value(self) -> str:
        summary = self._summary
        if summary is None or summary.empty:
            return "empty"
        return "enabled" if summary.enabled else "disabled"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        summary = self._summary
        if summary is None or summary.empty:
            return {"empty": True}
        return {
            "empty": False,
            "name": summary.name,
            "days": _fmt_days(summary),
            "start_times": [f"{m // 60:02d}:{m % 60:02d}" for m in summary.start_mins],
            "zones": [
                {"zone": sid + 1, "minutes": round(sec / 60, 2)} for sid, sec in summary.zones
            ],
            "budget": summary.budget,
        }
