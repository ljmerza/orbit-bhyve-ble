"""Orbit B-Hyve cloud HTTP client.

Setup-time only. No background polling. See PLAN.md §2 + §4 for the rule.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import aiohttp

from .const import (
    CLOUD_API_BASE,
    CLOUD_APP_ID,
    CLOUD_KEY_FIELDS,
    CLOUD_KEY_PATHS,
    CLOUD_USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class CloudAuthError(Exception):
    """Bad credentials, or session expired."""


class CloudConnectionError(Exception):
    """Network failure reaching api.orbitbhyve.com."""


class CloudKeyNotFound(Exception):
    """No candidate path returned a usable network_key."""


class OrbitCloudClient:
    """Minimal async wrapper around the Orbit cloud endpoints we use."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._token: str | None = None
        self._user_id: str | None = None

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def user_id(self) -> str | None:
        return self._user_id

    def _headers(self, *, include_auth: bool = True) -> dict[str, str]:
        # Orbit's WAF returns 403 to requests whose User-Agent contains
        # "HomeAssistant" — which is exactly what HA's shared aiohttp session
        # (async_get_clientsession) stamps on every request. Without an
        # override, every cloud call here is forbidden and config_flow
        # mislabels it "invalid email or password". Send a browser User-Agent
        # (matching the wider B-Hyve HA ecosystem's fix) so setup works.
        h = {
            "orbit-app-id": CLOUD_APP_ID,
            "User-Agent": CLOUD_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        if include_auth and self._token:
            h["orbit-api-key"] = self._token
        return h

    async def login(self, email: str, password: str) -> dict[str, Any]:
        url = f"{CLOUD_API_BASE}/session"
        try:
            async with self._session.post(
                url,
                json={"session": {"email": email, "password": password}},
                headers={**self._headers(include_auth=False), "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status in (400, 401, 403):
                    raise CloudAuthError(f"login rejected (HTTP {resp.status})")
                resp.raise_for_status()
                body = await resp.json()
        except aiohttp.ClientError as err:
            raise CloudConnectionError(str(err)) from err

        self._token = body.get("orbit_api_key")
        self._user_id = body.get("user_id")
        if not self._token:
            raise CloudAuthError("login succeeded but no orbit_api_key in response")
        return body

    async def list_devices(self) -> list[dict[str, Any]]:
        url = f"{CLOUD_API_BASE}/devices"
        try:
            async with self._session.get(
                url,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 401:
                    raise CloudAuthError("session expired")
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise CloudConnectionError(str(err)) from err

    async def get_mesh(self, mesh_id: str) -> dict[str, Any]:
        """Try /meshes/<id> first; fall back to legacy paths on 404."""
        last_status = None
        for path_tmpl in CLOUD_KEY_PATHS:
            path = path_tmpl.format(mesh_id=mesh_id)
            url = f"{CLOUD_API_BASE}{path}"
            try:
                async with self._session.get(
                    url,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 401:
                        raise CloudAuthError("session expired")
                    if resp.status == 404:
                        last_status = 404
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError as err:
                raise CloudConnectionError(str(err)) from err
        raise CloudKeyNotFound(
            f"none of {[p.format(mesh_id=mesh_id) for p in CLOUD_KEY_PATHS]} "
            f"returned 200 (last status: {last_status})"
        )

    async def discover(self, email: str, password: str) -> list[dict[str, Any]]:
        """One-shot: login, list devices, fetch keys, return joined records.

        Returns the list-of-dicts shape that gets stored in entry.data["devices"].
        """
        await self.login(email, password)
        raw_devices = await self.list_devices()

        mesh_cache: dict[str, dict[str, Any]] = {}
        joined: list[dict[str, Any]] = []
        for d in raw_devices:
            # Hubs (Wi-Fi bridges) don't actuate anything — drop them so
            # they never reach the picker, the device registry, or the
            # entity platforms.
            if (d.get("type") or "").lower() == "bridge":
                continue
            mesh_id = d.get("mesh_id") or d.get("network_topology_id")
            if not mesh_id:
                _LOGGER.warning("Skipping device %s — no mesh_id", d.get("name"))
                continue
            if mesh_id not in mesh_cache:
                mesh_cache[mesh_id] = await self.get_mesh(mesh_id)
            mesh = mesh_cache[mesh_id]
            joined.append(_join_device(d, mesh))
        return joined


def _normalize_fw(s: str | None) -> str:
    """'85' / '0041' / '0095' → '0085' / '0041' / '0095'. Inputs may be None."""
    if not s:
        return "?"
    try:
        return f"{int(s):04d}"
    except ValueError:
        return s


def _format_mac(no_colons: str | None) -> str | None:
    if not no_colons or len(no_colons) != 12:
        return None
    return ":".join(no_colons[i:i + 2] for i in range(0, 12, 2)).upper()


def _b64_to_hex(b64: str | None) -> str | None:
    if not b64:
        return None
    try:
        return base64.b64decode(b64).hex()
    except Exception:
        return None


def _key_from_mesh(mesh: dict[str, Any]) -> str | None:
    for f in CLOUD_KEY_FIELDS:
        v = mesh.get(f)
        if v:
            return _b64_to_hex(v)
    return None


def _ble_device_id_from_mesh(mesh: dict[str, Any], cloud_id: str) -> int | None:
    for member in mesh.get("devices") or []:
        if member.get("device_id") == cloud_id:
            v = member.get("ble_device_id")
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None


def _ble_device_id_from_reference(ref: str | None) -> int | None:
    if not ref or "-" not in ref:
        return None
    try:
        return int(ref.split("-", 1)[1])
    except ValueError:
        return None


def _join_device(d: dict[str, Any], mesh: dict[str, Any]) -> dict[str, Any]:
    cloud_id = d.get("id") or ""
    mac = _format_mac(d.get("mac_address"))
    ble_device_id = _ble_device_id_from_mesh(mesh, cloud_id)
    if ble_device_id is None:
        ble_device_id = _ble_device_id_from_reference(d.get("reference"))

    bridge_device_id = mesh.get("bridge_device_id")
    hub_mesh_device_id = (
        _ble_device_id_from_mesh(mesh, bridge_device_id) if bridge_device_id else None
    )

    battery = d.get("battery") or {}
    return {
        "cloud_id": cloud_id,
        "name": d.get("name") or "B-Hyve",
        "mac": mac,
        "type": d.get("type") or "unknown",
        "hardware": d.get("hardware_version") or "unknown",
        "firmware": _normalize_fw(d.get("firmware_version")),
        "stations": int(d.get("num_stations") or 0),
        "mesh_id": mesh.get("id"),
        "mesh_device_id": ble_device_id,
        "bridge_device_id": bridge_device_id,
        "hub_mesh_device_id": hub_mesh_device_id,
        "network_key": _key_from_mesh(mesh),
        "battery_pct": battery.get("percent"),
        "battery_mv": battery.get("mv"),
    }
