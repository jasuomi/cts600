"""Temperatures from the controller's input registers (FC4 0x0000-0x000F).

Nilan's Modbus register list for firmware T1.33 (this unit runs 1.22, so
the names are borrowed, not confirmed here) calls input registers 0-15
T1_Intake .. T16. The values are hundredths of a °C,
but this unit's replies carry only the LOW BYTE of each value: the high byte
is 0 on the wire (confirmed in the raw frames, 2026-09-17). That is why years
of reading this block looked like counters, 8-bit wraps and oscillators: the
low byte of a hundredths value rolls over every 2.56 °C.

The low byte alone pins the value down to one of a series 2.56 °C apart, so
a reading needs a reference to pick the right one:

- anchor: a NÄYTÄ DATA screen shows the same sensor in whole °C (the display
  rounds; step 0 saw 48.74 shown as 49). The display is within ±0.5 °C, so the
  candidate nearest to it is the right one.
- track: after that, each poll takes the candidate nearest the previous
  value. Correct while the sensor moves less than 1.28 °C between polls;
  a bigger step, or a gap too long for the sensor's plausible rate, marks the
  reading "uncertain" until the next anchor.

Step 0 (2026-09-17 11:17-11:19, one Update walk then 8 atomic block reads):
T1 13 °C -> 13.02, T5 14 -> 14.24, T11 62 -> 62.10, T12 49 -> 48.74,
T15 23 -> 23.00. T14 (MENO) did not fit: display 23, register low byte 175
gives 22.23 or 24.79. The next walk (11:30, poll running) anchored all six,
MENO included: display 22, the same low byte 175 = 22.23. So the step 0
miss was on the display side (lag or rounding), not a wrong register. An
anchor that disagrees with the display by more than ANCHOR_TOLERANCE_C is
still reported as "mismatch", never as "ok".

Pure logic plus a small JSON file; no bus access (the poll lives in
passive_listener.py / webapp/server.py).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

BLOCK_START = 0x0000
BLOCK_COUNT = 16

WRAP = 256                      # hundredths per low-byte rollover (2.56 °C)
ANCHOR_TOLERANCE_C = 0.6        # display rounds to whole °C (±0.5) plus a little lag
ANCHOR_PAIRING_SECONDS = 15.0   # a display value and a poll this far apart can anchor
AMBIGUOUS_STEP_C = 1.0          # nearest-candidate tracking is unsafe beyond this step
STALE_SECONDS = 60.0            # no successful poll for this long: "stale"
SAVE_INTERVAL_SECONDS = 60.0

# Swing detection, for fast polling: a sensor with a swing_rate is "swinging"
# when its value moved at least that fast over the last SWING_WINDOW_SECONDS
# (a window, not one poll step, so ±0.05 °C read noise at a 2.5 s poll
# doesn't keep fast polling on forever). Idle condenser drift on 2026-09-17
# was ~0.1 °C per 30 s; the compressor-start drop was 14.7 -> 1.9 °C in minutes.
SWING_WINDOW_SECONDS = 10.0
SWING_HISTORY_SECONDS = 60.0


@dataclass(frozen=True)
class Sensor:
    key: str             # display_data reading key
    t_number: int
    register: int
    max_rate_c_per_s: float  # plausible fastest change, for gap checks
    swing_rate_c_per_s: float | None = None  # faster than this: ask for fast polling


# Display readings that have a register, per Nilan's T1.33 register list
# (register index = T-number - 1).
SENSORS: tuple[Sensor, ...] = (
    Sensor("room", 15, 14, 0.01),
    Sensor("outdoor", 1, 0, 0.02),
    Sensor("tank_top", 11, 10, 0.02),
    Sensor("tank_bottom", 12, 11, 0.02),
    Sensor("supply", 14, 13, 0.03),
    Sensor("condenser", 5, 4, 0.08, swing_rate_c_per_s=0.02),  # 0.2 °C per 10 s
)
BY_KEY = {s.key: s for s in SENSORS}


def nearest(low: int, reference_hundredths: float) -> int:
    """The value (hundredths, may be negative) whose low byte is `low` and
    that lies closest to the reference."""
    base = low & 0xFF
    k = round((reference_hundredths - base) / WRAP)
    return base + k * WRAP


def _comparable(public: dict[str, dict]) -> dict[str, tuple]:
    """What a client sees change: value and status, not the read times."""
    return {k: (v["value"], v["status"]) for k, v in public.items()}


class SensorTracker:
    """Reconstructed register temperatures. Thread-safe: polls arrive from a
    worker thread, display readings from the serial-reading thread."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        # key -> {"hundredths", "status", "at", "anchored_at", "low"}
        self._values: dict[str, dict] = {}
        # key -> (display number, display time) not yet paired with a poll
        self._pending: dict[str, tuple[float, float]] = {}
        self._last_poll: tuple[list[int], float] | None = None
        self._last_poll_ok_at: float | None = None
        self._last_save = 0.0
        # key -> recent (time, hundredths), for sensors with a swing_rate
        self._history: dict[str, list[tuple[float, int]]] = {}
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._values = {k: v for k, v in data.items() if k in BY_KEY}
        except Exception:
            log.exception("Couldn't read %s; starting unanchored", self._path)

    def _save_locked(self, now: float, force: bool) -> dict | None:
        if not self._path or not (force or now - self._last_save >= SAVE_INTERVAL_SECONDS):
            return None
        self._last_save = now
        return {k: dict(v) for k, v in self._values.items()}

    def _write(self, snapshot: dict | None) -> None:
        if snapshot is None or not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception:
            log.exception("Couldn't save sensor values to %s", self._path)

    # -- inputs ---------------------------------------------------------------

    def on_poll(self, words: list[int], now: float | None = None) -> bool:
        """One atomic 16-word read. Returns True if any reported value or
        status changed (rounded to 0.1 °C, what /api/status shows)."""
        now = time.time() if now is None else now
        with self._lock:
            before = self._public_locked(now)
            self._last_poll = (list(words), now)
            self._last_poll_ok_at = now
            for sensor in SENSORS:
                self._track_locked(sensor, words[sensor.register], now)
            for key, (number, at) in list(self._pending.items()):
                if abs(now - at) <= ANCHOR_PAIRING_SECONDS:
                    self._anchor_locked(BY_KEY[key], number, words[BY_KEY[key].register], now)
                del self._pending[key]
            changed = _comparable(self._public_locked(now)) != _comparable(before)
            snapshot = self._save_locked(now, force=changed and self._status_changed(before, now))
        self._write(snapshot)
        return changed

    def on_display(self, key: str, number: float | None, at: float | None = None) -> bool:
        """A NÄYTÄ DATA temperature seen on the display. Anchors at once if
        a poll is close enough in time, otherwise at the next poll."""
        sensor = BY_KEY.get(key)
        if sensor is None or number is None:
            return False
        at = time.time() if at is None else at
        with self._lock:
            before = self._public_locked(at)
            poll = self._last_poll
            if poll is not None and abs(at - poll[1]) <= ANCHOR_PAIRING_SECONDS:
                self._anchor_locked(sensor, number, poll[0][sensor.register], at)
                self._pending.pop(key, None)
            else:
                self._pending[key] = (number, at)
            changed = _comparable(self._public_locked(at)) != _comparable(before)
            snapshot = self._save_locked(at, force=changed)
        self._write(snapshot)
        return changed

    def _anchor_locked(self, sensor: Sensor, display_c: float, low: int, now: float) -> None:
        value = nearest(low, display_c * 100)
        prev = self._values.get(sensor.key)
        status = "ok" if abs(value / 100 - display_c) <= ANCHOR_TOLERANCE_C else "mismatch"
        if prev and prev.get("status") == "ok" and prev.get("hundredths") != value and abs(prev["hundredths"] - value) >= WRAP // 2:
            log.warning("Sensor %s: tracking had slipped (%.2f), re-anchored to %.2f from display %s",
                        sensor.key, prev["hundredths"] / 100, value / 100, display_c)
        if status == "mismatch" and (not prev or prev.get("status") != "mismatch"):
            log.warning("Sensor %s (T%d, reg %d): register low byte %d doesn't fit display %s°C (nearest %.2f)",
                        sensor.key, sensor.t_number, sensor.register, low, display_c, value / 100)
        self._values[sensor.key] = {
            "hundredths": value, "status": status, "at": now,
            "anchored_at": now, "low": low, "display": display_c,
        }
        # A re-anchor can move the value by whole 2.56 °C windows: not a swing.
        self._history.pop(sensor.key, None)

    def _track_locked(self, sensor: Sensor, low: int, now: float) -> None:
        prev = self._values.get(sensor.key)
        if prev is None:
            return  # unanchored: nothing to track from
        value = nearest(low, prev["hundredths"])
        step_c = abs(value - prev["hundredths"]) / 100
        gap = now - prev["at"]
        status = prev["status"]
        uncertain_since = prev.get("uncertain_since")
        if status == "uncertain" and uncertain_since is None:
            uncertain_since = now  # saved by a version without the field: count from here
        if status == "ok" and (step_c > AMBIGUOUS_STEP_C or gap * sensor.max_rate_c_per_s > AMBIGUOUS_STEP_C):
            status = "uncertain"
            uncertain_since = now
            log.info("Sensor %s now uncertain: step %.2f °C, gap %.0f s", sensor.key, step_c, gap)
        self._values[sensor.key] = {**prev, "hundredths": value, "status": status, "at": now, "low": low,
                                    "uncertain_since": uncertain_since}
        if sensor.swing_rate_c_per_s is not None:
            hist = self._history.setdefault(sensor.key, [])
            hist.append((now, value))
            while hist and now - hist[0][0] > SWING_HISTORY_SECONDS:
                hist.pop(0)

    def uncertain_since(self, key: str) -> float | None:
        """When `key` went uncertain, or None if it isn't uncertain now."""
        with self._lock:
            v = self._values.get(key)
            if not v or v.get("status") != "uncertain":
                return None
            return v.get("uncertain_since")

    def swinging(self, now: float | None = None, only_ok: bool = False) -> list[str]:
        """Sensors currently changing faster than their swing_rate, judged
        over the last SWING_WINDOW_SECONDS of tracked values. Uncertain values
        count too unless only_ok: the window may be wrong, but the rate of
        change isn't. Fast polling passes only_ok, since polling faster can't
        recover a value that tracking has already lost."""
        now = time.time() if now is None else now
        out = []
        with self._lock:
            for sensor in SENSORS:
                hist = self._history.get(sensor.key)
                if sensor.swing_rate_c_per_s is None or not hist or now - hist[-1][0] > SWING_WINDOW_SECONDS:
                    continue
                if only_ok and (self._values.get(sensor.key) or {}).get("status") != "ok":
                    continue
                t1, v1 = hist[-1]
                # The newest sample at least SWING_WINDOW_SECONDS older than the latest.
                base = next((s for s in reversed(hist) if t1 - s[0] >= SWING_WINDOW_SECONDS), None)
                if base is None:
                    continue
                t0, v0 = base
                if abs(v1 - v0) / 100 / (t1 - t0) >= sensor.swing_rate_c_per_s:
                    out.append(sensor.key)
        return out

    def fast_poll_useful(self) -> bool:
        """True while some swing-watched sensor is still "ok", i.e. faster
        polling could keep it tracked."""
        with self._lock:
            return any(
                (self._values.get(s.key) or {}).get("status") == "ok"
                for s in SENSORS if s.swing_rate_c_per_s is not None
            )

    # -- output ---------------------------------------------------------------

    def _status_changed(self, before: dict, now: float) -> bool:
        after = self._public_locked(now)
        return any((before.get(k) or {}).get("status") != (after.get(k) or {}).get("status") for k in BY_KEY)

    def _public_locked(self, now: float) -> dict[str, dict]:
        stale = self._last_poll_ok_at is None or now - self._last_poll_ok_at > STALE_SECONDS
        out = {}
        for sensor in SENSORS:
            v = self._values.get(sensor.key)
            if v is None:
                out[sensor.key] = {"value": None, "status": "unanchored",
                                   "t_number": sensor.t_number, "register": sensor.register, "at": None}
                continue
            out[sensor.key] = {
                "value": round(v["hundredths"] / 100, 1),
                "status": "stale" if stale and v["status"] == "ok" else v["status"],
                "t_number": sensor.t_number, "register": sensor.register,
                "at": v["at"], "anchored_at": v.get("anchored_at"),
            }
        return out

    def snapshot(self, now: float | None = None, detail: bool = False) -> dict[str, dict]:
        """Per sensor: value (°C, 0.1), status (ok / uncertain / mismatch /
        stale / unanchored), T-number, register, times. detail adds the
        hundredths value and the raw low byte."""
        now = time.time() if now is None else now
        with self._lock:
            out = self._public_locked(now)
            if detail:
                for key, v in self._values.items():
                    out[key]["hundredths"] = v["hundredths"]
                    out[key]["low"] = v.get("low")
                    out[key]["age_s"] = now - v["at"]
            return out
