"""Diagnostics support: Settings → Devices & Services → Download diagnostics.

Kept free of module-level Home Assistant imports (the same pattern as
connection.py) so the snapshot/redaction helpers stay unit-testable under
tests/conftest.py's HA-less namespace shim — including the redaction rules,
which are exactly the part a regression must never break silently.

Redaction policy: account credentials and each device's AES network_key are
redacted. MACs, cloud_id and mesh_id are deliberately KEPT — every BLE log
line is keyed by MAC and cloud_id is the device-registry identifier, so an
issue report is uncorrelatable without them; none of them is a credential.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .const import CONF_DEVICES, CONF_EMAIL, CONF_PASSWORD, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

REDACTED = "**REDACTED**"
TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, "network_key"}


def _redact(value: Any) -> Any:
    """Recursively replace TO_REDACT keys' values with a placeholder.

    Empty values ("" / None) are kept as-is: a key-less device record showing
    network_key "" is itself diagnostic (it's why the device has no BLE
    connection), and there is nothing to leak."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if k in TO_REDACT and v not in (None, "") else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _jsonable(value: Any) -> Any:
    """Coerce snapshot values to JSON-safe types (datetimes, bytes, dataclass
    program summaries, int dict keys)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _device_snapshot(coord: Any) -> dict[str, Any]:
    """One coordinator's device identity, live state and poll health."""
    device = coord.device
    conn = device.connection
    try:
        rssi = device.rssi  # imports HA bluetooth internally; None off-HA
    except Exception:  # noqa: BLE001
        rssi = None
    return {
        "class": type(device).__name__,
        "name": device.name,
        "mac": device.mac,
        "hardware": device.hardware,
        "firmware": device.firmware,
        "stations": device.stations,
        "mesh_id": device.mesh_id,
        "mesh_device_id": device.mesh_device_id,
        "hub_mesh_device_id": device.hub_mesh_device_id,
        "has_flow": device.has_flow,
        "frame_magic": f"0x{device.frame_magic:02x}",
        "flow_counts_per_gallon": device.flow_counts_per_gallon,
        "battery_mv": device.battery_mv,
        "battery_pct": device.battery_pct,
        "battery_chemistry": device.battery_chemistry,
        "rssi": rssi,
        "state": _jsonable(dataclasses.asdict(device.state)),
        "connection": None
        if conn is None
        else {
            "is_connected": conn.is_connected,
            "idle_disconnect_sec": conn._idle_sec,
        },
        "coordinator": {
            "last_update_success": coord.last_update_success,
            "last_exception": str(coord.last_exception) if coord.last_exception else None,
            "update_interval_sec": _jsonable(coord.update_interval),
            "poll_idle_sec": coord.poll_idle,
            "poll_watering_sec": coord.poll_watering,
            "preferred_duration_sec": coord.preferred_duration_sec,
        },
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Account-level dump: redacted entry (data + options) and every device.

    Devices skipped at setup (UnsupportedModel) have no coordinator but still
    appear in the entry's device records — exactly what an issue report needs.
    """
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    return {
        "entry": _redact(entry.as_dict()),
        "devices": [_device_snapshot(coord) for coord in runtime.coordinators.values()]
        if runtime
        else [],
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Single-device dump: its redacted cloud record + live snapshot."""
    cloud_ids = {ident[1] for ident in device.identifiers if ident[0] == DOMAIN}
    records = [
        r for r in entry.data.get(CONF_DEVICES, []) if r.get("cloud_id") in cloud_ids
    ]
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    snapshot = None
    if runtime is not None:
        for cloud_id, coord in runtime.coordinators.items():
            if cloud_id in cloud_ids:
                snapshot = _device_snapshot(coord)
                break
    return {"record": _redact(records), "device": snapshot}
