"""Change the idle screen's settings -- mode, room setpoint, fan speed,
on/off -- by pressing keys the way a person would, checking the display
after every press.

What is known about the panel (planner.py's docstring and
display_decoder.py's notes, all confirmed live by the operator):

- On the idle screen, Enter selects a field: once = setpoint, twice =
  mode, three times = fan. Up/Down then change the selected field.
- Enter confirms the change (and moves on to the next field). Esc
  DISCARDS an unconfirmed change -- so Esc is also the safe way out when
  anything looks wrong mid-edit.
- Only the fan field shows a selection marker; the setpoint blink and the
  mode field show none. So which field is selected can't be read back.
  Instead each Up/Down press must change exactly the field being edited,
  by one step, and nothing else. If Enter was lost or doubled, the first
  press changes the wrong thing (or opens NÄYTÄ DATA from plain idle),
  which is caught, and Esc undoes it.
- The panel leaves the edit after ~10-13 s without a key press. Each step
  here takes a few seconds (both lines must be re-sent), well inside that.
- The mode list doesn't wrap. Its order is taken from the reference
  integration (display_data.MODE_ORDER); if a press doesn't move, the
  direction is reversed once.
- The unit is switched on and off with the On/Off keys directly.

Sequence per field: check idle, Enter xN (screen must stay idle with the
same values), Up/Down until the target (or the panel's limit), Enter to
confirm, Esc to leave the field selection, then read the idle screen
again to verify the value stuck. All general guards (physical key, bus
health, one operation at a time) come from panel_ops.py.
"""

from __future__ import annotations

import logging

from . import display_data
from .panel_ops import OperationBusyError, OperationStopped, PanelOperation, show  # noqa: F401
from .transport import BusBusyError

log = logging.getLogger(__name__)

FIELD_ENTERS = {"setpoint": 1, "mode": 2, "fan": 3}
SETPOINT_RANGE = (5, 30)  # loose guard, not the device's documented range (unknown)
FAN_RANGE = (1, 4)
MODES = ("off", "on", *display_data.MODE_ORDER)

SETTLE_SECONDS = 1.5         # after Enter/Esc, which don't visibly change the screen
IDLE_TIMEOUT_SECONDS = 8.0   # both lines re-sent after a press
POWER_TIMEOUT_SECONDS = 10.0
SETTINGS = ("setpoint", "mode", "fan")


