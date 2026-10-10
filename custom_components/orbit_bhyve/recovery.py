"""Firmware recovery for HT25s stuck in the bootloader.

Two entry points share one flashing core:
  - the Recover firmware button on a configured HT25-0000;
  - the recover_firmware action, which takes a bare MAC address so it also
    reaches a timer that is no longer on the account (and so was never set
    up here). If the MAC belongs to a configured device, it takes the button's
    path instead.

Lives outside __init__.py for the same reason as refresh.py. The protocol is in
firmware.py; this module adds the Home Assistant side: a Bluetooth path through
whatever adapter or proxy can reach the timer, the image from the Orbit cloud
using the entry's saved credentials (the endpoint is per hardware model, not
per device), and progress as a persistent notification.

Over an ESPHome proxy the transfer runs without write responses (see
firmware.NO_RESPONSE_CHECK_EVERY) — not yet verified on hardware.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import firmware as fw
from .cloud import CloudAuthError, CloudConnectionError, OrbitCloudClient
from .connection import (
    BHyveBleConnection,
    BleBootloaderMode,
    BleHandshakeError,
    BleNotConnectable,
    heal_proxy_address_type,
)
from .const import AES_CHAR, CONF_EMAIL, CONF_PASSWORD, DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices import BHyveBleDeviceBase

_LOGGER = logging.getLogger(__name__)

REBOOT_WAIT_SEC = 10
VERIFY_ATTEMPTS = 3
# The only family recovery is verified on; an address-only target is assumed
# to be one (the action's description says so).
ADDRESS_ONLY_HARDWARE = "HT25-0000"

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

Notify = Callable[[str], None]
# The job's stage, shared with _run for the failure message: "connect" until
# the timer is confirmed in its bootloader, then "transfer", then "installed"
# once the install command has gone out.
Stage = list[str]


def async_start_recovery(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: BHyveDeviceCoordinator
) -> None:
    """Button path: recover a configured device in the background."""
    device = coordinator.device

    async def job(notify: Notify, stage: Stage) -> int:
        return await _recover_device(hass, entry, device, notify, stage)

    _start(hass, entry, device.mac, device.name, job, coordinator.async_update_listeners)


def async_start_recovery_by_address(
    hass: HomeAssistant, entry: ConfigEntry, address: str
) -> None:
    """Action path: recover whatever HT25-0000 answers at `address`."""
    mac = address.strip().upper()
    for coordinator in _coordinators(hass):
        if coordinator.device.mac == mac and coordinator.device.connection is not None:
            if not fw.recovery_supported(coordinator.device.hardware):
                raise HomeAssistantError(
                    f"{coordinator.device.name} is {coordinator.device.hardware}; "
                    "firmware recovery only supports HT25-0000"
                )
            async_start_recovery(hass, entry, coordinator)
            return

    async def job(notify: Notify, stage: Stage) -> int:
        return await _recover_address(hass, entry, mac, notify, stage)

    _start(hass, entry, mac, mac, job, None)


def _coordinators(hass: HomeAssistant) -> list[BHyveDeviceCoordinator]:
    return [
        coordinator
        for runtime in hass.data.get(DOMAIN, {}).values()
        for coordinator in getattr(runtime, "coordinators", {}).values()
    ]


def _start(
    hass: HomeAssistant,
    entry: ConfigEntry,
    mac: str,
    name: str,
    job: Callable[[Notify, Stage], Awaitable[int]],
    on_done: Callable[[], None] | None,
) -> None:
    if mac in _running:
        raise HomeAssistantError(f"{name}: firmware recovery is already running")
    _running.add(mac)
    entry.async_create_background_task(
        hass, _run(hass, mac, name, job, on_done), f"orbit_bhyve firmware recovery {name}"
    )


async def _run(
    hass: HomeAssistant,
    mac: str,
    name: str,
    job: Callable[[Notify, Stage], Awaitable[int]],
    on_done: Callable[[], None] | None,
) -> None:
    notification_id = f"orbit_bhyve_recovery_{mac.replace(':', '').lower()}"

    def notify(message: str) -> None:
        persistent_notification.async_create(
            hass,
            message,
            title=f"B-Hyve firmware recovery: {name}",
            notification_id=notification_id,
        )

    stage: Stage = ["connect"]
    try:
        notify("Connecting…")
        version = await job(notify, stage)
        notify(
            f"Recovered: fw{version:04d} is installed and the timer is back in "
            "normal mode. Check that it actually waters before relying on it."
        )
    except _RECOVERY_ERRORS as err:
        _LOGGER.error("%s: firmware recovery failed: %s", mac, err)
        if stage[0] == "connect":
            notify(f"Recovery did not start: {err}")
        elif stage[0] == "installed":
            notify(
                f"The image was validated and installed, but: {err}\n\nGive the "
                "timer a minute, then check it again."
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
        _running.discard(mac)
        if on_done is not None:
            on_done()


async def _recover_device(
    hass, entry, device: BHyveBleDeviceBase, notify: Notify, stage: Stage
) -> int:
    conn = device.connection
    assert conn is not None
    # Hold the device's API lock for the whole job so no poll or actuation
    # opens a session underneath the transfer.
    async with device._api_lock:
        await conn.disconnect()
        version = await _flash(hass, entry, device.mac, device.hardware, notify, stage, conn)
        return await _verify_by_handshake(conn, device, version, notify)


async def _recover_address(hass, entry, mac: str, notify: Notify, stage: Stage) -> int:
    version = await _flash(hass, entry, mac, ADDRESS_ONLY_HARDWARE, notify, stage, None)
    # No network key for a timer that isn't configured, so check the GATT table
    # instead of the AES handshake.
    return await _verify_by_gatt(hass, mac, version, notify)


async def _connect(hass, mac: str) -> BleakClient:
    # Function-local like connection._open: the bluetooth component drags in
    # usb/pyserial, which the HA-plugin test environment doesn't have.
    from homeassistant.components.bluetooth import async_ble_device_from_address

    # An address-only timer never passes through BHyveBleConnection._open, so
    # heal a stale proxy address type here too (#60).
    heal_proxy_address_type(hass, mac)
    ble_device = async_ble_device_from_address(hass, mac, connectable=True)
    if ble_device is None:
        raise HomeAssistantError(
            f"{mac} is not in range of any connectable Bluetooth adapter or proxy"
        )
    return await establish_connection(BleakClient, ble_device, mac, max_attempts=3)


async def _flash(
    hass,
    entry: ConfigEntry,
    mac: str,
    hardware: str,
    notify: Notify,
    stage: Stage,
    conn: BHyveBleConnection | None,
) -> int:
    """Connect, check for the bootloader, fetch, upload, install. Returns the version."""
    client = await _connect(hass, mac)
    try:
        if not fw.is_bootloader(client):
            raise HomeAssistantError(
                "the timer is not in its bootloader — refusing to flash a working timer"
            )
        if conn is not None:
            conn.in_bootloader = True
        stage[0] = "transfer"

        notify("Downloading the official firmware from the Orbit cloud…")
        image, version = await _fetch_image(hass, entry, hardware)

        session = fw.BootloaderSession(client)
        await session.start_notify()
        acked = await session.detect_write_response()
        _LOGGER.info(
            "%s: bootloader link %s write responses",
            mac, "relays" if acked else "does NOT relay (proxy?)",
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
        return version
    finally:
        if client.is_connected:
            await client.disconnect()


async def _verify_by_handshake(
    conn: BHyveBleConnection, device: BHyveBleDeviceBase, version: int, notify: Notify
) -> int:
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


async def _verify_by_gatt(hass, mac: str, version: int, notify: Notify) -> int:
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        notify(f"Installed. Waiting for the timer to reboot (check {attempt}/{VERIFY_ATTEMPTS})…")
        await asyncio.sleep(REBOOT_WAIT_SEC)
        try:
            client = await _connect(hass, mac)
        except (HomeAssistantError, BleakError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.debug("%s: post-install check %d failed: %s", mac, attempt, err)
            continue
        try:
            if fw.is_bootloader(client):
                if attempt == VERIFY_ATTEMPTS:
                    raise HomeAssistantError("the timer is still in its bootloader after install")
                continue
            chars = {c.uuid for s in client.services for c in s.characteristics}
            if AES_CHAR in chars:
                return version
        finally:
            await client.disconnect()
    raise HomeAssistantError("the timer did not come back in normal mode after install")


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
