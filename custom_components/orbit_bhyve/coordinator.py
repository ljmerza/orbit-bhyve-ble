"""Per-device DataUpdateCoordinator.

Polls each device for state on an interval that varies with watering state
(faster while watering, slower when idle). All polling is BLE-only; the
cloud is never touched at runtime.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_POLL_IDLE, DEFAULT_POLL_WATERING
from .devices import BHyveBleDeviceBase, DeviceState

_LOGGER = logging.getLogger(__name__)

# Grace window past expected_off_at before we declare the cycle finished
# locally. Covers command-latency between our wall clock and the device's
# internal timer, plus coordinator-tick jitter.
EXPIRY_GRACE_SEC = 10


class BHyveDeviceCoordinator(DataUpdateCoordinator[DeviceState]):
    """One per BHyve device."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: BHyveBleDeviceBase,
        *,
        poll_idle_sec: int = DEFAULT_POLL_IDLE,
        poll_watering_sec: int = DEFAULT_POLL_WATERING,
    ):
        self.device = device
        self.poll_idle = poll_idle_sec
        self.poll_watering = poll_watering_sec
        # Set by the per-device NumberEntity (number.py) on first add and on
        # every UI change. Valve.async_open_valve reads this to decide the
        # watering duration. None until the NumberEntity registers.
        self.preferred_duration_sec: int | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=f"orbit_bhyve {device.name}",
            update_interval=timedelta(seconds=poll_idle_sec),
        )
        # A device that learns its state out of band (e.g. an HT25 START/STOP
        # ack notification) pokes us so the valve reflects it now and we switch
        # to the watering cadence — instead of waiting up to poll_idle (5 min).
        device.set_state_changed_callback(self._handle_device_state_change)

    def _handle_device_state_change(self) -> None:
        """Called from a device notification callback (event loop). Trigger an
        immediate poll, which applies the tick/auto-close logic and resets the
        cadence based on the freshly-armed timer."""
        self.hass.add_job(self.async_request_refresh)

    async def _async_update_data(self) -> DeviceState:
        try:
            state = await self.device.refresh_state()
        except Exception as err:
            raise UpdateFailed(str(err)) from err
        # The device's own timer auto-closes the valve after the duration we
        # commanded; mirror that on the wall clock so the entity doesn't sit
        # stuck-open forever once the BLE connection drops.
        if state.is_watering and state.expected_off_at is not None:
            now = datetime.now(timezone.utc)
            if now >= state.expected_off_at + timedelta(seconds=EXPIRY_GRACE_SEC):
                state.is_watering = False
                state.active_zone = None
                state.seconds_remaining = None
                state.started_at = None
                state.expected_off_at = None
            else:
                state.seconds_remaining = max(
                    0, int((state.expected_off_at - now).total_seconds())
                )
        # Adjust polling cadence based on observed state. While watering, if
        # we're inside the final stretch (expiry+grace lands sooner than the
        # next watering-cadence tick would), shorten just enough to land the
        # next tick on the expiry — so the entity flips closed promptly,
        # not on the next 30s boundary.
        if state.is_watering:
            target = self.poll_watering
            if state.expected_off_at is not None:
                secs_until_off = (
                    state.expected_off_at
                    + timedelta(seconds=EXPIRY_GRACE_SEC)
                    - datetime.now(timezone.utc)
                ).total_seconds()
                if 0 < secs_until_off < target:
                    target = max(1, int(secs_until_off))
        else:
            target = self.poll_idle
        new_interval = timedelta(seconds=target)
        if new_interval != self.update_interval:
            self.update_interval = new_interval
        return state
