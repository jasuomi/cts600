"""Shared guards for key-press sequences on the physical panel.

Both the NÄYTÄ DATA walk (data_walk.py) and the settings editor
(panel_settings.py) press keys on a display a person may also be using.
The rules they share live here:

- Start only with no physical key press in the last KEY_QUIET_SECONDS and
  a healthy bus.
- Every press is followed by reading the screen back; the caller decides
  whether it's the expected one.
- Stop at once, pressing nothing more, if a physical key shows up on the
  bus (our own presses are never seen by the listener -- RTS gates the
  local receiver -- so any non-zero key code is a person) or bus health
  degrades.
- One sequence at a time: all operations share one lock, so a walk and a
  settings change can't interleave their presses.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import display_data
from .state import DeviceState

KEY_QUIET_SECONDS = 10.0      # no physical key press this recently before starting
SCREEN_TIMEOUT_SECONDS = 6.0  # a press must change the screen within this
FIRST_SCREEN_TIMEOUT_SECONDS = 5.0
OWN_PRESS_GRACE_SECONDS = 0.5


class OperationStopped(Exception):
    """The sequence stopped early; the message says why, for the UI."""


class OperationBusyError(RuntimeError):
    """Another panel operation is already running."""


class PanelOperation:
    def __init__(
        self, state: DeviceState,
        press: Callable[[str], None],
        bus_healthy: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        lock: threading.Lock | None = None,
    ) -> None:
        self.state = state
        self._press_fn = press
        self._bus_healthy = bus_healthy
        self._clock = clock
        self._sleep = sleep
        self._lock = lock or threading.Lock()
        self._own_presses: list[tuple[float, float]] = []
        self._started_at = 0.0
        self._steps = 0

    @property
    def running(self) -> bool:
        """True while this or any operation sharing the lock is running."""
        return self._lock.locked()

    def _begin(self, busy_message: str) -> None:
        if not self._lock.acquire(blocking=False):
            raise OperationBusyError(busy_message)
        self._started_at = self._clock()
        self._own_presses = []
        self._steps = 0

    def _end(self) -> None:
        self._lock.release()

    def _status(self, phase: str, message: str) -> None:
        """Progress for the UI; subclasses publish it."""

    # -- guards ---------------------------------------------------------------

    def _check_start(self) -> None:
        last_key = self.state.last_physical_key_at
        if last_key is not None and self._clock() - last_key < KEY_QUIET_SECONDS:
            raise OperationStopped("the panel is in use (a key was pressed just now)")
        if not self._bus_healthy():
            raise OperationStopped("bus health is degraded")

    def _check_safe(self) -> None:
        pressed = self.state.last_physical_key_at
        if pressed is not None and pressed > self._started_at and not any(
            a <= pressed <= b for a, b in self._own_presses
        ):
            raise OperationStopped("a key was pressed on the panel")
        if not self._bus_healthy():
            raise OperationStopped("bus health degraded")

    # -- presses and screens ----------------------------------------------------

    def _tap(self, key: str, what: str) -> float:
        """Press one key (after the safety check). Returns the press time."""
        self._check_safe()
        self._steps += 1
        self._status("running", what)
        t0 = self._clock()
        self._press_fn(key)
        self._own_presses.append((t0, self._clock() + OWN_PRESS_GRACE_SECONDS))
        return t0

    def _press(self, key: str, prev: display_data.Screen, what: str,
               timeout: float = SCREEN_TIMEOUT_SECONDS) -> display_data.Screen | None:
        """Press one key and wait for the screen to change. Returns the new
        screen, or None if it didn't change in time."""
        t0 = self._tap(key, what)
        deadline = t0 + timeout
        while self._clock() < deadline:
            self._check_safe()
            got = self.state.screen()
            if got is not None:
                screen, confirmed_at = got
                if confirmed_at > t0 and (screen.line1, screen.line2) != (prev.line1, prev.line2):
                    return screen
            self._sleep(0.05)
        return None

    def _fresh_screen(self, after: float | None = None,
                      timeout: float = FIRST_SCREEN_TIMEOUT_SECONDS) -> display_data.Screen:
        """The screen, with both lines confirmed after `after` (default: now)."""
        t0 = self._clock() if after is None else after
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            self._check_safe()
            got = self.state.screen()
            if got is not None and got[1] > t0:
                return got[0]
            self._sleep(0.05)
        raise OperationStopped("no display data from the panel")

    def _result(self, **fields: Any) -> dict:
        return {**fields, "steps": self._steps}


def show(screen: display_data.Screen) -> str:
    text = " / ".join(t for t in (screen.line1, screen.line2) if t)
    return f"'{text}'" if text else "blank"
