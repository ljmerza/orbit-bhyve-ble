"""Diagnostics snapshot + redaction tests.

Exercises the pure helpers in diagnostics.py — no Home Assistant required
(the module keeps HA imports out of module level for exactly this reason).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from orbit_bhyve.diagnostics import REDACTED, _device_snapshot, _jsonable, _redact
from orbit_bhyve.devices.base import ProgramSummary
from orbit_bhyve.devices.ht25g2 import BHyveHT25G2Device


def _fake_coord() -> SimpleNamespace:
    # Key-less record -> no BLE connection, no hass needed (same trick as
    # test_devices.py::test_battery_pct_property_follows_chemistry).
    record = {
        "cloud_id": "abc123", "name": "Deck", "mac": "AA:BB:CC:DD:EE:FF",
        "hardware": "HT25G2-0001", "firmware": "0111", "stations": 1,
        "network_key": "",
    }
    dev = BHyveHT25G2Device(None, record)
    dev.battery_mv = 2828
    dev.state.is_watering = True
    dev.state.started_at = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    dev.state.programs = {1: ProgramSummary(slot=1, empty=False, name="Lawn")}
    return SimpleNamespace(
        device=dev,
        last_update_success=True,
        last_exception=None,
        update_interval=timedelta(seconds=900),
        poll_idle=900,
        poll_watering=30,
        preferred_duration_sec=600,
    )


def test_device_snapshot_is_json_serializable():
    snap = _device_snapshot(_fake_coord())
    # The whole point: HA's diagnostics view renders JSON, so datetimes,
    # program dataclasses and int dict keys must already be coerced.
    encoded = json.loads(json.dumps(snap))
    assert encoded["class"] == "BHyveHT25G2Device"
    assert encoded["mac"] == "AA:BB:CC:DD:EE:FF"
    assert encoded["has_flow"] is True
    assert encoded["battery_mv"] == 2828
    assert encoded["battery_pct"] == 71  # alkaline curve, HW cross-checked value
    assert encoded["state"]["started_at"] == "2026-07-18T12:00:00+00:00"
    assert encoded["state"]["programs"]["1"]["name"] == "Lawn"
    assert encoded["connection"] is None  # key-less record -> no BLE
    assert encoded["coordinator"]["update_interval_sec"] == 900.0
    assert encoded["rssi"] is None  # HA bluetooth unavailable here -> swallowed


def test_redact_entry_shaped_dict():
    entry = {
        "data": {
            "email": "user@example.com",
            "password": "hunter2",
            "devices": [
                {"cloud_id": "abc", "mac": "AA:BB:CC:DD:EE:FF",
                 "network_key": "f0983e39083a335644614ffb3bd67ee4"},
                {"cloud_id": "hub", "mac": "11:22:33:44:55:66", "network_key": ""},
            ],
        },
        "options": {"poll_idle_sec": 900},
    }
    red = _redact(entry)
    assert red["data"]["email"] == REDACTED
    assert red["data"]["password"] == REDACTED
    assert red["data"]["devices"][0]["network_key"] == REDACTED
    # Correlation fields survive; an empty key stays visibly empty.
    assert red["data"]["devices"][0]["mac"] == "AA:BB:CC:DD:EE:FF"
    assert red["data"]["devices"][1]["network_key"] == ""
    assert red["options"]["poll_idle_sec"] == 900
    # No secret string anywhere in the encoded output.
    blob = json.dumps(red)
    assert "hunter2" not in blob
    assert "f0983e39" not in blob
    assert "user@example.com" not in blob
    # Input is not mutated.
    assert entry["data"]["password"] == "hunter2"


def test_jsonable_edge_types():
    assert _jsonable(b"\xaa\x77") == "aa77"
    assert sorted(_jsonable({1: {"s": {2, 1}}})["1"]["s"]) == [1, 2]
    assert _jsonable(timedelta(minutes=2)) == 120.0
