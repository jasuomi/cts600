"""Sensors: the NÄYTÄ DATA readings, plus operation diagnostics.

The six temperatures and the operating state. The fan levels, software
versions and unit type were dropped on 2026-09-17 (user request): they have
no register, so they could only ever repeat the last walk's value.

Readings only change when a data screen is on the panel's display (someone
walks the menu, or the Update readings button / scheduled update runs), so
each carries its read time. With the "max age" option set, older readings
become unavailable instead of repeating a stale value.

Temperatures come live from the controller's input registers instead
whenever the service reports that register value as "ok" (anchored to the
panel and tracked since; see the service's sensor_regs.py). Otherwise they
fall back to the panel reading.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Cts600ConfigEntry
from .const import CONF_MAX_AGE_MINUTES
from .coordinator import OPERATIONS, Cts600Coordinator
from .entity import Cts600Entity


@dataclass(frozen=True, kw_only=True)
class ReadingDescription(SensorEntityDescription):
    field: str = "number"  # "number" or "value" (display text)
    register: bool = False  # the service may have a live register value for it


def _temperature(key: str) -> ReadingDescription:
    return ReadingDescription(
        key=key, translation_key=key,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        register=True,
    )


READINGS: tuple[ReadingDescription, ...] = (
    _temperature("room"),         # T15
    _temperature("outdoor"),      # T1
    _temperature("tank_top"),     # T11
    _temperature("tank_bottom"),  # T12
    _temperature("supply"),       # T14, heating supply
    _temperature("condenser"),    # T5
    ReadingDescription(key="status", translation_key="status", field="value"),
)


@dataclass(frozen=True, kw_only=True)
class StatusDescription(SensorEntityDescription):
    value_fn: Callable[[Cts600Coordinator, dict[str, Any]], Any]
    attrs_fn: Callable[[Cts600Coordinator, dict[str, Any]], dict[str, Any]] = lambda c, s: {}


def _last_operation(status: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    finished = [
        (kind, status[kind]) for kind in OPERATIONS
        if (status.get(kind) or {}).get("finished_at") is not None
    ]
    return max(finished, key=lambda item: item[1]["finished_at"], default=None)


def _readings_updated(c: Cts600Coordinator, status: dict[str, Any]) -> datetime | None:
    times = [r["at"] for r in (status.get("readings") or {}).values() if r.get("at")]
    return c.local_time(max(times)) if times else None


def _last_operation_attrs(c: Cts600Coordinator, status: dict[str, Any]) -> dict[str, Any]:
    last = _last_operation(status)
    if last is None:
        return {}
    kind, op = last
    return {
        "operation": kind, "message": op.get("message"),
        "finished_at": c.local_time(op.get("finished_at")),
    }


STATUS_SENSORS: tuple[StatusDescription, ...] = (
    StatusDescription(
        key="readings_updated", translation_key="readings_updated",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_readings_updated,
    ),
    StatusDescription(
        key="last_operation", translation_key="last_operation",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM, options=["done", "cancelled", "stopped"],
        value_fn=lambda c, s: (_last_operation(s) or (None, {}))[1].get("outcome"),
        attrs_fn=_last_operation_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: Cts600ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    max_age = entry.options.get(CONF_MAX_AGE_MINUTES, 0) * 60
    async_add_entities([
        *(ReadingSensor(coordinator, d, max_age) for d in READINGS),
        *(StatusSensor(coordinator, d) for d in STATUS_SENSORS),
    ])


class ReadingSensor(Cts600Entity, SensorEntity):
    entity_description: ReadingDescription

    def __init__(self, coordinator: Cts600Coordinator, description: ReadingDescription, max_age: float) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._max_age = max_age

    @property
    def _reading(self) -> dict[str, Any] | None:
        return self.reading(self.entity_description.key)

    @property
    def _register(self) -> dict[str, Any] | None:
        """The live register value, when the service trusts it."""
        if not self.entity_description.register:
            return None
        sensor = (self.status.get("sensors") or {}).get(self.entity_description.key)
        if sensor and sensor.get("status") == "ok" and sensor.get("value") is not None:
            return sensor
        return None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._register is not None:
            return True
        reading = self._reading
        if reading is None:
            return False
        if self._max_age:
            age = self.coordinator.age_seconds(reading.get("at"))
            return age is not None and age <= self._max_age
        return True

    @property
    def native_value(self) -> Any:
        register = self._register
        if register is not None:
            return register["value"]
        reading = self._reading
        return reading.get(self.entity_description.field) if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        reading = self._reading
        attrs: dict[str, Any] = {}
        if reading is not None:
            attrs = {"read_at": self.coordinator.local_time(reading.get("at")), "display_text": reading.get("text")}
        if self.entity_description.register:
            sensor = (self.status.get("sensors") or {}).get(self.entity_description.key) or {}
            attrs["source"] = "register" if self._register is not None else "display"
            attrs["register_status"] = sensor.get("status")
        return attrs or None


class StatusSensor(Cts600Entity, SensorEntity):
    entity_description: StatusDescription

    def __init__(self, coordinator: Cts600Coordinator, description: StatusDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator, self.status)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attrs_fn(self.coordinator, self.status)
