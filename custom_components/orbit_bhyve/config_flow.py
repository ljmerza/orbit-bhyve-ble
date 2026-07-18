"""Config flow for the Orbit B-Hyve BLE integration.

Two-step setup:
  1. Email + password → cloud login + device discovery.
  2. Device picker — uncheck any to exclude.

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
)

from .cloud import CloudAuthError, CloudConnectionError, CloudKeyNotFound, OrbitCloudClient
from .const import (
    CONF_DEFAULT_DURATION,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_FLOW_COUNTS_PER_GALLON,
    CONF_MESH_STATUS_POLL,
    CONF_IDLE_DISCONNECT,
    CONF_INCLUDE,
    CONF_PASSWORD,
    CONF_POLL_IDLE,
    CONF_POLL_WATERING,
    DEFAULT_DURATION,
    DEFAULT_FLOW_COUNTS_PER_GALLON,
    DEFAULT_MESH_STATUS_POLL,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_POLL_IDLE,
    DEFAULT_POLL_WATERING,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class BHyveConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._email: str | None = None
        self._password: str | None = None
        self._discovered: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(self._email.lower())
            self._abort_if_unique_id_configured()

            client = OrbitCloudClient(async_get_clientsession(self.hass))
            try:
                self._discovered = await client.discover(self._email, self._password)
            except CloudAuthError:
                errors["base"] = "invalid_auth"
            except CloudConnectionError:
                errors["base"] = "cannot_connect"
            except CloudKeyNotFound as err:
                _LOGGER.error("Network key fetch failed: %s", err)
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during discovery")
                errors["base"] = "unknown"
            else:
                if not self._discovered:
                    errors["base"] = "no_devices"
                else:
                    return await self.async_step_pick_devices()

        schema = vol.Schema({
            vol.Required(CONF_EMAIL, default=self._email or ""): str,
            vol.Required(CONF_PASSWORD): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_pick_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            included = set(user_input[CONF_INCLUDE] or [])
            kept = [d for d in self._discovered if d["cloud_id"] in included]
            return self.async_create_entry(
                title=self._email or "B-Hyve",
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_DEVICES: kept,
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

        options_list = [
            SelectOptionDict(
                value=d["cloud_id"],
                label=f"{d['name']} ({d['hardware']} fw{d['firmware']})",
            )
            for d in self._discovered
        ]
        defaults = [d["cloud_id"] for d in self._discovered]

        schema = vol.Schema({
            vol.Required(CONF_INCLUDE, default=defaults): SelectSelector(
                SelectSelectorConfig(
                    options=options_list,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(
            step_id="pick_devices",
            data_schema=schema,
            description_placeholders={"count": str(len(self._discovered))},
        )

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
        })
        return self.async_show_form(step_id="init", data_schema=schema)
