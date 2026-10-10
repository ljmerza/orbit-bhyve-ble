#!/usr/bin/env python3
"""Recover an HT25 stuck in its bootloader (blinking white) by re-sending firmware.

Runs on any machine with a BLE adapter and `bleak` + `aiohttp` installed; no
Home Assistant needed. See docs/firmware-recovery.md before using it.

  [--adapter hciN] picks the BLE adapter; default is the system default.

  scan                     list nearby 0xfe32 devices
  probe ADDR               connect and report bootloader vs normal mode
  fetch --email E          download the official image from the Orbit cloud
  flash ADDR --image F     send the image, verify it, install it
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import sys
import types
from pathlib import Path

# Import the integration's modules without its Home Assistant-heavy __init__
# (same trick as tests/conftest.py).
_CC = Path(__file__).resolve().parent.parent / "custom_components"
_pkg = types.ModuleType("orbit_bhyve")
_pkg.__path__ = [str(_CC / "orbit_bhyve")]
sys.modules.setdefault("orbit_bhyve", _pkg)

import aiohttp  # noqa: E402
from bleak import BleakClient, BleakScanner  # noqa: E402

from orbit_bhyve import firmware as fw  # noqa: E402
from orbit_bhyve.cloud import OrbitCloudClient  # noqa: E402
from orbit_bhyve.const import AES_CHAR, SERVICE_UUID  # noqa: E402

CONNECT_TIMEOUT_SEC = 20.0
REBOOT_WAIT_SEC = 10.0


def _redact_urls(obj):
    if isinstance(obj, dict):
        return {k: _redact_urls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_urls(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("http"):
        return re.sub(r"\?.*", "?<signature redacted>", obj)
    return obj


def _adapter_kwargs(args) -> dict:
    # BlueZ adapter name (hci0, hci1, ...); omit to use the system default.
    return {"adapter": args.adapter} if args.adapter else {}


async def _connect(args) -> BleakClient:
    kw = _adapter_kwargs(args)
    device = await BleakScanner.find_device_by_address(
        args.address, timeout=CONNECT_TIMEOUT_SEC, **kw
    )
    if device is None:
        raise SystemExit(f"{args.address} not found — is it in range and not connected elsewhere?")
    client = BleakClient(device, timeout=CONNECT_TIMEOUT_SEC, **kw)
    await client.connect()
    return client


async def cmd_scan(args) -> None:
    found = await BleakScanner.discover(
        timeout=args.seconds, return_adv=True, **_adapter_kwargs(args)
    )
    for address, (device, adv) in sorted(found.items()):
        if SERVICE_UUID in [u.lower() for u in adv.service_uuids]:
            print(f"{address}  rssi={adv.rssi:>4}  name={device.name or adv.local_name!r}")


async def cmd_probe(args) -> None:
    client = await _connect(args)
    try:
        chars = sorted(
            c.uuid for s in client.services for c in s.characteristics
        )
        print("characteristics:", *chars, sep="\n  ")
        if not fw.is_bootloader(client):
            mode = "normal (AES)" if AES_CHAR in chars else "unknown"
            print(f"mode: {mode} — nothing to recover")
            return
        print("mode: BOOTLOADER")
        session = fw.BootloaderSession(client)
        await session.start_notify()
        print("progress:", await session.read_progress())
    finally:
        await client.disconnect()


async def cmd_fetch(args) -> None:
    password = os.environ.get("BHYVE_PASSWORD") or getpass.getpass("B-hyve password: ")
    async with aiohttp.ClientSession() as http:
        cloud = OrbitCloudClient(http)
        await cloud.login(args.email, password)
        meta = await cloud.get_firmware_update(args.hardware)
        print(json.dumps(_redact_urls(meta), indent=2))
        urls = fw.find_download_urls(meta)
        if len(urls) != 1:
            raise SystemExit(f"expected one download URL in the response, found {len(urls)}")
        image = await cloud.download(urls[0])

    out = Path(args.out)
    out.write_bytes(image)
    print(f"wrote {len(image)} bytes to {out}")
    version = fw.identify_image(image)
    if version is None:
        print("WARNING: image does not match any known-good image — flash will refuse it")
    else:
        print(f"matches known-good fw{version:04d}")


async def cmd_flash(args) -> None:
    image = Path(args.image).read_bytes()
    fw.verify_image(image, args.version, allow_unknown=args.allow_unknown_image)

    client = await _connect(args)
    try:
        if not fw.is_bootloader(client):
            raise SystemExit("device is not in bootloader mode — refusing to flash a working timer")
        session = fw.BootloaderSession(client)
        await session.start_notify()

        def on_progress(offset: int, size: int) -> None:
            print(f"\r  {offset}/{size} bytes ({100 * offset // size}%)", end="", flush=True)

        final = await session.upload(image, args.version, on_progress)
        print(f"\nimage received and validated: {final}")
        if args.no_install:
            print("--no-install: stopping before install")
            return
        print("installing...")
        await session.install()
    finally:
        if client.is_connected:
            await client.disconnect()

    print(f"waiting {REBOOT_WAIT_SEC:.0f}s for reboot, then reconnecting...")
    await asyncio.sleep(REBOOT_WAIT_SEC)
    client = await _connect(args)
    try:
        if fw.is_bootloader(client):
            raise SystemExit("still in bootloader after install — recovery FAILED")
        chars = {c.uuid for s in client.services for c in s.characteristics}
        if AES_CHAR not in chars:
            raise SystemExit("reconnected but no AES characteristic — check manually")
        print("back in normal mode (AES characteristic present). "
              "Verify it waters before trusting it.")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--adapter", help="BlueZ adapter, e.g. hci1 (default: system default)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan")
    p.add_argument("--seconds", type=float, default=10.0)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("probe")
    p.add_argument("address")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("fetch", help="password from $BHYVE_PASSWORD or prompt")
    p.add_argument("--email", required=True)
    p.add_argument("--hardware", default=fw.FIRMWARE_HARDWARE_VERSION["HT25-0000"])
    p.add_argument("--out", default="ht25-firmware.bin")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("flash")
    p.add_argument("address")
    p.add_argument("--image", required=True)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--allow-unknown-image", action="store_true")
    p.add_argument("--no-install", action="store_true", help="upload + validate only")
    p.set_defaults(func=cmd_flash)

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
