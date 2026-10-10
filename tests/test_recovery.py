"""Firmware-recovery orchestration (recovery.py).

Same HA-guard and direct-drive style as test_refresh_service.py: fake entry,
fake hass, the cloud and BLE stubbed out. The protocol itself is covered by
test_firmware.py.
"""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from orbit_bhyve import firmware as fw  # noqa: E402
from orbit_bhyve import recovery as rc  # noqa: E402
from orbit_bhyve.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN  # noqa: E402

IMAGE = b"known-good-image"


@pytest.fixture(autouse=True)
def _known_image(monkeypatch):
    monkeypatch.setitem(fw.KNOWN_IMAGES, 85, (len(IMAGE), hashlib.sha256(IMAGE).hexdigest()))
    rc._running.clear()


def _entry(email="a@b.c", password="pw"):
    return SimpleNamespace(
        entry_id="entry-1",
        data={CONF_EMAIL: email, CONF_PASSWORD: password},
        tasks=[],
        async_create_background_task=lambda hass, coro, name: _entry_task(coro),
    )


_started: list = []


def _entry_task(coro):
    _started.append(coro)
    coro.close()  # don't run the job; these tests cover dispatch only


def _cloud(monkeypatch, meta, image):
    monkeypatch.setattr(rc, "async_get_clientsession", lambda hass: None)

    class _Cloud:
        async def login(self, email, password):
            pass

        async def get_firmware_update(self, hardware):
            assert hardware == "ht25-0001"
            return meta

        async def download(self, url):
            return image

    monkeypatch.setattr(rc, "OrbitCloudClient", lambda session: _Cloud())


def test_fetch_image_identifies_known_image(monkeypatch):
    _cloud(monkeypatch, {"url": "https://x/fw?sig=1"}, IMAGE)
    assert asyncio.run(rc._fetch_image(None, _entry(), "HT25-0000")) == (IMAGE, 85)


def test_fetch_image_refuses_unknown_image(monkeypatch):
    _cloud(monkeypatch, {"url": "https://x/fw?sig=1"}, b"something else")
    with pytest.raises(fw.FirmwareImageError, match="not a known-good image"):
        asyncio.run(rc._fetch_image(None, _entry(), "HT25-0000"))


def test_fetch_image_needs_exactly_one_url(monkeypatch):
    _cloud(monkeypatch, {"a": "https://x/1", "b": "https://x/2"}, IMAGE)
    with pytest.raises(HomeAssistantError, match="found 2"):
        asyncio.run(rc._fetch_image(None, _entry(), "HT25-0000"))


def test_fetch_image_needs_credentials():
    with pytest.raises(HomeAssistantError, match="credentials"):
        asyncio.run(rc._fetch_image(None, _entry(password=None), "HT25-0000"))


def _hass_with(*devices):
    coords = {
        d.mac: SimpleNamespace(device=d, async_update_listeners=lambda: None)
        for d in devices
    }
    return SimpleNamespace(data={DOMAIN: {"entry-1": SimpleNamespace(coordinators=coords)}})


def _device(mac, hardware):
    return SimpleNamespace(mac=mac, name=f"dev {mac}", hardware=hardware, connection=object())


def test_by_address_uses_configured_device_path(monkeypatch):
    dev = _device("AA:BB:CC:DD:EE:FF", "HT25-0000")
    hass = _hass_with(dev)
    picked = []
    monkeypatch.setattr(rc, "async_start_recovery", lambda h, e, c: picked.append(c.device))
    rc.async_start_recovery_by_address(hass, _entry(), "aa:bb:cc:dd:ee:ff")
    assert picked == [dev]


def test_by_address_refuses_configured_unsupported_hardware():
    hass = _hass_with(_device("AA:BB:CC:DD:EE:FF", "HT25A-0001"))
    with pytest.raises(HomeAssistantError, match="only supports HT25-0000"):
        rc.async_start_recovery_by_address(hass, _entry(), "AA:BB:CC:DD:EE:FF")


def test_by_address_unconfigured_starts_background_job():
    _started.clear()
    hass = _hass_with()
    rc.async_start_recovery_by_address(hass, _entry(), "11:22:33:44:55:66")
    assert len(_started) == 1
    assert "11:22:33:44:55:66" in rc._running


def test_second_start_for_same_mac_is_refused():
    hass = _hass_with()
    rc.async_start_recovery_by_address(hass, _entry(), "11:22:33:44:55:66")
    with pytest.raises(HomeAssistantError, match="already running"):
        rc.async_start_recovery_by_address(hass, _entry(), "11:22:33:44:55:66")


def test_connect_heals_proxy_address_type_first(monkeypatch):
    # An address-only timer never goes through BHyveBleConnection._open, so
    # recovery must run the #60 heal itself before resolving the device.
    import sys
    import types as _types

    order = []
    monkeypatch.setattr(rc, "heal_proxy_address_type", lambda hass, mac: order.append(("heal", mac)))
    bt = _types.ModuleType("homeassistant.components.bluetooth")

    def _resolve(hass, mac, connectable):
        order.append(("resolve", mac))
        return None
    bt.async_ble_device_from_address = _resolve
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", bt)

    with pytest.raises(HomeAssistantError, match="not in range"):
        asyncio.run(rc._connect(None, "AA:BB:CC:DD:EE:FF"))
    assert order == [("heal", "AA:BB:CC:DD:EE:FF"), ("resolve", "AA:BB:CC:DD:EE:FF")]
