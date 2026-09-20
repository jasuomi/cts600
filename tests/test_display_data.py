"""Offline tests for display_data.py and the walk in data_walk.py.

Run from the project root: python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cts600 import data_walk, display_data, protocol, state as state_mod  # noqa: E402
from cts600.display_data import classify  # noqa: E402

_CHARSET = {"Ä": 0x0B, "Ö": 0x0C, "°": 0xDF}


def encode_line(text: str, n_words: int = 5) -> list[int]:
    raw = bytes(_CHARSET.get(c, ord(c)) for c in text.ljust(8)[:8]) + b"\x00\x00"
    raw = raw.ljust(2 * n_words, b"\x00")
    return [(raw[2 * i] << 8) | raw[2 * i + 1] for i in range(n_words)]


def reg_block(start: int, words: list[int]) -> protocol.RegBlock:
    payload = protocol.encode_reg_block(start, words)
    return protocol.parse_reg_block(payload)


class ClassifyTests(unittest.TestCase):
    def test_idle(self):
        self.assertEqual(classify("AUTO    ..", ">3< 22°C..").key, "idle")

    def test_nayta_menu(self):
        self.assertEqual(classify("NÄYTÄ", "HÄLYT").key, "NAYTA")
        self.assertEqual(classify("NÄYTÄ", "DATA").key, "NAYTA_DATA")

    def test_temperature(self):
        s = classify("HUONE   ..", "T15 23°C..")
        self.assertEqual((s.key, s.reading, s.value, s.number), ("HUONE", "room", "23°C", 23.0))
        s = classify("ULKOILMA", "T1  -5°C")
        self.assertEqual((s.reading, s.value), ("outdoor", "-5°C"))

    def test_wrong_t_number_is_unknown(self):
        # line 1 already changed to HUONE, line 2 still from ULKOILMA
        self.assertEqual(classify("HUONE", "T1  11°C").key, "unknown")
        # an editable screen with a similar name
        self.assertEqual(classify("MENO MIN", "20°C").key, "unknown")

    def test_text_screens(self):
        # Recognised so a walk can verify them, but no longer collected.
        for line1, line2, key in (
            ("POISTPUH", "TEHO 3", "POISTPUH"), ("TULOPUH", "TEHO 3", "TULOPUH"),
            ("TYYPPI", "VP 18cek", "TYYPPI"), ("SOFTA 2", "1.01", "SOFTA2"),
            ("SOFTA", "1  1.22", "SOFTA1"),
        ):
            s = classify(line1, line2)
            self.assertEqual((s.key, s.reading), (key, None))
        self.assertEqual(classify("SOFTA", "").key, "unknown")
        self.assertEqual(classify("NYKYTILA", "JÄÄH+VES").reading, "status")

    def test_store_persists(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "readings.json"
            store = display_data.ReadingsStore(path)
            self.assertTrue(store.update(classify("HUONE", "T15 23°C"), now=100.0))
            self.assertFalse(store.update(classify("HUONE", "T15 23°C"), now=101.0))
            again = display_data.ReadingsStore(path)
            self.assertEqual(again.snapshot(now=160.0)["room"]["value"], "23°C")
            self.assertAlmostEqual(again.snapshot(now=160.0)["room"]["age_s"], 60.0)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        return self.t


SCREENS_DATA = [
    ("NYKYTILA", "AUTO"), ("HUONE", "T15 23°C"), ("ULKOILMA", "T1  11°C"),
    ("VESI-YLÄ", "T11 59°C"), ("VESI-ALA", "T12 48°C"), ("MENO", "T14 22°C"),
    ("LAUHDUT.", "T5  34°C"), ("TULOPUH", "TEHO 2"), ("POISTPUH", "TEHO 3"),
    ("SOFTA 1", "1.22"), ("SOFTA 2", "1.01"), ("TYYPPI", "VP 18cek"),
]


class SimPanel:
    """Menu model of the panel as seen live: Up from idle shows NÄYTÄ DATA,
    Enter opens the list, Down scrolls it (bounded), Esc goes back one level. Retransmits one display
    line every 0.5 s, and applies a key 0.3 s after it's pressed."""

    def __init__(self, state, clock: FakeClock) -> None:
        self.state = state
        self.clock = clock
        self.where = ("idle", 0)
        self.pending: list[tuple[float, str]] = []
        self.presses: list[str] = []
        self.next_tx = clock.t
        self.tx_line = 0
        self.overrides: dict[tuple[str, int], tuple[str, str]] = {}
        self.on_press = None
        self.scheduled: list[tuple[float, object]] = []

    def lines(self) -> tuple[str, str]:
        if self.where in self.overrides:
            return self.overrides[self.where]
        kind, i = self.where
        if kind == "idle":
            return ("AUTO", ">3< 22°C")
        if kind == "nayta":
            return ("NÄYTÄ", ["HÄLYT", "DATA"][i])
        return SCREENS_DATA[i]

    def press(self, key: str) -> None:
        self.presses.append(key)
        self.pending.append((self.clock.t + 0.3, key))
        self.clock.t += 0.4  # press + release
        if self.on_press:
            self.on_press(self, key)

    def _apply(self, key: str) -> None:
        kind, i = self.where
        if kind == "idle" and key == "up":
            self.where = ("nayta", 1)
        elif kind == "nayta" and key == "up":
            self.where = ("nayta", max(i - 1, 0))
        elif kind == "nayta" and key == "enter" and i == 1:
            self.where = ("data", 0)
        elif kind == "nayta" and key == "esc":
            self.where = ("idle", 0)
        elif kind == "data" and key == "down":
            self.where = ("data", min(i + 1, len(SCREENS_DATA) - 1))
        elif kind == "data" and key == "esc":
            self.where = ("nayta", 1)

    def sleep(self, dt: float) -> None:
        end = self.clock.t + dt
        while True:
            due = [p for p in self.pending if p[0] <= end]
            nxt = min([self.next_tx] + [p[0] for p in due])
            if nxt > end:
                break
            self.clock.t = max(self.clock.t, nxt)
            for s in [s for s in self.scheduled if s[0] <= self.clock.t]:
                self.scheduled.remove(s)
                s[1]()
            for p in [p for p in self.pending if p[0] <= self.clock.t]:
                self.pending.remove(p)
                self._apply(p[1])
            if self.next_tx <= self.clock.t:
                l1, l2 = self.lines()
                reg, text = ((display_data.LINE1_REG, l1), (display_data.LINE2_REG, l2))[self.tx_line]
                self.state.note_reg_block(3, 0x42, reg_block(reg, encode_line(text)))
                self.tx_line ^= 1
                self.next_tx += 0.5
        self.clock.t = end


class WalkTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch.object(state_mod, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = state_mod.DeviceState(readings_path=None)
        self.panel = SimPanel(self.state, self.clock)
        self.healthy = True
        self.walker = data_walk.DataWalker(
            self.state, press=self.panel.press, bus_healthy=lambda: self.healthy,
            clock=self.clock.time, sleep=self.panel.sleep,
        )
        self.panel.sleep(2.0)  # let the idle screen settle

    def test_full_walk(self):
        result = self.walker.run()
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.panel.where, ("idle", 0))
        self.assertEqual(self.panel.presses[:2], ["up", "enter"])
        self.assertEqual(self.panel.presses.count("up"), 1)
        self.assertEqual(self.panel.presses.count("enter"), 1)
        self.assertEqual(self.panel.presses.count("down"), len(SCREENS_DATA) - 1)
        readings = self.state.readings.snapshot()
        self.assertEqual(set(readings), set(display_data.READING_KEYS))
        self.assertEqual(readings["tank_top"]["value"], "59°C")
        self.assertNotIn("sw2", readings)
        self.assertNotIn("unit_type", readings)
        self.assertFalse(self.state.walk_status["running"])

    def test_target_walk_stops_at_condenser(self):
        result = self.walker.run(target="LAUHDUT", purpose="condenser re-anchor (automatic)")
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.panel.where, ("idle", 0))
        # Up, Enter (NYKYTILA), Down x6 to LAUHDUT, Esc x2.
        self.assertEqual(self.panel.presses, ["up", "enter"] + ["down"] * 6 + ["esc", "esc"])
        readings = self.state.readings.snapshot()
        self.assertEqual(readings["condenser"]["value"], "34°C")
        self.assertNotIn("supply_fan", readings)
        self.assertTrue(self.state.walk_status["message"].startswith("condenser re-anchor (automatic): "))

    def test_target_walk_rejects_unknown_screen(self):
        with self.assertRaises(ValueError):
            self.walker.run(target="NOPE")
        self.assertEqual(self.panel.presses, [])

    def test_refuses_when_not_idle(self):
        self.panel.where = ("data", 3)
        self.panel.sleep(2.0)
        result = self.walker.run()
        self.assertEqual(result["outcome"], "stopped")
        self.assertIn("idle", result["message"])
        self.assertEqual(self.panel.presses, [])

    def test_refuses_right_after_physical_key(self):
        self.state.note_reg_block(1, 0x42, reg_block(state_mod.AID_REG, [0x0004]))
        result = self.walker.run()
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(self.panel.presses, [])

    def test_stops_on_physical_key_mid_walk(self):
        def human(panel, key):
            if len(panel.presses) == 5:  # a person presses Down 1.5 s after our 5th press
                panel.scheduled.append((panel.clock.t + 1.5, lambda: panel.state.note_reg_block(
                    1, 0x42, reg_block(state_mod.AID_REG, [0x0004]))))
        self.panel.on_press = human
        result = self.walker.run()
        self.assertEqual(result["outcome"], "stopped")
        self.assertIn("pressed on the panel", result["message"])
        self.assertEqual(len(self.panel.presses), 5)

    def test_stops_on_unexpected_screen(self):
        self.panel.overrides[("data", 0)] = ("KESKUS", "ON")
        result = self.walker.run()
        self.assertEqual(result["outcome"], "stopped")
        self.assertIn("KESKUS", result["message"])
        self.assertEqual(self.panel.presses, ["up", "enter"])

    def test_stops_if_up_does_not_land_on_data(self):
        self.panel.overrides[("nayta", 1)] = ("NÄYTÄ", "HÄLYT")
        result = self.walker.run()
        self.assertEqual(result["outcome"], "stopped")
        self.assertIn("HÄLYT", result["message"])
        self.assertEqual(self.panel.presses, ["up"])

    def test_stops_when_bus_unhealthy(self):
        def degrade(panel, key):
            if len(panel.presses) == 2:
                self.healthy = False
        self.panel.on_press = degrade
        result = self.walker.run()
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(len(self.panel.presses), 2)

    def test_cancel_mid_list_returns_to_idle(self):
        def cancel_after_4(panel, key):
            if len(panel.presses) == 4:
                self.walker.cancel()
        self.panel.on_press = cancel_after_4
        result = self.walker.run()
        self.assertEqual(result["outcome"], "cancelled", result)
        self.assertIn("back on idle", result["message"])
        self.assertEqual(self.panel.where, ("idle", 0))
        self.assertEqual(self.panel.presses, ["up", "enter", "down", "down", "esc", "esc"])
        self.assertFalse(self.state.walk_status["running"])
        self.assertEqual(self.state.walk_status["outcome"], "cancelled")

    def test_cancel_after_up_only(self):
        self.panel.on_press = lambda panel, key: self.walker.cancel()
        result = self.walker.run()
        self.assertEqual(result["outcome"], "cancelled", result)
        self.assertEqual(self.panel.presses, ["up", "esc"])
        self.assertEqual(self.panel.where, ("idle", 0))

    def test_cancel_when_not_running(self):
        self.assertFalse(self.walker.cancel())
        self.assertEqual(self.walker.run()["outcome"], "done")  # a stale cancel doesn't carry over

    def test_passive_harvest_ignores_mixed_lines(self):
        self.panel.where = ("data", 2)  # ULKOILMA
        self.panel.sleep(2.0)
        # line 1 switches to HUONE while line 2 still shows ULKOILMA's value
        self.state.note_reg_block(3, 0x42, reg_block(display_data.LINE1_REG, encode_line("HUONE")))
        self.assertNotIn("room", self.state.readings.snapshot())
        self.assertEqual(self.state.readings.snapshot()["outdoor"]["value"], "11°C")


if __name__ == "__main__":
    unittest.main()
