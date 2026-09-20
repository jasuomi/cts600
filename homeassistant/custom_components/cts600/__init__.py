"""Nilan CTS600 panel bridge.

Connects to the CTS600 dashboard service (python -m cts600), which sits on
the RS485 bus next to the original control panel. The service does all bus
access and safety checks; this integration shows its status and asks it to
change settings.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Cts600Client
from .coordinator import Cts600Coordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.CLIMATE, Platform.SENSOR]

type Cts600ConfigEntry = ConfigEntry[Cts600Coordinator]


async def async_setup_entry(hass: HomeAssistant, entry: Cts600ConfigEntry) -> bool:
    client = Cts600Client(async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT])
    coordinator = Cts600Coordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Cts600ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: Cts600ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
