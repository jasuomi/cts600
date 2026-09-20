"""Walk the panel's "NÄYTÄ DATA" menu to refresh every reading at once.

This presses keys on a display a person may also be using, so every step
is checked before the next one (active walking was deliberately left
unimplemented for a long time for exactly that reason, and was added on
2026-09-16 only with these guards in place). The general guards are in
panel_ops.py; specific to the walk:

- Starts only from the idle screen.
- Every press must land on the expected screen: Up -> NÄYTÄ DATA,
  Enter -> a data screen, then each Down -> a data screen further
  down DATA_SCREENS. Anything else stops the walk at once, with no
  further key presses; the panel returns to idle by itself (~2 min 45 s).
- Enter is pressed exactly once, on NÄYTÄ DATA. Inside the list only
  Down and Esc are used, so nothing can be confirmed or edited.
- At the end of the list (TYYPPI), Esc back to idle, checking each step.
- cancel() stops stepping through the list: after the press in progress,
  the walk goes straight to the Esc-back-to-idle part (still checked) and
  finishes as "cancelled". Cancelled before the first press, it presses
  nothing.
- run(target=...) stops stepping down once that data screen is shown and
  goes back to idle from there: the short walk used to re-anchor one
  register temperature (sensor_regs.py), e.g. the condenser after a swing.

The readings themselves are not collected here: DeviceState harvests any
data screen it sees (display_data.py). The walk only makes the screens
appear, waiting on each until both display lines are confirmed.
"""

from __future__ import annotations

import logging
import threading

from . import display_data
from .panel_ops import (  # noqa: F401 -- KEY_QUIET_SECONDS etc. re-exported
    KEY_QUIET_SECONDS, OperationBusyError, OperationStopped, PanelOperation, show,
)
from .transport import BusBusyError

log = logging.getLogger(__name__)

MAX_ESC_PRESSES = 4

DATA_KEYS = set(display_data.SCREEN_ORDER)

# Names kept from before panel_ops.py existed.
WalkStopped = OperationStopped
WalkBusyError = OperationBusyError


class WalkCancelled(Exception):
    """cancel() was called; the message says where the walk ended."""


