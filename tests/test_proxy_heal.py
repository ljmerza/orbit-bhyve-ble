"""Stale proxy address-type heal (connection.heal_proxy_address_type).

Cases ported from @FabsFabios's tests in #60, adapted to the module-level
function, plus the review follow-ups: a read-only record no longer stops the
rest from healing, and an unexpected record shape never escapes.

Record shapes mirror HA 2026.10 Bluetooth diagnostics: an ESPHome proxy record
carries ``details["address_type"]`` (0 public, 1 random); a local BlueZ record
carries ``details["props"]["AddressType"]`` as a string.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types
from types import MappingProxyType, SimpleNamespace

import pytest

from orbit_bhyve.connection import BHyveBleConnection, BleNotConnectable, heal_proxy_address_type

MAC = "AA:BB:CC:DD:EE:FF"
_BT = "homeassistant.components.bluetooth"


def _proxy(address_type, name="garage-proxy (AC:A7:04:00:00:01)"):
    details = {"source": "AC:A7:04:00:00:01", "address_type": address_type}
    return SimpleNamespace(scanner=SimpleNamespace(name=name), ble_device=SimpleNamespace(details=details))


def _bluez(address_type):
    details = {"path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
               "props": {"Address": MAC, "AddressType": address_type}}
    return SimpleNamespace(scanner=SimpleNamespace(name="hci0"), ble_device=SimpleNamespace(details=details))


def _stub_bluetooth(monkeypatch, scanner_devices=None, ble_device=None):
    """Stub the function-local HA bluetooth imports. ``scanner_devices`` may be a
    list (returned), an Exception (raised), or None (API absent)."""
    for name in ("homeassistant", "homeassistant.components", _BT):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    calls = []
    if scanner_devices is not None:
        def _lookup(hass, address, connectable):
            calls.append((address, connectable))
            if isinstance(scanner_devices, Exception):
                raise scanner_devices
            return scanner_devices
        monkeypatch.setattr(sys.modules[_BT], "async_scanner_devices_by_address", _lookup, raising=False)
    monkeypatch.setattr(sys.modules[_BT], "async_ble_device_from_address",
                        lambda *a, **k: ble_device, raising=False)
    return calls


def _heal():
    heal_proxy_address_type(None, MAC)


def test_heals_stale_public_proxy_record(monkeypatch, caplog):
    proxy, bluez = _proxy(0), _bluez("random")
    calls = _stub_bluetooth(monkeypatch, [proxy, bluez])
    with caplog.at_level(logging.WARNING):
        _heal()
    assert proxy.ble_device.details["address_type"] == 1
    assert "address_type" not in bluez.ble_device.details
    assert calls == [(MAC, True)]
    assert "corrected to random" in caplog.text


def test_healthy_record_is_silent(monkeypatch, caplog):
    proxy = _proxy(1)
    _stub_bluetooth(monkeypatch, [proxy, _bluez("random")])
    with caplog.at_level(logging.WARNING):
        _heal()
    assert proxy.ble_device.details["address_type"] == 1
    assert caplog.text == ""


def test_proxy_only_setup_untouched(monkeypatch):
    # No local adapter to vouch for random: never guess.
    proxy = _proxy(0)
    _stub_bluetooth(monkeypatch, [proxy])
    _heal()
    assert proxy.ble_device.details["address_type"] == 0


@pytest.mark.parametrize("proxy_type", [0, 1])
def test_local_public_never_changes_proxy(monkeypatch, proxy_type):
    proxy = _proxy(proxy_type)
    _stub_bluetooth(monkeypatch, [proxy, _bluez("public")])
    _heal()
    assert proxy.ble_device.details["address_type"] == proxy_type


def test_only_the_stale_proxy_of_two_changes(monkeypatch):
    stale, healthy = _proxy(0, "a"), _proxy(1, "b")
    _stub_bluetooth(monkeypatch, [stale, healthy, _bluez("random")])
    _heal()
    assert stale.ble_device.details["address_type"] == 1
    assert healthy.ble_device.details["address_type"] == 1


def test_mapping_proxy_record_skipped(monkeypatch):
    proxy = _proxy(0)
    proxy.ble_device.details = MappingProxyType(dict(proxy.ble_device.details))
    _stub_bluetooth(monkeypatch, [proxy, _bluez("random")])
    _heal()
    assert proxy.ble_device.details["address_type"] == 0


def test_read_only_dict_record_does_not_stop_the_rest(monkeypatch):
    class _FrozenDict(dict):
        def __setitem__(self, key, value):
            raise TypeError("read-only")

    frozen, later = _proxy(0, "frozen"), _proxy(0, "later")
    frozen.ble_device.details = _FrozenDict(frozen.ble_device.details)
    _stub_bluetooth(monkeypatch, [frozen, later, _bluez("random")])
    _heal()
    assert frozen.ble_device.details["address_type"] == 0
    assert later.ble_device.details["address_type"] == 1


def test_lookup_error_swallowed(monkeypatch):
    _stub_bluetooth(monkeypatch, RuntimeError("bluetooth not loaded"))
    _heal()


def test_unexpected_record_shape_swallowed(monkeypatch):
    # A future habluetooth reshaping BluetoothScannerDevice must not block connects.
    _stub_bluetooth(monkeypatch, [SimpleNamespace(scanner=None)])
    _heal()


def test_open_heals_before_resolving_device(monkeypatch):
    proxy = _proxy(0)
    _stub_bluetooth(monkeypatch, [proxy, _bluez("random")], ble_device=None)
    seen = {}

    def _resolve(*_a, **_k):
        seen["address_type"] = proxy.ble_device.details["address_type"]
        return None

    monkeypatch.setattr(sys.modules[_BT], "async_ble_device_from_address", _resolve)
    with pytest.raises(BleNotConnectable):
        asyncio.run(BHyveBleConnection(None, MAC, "00" * 16)._open())
    assert seen == {"address_type": 1}


def test_open_unaffected_when_scanner_api_absent(monkeypatch):
    # An HA build without async_scanner_devices_by_address must not turn the
    # heal into an ImportError that blocks every connection.
    _stub_bluetooth(monkeypatch, scanner_devices=None, ble_device=None)
    with pytest.raises(BleNotConnectable):
        asyncio.run(BHyveBleConnection(None, MAC, "00" * 16)._open())
