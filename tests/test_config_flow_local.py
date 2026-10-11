"""No-cloud setup: the setup menu, import/manual entry, local reconfigure, and
the export_devices / refresh_devices actions for an entry with no account.

Same HA guard and direct-drive style as test_config_flow.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import ServiceValidationError, Unauthorized  # noqa: E402

from orbit_bhyve import config_flow as cf  # noqa: E402
from orbit_bhyve.const import (  # noqa: E402
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_IMPORT,
    CONF_INCLUDE,
    CONF_LOCAL,
    CONF_PASSWORD,
)

KEY = "0123456789abcdef0123456789abcdef"


def _load_integration_init():
    """conftest.py swaps the package __init__ for a stub; load the real one,
    as a submodule of that stub so its relative imports resolve."""
    path = Path(cf.__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location(
        "orbit_bhyve._integration", path, submodule_search_locations=None
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


integration = _load_integration_init()


def _rec(cloud_id="a", **over):
    rec = {
        "cloud_id": cloud_id,
        "name": f"Valve {cloud_id}",
        "mac": "AA:BB:CC:DD:EE:FF",
        "type": "sprinkler_timer",
        "hardware": "HT25-0000",
        "firmware": "0085",
        "stations": 1,
        "mesh_device_id": 0x47D7,
        "hub_mesh_device_id": 0xEB42,
        "network_key": KEY,
    }
    rec.update(over)
    return rec


def _manual(**over):
    data = {
        "name": "Deck", "mac": "44:67:55:52:99:01", "hardware": "HT25-0000",
        "firmware": "0085", "stations": 1, "network_key": KEY,
        "mesh_device_id": "0x47D7", "hub_mesh_device_id": "0xEB42",
    }
    data.update(over)
    return data


class _FakeEntry:
    def __init__(self, data, entry_id="entry-1", title="B-Hyve"):
        self.entry_id = entry_id
        self.title = title
        self.data = data


class _FakeConfigEntries:
    def __init__(self, entries):
        self._entries = list(entries)
        self.updated: list[dict] = []
        self.reloaded: list[str] = []

    def async_entries(self, domain=None):
        return list(self._entries)

    def async_get_entry(self, entry_id):
        return next((e for e in self._entries if e.entry_id == entry_id), None)

    def async_update_entry(self, entry, data=None, **_kw):
        entry.data = data
        self.updated.append(data)

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)


def _flow(entries=(), source="user", entry_id=None):
    flow = cf.BHyveConfigFlow()
    flow.hass = SimpleNamespace(config_entries=_FakeConfigEntries(entries))
    flow.context = {"source": source}
    if entry_id:
        flow.context["entry_id"] = entry_id
    flow.flow_id = "test-flow"
    flow.handler = "orbit_bhyve"
    unique_ids: list[str] = []

    async def _set_unique_id(uid, **_kw):
        unique_ids.append(uid)

    flow.async_set_unique_id = _set_unique_id
    flow._abort_if_unique_id_configured = lambda **_kw: None
    flow.unique_ids = unique_ids
    return flow


def _run(coro):
    return asyncio.run(coro)


# --- setup -----------------------------------------------------------------------

def test_setup_starts_with_a_menu():
    result = _run(_flow().async_step_user())
    assert result["type"] == "menu"
    assert result["menu_options"] == ["cloud", "local_import", "local_manual"]


def test_cloud_step_still_shows_the_login_form():
    result = _run(_flow().async_step_cloud())
    assert result["type"] == "form"
    assert result["step_id"] == "cloud"


def test_import_creates_a_local_entry():
    flow = _flow()
    export = {"entries": [{"devices": [_rec("a")]}]}
    result = _run(flow.async_step_local_import({CONF_IMPORT: json.dumps(export)}))
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_LOCAL: True, CONF_DEVICES: [_rec("a")]}
    assert CONF_EMAIL not in result["data"]
    assert result["options"]
    assert flow.unique_ids and flow.unique_ids[0].startswith("local_")


def test_import_error_reshows_the_form_with_detail():
    result = _run(_flow().async_step_local_import({CONF_IMPORT: json.dumps([_rec("a", mac="nope")])}))
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_mac"}
    assert result["description_placeholders"] == {"detail": "Valve a"}


def test_import_refuses_a_mac_another_entry_owns():
    cloud = _FakeEntry({CONF_EMAIL: "a@b.c", CONF_PASSWORD: "pw", CONF_DEVICES: [_rec("a")]})
    result = _run(_flow([cloud]).async_step_local_import({CONF_IMPORT: json.dumps([_rec("a")])}))
    assert result["errors"] == {"base": "mac_already_configured"}


def test_manual_entry_creates_a_local_entry():
    result = _run(_flow().async_step_local_manual(_manual()))
    assert result["type"] == "create_entry"
    [rec] = result["data"][CONF_DEVICES]
    assert rec["cloud_id"] == "local_446755529901"
    assert (rec["mesh_device_id"], rec["hub_mesh_device_id"]) == (0x47D7, 0xEB42)


def test_manual_entry_error_keeps_what_was_typed():
    typed = _manual(mesh_device_id="")
    result = _run(_flow().async_step_local_manual(typed))
    assert result["errors"] == {"base": "mesh_id_required"}
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults["name"] == "Deck"
    assert defaults["network_key"] == KEY


# --- reconfigure on a local entry ------------------------------------------------

def _local_entry(devices):
    return _FakeEntry({CONF_LOCAL: True, CONF_DEVICES: devices})


def test_local_reconfigure_shows_its_own_menu():
    entry = _local_entry([_rec("a")])
    result = _run(_flow([entry], "reconfigure", entry.entry_id).async_step_reconfigure())
    assert result["type"] == "menu"
    assert result["menu_options"] == ["reconfigure_manual", "reconfigure_import", "reconfigure_keep"]


def test_local_reconfigure_manual_prefills_the_key_and_adds_a_timer():
    entry = _local_entry([_rec("a")])
    flow = _flow([entry], "reconfigure", entry.entry_id)
    form = _run(flow.async_step_reconfigure_manual())
    defaults = {str(k): k.default() for k in form["data_schema"].schema}
    assert defaults["network_key"] == KEY

    result = _run(flow.async_step_reconfigure_manual(_manual()))
    assert result == {**result, "type": "abort", "reason": "reconfigure_successful"}
    assert [r["cloud_id"] for r in entry.data[CONF_DEVICES]] == ["a", "local_446755529901"]
    assert entry.data[CONF_LOCAL] is True
    assert flow.hass.config_entries.reloaded == [entry.entry_id]


def test_local_reconfigure_edit_keeps_the_device_identity():
    entry = _local_entry([_rec("a", mac="44:67:55:52:99:01")])
    flow = _flow([entry], "reconfigure", entry.entry_id)
    _run(flow.async_step_reconfigure_manual(_manual(name="Deck renamed")))
    [rec] = entry.data[CONF_DEVICES]
    assert (rec["cloud_id"], rec["name"]) == ("a", "Deck renamed")


def test_local_reconfigure_import_merges():
    entry = _local_entry([_rec("a")])
    flow = _flow([entry], "reconfigure", entry.entry_id)
    text = json.dumps([_rec("b", mac="11:22:33:44:55:66")])
    _run(flow.async_step_reconfigure_import({CONF_IMPORT: text}))
    assert [r["cloud_id"] for r in entry.data[CONF_DEVICES]] == ["a", "b"]


def test_local_reconfigure_keep_removes_unchecked_and_refuses_empty():
    entry = _local_entry([_rec("a"), _rec("b", mac="11:22:33:44:55:66")])
    flow = _flow([entry], "reconfigure", entry.entry_id)
    empty = _run(flow.async_step_reconfigure_keep({CONF_INCLUDE: []}))
    assert empty["errors"] == {"base": "no_devices_selected"}
    _run(flow.async_step_reconfigure_keep({CONF_INCLUDE: ["b"]}))
    assert [r["cloud_id"] for r in entry.data[CONF_DEVICES]] == ["b"]


# --- actions -------------------------------------------------------------------------

class _FakeServices:
    def __init__(self):
        self.handlers = {}

    def has_service(self, domain, service):
        return False

    def async_register(self, domain, service, handler, schema=None, supports_response=None):
        self.handlers[service] = handler


def _handlers(entries, users=None):
    users = users or {}

    async def _get_user(user_id):
        return users.get(user_id)

    hass = SimpleNamespace(
        services=_FakeServices(),
        config_entries=_FakeConfigEntries(entries),
        auth=SimpleNamespace(async_get_user=_get_user),
    )
    integration._register_services(hass)
    return hass.services.handlers


def _call(data=None, user_id=None):
    return SimpleNamespace(data=data or {}, context=SimpleNamespace(user_id=user_id))


def test_export_returns_every_entry_with_keys():
    cloud = _FakeEntry({CONF_EMAIL: "a@b.c", CONF_DEVICES: [_rec("a")]}, "e1", "a@b.c")
    local = _local_entry([_rec("b", mac="11:22:33:44:55:66")])
    admin = SimpleNamespace(is_admin=True)
    export = _handlers([cloud, local], {"u": admin})["export_devices"]
    result = _run(export(_call(user_id="u")))
    assert [e["entry_id"] for e in result["entries"]] == ["e1", "entry-1"]
    assert result["entries"][0]["devices"][0]["network_key"] == KEY
    # The export re-imports as-is.
    assert cf.records_from_import(json.dumps(result)) == [_rec("a"), _rec("b", mac="11:22:33:44:55:66")]


def test_export_can_target_one_entry():
    cloud = _FakeEntry({CONF_DEVICES: [_rec("a")]}, "e1")
    local = _local_entry([_rec("b")])
    export = _handlers([cloud, local])["export_devices"]
    result = _run(export(_call({"config_entry_id": ["entry-1"]})))
    assert [e["entry_id"] for e in result["entries"]] == ["entry-1"]


def test_export_refuses_non_admins():
    export = _handlers([_local_entry([_rec("a")])], {"u": SimpleNamespace(is_admin=False)})["export_devices"]
    with pytest.raises(Unauthorized):
        _run(export(_call(user_id="u")))


def test_refresh_refuses_a_targeted_local_entry():
    refresh = _handlers([_local_entry([_rec("a")])])["refresh_devices"]
    with pytest.raises(ServiceValidationError):
        _run(refresh(_call({"config_entry_id": ["entry-1"]})))