class DataWalker(PanelOperation):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cancel = threading.Event()
        self._walking = False
        self._target: str | None = None
        self._purpose: str | None = None

    def cancel(self) -> bool:
        """Ask a running walk to stop and return to idle. False if no walk
        is running (another operation holding the shared lock doesn't count)."""
        if not self._walking:
            return False
        self._cancel.set()
        log.info("NÄYTÄ DATA walk cancel requested")
        self._status("running", "cancelling, finishing the current step")
        return True

    @property
    def cancelling(self) -> bool:
        return self._walking and self._cancel.is_set()

    def run(self, target: str | None = None, purpose: str | None = None) -> dict:
        """Walk the list, or only down to the `target` screen key (a
        DATA_SCREENS key such as "LAUHDUT"). `purpose` is shown in the status
        and result message, e.g. "condenser re-anchor"."""
        if target is not None and target not in DATA_KEYS:
            raise ValueError(f"unknown data screen {target!r}")
        self._begin("a panel operation is already running")
        self._cancel.clear()
        self._walking = True
        self._target = target
        self._purpose = purpose
        try:
            self._status("starting", "checking the display")
            try:
                screens = self._walk()
            except WalkCancelled as exc:
                return self._finish("cancelled", str(exc))
            except OperationStopped as exc:
                log.warning("NÄYTÄ DATA walk stopped: %s", exc)
                return self._finish("stopped", str(exc))
            except BusBusyError as exc:
                log.warning("NÄYTÄ DATA walk stopped, bus busy: %s", exc)
                return self._finish("stopped", f"bus busy, key not sent ({exc})")
            except Exception as exc:  # never leave the status stuck at "running"
                log.exception("NÄYTÄ DATA walk failed")
                return self._finish("stopped", f"error: {exc}")
            return self._finish("done", f"read {len(screens)} screens", screens)
        finally:
            self._walking = False
            self._end()

    # -- steps ----------------------------------------------------------------

    def _walk(self) -> list[str]:
        self._check_start()
        screen = self._fresh_screen()
        if screen.key != "idle":
            raise OperationStopped(f"not on the idle screen ({show(screen)})")
        if self._cancel.is_set():
            raise WalkCancelled("cancelled before any key was pressed")

        visited: list[str] = []
        screen = self._press_expect("up", screen, {"NAYTA_DATA"}, "opening NÄYTÄ DATA")
        if not self._cancel.is_set():
            screen = self._press_expect("enter", screen, DATA_KEYS, "opening DATA")
            visited.append(screen.key)

        last = self._target or display_data.LAST_SCREEN
        retried = False
        while (not self._cancel.is_set() and screen.key != last
               and display_data.SCREEN_ORDER.get(screen.key, 0) < display_data.SCREEN_ORDER[last]
               and len(visited) <= len(display_data.DATA_SCREENS)):
            nxt = self._press("down", screen, f"reading {_label(screen)}")
            if nxt is None:
                if not retried:  # a lost press looks the same as the end of the list
                    retried = True
                    continue
                log.info("Down didn't change %s; treating it as the end of the list", screen.key)
                break
            retried = False
            if nxt.key not in DATA_KEYS or display_data.SCREEN_ORDER[nxt.key] < display_data.SCREEN_ORDER[screen.key]:
                raise OperationStopped(f"unexpected screen after Down ({show(nxt)})")
            screen = nxt
            visited.append(screen.key)

        cancelled = self._cancel.is_set()
        for _ in range(MAX_ESC_PRESSES):
            if screen.key == "idle":
                break
            nxt = self._press("esc", screen, "cancelled, returning to idle" if cancelled else "returning to idle")
            if nxt is None:
                raise OperationStopped(f"Esc didn't change the screen ({show(screen)})")
            if nxt.key not in DATA_KEYS | {"NAYTA", "NAYTA_DATA", "idle"}:
                raise OperationStopped(f"unexpected screen after Esc ({show(nxt)})")
            screen = nxt
        if screen.key != "idle":
            raise OperationStopped(f"didn't get back to idle ({show(screen)})")
        if cancelled:
            raise WalkCancelled(f"cancelled after reading {len(visited)} screens, back on idle")
        if self._target is not None and self._target not in visited:
            raise OperationStopped(f"didn't reach {self._target}, back on idle")
        return visited

    def _press_expect(self, key: str, prev: display_data.Screen, expected: set[str], what: str) -> display_data.Screen:
        nxt = self._press(key, prev, what)
        if nxt is None:
            raise OperationStopped(f"{key.capitalize()} didn't change the screen ({show(prev)})")
        if nxt.key not in expected:
            raise OperationStopped(f"unexpected screen after {key.capitalize()} ({show(nxt)})")
        return nxt

    # -- status -----------------------------------------------------------------

    def _status(self, phase: str, message: str) -> None:
        last = display_data.SCREEN_ORDER[self._target or display_data.LAST_SCREEN]
        self.state.set_walk_status(
            running=True, phase=phase, message=self._with_purpose(message), steps=self._steps,
            cancelling=self._cancel.is_set(),
            # Up, Enter, a Down per further screen, Esc, Esc
            total_steps=2 + last + 2,
            started_at=self._started_at,
        )

    def _with_purpose(self, message: str) -> str:
        return f"{self._purpose}: {message}" if self._purpose else message

    def _finish(self, outcome: str, message: str, screens: list[str] | None = None) -> dict:
        message = self._with_purpose(message)
        self.state.set_walk_status(
            running=False, outcome=outcome, message=message, steps=self._steps,
            started_at=self._started_at, finished_at=self._clock(),
        )
        log.info("NÄYTÄ DATA walk %s: %s", outcome, message)
        return self._result(outcome=outcome, message=message, screens=screens or [])


def _label(screen: display_data.Screen) -> str:
    return screen.line1 or screen.key
