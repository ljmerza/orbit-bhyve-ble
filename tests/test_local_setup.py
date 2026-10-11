"""No-cloud setup records: parsing an export and checking typed-in values.

local_setup.py has no Home Assistant imports, so this runs in the HA-less job.
"""
from __future__ import annotations

import base64
import json

import pytest

from orbit_bhyve.local_setup import (
    LocalRecordError,
    local_unique_id,
    merge_local,
    normalize_key,
    normalize_mac,
    normalize_mesh_id,
    normalize_record,
    parse_import,
    records_from_import,
)

KEY = "0123456789abcdef0123456789abcdef"


def _rec(cloud_id="a", **over):
    rec = {
        "cloud_id": cloud_id,
        "name": f"Valve {cloud_id}",
        "mac": "AA:BB:CC:DD:EE:FF",
        "type": "sprinkler_timer",
        "hardware": "HT25-0000",
        "firmware": "0085",
        "stations": 1,
        "mesh_id": "mesh-1",
        "mesh_device_id": 0x47D7,
        "bridge_device_id": "bridge-1",
        "hub_mesh_device_id": 0xEB42,
        "network_key": KEY,
        "battery_pct": 88,
        "battery_mv": 3100,
    }
    rec.update(over)
    return rec


def _err(fn, *args):
    with pytest.raises(LocalRecordError) as exc:
        fn(*args)
    return exc.value


# --- parse_import ------------------------------------------------------------

def test_export_response_round_trips_unchanged():
    export = {"entries": [{"entry_id": "e1", "title": "a@b.c", "devices": [_rec("a")]}]}
    assert records_from_import(json.dumps(export)) == [_rec("a")]


def test_accepts_devices_object_and_bare_list():
    assert parse_import(json.dumps({"devices": [_rec("a")]})) == [_rec("a")]
    assert parse_import(json.dumps([_rec("a")])) == [_rec("a")]


def test_entries_from_several_exports_are_flattened():
    export = {"entries": [
        {"devices": [_rec("a")]},
        {"devices": [_rec("b", mac="11:22:33:44:55:66")]},
    ]}
    assert [r["cloud_id"] for r in records_from_import(json.dumps(export))] == ["a", "b"]


def test_yaml_as_shown_by_the_ha_ui_is_accepted():
    pytest.importorskip("yaml")
    text = (
        "entries:\n"
        "  - entry_id: e1\n"
        "    devices:\n"
        "      - cloud_id: a\n"
        "        name: Deck\n"
        "        mac: AA:BB:CC:DD:EE:FF\n"
        "        hardware: HT25-0000\n"
        "        firmware: '0085'\n"
        "        stations: 1\n"
        "        mesh_device_id: 18391\n"
        f"        network_key: {KEY}\n"
    )
    [rec] = records_from_import(text)
    assert rec["mac"] == "AA:BB:CC:DD:EE:FF"
    assert rec["mesh_device_id"] == 0x47D7


@pytest.mark.parametrize("text", ["", "[]", '{"devices": []}', '"just a string"', "[1, 2]"])
def test_unusable_imports_are_rejected(text):
    assert _err(parse_import, text).key == "invalid_import"


def test_duplicate_mac_in_one_import_is_rejected():
    text = json.dumps([_rec("a"), _rec("b")])
    err = _err(records_from_import, text)
    assert (err.key, err.detail) == ("duplicate_mac", "AA:BB:CC:DD:EE:FF")


# --- normalize_record ----------------------------------------------------------

def test_minimal_manual_record_gets_defaults():
    rec = normalize_record({
        "name": "Deck", "mac": "44:67:55:52:99:01", "hardware": "HT25-0000",
        "firmware": "85", "stations": 1, "network_key": KEY,
        "mesh_device_id": "0x47D7", "hub_mesh_device_id": "",
    })
    assert rec["cloud_id"] == "local_446755529901"
    assert rec["type"] == "sprinkler_timer"
    assert rec["firmware"] == "0085"
    assert rec["mesh_device_id"] == 0x47D7
    assert rec["hub_mesh_device_id"] is None


def test_protobuf_timer_needs_no_mesh_id():
    rec = normalize_record({"mac": "446755aabbcc", "hardware": "HT25A-0001", "network_key": KEY})
    assert rec["mac"] == "44:67:55:AA:BB:CC"
    assert rec["mesh_device_id"] is None
    assert rec["stations"] == 1


def test_mesh_timer_without_mesh_id_is_rejected():
    err = _err(normalize_record, _rec("a", mesh_device_id=None))
    assert (err.key, err.detail) == ("mesh_id_required", "Valve a")


def test_unknown_model_and_hubs_are_rejected():
    assert _err(normalize_record, _rec("a", hardware="XX99")).key == "unsupported_model"
    assert _err(normalize_record, _rec("a", type="bridge")).key == "unsupported_model"


# --- field normalizers ---------------------------------------------------------

def test_key_accepts_hex_and_base64():
    assert normalize_key(KEY.upper()) == KEY
    assert normalize_key(base64.b64encode(bytes.fromhex(KEY)).decode()) == KEY


@pytest.mark.parametrize("bad", ["", "abc", KEY[:-2], base64.b64encode(b"x" * 15).decode()])
def test_bad_keys_are_rejected(bad):
    assert _err(normalize_key, bad).key == "invalid_key"


@pytest.mark.parametrize("value,expected", [
    ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
    ("AABBCCDDEEFF", "AA:BB:CC:DD:EE:FF"),
])
def test_mac_forms(value, expected):
    assert normalize_mac(value) == expected


@pytest.mark.parametrize("bad", ["", "AA:BB:CC:DD:EE", "GG:BB:CC:DD:EE:FF"])
def test_bad_macs_are_rejected(bad):
    assert _err(normalize_mac, bad).key == "invalid_mac"


@pytest.mark.parametrize("value,expected", [
    (18391, 18391), ("18391", 18391), ("0x47d7", 0x47D7), ("", None), (None, None),
])
def test_mesh_id_forms(value, expected):
    assert normalize_mesh_id(value) == expected


@pytest.mark.parametrize("bad", ["0x10000", "-1", "abc", True])
def test_bad_mesh_ids_are_rejected(bad):
    assert _err(normalize_mesh_id, bad).key == "invalid_mesh_id"


# --- entry helpers -------------------------------------------------------------

def test_unique_id_depends_only_on_the_keys():
    a = local_unique_id([_rec("a"), _rec("b")])
    assert a == local_unique_id([_rec("c")])
    assert a != local_unique_id([_rec("a", network_key="f" * 32)])
    assert KEY not in a


def test_merge_replaces_by_mac_and_keeps_the_cloud_id():
    stored = [_rec("a"), _rec("b", mac="11:22:33:44:55:66")]
    edited = _rec("local_aabbccddeeff", name="Renamed")
    new = _rec("c", mac="22:33:44:55:66:77")
    merged = merge_local(stored, [edited, new])
    assert [r["cloud_id"] for r in merged] == ["a", "b", "c"]
    assert merged[0]["name"] == "Renamed"
