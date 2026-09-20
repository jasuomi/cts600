"""Base entity."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Cts600Error
from .const import DOMAIN
from .coordinator import Cts600Coordinator


class Cts600Entity(CoordinatorEntity[Cts600Coordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: Cts600Coordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        data = coordinator.data or {}
        device = data.get("device") or {}
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Nilan",
            model="CTS600",
            model_id=device.get("product"),
            name=entry.title,
            sw_version=device.get("sw_version"),
            configuration_url=f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}/",
        )

    @property
    def status(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def panel(self) -> dict[str, Any] | None:
        return self.status.get("panel")

    @property
    def led(self) -> dict[str, Any] | None:
        return self.status.get("led")

    def reading(self, key: str) -> dict[str, Any] | None:
        return (self.status.get("readings") or {}).get(key)

    async def _call(self, func: Callable[..., Awaitable[None]], **kwargs: Any) -> None:
        """Run a service request, turning its refusal into a UI error."""
        if not self.coordinator.control_enabled:
            raise HomeAssistantError(
                "Control is disabled on the CTS600 service (start it with --enable-control)"
            )
        try:
            await func(**kwargs)
        except Cts600Error as err:
            raise HomeAssistantError(f"CTS600: {err}") from err
