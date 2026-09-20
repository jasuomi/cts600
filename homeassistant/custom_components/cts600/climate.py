"""Climate entity: the idle screen's mode, setpoint and fan speed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Cts600ConfigEntry
from .entity import Cts600Entity

HVAC_MODES = {"off": HVACMode.OFF, "auto": HVACMode.AUTO, "cool": HVACMode.COOL, "heat": HVACMode.HEAT}
PANEL_MODES = {v: k for k, v in HVAC_MODES.items()}
FAN_MODES = ["1", "2", "3", "4"]

# The NYKYTILA ("current state") reading, e.g. "JÄÄH+VES", says what AUTO
# is doing. Only trusted while fresh.
STATUS_READING_MAX_AGE = 3600


async def async_setup_entry(
    hass: HomeAssistant, entry: Cts600ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([Cts600Climate(entry.runtime_data)])


class Cts600Climate(Cts600Entity, ClimateEntity):
    _attr_name = None
    _attr_translation_key = "cts600"
    _attr_hvac_modes = list(HVAC_MODES.values())
    _attr_fan_modes = FAN_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_precision = 0.1  # the live room register value has tenths; setpoints stay whole
    _attr_min_temp = 5
    _attr_max_temp = 30

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "climate")

    @property
    def available(self) -> bool:
        return super().available and self.panel is not None and self.coordinator.bus_live

    @property
    def hvac_mode(self) -> HVACMode | None:
        return HVAC_MODES.get(self.panel["mode"]) if self.panel else None

    @property
    def target_temperature(self) -> float | None:
        return self.panel.get("setpoint") if self.panel else None

    @property
    def fan_mode(self) -> str | None:
        fan = self.panel.get("fan") if self.panel else None
        return str(fan) if fan is not None else None

    @property
    def current_temperature(self) -> float | None:
        live = (self.status.get("sensors") or {}).get("room") or {}
        if live.get("status") == "ok" and live.get("value") is not None:
            return live["value"]
        room = self.reading("room")
        return room.get("number") if room else None

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self.panel.get("mode") if self.panel else None
        if mode == "off":
            return HVACAction.OFF
        if self.led is None:
            return None
        if not self.led["on"]:  # status LED = compressor running
            return HVACAction.IDLE
        if mode == "heat":
            return HVACAction.HEATING
        if mode == "cool":
            return HVACAction.COOLING
        state = self.reading("status")
        age = self.coordinator.age_seconds(state["at"]) if state else None
        if age is not None and age < STATUS_READING_MAX_AGE:
            text = state.get("value") or ""
            if "JÄÄH" in text or "VIIL" in text:
                return HVACAction.COOLING
            if "LÄM" in text:
                return HVACAction.HEATING
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        panel = self.panel or {}
        room = self.reading("room")
        edit = self.status.get("edit") or {}
        return {
            "water_heating": panel.get("water_heating"),
            "boost": panel.get("boost"),
            "panel_mode_text": panel.get("mode_text"),
            "room_temperature_read_at": self.coordinator.local_time(room["at"]) if room else None,
            "settings_change_running": bool(edit.get("running")),
            "settings_change_message": edit.get("message"),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        temperature = kwargs.get(ATTR_TEMPERATURE)
        await self._call(
            self.coordinator.client.settings,
            mode=PANEL_MODES[hvac_mode] if hvac_mode is not None else None,
            setpoint=round(temperature) if temperature is not None else None,
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._call(self.coordinator.client.settings, mode=PANEL_MODES[hvac_mode])

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._call(self.coordinator.client.settings, fan=int(fan_mode))

    async def async_turn_on(self) -> None:
        await self._call(self.coordinator.client.settings, mode="on")

    async def async_turn_off(self) -> None:
        await self._call(self.coordinator.client.settings, mode="off")
