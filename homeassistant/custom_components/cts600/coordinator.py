"""Status coordinator: pushed over /ws/status, polled as a fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import Cts600Client, Cts600Error, Cts600RequestError
from .const import (
    BUS_SILENT_SECONDS,
    CONF_REFRESH_MINUTES,
    DOMAIN,
    EVENT_OPERATION_FINISHED,
    POLL_INTERVAL,
    RECONNECT_MAX_SECONDS,
    RECONNECT_MIN_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

OPERATIONS = {"walk": "readings update", "edit": "settings change"}


class Cts600Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: Cts600Client) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=POLL_INTERVAL,
        )
        self.client = client
        self.clock_offset = 0.0  # local epoch - service epoch
        self._finished_at: dict[str, float | None] | None = None

    # -- data -------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.client.status()
        except Cts600Error as err:
            raise UpdateFailed(str(err)) from err
        self._note(status)
        return status

    @callback
    def _on_push(self, status: dict[str, Any]) -> None:
        self._note(status)
        self.async_set_updated_data(status)

    def _note(self, status: dict[str, Any]) -> None:
        self.clock_offset = time.time() - status["server_time"]
        self._check_operations(status)

    def _check_operations(self, status: dict[str, Any]) -> None:
        """Log and fire an event when a walk or settings change finishes."""
        finished = {kind: (status.get(kind) or {}).get("finished_at") for kind in OPERATIONS}
        previous, self._finished_at = self._finished_at, finished
        if previous is None:
            return
        for kind, at in finished.items():
            if at is None or at == previous.get(kind):
                continue
            op = status[kind]
            if op.get("outcome") == "stopped":
                _LOGGER.warning("CTS600 %s stopped: %s", OPERATIONS[kind], op.get("message"))
            else:
                _LOGGER.info("CTS600 %s done: %s", OPERATIONS[kind], op.get("message"))
            self.hass.bus.async_fire(EVENT_OPERATION_FINISHED, {
                "entry_id": self.config_entry.entry_id, "operation": kind,
                "outcome": op.get("outcome"), "message": op.get("message"),
            })

    def local_time(self, at: float | None) -> datetime | None:
        """A service timestamp as a local, timezone-aware datetime."""
        if at is None:
            return None
        return dt_util.utc_from_timestamp(at + self.clock_offset)

    def age_seconds(self, at: float | None) -> float | None:
        """Seconds since a service timestamp, on the service's clock."""
        if at is None:
            return None
        return time.time() - self.clock_offset - at

    @property
    def bus_live(self) -> bool:
        data = self.data or {}
        age = self.age_seconds(data.get("last_frame_at"))
        return bool(data.get("connected")) and age is not None and age < BUS_SILENT_SECONDS

    @property
    def control_enabled(self) -> bool:
        return bool((self.data or {}).get("control_enabled"))

    @property
    def operation_running(self) -> bool:
        data = self.data or {}
        return any((data.get(kind) or {}).get("running") for kind in OPERATIONS)

    # -- background work ------------------------------------------------------------

    @callback
    def start(self) -> None:
        entry = self.config_entry
        entry.async_create_background_task(self.hass, self._listen(), f"{DOMAIN} status push")
        minutes = entry.options.get(CONF_REFRESH_MINUTES, 0)
        if minutes:
            entry.async_on_unload(async_track_time_interval(
                self.hass, self._auto_refresh, timedelta(minutes=minutes),
                name=f"{DOMAIN} readings update",
            ))

    async def _listen(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        while True:
            started = time.monotonic()
            try:
                await self.client.listen(self._on_push)
                _LOGGER.debug("Status push closed by %s", self.client.base_url)
            except Cts600Error as err:
                _LOGGER.debug("Status push unavailable: %s", err)
            if time.monotonic() - started > 60:
                delay = RECONNECT_MIN_SECONDS
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    async def _auto_refresh(self, _now: datetime) -> None:
        """Periodic readings update (option). The service refuses it when
        the panel is in use or another operation runs; that's fine, the
        next interval tries again."""
        if not self.control_enabled or self.operation_running:
            return
        try:
            await self.client.refresh()
        except Cts600RequestError as err:
            _LOGGER.debug("Scheduled readings update not started: %s", err)
        except Cts600Error as err:
            _LOGGER.debug("Scheduled readings update failed: %s", err)
