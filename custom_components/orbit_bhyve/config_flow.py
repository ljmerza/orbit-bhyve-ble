"""Config flow for the Orbit B-Hyve BLE integration.

Setup starts with a menu:
  - Orbit account: email + password → cloud login + device discovery, then a
    device picker — uncheck any to exclude.
  - No cloud: paste an `export_devices` response, or enter one timer by hand.
    The entry is marked CONF_LOCAL and never talks to the cloud.

Reconfigure flow (⋮ → Reconfigure) re-runs discovery against the same account
and re-shows the picker, so a device bought after setup can be added without
deleting the entry — entry.data[CONF_DEVICES] is otherwise a frozen snapshot
that async_setup_entry never refreshes. On a no-cloud entry it instead offers
adding/editing a timer by hand, importing more, or picking which to keep.

Reauth flow re-prompts only the password.
Options flow exposes polling intervals + idle disconnect.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .cloud import CloudAuthError, CloudConnectionError, CloudKeyNotFound, OrbitCloudClient
from .const import (
    CONF_DEFAULT_DURATION,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_FLOW_COUNTS_PER_GALLON,
    CONF_HUB_MESH_OVERRIDES,
    CONF_MESH_STATUS_POLL,
    CONF_IDLE_DISCONNECT,
    CONF_INCLUDE,
    CONF_IMPORT,
    CONF_KNOWN_CLOUD_IDS,
    CONF_LOCAL,
    CONF_PASSWORD,
    CONF_POLL_IDLE,
    CONF_POLL_WATERING,
    DEFAULT_DURATION,
    DEFAULT_FLOW_COUNTS_PER_GALLON,
    DEFAULT_HUB_MESH_OVERRIDES,
    DEFAULT_MESH_STATUS_POLL,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_POLL_IDLE,
    DEFAULT_POLL_WATERING,
    DOMAIN,
)
from .local_setup import (
    KNOWN_HARDWARE,
    LocalRecordError,
    local_unique_id,
    merge_local,
    normalize_record,
    records_from_import,
)
from .merge import default_selection, merge_device_lists, next_known_ids

_LOGGER = logging.getLogger(__name__)


class BHyveConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._email: str | None = None
        self._password: str | None = None
        self._discovered: list[dict[str, Any]] = []
        # Reconfigure-only state.
        self._stored: list[dict[str, Any]] = []      # entry.data[CONF_DEVICES]
        self._catalogue: list[dict[str, Any]] = []   # discovered + stored-only survivors
        self._default_ids: list[str] = []            # picker pre-check set

    async def _async_discover(self) -> str | None:
        """Run cloud discovery into self._discovered.

        Returns a strings.json error key, or None on success. Shared by the
        setup, reconfigure and reconfigure-auth steps so all three classify
        cloud failures identically.
        """
        client = OrbitCloudClient(async_get_clientsession(self.hass))
        try:
            self._discovered = await client.discover(self._email, self._password)
        except CloudAuthError:
            return "invalid_auth"
        except CloudConnectionError:
            return "cannot_connect"
        except CloudKeyNotFound as err:
            _LOGGER.error("Network key fetch failed: %s", err)
            return "unknown"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during discovery")
            return "unknown"
        return None if self._discovered else "no_devices"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """First screen: the Orbit account, or one of the two no-cloud paths."""
        return self.async_show_menu(
            step_id="user", menu_options=["cloud", "local_import", "local_manual"]
        )

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(self._email.lower())
            self._abort_if_unique_id_configured()

            error = await self._async_discover()
            if error is None:
                return await self.async_step_pick_devices()
            errors["base"] = error

        schema = vol.Schema({
            vol.Required(CONF_EMAIL, default=self._email or ""): str,
            vol.Required(CONF_PASSWORD): str,
        })
        return self.async_show_form(step_id="cloud", data_schema=schema, errors=errors)

    # --- no-cloud setup ------------------------------------------------------

    async def async_step_local_import(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            try:
                records = records_from_import(user_input[CONF_IMPORT])
                self._check_not_configured(records)
            except LocalRecordError as err:
                errors["base"], detail = err.key, err.detail
            else:
                return await self._async_create_local_entry(records)
        return self._local_import_form("local_import", errors, detail)

    async def async_step_local_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            try:
                records = [normalize_record(dict(user_input))]
                self._check_not_configured(records)
            except LocalRecordError as err:
                errors["base"], detail = err.key, err.detail
            else:
                return await self._async_create_local_entry(records)
        return self._local_manual_form("local_manual", errors, detail, user_input)

    async def _async_create_local_entry(self, records: list[dict[str, Any]]) -> ConfigFlowResult:
        await self.async_set_unique_id(local_unique_id(records))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="B-Hyve (no cloud)",
            data={CONF_LOCAL: True, CONF_DEVICES: records},
            options=_default_options(),
        )

    def _check_not_configured(
        self, records: list[dict[str, Any]], own_entry_id: str | None = None
    ) -> None:
        """Refuse a MAC another entry already controls — two entries driving the
        same timer would fight over its single BLE connection."""
        taken = {
            r.get("mac")
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != own_entry_id
            for r in e.data.get(CONF_DEVICES, [])
        }
        for r in records:
            if r["mac"] in taken:
                raise LocalRecordError("mac_already_configured", r["mac"])

    def _local_import_form(self, step_id: str, errors: dict[str, str], detail: str) -> ConfigFlowResult:
        schema = vol.Schema({
            vol.Required(CONF_IMPORT): TextSelector(TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(
            step_id=step_id, data_schema=schema, errors=errors,
            description_placeholders={"detail": detail},
        )

    def _local_manual_form(
        self,
        step_id: str,
        errors: dict[str, str],
        detail: str,
        user_input: dict[str, Any] | None,
        default_key: str = "",
    ) -> ConfigFlowResult:
        u = user_input or {}
        schema = vol.Schema({
            vol.Required("name", default=u.get("name", "")): str,
            vol.Required("mac", default=u.get("mac", "")): str,
            vol.Required("hardware", default=u.get("hardware", KNOWN_HARDWARE[0])): SelectSelector(
                SelectSelectorConfig(
                    options=KNOWN_HARDWARE,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional("firmware", default=u.get("firmware", "")): str,
            vol.Required("stations", default=u.get("stations", 1)):
                vol.All(vol.Coerce(int), vol.Range(min=1, max=16)),
            vol.Required("network_key", default=u.get("network_key", default_key)): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Optional("mesh_device_id", default=u.get("mesh_device_id", "")): str,
            vol.Optional("hub_mesh_device_id", default=u.get("hub_mesh_device_id", "")): str,
        })
        return self.async_show_form(
            step_id=step_id, data_schema=schema, errors=errors,
            description_placeholders={"detail": detail},
        )

    def _picker_form(
        self, step_id: str, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Shared device-picker form for the setup and reconfigure paths."""
        stored_ids = {r["cloud_id"] for r in self._stored}
        discovered_ids = {r["cloud_id"] for r in self._discovered}

        options_list = [
            SelectOptionDict(
                value=d["cloud_id"],
                label=f"{d['name']} ({d['hardware']} fw{d['firmware']})"
                + (
                    ""
                    if d["cloud_id"] in discovered_ids
                    else "  —  no longer on the Orbit account"
                ),
            )
            for d in self._catalogue
        ]
        schema = vol.Schema({
            vol.Required(CONF_INCLUDE, default=self._default_ids): SelectSelector(
                SelectSelectorConfig(
                    options=options_list,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "count": str(len(self._catalogue)),
                "new": str(len([c for c in self._default_ids if c not in stored_ids])),
                "missing": str(
                    len([r for r in self._catalogue if r["cloud_id"] not in discovered_ids])
                ),
            },
        )

    async def async_step_pick_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._catalogue = list(self._discovered)
        self._default_ids = [d["cloud_id"] for d in self._discovered]

        if user_input is not None:
            included = set(user_input[CONF_INCLUDE] or [])
            if not included:
                # vol.Required accepts [] on a multi-select, and an empty device
                # list makes async_setup_entry return False — the entry would be
                # bricked with nothing but a log warning to explain it.
                return self._picker_form("pick_devices", {"base": "no_devices_selected"})
            kept = [d for d in self._discovered if d["cloud_id"] in included]
            return self.async_create_entry(
                title=self._email or "B-Hyve",
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_DEVICES: kept,
                    CONF_KNOWN_CLOUD_IDS: next_known_ids(None, [], self._default_ids),
                },
                options={
                    CONF_DEFAULT_DURATION: DEFAULT_DURATION,
                    CONF_IDLE_DISCONNECT: DEFAULT_IDLE_DISCONNECT,
                    CONF_POLL_IDLE: DEFAULT_POLL_IDLE,
                    CONF_POLL_WATERING: DEFAULT_POLL_WATERING,
                    CONF_FLOW_COUNTS_PER_GALLON: DEFAULT_FLOW_COUNTS_PER_GALLON,
                    CONF_MESH_STATUS_POLL: DEFAULT_MESH_STATUS_POLL,
                },
            )

        return self._picker_form("pick_devices")

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """⋮ → Reconfigure: re-run cloud discovery and re-show the device picker.

        Saved credentials are reused silently; only a CloudAuthError drops the
        user to a password prompt.
        """
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_LOCAL):
            return self.async_show_menu(
                step_id="reconfigure",
                menu_options=["reconfigure_manual", "reconfigure_import", "reconfigure_keep"],
            )
        self._email = entry.data.get(CONF_EMAIL)
        self._password = entry.data.get(CONF_PASSWORD)
        self._stored = list(entry.data.get(CONF_DEVICES, []))

        if not (self._email and self._password):
            return await self.async_step_reconfigure_auth()

        error = await self._async_discover()
        if error == "invalid_auth":
            return await self.async_step_reconfigure_auth()
        if error is not None:
            # No form is on screen yet, so there's nowhere to render an inline
            # error — abort with a reason the user can read, entry untouched.
            return self.async_abort(reason=error)
        return await self.async_step_reconfigure_devices()

    async def async_step_reconfigure_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Password re-prompt when the saved credential no longer authenticates.

        The password captured here is written back to entry.data by the finish
        path, so a reconfigure doubles as a reauth.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            error = await self._async_discover()
            if error is None:
                return await self.async_step_reconfigure_devices()
            errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure_auth",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )

    async def async_step_reconfigure_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        stored_ids = [r["cloud_id"] for r in self._stored]
        discovered_ids = {r["cloud_id"] for r in self._discovered}

        # Catalogue = fresh discovery, plus still-configured devices the cloud no
        # longer returns. Keeping the vanished ones visible and pre-checked is
        # deliberate: discover() silently skips bridges and anything without a
        # mesh_id, so a partial response must not delete a working BLE device.
        self._catalogue = list(self._discovered) + [
            r for r in self._stored if r["cloud_id"] not in discovered_ids
        ]
        known = entry.data.get(CONF_KNOWN_CLOUD_IDS)  # None => pre-reconfigure entry
        catalogue_ids = [r["cloud_id"] for r in self._catalogue]
        self._default_ids = default_selection(stored_ids, known, catalogue_ids)

        if user_input is not None:
            included = set(user_input[CONF_INCLUDE] or [])
            if not included:
                return self._picker_form(
                    "reconfigure_devices", {"base": "no_devices_selected"}
                )
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_PASSWORD: self._password,  # picked up if reconfigure_auth ran
                    CONF_DEVICES: merge_device_lists(
                        self._stored, self._discovered, included
                    ),
                    CONF_KNOWN_CLOUD_IDS: next_known_ids(
                        known, stored_ids, catalogue_ids
                    ),
                },
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reconfigure_successful")

        return self._picker_form("reconfigure_devices")

    # --- no-cloud reconfigure -------------------------------------------------

    async def async_step_reconfigure_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        stored = list(entry.data.get(CONF_DEVICES, []))
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            try:
                records = [normalize_record(dict(user_input))]
                self._check_not_configured(records, entry.entry_id)
            except LocalRecordError as err:
                errors["base"], detail = err.key, err.detail
            else:
                return await self._async_update_local_entry(entry, merge_local(stored, records))
        # Prefill the key when the entry has exactly one — the usual case, since
        # a key is per Orbit mesh, and it saves pasting it again.
        keys = {r.get("network_key") for r in stored}
        default_key = next(iter(keys)) if len(keys) == 1 else ""
        return self._local_manual_form(
            "reconfigure_manual", errors, detail, user_input, default_key
        )

    async def async_step_reconfigure_import(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            try:
                records = records_from_import(user_input[CONF_IMPORT])
                self._check_not_configured(records, entry.entry_id)
            except LocalRecordError as err:
                errors["base"], detail = err.key, err.detail
            else:
                stored = list(entry.data.get(CONF_DEVICES, []))
                return await self._async_update_local_entry(entry, merge_local(stored, records))
        return self._local_import_form("reconfigure_import", errors, detail)

    async def async_step_reconfigure_keep(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        stored = list(entry.data.get(CONF_DEVICES, []))
        errors: dict[str, str] = {}
        if user_input is not None:
            included = set(user_input[CONF_INCLUDE] or [])
            if included:
                return await self._async_update_local_entry(
                    entry, [r for r in stored if r["cloud_id"] in included]
                )
            errors["base"] = "no_devices_selected"

        schema = vol.Schema({
            vol.Required(CONF_INCLUDE, default=[r["cloud_id"] for r in stored]): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=r["cloud_id"],
                            label=f"{r['name']} ({r['hardware']}, {r['mac']})",
                        )
                        for r in stored
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(
            step_id="reconfigure_keep", data_schema=schema, errors=errors
        )

    async def _async_update_local_entry(
        self, entry: ConfigEntry, devices: list[dict[str, Any]]
    ) -> ConfigFlowResult:
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: devices}
        )
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reconfigure_successful")

    def _get_reconfigure_entry(self) -> ConfigEntry:
        # Deliberately shadows HA >= 2024.11's ConfigFlow._get_reconfigure_entry
        # so this flow still works on the 2024.6 floor declared in hacs.json —
        # same trick as _get_reauth_entry below.
        return self.hass.config_entries.async_get_entry(self.context["entry_id"])

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._email = entry_data.get(CONF_EMAIL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            password = user_input[CONF_PASSWORD]
            client = OrbitCloudClient(async_get_clientsession(self.hass))
            try:
                await client.login(self._email or entry.data[CONF_EMAIL], password)
            except CloudAuthError:
                errors["base"] = "invalid_auth"
            except CloudConnectionError:
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_PASSWORD: password},
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    def _get_reauth_entry(self) -> ConfigEntry:
        return self.hass.config_entries.async_get_entry(self.context["entry_id"])

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "BHyveOptionsFlow":
        return BHyveOptionsFlow(entry)


def _default_options() -> dict[str, Any]:
    """Options a no-cloud entry starts with — the same defaults the cloud
    setup writes in async_step_pick_devices."""
    return {
        CONF_DEFAULT_DURATION: DEFAULT_DURATION,
        CONF_IDLE_DISCONNECT: DEFAULT_IDLE_DISCONNECT,
        CONF_POLL_IDLE: DEFAULT_POLL_IDLE,
        CONF_POLL_WATERING: DEFAULT_POLL_WATERING,
        CONF_FLOW_COUNTS_PER_GALLON: DEFAULT_FLOW_COUNTS_PER_GALLON,
        CONF_MESH_STATUS_POLL: DEFAULT_MESH_STATUS_POLL,
    }


class BHyveOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: ConfigEntry):
        self.entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        opts = self.entry.options
        schema = vol.Schema({
            vol.Required(CONF_DEFAULT_DURATION,
                         default=opts.get(CONF_DEFAULT_DURATION, DEFAULT_DURATION)):
                vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required(CONF_IDLE_DISCONNECT,
                         default=opts.get(CONF_IDLE_DISCONNECT, DEFAULT_IDLE_DISCONNECT)):
                vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
            vol.Required(CONF_POLL_IDLE,
                         default=opts.get(CONF_POLL_IDLE, DEFAULT_POLL_IDLE)):
                vol.All(vol.Coerce(int), vol.Range(min=10, max=86400)),
            vol.Required(CONF_POLL_WATERING,
                         default=opts.get(CONF_POLL_WATERING, DEFAULT_POLL_WATERING)):
                vol.All(vol.Coerce(int), vol.Range(min=5, max=600)),
            vol.Required(CONF_FLOW_COUNTS_PER_GALLON,
                         default=opts.get(CONF_FLOW_COUNTS_PER_GALLON,
                                          DEFAULT_FLOW_COUNTS_PER_GALLON)):
                vol.All(vol.Coerce(int), vol.Range(min=1, max=100000)),
            vol.Required(CONF_MESH_STATUS_POLL,
                         default=opts.get(CONF_MESH_STATUS_POLL,
                                          DEFAULT_MESH_STATUS_POLL)):
                bool,
            vol.Optional(CONF_HUB_MESH_OVERRIDES,
                         default=opts.get(CONF_HUB_MESH_OVERRIDES,
                                          DEFAULT_HUB_MESH_OVERRIDES)):
                str,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
