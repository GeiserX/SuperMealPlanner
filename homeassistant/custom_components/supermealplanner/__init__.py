"""Talk to the SuperMealPlanner chef through Home Assistant's Assist.

Each config entry is one paired device, so one person: its conversation agent says what it hears to
the chef as that person, and its speech-to-text uses the server's own transcriber. A pipeline per
person (speech to text and agent from that person's entry) is what a phone's "Assist" opens.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import Chef, ChefError

PLATFORMS = [Platform.CONVERSATION, Platform.STT]

type ChefConfigEntry = ConfigEntry[Chef]


async def async_setup_entry(hass: HomeAssistant, entry: ChefConfigEntry) -> bool:
    """Check the server answers, then set up the agent and the speech to text."""
    chef = Chef(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    try:
        await chef.ping()
    except ChefError as err:
        if err.status == 401:
            raise ConfigEntryAuthFailed(str(err)) from err
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = chef
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ChefConfigEntry) -> bool:
    """Take the agent and the speech to text away."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
