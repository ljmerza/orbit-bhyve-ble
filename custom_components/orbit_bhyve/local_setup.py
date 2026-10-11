"""No-cloud setup: device records from an export or typed in by hand.

A cloud setup stores everything BLE control needs in entry.data[CONF_DEVICES]
and never talks to the Orbit cloud again after that. These helpers build the
same records without the cloud, from an `export_devices` response or the manual
form, so an install can be rebuilt (or a timer added) if the cloud is gone.

No Home Assistant imports, so the HA-less test job covers it.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any

from .devices import UnsupportedModel, resolve_device_class
from .devices.ht25 import BHyveHT25Device

# Offered in the manual form's model picker. Any other string resolve_device_class
# accepts still works (the picker allows a custom value).
KNOWN_HARDWARE = [
    "HT25-0000",
    "HT25A-0001",
    "HT25G2-0001",
    "HT31-0001",
    "HT32A-0001",
    "HT34A-0001",
]

_MAC_RE = re.compile(r"^[0-9A-F]{12}$")


class LocalRecordError(ValueError):
    """A record that can't be used. `key` is a strings.json error key; `detail`
    names the offending record or value for the {detail} placeholder."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(f"{key}: {detail}" if detail else key)
        self.key = key
        self.detail = detail


def parse_import(text: str) -> list[dict[str, Any]]:
    """Raw records from a pasted export.

    Accepts the `export_devices` response ({"entries": [{"devices": [...]}]}),
    a single {"devices": [...]}, or a bare list of records. HA shows action
    responses as YAML, so YAML is accepted too (when PyYAML is available, which
    it always is inside Home Assistant).
    """
    try:
        data = json.loads(text)
    except ValueError:
        try:
            import yaml  # noqa: PLC0415 — HA ships it; the HA-less tests may not
        except ImportError as err:
            raise LocalRecordError("invalid_import") from err
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as err:
            raise LocalRecordError("invalid_import") from err

    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        records = [
            r for e in data["entries"] if isinstance(e, dict)
            for r in (e.get("devices") or [])
        ]
    elif isinstance(data, dict) and isinstance(data.get("devices"), list):
        records = data["devices"]
    elif isinstance(data, list):
        records = data
    else:
        raise LocalRecordError("invalid_import")

    if not records or not all(isinstance(r, dict) for r in records):
        raise LocalRecordError("invalid_import")
    return records


def normalize_record(raw: dict[str, Any]) -> dict[str, Any]:
    """A raw record → the shape cloud._join_device produces, or LocalRecordError.

    Fields this doesn't know about (mesh_id, bridge_device_id, …) are kept, so
    an exported cloud record re-imports unchanged.
    """
    label = str(raw.get("name") or raw.get("mac") or "?")
    mac = normalize_mac(raw.get("mac"), label)
    hardware = str(raw.get("hardware") or "").strip()
    firmware = _normalize_fw(raw.get("firmware"))
    type_ = str(raw.get("type") or "sprinkler_timer")
    if type_.lower() == "bridge":
        raise LocalRecordError("unsupported_model", f"{label}: hubs aren't controlled over BLE")
    try:
        cls = resolve_device_class(hardware=hardware, firmware=firmware, type_=type_)
    except UnsupportedModel as err:
        raise LocalRecordError("unsupported_model", f"{label}: {hardware or '?'}") from err

    mesh_device_id = normalize_mesh_id(raw.get("mesh_device_id"), label)
    if issubclass(cls, BHyveHT25Device) and mesh_device_id is None:
        raise LocalRecordError("mesh_id_required", label)

    try:
        stations = int(raw.get("stations") or 1)
    except (TypeError, ValueError) as err:
        raise LocalRecordError("invalid_import", f"{label}: stations") from err

    record = dict(raw)
    record.update(
        cloud_id=str(raw.get("cloud_id") or local_cloud_id(mac)),
        name=str(raw.get("name") or f"B-Hyve {mac[-5:]}"),
        mac=mac,
        type=type_,
        hardware=hardware,
        firmware=firmware,
        stations=max(stations, 1),
        mesh_device_id=mesh_device_id,
        hub_mesh_device_id=normalize_mesh_id(raw.get("hub_mesh_device_id"), label),
        network_key=normalize_key(raw.get("network_key"), label),
    )
    return record


def records_from_import(text: str) -> list[dict[str, Any]]:
    """parse_import + normalize_record, rejecting a MAC listed twice."""
    records = [normalize_record(r) for r in parse_import(text)]
    seen: set[str] = set()
    for r in records:
        if r["mac"] in seen:
            raise LocalRecordError("duplicate_mac", r["mac"])
        seen.add(r["mac"])
    return records


def normalize_mac(value: Any, label: str = "") -> str:
    """AA:BB:CC:DD:EE:FF, from colon/dash-separated or bare hex."""
    bare = re.sub(r"[\s:\-]", "", str(value or "")).upper()
    if not _MAC_RE.match(bare):
        raise LocalRecordError("invalid_mac", label or str(value))
    return ":".join(bare[i:i + 2] for i in range(0, 12, 2))


def normalize_key(value: Any, label: str = "") -> str:
    """The 16-byte network key as 32 lowercase hex chars, from hex or base64
    (the cloud and the app's own storage hold it as base64)."""
    text = re.sub(r"\s", "", str(value or ""))
    if re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return text.lower()
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 16:
        raise LocalRecordError("invalid_key", label)
    return raw.hex()


def normalize_mesh_id(value: Any, label: str = "") -> int | None:
    """A 16-bit mesh id from an int, a decimal string or a 0x-hex string.
    Blank → None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        num = value if isinstance(value, int) else int(str(value).strip(), 0)
    except ValueError as err:
        raise LocalRecordError("invalid_mesh_id", label) from err
    if isinstance(num, bool) or not 0 <= num <= 0xFFFF:
        raise LocalRecordError("invalid_mesh_id", label)
    return num


def local_cloud_id(mac: str) -> str:
    """Stand-in device-registry id for a record that never came from the cloud."""
    return "local_" + mac.replace(":", "").lower()


def local_unique_id(records: list[dict[str, Any]]) -> str:
    """Config-entry unique id for a no-cloud entry: a digest of its network
    key(s), so the same network can't be set up twice without the key itself
    ending up in .storage/core.config_entries' unique_id field."""
    keys = ",".join(sorted({r["network_key"] for r in records}))
    return "local_" + hashlib.sha256(keys.encode()).hexdigest()[:16]


def merge_local(
    stored: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add incoming records to a no-cloud entry. A MAC that's already there is
    replaced in place but keeps its cloud_id, so its HA device stays the same."""
    by_mac = {r["mac"]: dict(r) for r in incoming}
    out = []
    for r in stored:
        new = by_mac.pop(r["mac"], None)
        out.append({**new, "cloud_id": r["cloud_id"]} if new else r)
    return out + list(by_mac.values())


def _normalize_fw(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(4) if text.isdigit() else text
