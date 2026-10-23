"""Pair Home Assistant with SuperMealPlanner as one person, with a code from the bot's /pair."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import ChefError, pair
from .const import CONF_CODE, CONF_PERSON, DOMAIN


class ChefConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per person: the code from their private chat with the bot says who they are."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the server's address and a pairing code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            try:
                got = await pair(
                    async_get_clientsession(self.hass), url, user_input[CONF_CODE], "Home Assistant"
                )
            except ChefError as err:
                errors["base"] = "invalid_code" if err.status in (401, 429) else "cannot_connect"
            else:
                await self.async_set_unique_id(f"{url}#{got['device_id']}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=got.get("person_name") or got.get("person") or "Chef",
                    data={CONF_URL: url, CONF_TOKEN: got["token"], CONF_PERSON: got.get("person")},
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=(user_input or {}).get(CONF_URL, "http://")): str,
                vol.Required(CONF_CODE): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
