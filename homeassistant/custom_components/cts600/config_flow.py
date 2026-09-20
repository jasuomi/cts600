"""Config flow: the CTS600 service's host and port, plus options."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Cts600Client, Cts600Error
from .const import (
    CONF_MAX_AGE_MINUTES,
    CONF_REFRESH_MINUTES,
    DEFAULT_PORT,
    DOMAIN,
    MIN_REFRESH_MINUTES,
)

USER_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
})


class Cts600ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST].strip(), user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            client = Cts600Client(async_get_clientsession(self.hass), host, port)
            try:
                status = await client.status()
            except Cts600Error:
                errors["base"] = "cannot_connect"
            else:
                if "panel" not in status:
                    errors["base"] = "not_cts600"
                else:
                    return self.async_create_entry(
                        title="Nilan CTS600", data={CONF_HOST: host, CONF_PORT: port},
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return Cts600OptionsFlow()


class Cts600OptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            values = {k: int(v) for k, v in user_input.items()}
            if 0 < values[CONF_REFRESH_MINUTES] < MIN_REFRESH_MINUTES:
                errors[CONF_REFRESH_MINUTES] = "refresh_too_often"
            else:
                return self.async_create_entry(data=values)

        minutes = selector.NumberSelector(selector.NumberSelectorConfig(
            min=0, max=1440, step=1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX,
        ))
        schema = vol.Schema({
            vol.Required(CONF_REFRESH_MINUTES, default=0): minutes,
            vol.Required(CONF_MAX_AGE_MINUTES, default=0): minutes,
        })
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or self.config_entry.options),
            errors=errors,
            description_placeholders={"min_refresh": str(MIN_REFRESH_MINUTES)},
        )
