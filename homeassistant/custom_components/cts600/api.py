"""Client for the CTS600 dashboard service (python -m cts600).

The service owns the RS485 port and all the bus-safety logic; this client
only talks HTTP and one websocket to it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import aiohttp

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class Cts600Error(Exception):
    """Base error."""


class Cts600ConnectionError(Cts600Error):
    """The service couldn't be reached."""


class Cts600RequestError(Cts600Error):
    """The service refused a request; the message is its explanation."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Cts600Client:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int) -> None:
        self._session = session
        self.base_url = f"http://{host}:{port}"

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/status")

    async def settings(self, **values: Any) -> None:
        """Start a settings change (mode, setpoint, fan). Returns once the
        service accepted it; progress and outcome arrive in status["edit"]."""
        await self._request("POST", "/api/settings", {k: v for k, v in values.items() if v is not None})

    async def refresh(self) -> None:
        """Start a NÄYTÄ DATA walk; outcome arrives in status["walk"]."""
        await self._request("POST", "/api/display_data/refresh")

    async def cancel_refresh(self) -> None:
        """Stop a running NÄYTÄ DATA walk; it returns the panel to idle."""
        await self._request("POST", "/api/display_data/cancel")

    async def press(self, key: str) -> None:
        await self._request("POST", "/api/press", {"key": key})

    async def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        try:
            async with self._session.request(
                method, self.base_url + path, json=json, timeout=REQUEST_TIMEOUT,
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                if resp.status >= 400:
                    message = body.get("error") if isinstance(body, dict) else None
                    raise Cts600RequestError(resp.status, message or f"HTTP {resp.status}")
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise Cts600ConnectionError(f"{self.base_url}: {err or type(err).__name__}") from err

    async def listen(self, on_status: Callable[[dict[str, Any]], None]) -> None:
        """Receive pushed status until the connection closes."""
        try:
            async with self._session.ws_connect(self.base_url + "/ws/status", heartbeat=30) as ws:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        on_status(msg.json())
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise Cts600ConnectionError(f"{self.base_url}/ws/status: {err or type(err).__name__}") from err
