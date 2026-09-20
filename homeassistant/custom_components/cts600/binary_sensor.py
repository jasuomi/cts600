"""Binary sensors: status LED, idle-screen flags, bus and operation state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Cts600ConfigEntry
from .coordinator import Cts600Coordinator
from .entity import Cts600Entity


@dataclass(frozen=True, kw_only=True)
class Cts600BinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Cts600Coordinator, dict[str, Any]], bool | None]
    needs_bus: bool = True  # unavailable while the bus is silent


def _led(key: str) -> Callable[[Cts600Coordinator, dict[str, Any]], bool | None]:
    return lambda c, s: s["led"][key] if s.get("led") else None


def _flag(key: str) -> Callable[[Cts600Coordinator, dict[str, Any]], bool | None]:
    return lambda c, s: s["panel"][key] if s.get("panel") else None


DESCRIPTIONS: tuple[Cts600BinaryDescription, ...] = (
    # Status LED: on while the compressor runs, blinking on an alarm.
    Cts600BinaryDescription(
        key="compressor", translation_key="compressor",
        device_class=BinarySensorDeviceClass.RUNNING, value_fn=_led("on"),
    ),
    Cts600BinaryDescription(
        key="alarm", translation_key="alarm",
        device_class=BinarySensorDeviceClass.PROBLEM, value_fn=_led("blink"),
    ),
    Cts600BinaryDescription(
        key="water_heating", translation_key="water_heating",
        device_class=BinarySensorDeviceClass.RUNNING, value_fn=_flag("water_heating"),
    ),
    Cts600BinaryDescription(
        key="boost", translation_key="boost", value_fn=_flag("boost"),
    ),
    Cts600BinaryDescription(
        key="bus_problem", translation_key="bus_problem",
        device_class=BinarySensorDeviceClass.PROBLEM, entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, s: bool(s.get("bus_at_risk")) or not c.bus_live, needs_bus=False,
    ),
    Cts600BinaryDescription(
        key="operation_running", translation_key="operation_running",
        device_class=BinarySensorDeviceClass.RUNNING, entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, s: c.operation_running, needs_bus=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: Cts600ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(Cts600BinarySensor(entry.runtime_data, d) for d in DESCRIPTIONS)


class Cts600BinarySensor(Cts600Entity, BinarySensorEntity):
    entity_description: Cts600BinaryDescription

    def __init__(self, coordinator: Cts600Coordinator, description: Cts600BinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self.entity_description.needs_bus and not self.coordinator.bus_live:
            return False
        return self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator, self.status)
