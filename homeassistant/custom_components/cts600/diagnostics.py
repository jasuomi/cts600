"""Diagnostics: the entry and the service's last status."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import Cts600ConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: Cts600ConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "data": dict(entry.data),
        "options": dict(entry.options),
        "clock_offset_s": coordinator.clock_offset,
        "bus_live": coordinator.bus_live,
        "status": coordinator.data,
    }
