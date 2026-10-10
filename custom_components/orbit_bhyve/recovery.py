"""Firmware recovery for HT25s stuck in the bootloader, behind the Recover firmware button.

Lives outside __init__.py for the same reason as refresh.py. The protocol is in
firmware.py; this module adds the Home Assistant side: a Bluetooth path through
whatever adapter or proxy can reach the timer, the image from the Orbit cloud
using the entry's saved credentials, and progress as a persistent notification.

Over an ESPHome proxy the transfer runs without write responses (see
firmware.NO_RESPONSE_CHECK_EVERY) — not yet verified on hardware.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import persistent_notification
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import firmware as fw
from .cloud import CloudAuthError, CloudConnectionError, OrbitCloudClient
from .connection import BleBootloaderMode, BleHandshakeError, BleNotConnectable
from .const import CONF_EMAIL, CONF_PASSWORD
from .coordinator import BHyveDeviceCoordinator
from .devices import BHyveBleDeviceBase

_LOGGER = logging.getLogger(__name__)

REBOOT_WAIT_SEC = 10
VERIFY_ATTEMPTS = 3

# MACs with a recovery in flight, so a second press can't start a parallel one.
_running: set[str] = set()

_RECOVERY_ERRORS = (
    HomeAssistantError,
    fw.BootloaderError,
    fw.FirmwareImageError,
    BleakError,
    asyncio.TimeoutError,
    OSError,
)


def async_start_recovery(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: BHyveDeviceCoordinator
) -> None:
    """Kick off recovery in the background; progress goes to a notification."""
    device = coordinator.device
    if device.mac in _running:
        raise HomeAssistantError(f"{device.name}: firmware recovery is already running")
    _running.add(device.mac)
    entry.async_create_background_task(
        hass,
        _run(hass, entry, coordinator),
        f"orbit_bhyve firmware recovery {device.name}",
    )


async def _run(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: BHyveDeviceCoordinator
) -> None:
    device = coordinator.device
    notification_id = f"orbit_bhyve_recovery_{device.mac.replace(':', '').lower()}"

    def notify(message: str) -> None:
        persistent_notification.async_create(
            hass,
            message,
            title=f"B-Hyve firmware recovery: {device.name}",
            notification_id=notification_id,
        )

    # "connect" until the timer is confirmed in its bootloader, then
    # "transfer", then "installed" once the install command has gone out.
    stage = ["connect"]

    try:
        notify("Connecting…")
        version = await _recover(hass, entry, device, notify, stage)
        notify(
            f"Recovered: fw{version:04d} is installed and the timer is talking "
            "normally again. Check that it actually waters before relying on it."
        )
    except _RECOVERY_ERRORS as err:
        _LOGGER.error("%s: firmware recovery failed: %s", device.mac, err)
        if stage[0] == "connect":
            notify(f"Recovery did not start: {err}")
        elif stage[0] == "installed":
            notify(
                f"The image was validated and installed, but: {err}\n\nPress Sync "
                "in a minute to check whether the timer came back."
            )
        else:
            notify(
                f"Recovery failed: {err}\n\nNothing was installed — the timer only "
                "installs an image it has fully received and validated — so it is "
                "still in its bootloader. You can retry, or run "
                "scripts/recover_firmware.py from a machine next to the timer "
                "(see docs/firmware-recovery.md)."
            )
    finally:
        _running.discard(device.mac)
        coordinator.async_update_listeners()


async def _recover(hass, entry, device: BHyveBleDeviceBase, notify, stage: list[str]) -> int:
    conn = device.connection
    assert conn is not None
    # Hold the device's API lock for the whole job so no poll or actuation
    # opens a session underneath the transfer.
    async with device._api_lock:
        await conn.disconnect()
        ble_device = async_ble_device_from_address(hass, device.mac, connectable=True)
        if ble_device is None:
            raise HomeAssistantError(
                "not in range of any connectable Bluetooth adapter or proxy"
            )
        client = await establish_connection(BleakClient, ble_device, device.mac, max_attempts=3)
        try:
            if not fw.is_bootloader(client):
                raise HomeAssistantError(
                    "the timer is not in its bootloader — refusing to flash a working timer"
                )
            conn.in_bootloader = True
            stage[0] = "transfer"

            notify("Downloading the official firmware from the Orbit cloud…")
            image, version = await _fetch_image(hass, entry, device.hardware)

            session = fw.BootloaderSession(client)
            await session.start_notify()
            acked = await session.detect_write_response()
            _LOGGER.info(
                "%s: bootloader link %s write responses",
                device.mac, "relays" if acked else "does NOT relay (proxy?)",
            )
            mode = "acknowledged" if acked else "unacknowledged (proxy) — experimental"
            last_decile = -1

            def on_progress(offset: int, size: int) -> None:
                nonlocal last_decile
                pct = 100 * offset // size
                if pct // 10 != last_decile:
                    last_decile = pct // 10
                    notify(f"Sending fw{version:04d} using {mode} writes: {pct}%")

            await session.upload(image, version, on_progress)
            notify("The timer validated the image. Installing…")
            stage[0] = "installed"
            await session.install()
        finally:
            if client.is_connected:
                await client.disconnect()

        return await _verify_normal_mode(device, version, notify)


async def _verify_normal_mode(device: BHyveBleDeviceBase, version: int, notify) -> int:
    conn = device.connection
    assert conn is not None
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        notify(f"Installed. Waiting for the timer to reboot (check {attempt}/{VERIFY_ATTEMPTS})…")
        await asyncio.sleep(REBOOT_WAIT_SEC)
        try:
            await conn.ensure_connected()
        except BleBootloaderMode:
            if attempt == VERIFY_ATTEMPTS:
                raise HomeAssistantError("the timer is still in its bootloader after install")
        except (BleHandshakeError, BleNotConnectable, BleakError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.debug("%s: post-install check %d failed: %s", device.mac, attempt, err)
        else:
            device._mark_reached()
            return version
        finally:
            await conn.disconnect()
    raise HomeAssistantError("the timer did not reconnect in normal mode after install")


async def _fetch_image(hass, entry: ConfigEntry, hardware: str) -> tuple[bytes, int]:
    email = entry.data.get(CONF_EMAIL)
    password = entry.data.get(CONF_PASSWORD)
    if not (email and password):
        raise HomeAssistantError("this entry has no saved B-Hyve credentials")

    client = OrbitCloudClient(async_get_clientsession(hass))
    try:
        await client.login(email, password)
        meta = await client.get_firmware_update(fw.FIRMWARE_HARDWARE_VERSION[hardware])
        urls = fw.find_download_urls(meta)
        if len(urls) != 1:
            raise HomeAssistantError(
                f"expected one download URL in the firmware response, found {len(urls)}"
            )
        image = await client.download(urls[0])
    except CloudAuthError as err:
        raise HomeAssistantError("the Orbit cloud rejected the saved credentials") from err
    except CloudConnectionError as err:
        raise HomeAssistantError(f"the Orbit cloud is unreachable: {err}") from err

    version = fw.identify_image(image)
    if version is None:
        raise fw.FirmwareImageError(
            f"the cloud served {len(image)} bytes (sha256 "
            f"{hashlib.sha256(image).hexdigest()}), which is not a known-good image. "
            "Refusing to send it from Home Assistant."
        )
    return image, version
