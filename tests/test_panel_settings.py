"""Offline tests for panel_settings.py and DeviceState.status().

Run from the project root: python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_display_data import FakeClock, SimPanel, encode_line, reg_block

from cts600 import data_walk, display_data, panel_settings, protocol, state as state_mod
from cts600.display_data import MODE_ORDER, classify, parse_idle

MODE_TEXT = {"auto": "AUTO", "cool": "VIILEN", "heat": "LÄMPÖ"}


class EditPanel(SimPanel):
    """SimPanel plus the idle screen's field editing, as confirmed live:
    Enter selects setpoint -> mode -> fan (then back to none), Up/Down
    change the selected field (bounded), Enter confirms and moves on, Esc
    discards the unconfirmed change and leaves the selection. Off/On keys
    switch the unit."""

    def __init__(self, state, clock) -> None:
        super().__init__(state, clock)
        self.saved = {"setpoint": 22, "mode": "auto", "fan": 3}
        self.edit = dict(self.saved)
        self.field = None  # None, "setpoint", "mode", "fan"
        self.on = True
        self.flags = ""
        self.limits = {"setpoint": (10, 26), "fan": (1, 4)}
        self.ignore_next = 0  # lose this many presses

    def lines(self):
        if self.where[0] == "idle":
            if not self.on:
                return ("OFF", "")
            v = self.edit
            return (f"{MODE_TEXT[v['mode']]:<6}{self.flags}"[:8], f">{v['fan']}< {v['setpoint']}°C")
        return super().lines()

    def _apply(self, key):
        if self.ignore_next:
            self.ignore_next -= 1
            return
        if key == "off":
            self.on, self.field, self.where = False, None, ("idle", 0)
            return
        if key == "on":
            self.on = True
            return
        if self.where[0] != "idle" or not self.on or (self.field is None and key != "enter"):
            return super()._apply(key)
        order = [None, "setpoint", "mode", "fan"]
        if key == "enter":
            if self.field is not None:
                self.saved[self.field] = self.edit[self.field]
            self.field = order[(order.index(self.field) + 1) % len(order)]
        elif key == "esc":
            self.edit = dict(self.saved)
            self.field = None
        elif key in ("up", "down"):
            step = 1 if key == "up" else -1
            if self.field == "mode":
                i = MODE_ORDER.index(self.edit["mode"]) - step  # Up moves toward AUTO
                self.edit["mode"] = MODE_ORDER[min(max(i, 0), len(MODE_ORDER) - 1)]
            else:
                lo, hi = self.limits[self.field]
                self.edit[self.field] = min(max(self.edit[self.field] + step, lo), hi)


class ParseIdleTests(unittest.TestCase):
    def test_modes_and_flags(self):
        p = parse_idle(classify("AUTO  W*..", ">3< 22°C.."))
        self.assertEqual((p["mode"], p["setpoint"], p["fan"], p["water_heating"], p["boost"]),
                         ("auto", 22, 3, True, True))
        p = parse_idle(classify("VIILEN*.", ">2< 20°C*."))  # trailing "*." is the selection marker
        self.assertEqual((p["mode"], p["boost"], p["fan"]), ("cool", False, 2))
        self.assertEqual(parse_idle(classify("LÄMPÖ", ">1< 19°C"))["mode"], "heat")
        self.assertEqual(parse_idle(classify("AUTOW", ">1< 19°C"))["water_heating"], True)
        off = parse_idle(classify("OFF", ""))
        self.assertEqual((off["mode"], off["setpoint"]), ("off", None))
        self.assertIsNone(parse_idle(classify("HUONE", "T15 23°C")))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch.object(state_mod, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = state_mod.DeviceState(readings_path=None)
        self.panel = EditPanel(self.state, self.clock)
        self.editor = panel_settings.SettingsEditor(
            self.state, press=self.panel.press, clock=self.clock.time, sleep=self.panel.sleep,
        )
        self.panel.sleep(3.0)

    def test_setpoint(self):
        result = self.editor.apply(setpoint=25)
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.panel.saved["setpoint"], 25)
        self.assertEqual(self.panel.presses, ["enter", "up", "up", "up", "enter", "esc"])
        self.assertEqual(self.state.status()["panel"]["setpoint"], 25)
        self.assertFalse(self.state.edit_status["running"])

    def test_setpoint_already_set_presses_nothing(self):
        result = self.editor.apply(setpoint=22)
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(self.panel.presses, [])

    def test_setpoint_limit(self):
        result = self.editor.apply(setpoint=30)
        self.assertEqual(result["outcome"], "done", result)
        self.assertIn("limit", result["message"])
        self.assertEqual(self.panel.saved["setpoint"], 26)

    def test_lost_press_is_retried(self):
        def lose_first_up(panel, key):
            if key == "up" and panel.presses.count("up") == 1:
                panel.ignore_next = 1
        self.panel.on_press = lose_first_up
        result = self.editor.apply(setpoint=24)
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.panel.saved["setpoint"], 24)

    def test_fan_and_mode(self):
        result = self.editor.apply(mode="heat", fan=1)
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual((self.panel.saved["mode"], self.panel.saved["fan"]), ("heat", 1))
        self.assertEqual(self.panel.presses[:5], ["enter", "enter", "down", "down", "enter"])

    def test_mode_reverses_when_order_differs(self):
        self.panel.edit["mode"] = self.panel.saved["mode"] = "heat"
        with mock.patch.object(display_data, "MODE_ORDER", ("heat", "auto", "cool")):
            result = self.editor.apply(mode="auto")
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.panel.saved["mode"], "auto")

    def test_lost_enter_is_caught_and_undone(self):
        # Enter lost: Up from plain idle opens NÄYTÄ DATA instead of editing.
        def lose_enter(panel, key):
            if key == "enter" and panel.presses.count("enter") == 1:
                panel.ignore_next = 1
        self.panel.on_press = lose_enter
        result = self.editor.apply(setpoint=24)
        self.assertEqual(result["outcome"], "stopped")
        self.assertIn("pressed Esc", result["message"])
        self.assertEqual(self.panel.presses, ["enter", "up", "esc"])
        self.assertEqual(self.panel.where, ("idle", 0))
        self.assertEqual(self.panel.saved["setpoint"], 22)

    def test_wrong_field_is_discarded(self):
        # Enter registered twice: Up changes mode, not the setpoint.
        def double_enter(panel, key):
            if key == "enter" and panel.presses.count("enter") == 1:
                panel.pending.append((panel.clock.t, "enter"))
        self.panel.edit["mode"] = self.panel.saved["mode"] = "cool"
        self.panel.on_press = double_enter
        result = self.editor.apply(setpoint=24)
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(self.panel.saved, {"setpoint": 22, "mode": "cool", "fan": 3})
        self.assertEqual(self.panel.edit, self.panel.saved)

    def test_off_and_on_with_mode(self):
        self.assertEqual(self.editor.apply(mode="off")["outcome"], "done")
        self.assertFalse(self.panel.on)
        self.assertEqual(self.state.status()["panel"]["mode"], "off")
        result = self.editor.apply(mode="cool")
        self.assertEqual(result["outcome"], "done", result)
        self.assertTrue(self.panel.on)
        self.assertEqual(self.panel.saved["mode"], "cool")

    def test_refuses_setpoint_when_off(self):
        self.panel.on = False
        self.panel.sleep(3.0)
        result = self.editor.apply(setpoint=24)
        self.assertEqual(result["outcome"], "stopped")
        self.assertIn("off", result["message"])
        self.assertEqual(self.panel.presses, [])

    def test_stops_on_physical_key(self):
        def human(panel, key):
            if len(panel.presses) == 2:
                panel.scheduled.append((panel.clock.t + 0.8, lambda: panel.state.note_reg_block(
                    1, 0x42, reg_block(state_mod.AID_REG, [0x0002]))))
        self.panel.on_press = human
        result = self.editor.apply(setpoint=26)
        self.assertEqual(result["outcome"], "stopped")
        self.assertIn("pressed on the panel", result["message"])
        self.assertEqual(len(self.panel.presses), 2)

    def test_validation(self):
        for kwargs in ({}, {"setpoint": 50}, {"fan": 0}, {"mode": "dry"}, {"mode": "off", "fan": 2}):
            with self.assertRaises(ValueError):
                panel_settings.validate(**kwargs)

    def test_shared_lock_with_walk(self):
        walker = data_walk.DataWalker(self.state, press=self.panel.press, lock=self.editor._lock)
        seen = []
        self.panel.on_press = lambda panel, key: seen.append(walker.running)
        self.editor.apply(setpoint=23)
        self.assertTrue(seen and all(seen))
        self.assertFalse(walker.running)
        with self.editor._lock:
            with self.assertRaises(panel_settings.OperationBusyError):
                self.editor.apply(setpoint=24)
            with self.assertRaises(data_walk.WalkBusyError):
                walker.run()


class StatusTests(unittest.TestCase):
    def test_status_shape(self):
        state = state_mod.DeviceState(readings_path=None)
        for reg, text in ((display_data.LINE1_REG, "AUTO  W"), (display_data.LINE2_REG, ">3< 22°C")) * 2:
            state.note_reg_block(3, 0x42, reg_block(reg, encode_line(text)))
        state.note_bit_block(3, 0x41, protocol.BitBlock(0x0100, 2, 1, bytes([0b01])))
        status = state.status()
        self.assertEqual(status["screen"], "idle")
        self.assertEqual(status["panel"]["mode"], "auto")
        self.assertTrue(status["panel"]["water_heating"])
        self.assertEqual(status["led"], {"on": True, "blink": False})
        self.assertIsNone(status["device"])
        self.assertFalse(status["edit"]["running"])


class SnapshotTests(unittest.TestCase):
    """state.snapshot() (the full /api/state and /ws payload app.js's
    settings controls read) must carry current mode/setpoint/fan too, and
    give edit_status the same started_age_s/finished_age_s treatment
    walk_status already gets -- both are "Operation, N seconds ago" texts
    in the same UI."""

    def test_snapshot_carries_panel(self):
        state = state_mod.DeviceState(readings_path=None)
        for reg, text in ((display_data.LINE1_REG, "AUTO"), (display_data.LINE2_REG, ">3< 22°C")) * 2:
            state.note_reg_block(3, 0x42, reg_block(reg, encode_line(text)))
        snap = state.snapshot()
        self.assertEqual(snap["panel"]["mode"], "auto")
        self.assertEqual(snap["panel"]["setpoint"], 22)

    def test_snapshot_edit_status_has_ages_like_walk_status(self):
        state = state_mod.DeviceState(readings_path=None)
        state.set_edit_status(running=False, outcome="done", message="fan set to 3",
                               steps=4, started_at=100.0, finished_at=105.0)
        snap = state.snapshot()
        self.assertIn("started_age_s", snap["edit"])
        self.assertIn("finished_age_s", snap["edit"])
        self.assertGreater(snap["edit"]["finished_age_s"], 0)


if __name__ == "__main__":
    unittest.main()
