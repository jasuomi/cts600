"""Integration tests against a mocked CTS600 service."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.cts600.api import Cts600ConnectionError, Cts600RequestError
from custom_components.cts600.const import DOMAIN, EVENT_OPERATION_FINISHED

NOW = time.time()

STATUS = {
    "server_time": NOW,
    "version": "1.0.0",
    "control_enabled": True,
    "connected": True,
    "port": "/dev/ttyS0",
    "last_frame_at": NOW - 1,
    "screen": "idle",
    "panel": {"mode": "cool", "mode_text": "VIILEN", "setpoint": 22, "fan": 3,
              "water_heating": True, "boost": False, "at": NOW - 1},
    "led": {"on": True, "blink": False},
    "readings": {
        "room": {"value": "23°C", "number": 23.0, "text": "HUONE T15 23°C", "at": NOW - 600},
        "outdoor": {"value": "11°C", "number": 11.0, "text": "ULKOILMA T1  11°C", "at": NOW - 7200},
        "status": {"value": "JÄÄH+VES", "number": None, "text": "NYKYTILA JÄÄH+VES", "at": NOW - 600},
    },
    "walk": {"running": False},
    "edit": {"running": False},
    "device": None,
    "bus_at_risk": False,
}

CLIENT = "custom_components.cts600.api.Cts600Client"


@pytest.fixture
def client():
    listen_gate = asyncio.Event()

    async def listen(on_status):
        await listen_gate.wait()

    with (
        patch(f"{CLIENT}.status", AsyncMock(return_value=deepcopy(STATUS))) as status,
        patch(f"{CLIENT}.settings", AsyncMock()) as settings,
        patch(f"{CLIENT}.refresh", AsyncMock()) as refresh,
        patch(f"{CLIENT}.cancel_refresh", AsyncMock()) as cancel_refresh,
        patch(f"{CLIENT}.press", AsyncMock()) as press,
        patch(f"{CLIENT}.listen", side_effect=listen) as listen_mock,
    ):
        yield type("Mocks", (), dict(status=status, settings=settings, refresh=refresh,
                                     cancel_refresh=cancel_refresh,
                                     press=press, listen=listen_mock))


async def setup(hass: HomeAssistant, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "cts600.local", CONF_PORT: 8600},
                            unique_id="cts600.local:8600", title="Nilan CTS600", options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow(hass: HomeAssistant, client) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    client.status.side_effect = Cts600ConnectionError("down")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "h", CONF_PORT: 8600})
    assert result["errors"] == {"base": "cannot_connect"}

    client.status.side_effect = None
    with patch("custom_components.cts600.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "h", CONF_PORT: 8600})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: "h", CONF_PORT: 8600}


async def test_options_flow(hass: HomeAssistant, client) -> None:
    entry = await setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"refresh_minutes": 5, "max_age_minutes": 0})
    assert result["errors"] == {"refresh_minutes": "refresh_too_often"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"refresh_minutes": 30, "max_age_minutes": 60})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {"refresh_minutes": 30, "max_age_minutes": 60}
    # outdoor is 2 h old -> unavailable with a 60 min max age
    assert hass.states.get("sensor.nilan_cts600_outdoor_temperature").state == "unavailable"
    assert hass.states.get("sensor.nilan_cts600_room_temperature").state == "23.0"


async def test_entities(hass: HomeAssistant, client) -> None:
    await setup(hass)
    climate = hass.states.get("climate.nilan_cts600")
    assert climate.state == "cool"
    assert climate.attributes["temperature"] == 22
    assert climate.attributes["current_temperature"] == 23.0
    assert climate.attributes["fan_mode"] == "3"
    assert climate.attributes["hvac_action"] == "cooling"
    assert climate.attributes["water_heating"] is True

    assert hass.states.get("sensor.nilan_cts600_outdoor_temperature").state == "11.0"
    assert hass.states.get("sensor.nilan_cts600_operating_state").state == "JÄÄH+VES"
    assert hass.states.get("sensor.nilan_cts600_water_tank_top_temperature").state == "unavailable"
    assert hass.states.get("binary_sensor.nilan_cts600_compressor").state == "on"
    assert hass.states.get("binary_sensor.nilan_cts600_alarm").state == "off"
    assert hass.states.get("binary_sensor.nilan_cts600_water_heating").state == "on"
    assert hass.states.get("binary_sensor.nilan_cts600_bus_problem").state == "off"
    assert hass.states.get("button.nilan_cts600_update_readings").state == "unknown"

    registry = er.async_get(hass)
    key = registry.async_get("button.nilan_cts600_panel_key_up")
    assert key is not None and key.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_climate_calls(hass: HomeAssistant, client) -> None:
    await setup(hass)
    target = {"entity_id": "climate.nilan_cts600"}
    await hass.services.async_call("climate", "set_temperature", {**target, "temperature": 23.4}, blocking=True)
    client.settings.assert_awaited_with(mode=None, setpoint=23)
    await hass.services.async_call("climate", "set_hvac_mode", {**target, "hvac_mode": "heat"}, blocking=True)
    client.settings.assert_awaited_with(mode="heat")
    await hass.services.async_call("climate", "set_fan_mode", {**target, "fan_mode": "1"}, blocking=True)
    client.settings.assert_awaited_with(fan=1)
    await hass.services.async_call("climate", "turn_off", target, blocking=True)
    client.settings.assert_awaited_with(mode="off")

    client.settings.side_effect = Cts600RequestError(409, "a panel operation is already running")
    with pytest.raises(HomeAssistantError, match="already running"):
        await hass.services.async_call("climate", "set_fan_mode", {**target, "fan_mode": "2"}, blocking=True)

    await hass.services.async_call("button", "press", {"entity_id": "button.nilan_cts600_update_readings"},
                                   blocking=True)
    client.refresh.assert_awaited_once()

    # Cancel is only available while a walk runs.
    cancel = {"entity_id": "button.nilan_cts600_cancel_readings_update"}
    assert hass.states.get(cancel["entity_id"]).state == "unavailable"
    walking = deepcopy(STATUS)
    walking["walk"] = {"running": True, "message": "reading HUONE"}
    hass.config_entries.async_entries(DOMAIN)[0].runtime_data._on_push(walking)
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", cancel, blocking=True)
    client.cancel_refresh.assert_awaited_once()


async def test_push_and_operation_event(hass: HomeAssistant, client) -> None:
    entry = await setup(hass)
    coordinator = entry.runtime_data
    events = async_capture_events(hass, EVENT_OPERATION_FINISHED)

    running = deepcopy(STATUS)
    running["edit"] = {"running": True, "phase": "running", "message": "setpoint 22 -> 24"}
    coordinator._on_push(running)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.nilan_cts600_panel_operation_running").state == "on"

    done = deepcopy(STATUS)
    done["panel"]["setpoint"] = 24
    done["edit"] = {"running": False, "outcome": "stopped", "message": "a key was pressed on the panel",
                    "finished_at": NOW + 5}
    coordinator._on_push(done)
    await hass.async_block_till_done()
    assert hass.states.get("climate.nilan_cts600").attributes["temperature"] == 24
    assert len(events) == 1
    assert events[0].data["outcome"] == "stopped"
    assert hass.states.get("sensor.nilan_cts600_last_panel_operation").state == "stopped"


async def test_register_temperatures(hass: HomeAssistant, client) -> None:
    status = deepcopy(STATUS)
    status["sensors"] = {
        "room": {"value": 23.4, "status": "ok", "t_number": 15, "register": 14, "at": NOW - 5},
        "outdoor": {"value": 13.5, "status": "uncertain", "t_number": 1, "register": 0, "at": NOW - 5},
        "tank_top": {"value": 62.0, "status": "ok", "t_number": 11, "register": 10, "at": NOW - 5},
    }
    client.status.return_value = status
    await setup(hass, options={"max_age_minutes": 60})
    room = hass.states.get("sensor.nilan_cts600_room_temperature")
    assert room.state == "23.4"
    assert room.attributes["source"] == "register"
    assert hass.states.get("climate.nilan_cts600").attributes["current_temperature"] == 23.4
    # Uncertain register value -> the panel reading (here too old -> unavailable).
    outdoor = hass.states.get("sensor.nilan_cts600_outdoor_temperature")
    assert outdoor.state == "unavailable"
    # A trusted register value is available even with no panel reading at all.
    tank = hass.states.get("sensor.nilan_cts600_water_tank_top_temperature")
    assert tank.state == "62.0"
    assert tank.attributes["register_status"] == "ok"


async def test_control_disabled_and_silent_bus(hass: HomeAssistant, client) -> None:
    status = deepcopy(STATUS)
    status["control_enabled"] = False
    status["last_frame_at"] = NOW - 120
    client.status.return_value = status
    await setup(hass)
    assert hass.states.get("climate.nilan_cts600").state == "unavailable"
    assert hass.states.get("binary_sensor.nilan_cts600_bus_problem").state == "on"
    assert hass.states.get("button.nilan_cts600_update_readings").state == "unavailable"
    assert hass.states.get("sensor.nilan_cts600_room_temperature").state == "23.0"