class SettingsEditor(PanelOperation):
    def apply(self, mode: str | None = None, setpoint: int | None = None, fan: int | None = None) -> dict:
        """Change any of mode (off/on/auto/cool/heat), setpoint (°C) and fan
        (1-4), in that order. Raises ValueError for invalid values and
        OperationBusyError if another panel operation runs; otherwise
        returns {"outcome": "done"|"stopped", "message": ...}."""
        targets = validate(mode=mode, setpoint=setpoint, fan=fan)
        self._begin("a panel operation is already running")
        try:
            what = ", ".join(f"{k} {v}" for k, v in targets.items())
            self._status("starting", f"setting {what}")
            messages: list[str] = []
            try:
                self._check_start()
                for field, target in targets.items():
                    if field == "mode":
                        messages.append(self._set_mode(target))
                    else:
                        messages.append(self._set_field(field, target))
            except OperationStopped as exc:
                log.warning("Settings change stopped: %s", exc)
                return self._finish("stopped", "; ".join(messages + [str(exc)]))
            except BusBusyError as exc:
                log.warning("Settings change stopped, bus busy: %s", exc)
                return self._finish("stopped", "; ".join(messages + [f"bus busy, key not sent ({exc})"]))
            except Exception as exc:  # never leave the status stuck at "running"
                log.exception("Settings change failed")
                return self._finish("stopped", f"error: {exc}")
            return self._finish("done", "; ".join(messages))
        finally:
            self._end()

    # -- fields -------------------------------------------------------------------

    def _idle(self, after: float | None = None) -> tuple[display_data.Screen, dict]:
        screen = self._fresh_screen(after, timeout=IDLE_TIMEOUT_SECONDS)
        status = display_data.parse_idle(screen)
        if status is None:
            raise OperationStopped(f"not on the idle screen ({show(screen)})")
        return screen, status

    def _set_mode(self, target: str) -> str:
        screen, status = self._idle()
        is_off = status["mode"] == "off"
        if target == "off":
            if is_off:
                return "already off"
            self._power("off", screen)
            return "switched off"
        if target == "on":
            if not is_off:
                return "already on"
            self._power("on", screen)
            return "switched on"
        prefix = ""
        if is_off:
            screen, status = self._power("on", screen)
            prefix = "switched on, "
        if status["mode"] == target:
            return f"{prefix}mode already {target}"
        return prefix + self._set_field("mode", target)

    def _power(self, key: str, prev: display_data.Screen) -> tuple[display_data.Screen, dict]:
        screen = self._press(key, prev, f"pressing {key.capitalize()}", timeout=POWER_TIMEOUT_SECONDS)
        if screen is None:
            raise OperationStopped(f"{key.capitalize()} didn't change the screen ({show(prev)})")
        status = display_data.parse_idle(screen)
        if status is None or (status["mode"] == "off") != (key == "off"):
            raise OperationStopped(f"unexpected screen after {key.capitalize()} ({show(screen)})")
        return screen, status

    def _set_field(self, field: str, target: int | str) -> str:
        screen, status = self._idle()
        if status["mode"] == "off":
            raise OperationStopped(f"can't set {field}: the unit is off")
        if status[field] is None:
            raise OperationStopped(f"can't read {field} from the idle screen ({show(screen)})")
        if status[field] == target:
            return f"{field} already {target}"

        for n in range(FIELD_ENTERS[field]):
            t0 = self._tap("enter", f"selecting {field} ({n + 1}/{FIELD_ENTERS[field]})")
            self._sleep(SETTLE_SECONDS)
            screen, new = self._idle_or_abort(t0, "after Enter")
            if _values(new) != _values(status):
                self._abort(screen, f"Enter changed the screen ({show(screen)})")
            status = new

        original = status[field]
        key = _direction(field, status[field], target)
        retried = reversed_ = False
        for _ in range(_max_steps(field, original, target)):
            if status[field] == target:
                break
            nxt = self._press(key, screen, f"{field} {status[field]} -> {target}")
            if nxt is None:
                if not retried:  # a lost press looks the same as the panel's limit
                    retried = True
                    continue
                if field == "mode" and not reversed_:
                    reversed_, retried = True, False
                    key = "down" if key == "up" else "up"
                    continue
                break
            retried = False
            new = display_data.parse_idle(nxt)
            if new is None or not _one_step(field, status, new, key):
                self._abort(nxt, f"unexpected change after {key.capitalize()} ({show(nxt)})")
            screen, status = nxt, new

        reached = status[field]
        if reached == original:
            self._abort(screen, f"{field} didn't change from {original}")
        self._tap("enter", f"confirming {field} {reached}")
        self._sleep(SETTLE_SECONDS)
        t0 = self._tap("esc", "leaving the field selection")
        self._sleep(SETTLE_SECONDS)
        screen, final = self._idle(after=t0)
        if final[field] != reached:
            raise OperationStopped(f"{field} shows {final[field]} after confirming {reached}")
        if reached != target:
            return f"{field} set to {reached}, the panel's limit (asked {target})"
        return f"{field} set to {reached}"

    def _idle_or_abort(self, after: float, what: str) -> tuple[display_data.Screen, dict]:
        screen = self._fresh_screen(after, timeout=IDLE_TIMEOUT_SECONDS)
        status = display_data.parse_idle(screen)
        if status is None:
            self._abort(screen, f"not on the idle screen {what} ({show(screen)})")
        return screen, status

    def _abort(self, screen: display_data.Screen, reason: str) -> None:
        """Press Esc once -- it discards an unconfirmed edit, or leaves a
        menu opened by mistake -- then stop."""
        try:
            self._press("esc", screen, "discarding the change")
        except OperationStopped:
            raise OperationStopped(f"{reason}; not pressing Esc, check the panel") from None
        raise OperationStopped(f"{reason}; pressed Esc to discard")

    # -- status -----------------------------------------------------------------

    def _status(self, phase: str, message: str) -> None:
        self.state.set_edit_status(
            running=True, phase=phase, message=message, steps=self._steps,
            started_at=self._started_at,
        )

    def _finish(self, outcome: str, message: str) -> dict:
        self.state.set_edit_status(
            running=False, outcome=outcome, message=message, steps=self._steps,
            started_at=self._started_at, finished_at=self._clock(),
        )
        log.info("Settings change %s: %s", outcome, message)
        return self._result(outcome=outcome, message=message)


def validate(mode: str | None = None, setpoint: int | None = None, fan: int | None = None) -> dict:
    targets: dict = {}
    if mode is not None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        targets["mode"] = mode
    if setpoint is not None:
        if not SETPOINT_RANGE[0] <= setpoint <= SETPOINT_RANGE[1]:
            raise ValueError(f"setpoint must be {SETPOINT_RANGE[0]}-{SETPOINT_RANGE[1]} °C")
        targets["setpoint"] = int(setpoint)
    if fan is not None:
        if not FAN_RANGE[0] <= fan <= FAN_RANGE[1]:
            raise ValueError(f"fan must be {FAN_RANGE[0]}-{FAN_RANGE[1]}")
        targets["fan"] = int(fan)
    if not targets:
        raise ValueError("nothing to change")
    if targets.get("mode") == "off" and len(targets) > 1:
        raise ValueError("can't change other settings while switching off")
    return targets


def _values(status: dict) -> tuple:
    return tuple(status[k] for k in SETTINGS)


def _direction(field: str, current, target) -> str:
    if field == "mode":
        order = display_data.MODE_ORDER
        if current in order and target in order:
            return "down" if order.index(target) > order.index(current) else "up"
        return "down"
    return "up" if target > current else "down"


def _max_steps(field: str, current, target) -> int:
    if field == "mode":
        return 2 * len(display_data.MODE_ORDER) + 2
    return abs(target - current) + 2  # room for one lost press


def _one_step(field: str, old: dict, new: dict, key: str) -> bool:
    """True if the press changed exactly `field`, by one step in the key's
    direction (for mode: to another known mode)."""
    if any(new[k] != old[k] for k in SETTINGS if k != field):
        return False
    if field == "mode":
        return new["mode"] in display_data.MODE_ORDER and new["mode"] != old["mode"]
    return new[field] == old[field] + (1 if key == "up" else -1)
