"""Readings from the panel's "NÄYTÄ DATA" (show data) screens.

The panel traffic never carries temperatures as numbers, so this module
classifies a two-line data screen and pulls the value out of it. Since
2026-09-17 these whole-°C readings also anchor the live register
temperatures (sensor_regs.py: FC4 0x0000-0x000F holds only the low byte of
each value, which needs a reference). Pure functions plus a small
JSON-backed store; no bus access.

Menu path, confirmed live on 2026-09-16 (first Update walk, then by the
panel operator): from idle, one Up shows NÄYTÄ DATA, Enter opens the
list, Down steps through it. DATA_SCREENS is that list in order; data_walk.py
relies on the order to verify each Down press.

Line formats come from captures and the 2026-09-14 menu walk (see
regmap.py's comment above DISPLAY_REG_START). Temperature screens carry
their T-number on line 2 ("T15 23°C"); requiring the expected T-number
keeps a stale line 2 from the previous screen, or an editable screen
with a similar name (MENO MIN/MAX), from being read as a data screen.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

LINE1_REG = 0x0200
LINE2_REG = 0x020A
LINE_END_REG = 0x0212  # the display buffer is 18 words (FC3 sweep, 2026-09-16)

# (screen key, line-1 prefix, reading key or None, T-number or None)
# Temperature screens need their T-number on line 2; the rest store line 2
# as text. A reading key of None means the screen is still recognised and
# verified while walking past it, but its value is not kept: the fan levels,
# software versions and unit type were dropped from the dashboard
# (2026-09-17, user request) since nothing reads them and, having no
# register, they could only ever show the last walk's value.
DATA_SCREENS: list[tuple[str, str, str | None, int | None]] = [
    ("NYKYTILA", "NYKYTILA", "status", None),
    ("HUONE", "HUONE", "room", 15),
    ("ULKOILMA", "ULKOILMA", "outdoor", 1),
    ("VESI-YLÄ", "VESI-YLÄ", "tank_top", 11),
    ("VESI-ALA", "VESI-ALA", "tank_bottom", 12),
    ("MENO", "MENO", "supply", 14),
    ("LAUHDUT", "LAUHDUT", "condenser", 5),
    ("TULOPUH", "TULOPUH", None, None),
    ("POISTPUH", "POISTPUH", None, None),
    ("SOFTA1", "SOFTA", None, None),
    ("SOFTA2", "SOFTA", None, None),
    ("TYYPPI", "TYYPPI", None, None),
]
SCREEN_ORDER = {key: i for i, (key, *_rest) in enumerate(DATA_SCREENS)}
LAST_SCREEN = DATA_SCREENS[-1][0]

READING_KEYS = [reading for _k, _p, reading, _t in DATA_SCREENS if reading]

_TEMP_RE = re.compile(r"T\s*(\d+)\s+(-?\d+)\s*°C")
_IDLE_LINE2_RE = re.compile(r"^>(\d)< *(\d+)°C")  # same shape planner.py parses
_SOFTA_RE = re.compile(r"SOFTA\s*(\d)\s+(\d+\.\d+)")


def clean(text: str | None) -> str:
    """Strip padding and the trailing attribute bytes (rendered as '.', '*')
    that follow the 8 visible characters of a line."""
    return (text or "").rstrip(" .*").strip()


@dataclass
class Screen:
    key: str               # "idle", "off", "NAYTA", "NAYTA_DATA", a DATA_SCREENS key, or "unknown"
    line1: str
    line2: str
    reading: str | None = None    # READING_KEYS entry, for data screens
    value: str | None = None      # display value, e.g. "23°C", "TEHO 3", "1.22"
    number: float | None = None   # numeric value when there is one
    raw1: str = ""                # line 1 before clean(), for the idle flags


def classify(line1: str | None, line2: str | None) -> Screen:
    screen = _classify(clean(line1), clean(line2))
    screen.raw1 = line1 or ""
    return screen


def _classify(l1: str, l2: str) -> Screen:
    if _IDLE_LINE2_RE.match(l2):
        return Screen("idle", l1, l2)
    if l1.startswith("OFF"):  # seen live: "OFF" -> "AUTO" after pressing On
        return Screen("off", l1, l2)
    if l1.startswith("NÄYTÄ"):
        # Line 2 shows the highlighted sub-item; line 1 alone for some layouts.
        return Screen("NAYTA_DATA" if "DATA" in f"{l1} {l2}" else "NAYTA", l1, l2)

    for key, prefix, reading, t_number in DATA_SCREENS:
        if not l1.startswith(prefix):
            continue
        if t_number is not None:
            m = _TEMP_RE.search(l2)
            if not m or int(m.group(1)) != t_number:
                return Screen("unknown", l1, l2)
            temp = int(m.group(2))
            return Screen(key, l1, l2, reading, f"{temp}°C", float(temp))
        if prefix == "SOFTA":
            # Still told apart, so a walk can verify both SOFTA screens.
            m = _SOFTA_RE.search(f"{l1} {l2}")
            if not m or m.group(1) not in ("1", "2"):
                return Screen("unknown", l1, l2)
            return Screen(f"SOFTA{m.group(1)}", l1, l2, reading, m.group(2))
        if not l2:
            return Screen("unknown", l1, l2)
        return Screen(key, l1, l2, reading, l2)
    return Screen("unknown", l1, l2)


# Mode names on the idle screen's line 1, by prefix. AUTO, VIILEN(NYS) and
# LÄMPÖ/LÄMMITYS are from captures; OFF from the live On test. The others
# are guesses kept loose on purpose (8-character LCD abbreviations).
IDLE_MODES = (
    ("AUTO", "auto"), ("VIIL", "cool"), ("JÄÄH", "cool"),
    ("LÄM", "heat"), ("OFF", "off"), ("POIS", "off"),
)
# Order of the mode field's values, top to bottom. From the reference
# integration (English menu: Up x3 reaches AUTO, then Down gives COOL,
# HEAT). The settings editor checks every step, so a different order on
# this unit costs one wasted press, not a wrong setting.
MODE_ORDER = ("auto", "cool", "heat")
IDLE_FLAGS = {"W": "water_heating", "*": "boost"}


def parse_idle(screen: Screen) -> dict | None:
    """The idle screen as values: mode, setpoint, fan and the flags after
    the mode name (W = water heating, * = ventilation boost). None for any
    other screen. Only the 8 visible characters of line 1 are used: the
    trailing attribute bytes can render as "*" too (a selection marker)."""
    if screen.key not in ("idle", "off"):
        return None
    tokens = (screen.raw1 or screen.line1)[:8].split()
    name = tokens[0] if tokens else ""
    flags = "".join(tokens[1:])
    while len(name) > 1 and name[-1] in IDLE_FLAGS:  # "AUTOW" as well as "AUTO W"
        flags, name = name[-1] + flags, name[:-1]
    m = _IDLE_LINE2_RE.match(screen.line2)
    return {
        "mode": next((mode for prefix, mode in IDLE_MODES if name.startswith(prefix)), "unknown"),
        "mode_text": name,
        "setpoint": int(m.group(2)) if m else None,
        "fan": int(m.group(1)) if m else None,
        **{flag: ch in flags for ch, flag in IDLE_FLAGS.items()},
    }


class ReadingsStore:
    """Last seen value per reading, persisted to a small JSON file so a
    restart doesn't blank the page. Thread-safe. Writes are throttled:
    immediately when a value changes, otherwise at most once a minute."""

    SAVE_INTERVAL_SECONDS = 60.0

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._readings: dict[str, dict] = {}
        self._last_save = 0.0
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._readings = {k: v for k, v in data.items() if k in READING_KEYS}
        except Exception:
            log.exception("Couldn't read %s; starting with no readings", self._path)

    def update(self, screen: Screen, now: float | None = None) -> bool:
        """Record a data screen's value. Returns True if the value changed
        (or was seen for the first time)."""
        if screen.reading is None:
            return False
        now = time.time() if now is None else now
        with self._lock:
            prev = self._readings.get(screen.reading)
            changed = prev is None or prev.get("value") != screen.value
            self._readings[screen.reading] = {
                "value": screen.value, "number": screen.number,
                "text": f"{screen.line1} {screen.line2}".strip(),
                "at": now,
            }
            due = changed or now - self._last_save >= self.SAVE_INTERVAL_SECONDS
            if due:
                self._last_save = now
                snapshot = dict(self._readings)
        if due:
            self._save(snapshot)
        return changed

    def _save(self, readings: dict) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(readings, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception:
            log.exception("Couldn't save readings to %s", self._path)

    def snapshot(self, now: float | None = None) -> dict[str, dict]:
        now = time.time() if now is None else now
        with self._lock:
            return {
                k: {**v, "age_s": now - v["at"]}
                for k, v in self._readings.items()
            }
