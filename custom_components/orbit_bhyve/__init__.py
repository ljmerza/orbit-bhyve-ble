"""Orbit B-Hyve BLE integration — account-level setup.

Discovers all devices on an Orbit account, instantiates a per-device class
based on hardware/firmware, and creates entity platforms. Cloud is touched
only at setup time and on user-triggered refresh; runtime is BLE-only.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloud import CloudAuthError, CloudConnectionError, OrbitCloudClient
from .const import (
    CONF_DEFAULT_DURATION,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_FLOW_COUNTS_PER_GALLON,
    CONF_IDLE_DISCONNECT,
    CONF_PASSWORD,
    CONF_POLL_IDLE,
    CONF_POLL_WATERING,
    DEFAULT_DURATION,
    DEFAULT_FLOW_COUNTS_PER_GALLON,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_POLL_IDLE,
    DEFAULT_POLL_WATERING,
    DOMAIN,
)
from .coordinator import BHyveDeviceCoordinator
from .devices import UnsupportedModel, build_device

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.VALVE,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.BUTTON,
]


class EntryRuntime:
    """Lives at hass.data[DOMAIN][entry_id]."""

    def __init__(self):
        self.coordinators: dict[str, BHyveDeviceCoordinator] = {}  # cloud_id → coordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    devices = entry.data.get(CONF_DEVICES, [])
    if not devices:
        _LOGGER.warning("%s: no devices in config entry", entry.entry_id)
        return False

    opts = entry.options
    idle_disconnect = opts.get(CONF_IDLE_DISCONNECT, DEFAULT_IDLE_DISCONNECT)
    poll_idle = opts.get(CONF_POLL_IDLE, DEFAULT_POLL_IDLE)
    poll_watering = opts.get(CONF_POLL_WATERING, DEFAULT_POLL_WATERING)
    flow_counts_per_gallon = opts.get(
        CONF_FLOW_COUNTS_PER_GALLON, DEFAULT_FLOW_COUNTS_PER_GALLON
    )

    runtime = EntryRuntime()
    for record in devices:
        # Skip hubs even if a stale entry from before the bridge filter
        # still has them in CONF_DEVICES. Going forward they're filtered
        # at cloud.discover() and never reach here.
        if (record.get("type") or "").lower() == "bridge":
            continue
        try:
            device = build_device(
                hass, record,
                idle_disconnect_sec=idle_disconnect,
                flow_counts_per_gallon=flow_counts_per_gallon,
            )
        except UnsupportedModel as err:
            _LOGGER.warning("%s: %s — skipping", record.get("name"), err)
            continue
        coord = BHyveDeviceCoordinator(
            hass, device, poll_idle_sec=poll_idle, poll_watering_sec=poll_watering,
        )
        # Don't await first refresh — many BHyve timers deep-sleep and would
        # block setup while we wait for them. Coordinators self-update.
        runtime.coordinators[record["cloud_id"]] = coord

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _register_services(hass)

    # Kick a prompt (non-blocking) first poll so an in-progress run / live state
    # is read within one cycle instead of waiting a full idle interval. Matters
    # after an HA restart mid-run: without this the valve shows idle until the
    # next idle-cadence tick. async_request_refresh only *schedules* the poll, so
    # this doesn't block setup on a slow/deep-sleeping device.
    for coord in runtime.coordinators.values():
        await coord.async_request_refresh()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime: EntryRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime:
        for coord in runtime.coordinators.values():
            await coord.device.async_unload()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options IN PLACE — no reload.

    A reload would rebuild every device with a fresh idle DeviceState (wiping an
    in-progress run's live state) and immediately disconnect+reconnect BLE. Orbit
    valves allow only ONE BLE session, so reconnecting that fast — before the
    device/proxy releases the old session — leaves comms broken until something
    (a full HA restart) clears the stale session. So for our tuning options (flow
    calibration, poll intervals, idle disconnect) we mutate the live coordinators
    instead. Device-list/credential changes take a different path
    (refresh_devices / reauth) that reloads explicitly."""
    runtime: EntryRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    opts = entry.options
    idle_disconnect = opts.get(CONF_IDLE_DISCONNECT, DEFAULT_IDLE_DISCONNECT)
    poll_idle = opts.get(CONF_POLL_IDLE, DEFAULT_POLL_IDLE)
    poll_watering = opts.get(CONF_POLL_WATERING, DEFAULT_POLL_WATERING)
    flow_counts_per_gallon = opts.get(
        CONF_FLOW_COUNTS_PER_GALLON, DEFAULT_FLOW_COUNTS_PER_GALLON
    )
    for coord in runtime.coordinators.values():
        coord.poll_idle = poll_idle
        coord.poll_watering = poll_watering
        coord.device.flow_counts_per_gallon = flow_counts_per_gallon
        if coord.device.connection is not None:
            coord.device.connection._idle_sec = idle_disconnect
        # Re-apply the interval for the current state so a changed cadence takes
        # effect now (the coordinator re-derives it each poll anyway).
        coord.update_interval = timedelta(
            seconds=poll_watering if coord.device.state.is_watering else poll_idle
        )
    _LOGGER.debug("%s: options applied in place (no reload)", entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "refresh_devices"):
        return

    async def refresh_devices(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            email = entry.data.get(CONF_EMAIL)
            password = entry.data.get(CONF_PASSWORD)
            if not (email and password):
                continue
            client = OrbitCloudClient(async_get_clientsession(hass))
            try:
                discovered = await client.discover(email, password)
            except CloudAuthError as err:
                _LOGGER.error("Refresh: auth failed for %s: %s", email, err)
                raise ConfigEntryAuthFailed(str(err)) from err
            except CloudConnectionError as err:
                _LOGGER.error("Refresh: cloud unreachable for %s: %s", email, err)
                continue
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DEVICES: discovered},
            )
            await hass.config_entries.async_reload(entry.entry_id)

    hass.services.async_register(
        DOMAIN, "refresh_devices", refresh_devices, schema=vol.Schema({}),
    )

    async def start_watering(call: ServiceCall) -> None:
        duration = call.data.get("duration", DEFAULT_DURATION)
        from homeassistant.helpers.entity_platform import async_get_platforms
        for platform in async_get_platforms(hass, DOMAIN):
            for entity in platform.entities.values():
                if entity.entity_id in call.data.get("entity_id", []):
                    await entity.async_open_valve(duration=duration)

    hass.services.async_register(
        DOMAIN,
        "start_watering",
        start_watering,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_ids,
            vol.Optional("duration", default=DEFAULT_DURATION):
                vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        }),
    )

    async def stop_all(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            runtime: EntryRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if not runtime:
                continue
            for coord in runtime.coordinators.values():
                if coord.device.connection is None:
                    continue
                try:
                    await coord.device.stop_watering()
                except Exception as err:
                    _LOGGER.warning("stop_all on %s: %s", coord.device.name, err)

    hass.services.async_register(
        DOMAIN, "stop_all", stop_all, schema=vol.Schema({}),
    )

    async def probe_magic(call: ServiceCall) -> None:
        """Debug-only: override frame_magic + trailer_const on a device's
        BLE connection and force-disconnect so the next command goes through
        a fresh handshake using the new values. Used to test whether fw0041
        devices use a different inner-protocol magic byte (e.g. 0x11) than
        fw0085's 0x10. Persists only until HA restart."""
        mac = call.data["mac"].upper()
        magic = int(call.data["magic"]) & 0xFF
        found = False
        for runtime in hass.data.get(DOMAIN, {}).values():
            for coord in runtime.coordinators.values():
                if (coord.device.mac or "").upper() != mac:
                    continue
                conn = coord.device.connection
                if conn is None:
                    _LOGGER.warning("probe_magic: %s has no BLE connection (hub?)", mac)
                    return
                _LOGGER.warning(
                    "probe_magic: %s magic 0x%02x→0x%02x, trailer 0x%02x→0x%02x; forcing reconnect",
                    mac, conn._frame_magic, magic, conn._trailer_const, magic,
                )
                conn._frame_magic = magic
                conn._trailer_const = magic
                await conn.disconnect()
                found = True
        if not found:
            _LOGGER.warning("probe_magic: no device with mac=%s", mac)

    hass.services.async_register(
        DOMAIN,
        "probe_magic",
        probe_magic,
        schema=vol.Schema({
            vol.Required("mac"): str,
            vol.Required("magic"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        }),
    )

    async def probe_status(call: ServiceCall) -> None:
        """Force a fresh BLE connect + 8-step init on the named device, no
        actuation. connection._on_notify already decrypts and logs every
        plaintext at INFO, so the captured init responses land in
        `docker logs hass`. Used to gather data for offline byte-by-byte
        battery decoding."""
        mac = call.data["mac"].upper()
        for runtime in hass.data.get(DOMAIN, {}).values():
            for coord in runtime.coordinators.values():
                if (coord.device.mac or "").upper() != mac:
                    continue
                conn = coord.device.connection
                if conn is None:
                    _LOGGER.warning("probe_status: %s has no BLE connection", mac)
                    return
                _LOGGER.warning("probe_status: %s — forcing reconnect", mac)
                await conn.disconnect()
                await conn.ensure_connected()
                return
        _LOGGER.warning("probe_status: no device with mac=%s", mac)

    hass.services.async_register(
        DOMAIN,
        "probe_status",
        probe_status,
        schema=vol.Schema({vol.Required("mac"): str}),
    )

    async def probe_send(call: ServiceCall) -> None:
        """Debug-only: send an arbitrary inner-plaintext frame to a device and
        let _on_notify decrypt+log the replies. For reverse-engineering — e.g.
        a protobuf getDeviceInfo query to the quad. The `plaintext` hex is the
        inner frame (for HT34A: AA775A0F + len + 00 + protobuf + CRC16);
        connection.encrypt() adds the [magic][len]..[trailer] wrapper. Optional
        `magic` overrides frame_magic/trailer_const (forces a reconnect). A
        reply that decodes is logged as 'notif pt=...'; one with a mismatched
        magic logs 'notif decrypt failed: bad frame magic raw=...', which
        reveals the device's actual magic byte."""
        mac = call.data["mac"].upper()
        plaintext = bytes.fromhex(call.data["plaintext"])
        magic = call.data.get("magic")
        drain = int(call.data.get("drain_ms", 2000))
        for runtime in hass.data.get(DOMAIN, {}).values():
            for coord in runtime.coordinators.values():
                if (coord.device.mac or "").upper() != mac:
                    continue
                conn = coord.device.connection
                if conn is None:
                    _LOGGER.warning("probe_send: %s has no BLE connection", mac)
                    return
                if magic is not None:
                    m = int(magic) & 0xFF
                    _LOGGER.warning("probe_send: %s magic->0x%02x; reconnecting", mac, m)
                    conn._frame_magic = m
                    conn._trailer_const = m
                    await conn.disconnect()
                _LOGGER.warning("probe_send: %s -> %s", mac, plaintext.hex())
                notifs = await conn.send(plaintext, drain_ms=drain)
                _LOGGER.warning("probe_send: %s got %d notification(s)", mac, len(notifs))
                return
        _LOGGER.warning("probe_send: no device with mac=%s", mac)

    hass.services.async_register(
        DOMAIN,
        "probe_send",
        probe_send,
        schema=vol.Schema({
            vol.Required("mac"): str,
            vol.Required("plaintext"): str,
            vol.Optional("magic"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
            vol.Optional("drain_ms", default=2000):
                vol.All(vol.Coerce(int), vol.Range(min=100, max=10000)),
        }),
    )
