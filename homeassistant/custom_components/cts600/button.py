"""Buttons: start a readings update, and the six raw panel keys.

The raw keys are disabled by default: one press moves the real panel's
menu, and nothing checks where it lands. Use the climate entity for
settings.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Cts600ConfigEntry
from .const import PANEL_KEYS
from .coordinator import Cts600Coordinator
from .entity import Cts600Entity


async def async_setup_entry(
    hass: HomeAssistant, entry: Cts600ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([
        UpdateReadingsButton(coordinator),
        CancelUpdateButton(coordinator),
        *(PanelKeyButton(coordinator, key) for key in PANEL_KEYS),
    ])


class UpdateReadingsButton(Cts600Entity, ButtonEntity):
    _attr_translation_key = "update_readings"

    def __init__(self, coordinator: Cts600Coordinator) -> None:
        super().__init__(coordinator, "update_readings")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.control_enabled

    async def async_press(self) -> None:
        await self._call(self.coordinator.client.refresh)


class CancelUpdateButton(Cts600Entity, ButtonEntity):
    _attr_translation_key = "cancel_update"

    def __init__(self, coordinator: Cts600Coordinator) -> None:
        super().__init__(coordinator, "cancel_update")

    @property
    def available(self) -> bool:
        walk = self.status.get("walk") or {}
        return super().available and self.coordinator.control_enabled and bool(walk.get("running"))

    async def async_press(self) -> None:
        await self._call(self.coordinator.client.cancel_refresh)


class PanelKeyButton(Cts600Entity, ButtonEntity):
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: Cts600Coordinator, key: str) -> None:
        super().__init__(coordinator, f"key_{key}")
        self._key = key
        self._attr_translation_key = f"key_{key}"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.control_enabled

    async def async_press(self) -> None:
        await self._call(self.coordinator.client.press, key=self._key)
