"""Offline tests for sensor_regs.py: low-byte reconstruction, anchoring and tracking.

Run from the project root: python -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from test_display_data import encode_line, reg_block

from cts600 import display_data, sensor_regs, state as state_mod
from cts600.sensor_regs import SensorTracker, nearest

# Step 0, 2026-09-17 11:19:05: first atomic FC4 0x0000-0x000F read after an Update walk.
STEP0_WORDS = [22, 0, 0, 0, 144, 160, 0, 22, 0, 0, 68, 10, 0, 175, 11, 0]
STEP0_DISPLAY = {"room": 23, "outdoor": 13, "tank_top": 62, "tank_bottom": 49, "supply": 23, "condenser": 14}


def words_with(**lows: int) -> list[int]:
    words = list(STEP0_WORDS)
    for key, low in lows.items():
        words[sensor_regs.BY_KEY[key].register] = low
    return words


class NearestTests(unittest.TestCase):
    def test_step0_values(self):
        self.assertEqual(nearest(22, 1300), 1302)
        self.assertEqual(nearest(144, 1400), 1424)
        self.assertEqual(nearest(68, 6200), 6212)
        self.assertEqual(nearest(10, 4900), 4874)
        self.assertEqual(nearest(11, 2300), 2315)

    def test_negative(self):
        # -1.50 °C = -150 hundredths; its low byte in two's complement is 0x6A.
        self.assertEqual(nearest(-150 & 0xFF, -100), -150)
        self.assertEqual(nearest(0x6A, -50), -150)
        self.assertEqual(nearest(0x6A, 50), 106)  # 1.06 is nearer to 0.50 than -1.50 is


class TrackerTests(unittest.TestCase):
    def anchored(self, t0=1000.0) -> SensorTracker:
        tr = SensorTracker()
        tr.on_poll(STEP0_WORDS, now=t0)
        for key, number in STEP0_DISPLAY.items():
            tr.on_display(key, number, at=t0 + 2)
        return tr

    def test_step0_anchors(self):
        snap = self.anchored().snapshot(now=1003)
        self.assertEqual(snap["outdoor"]["value"], 13.0)
        self.assertEqual(snap["condenser"]["value"], 14.2)
        self.assertEqual(snap["tank_top"]["value"], 62.1)
        self.assertEqual(snap["tank_bottom"]["value"], 48.7)
        self.assertEqual(snap["room"]["value"], 23.1)  # 23.15
        for key in ("outdoor", "condenser", "tank_top", "tank_bottom", "room"):
            self.assertEqual(snap[key]["status"], "ok", key)
        # MENO didn't fit the display in step 0: never reported as ok.
        self.assertEqual(snap["supply"]["status"], "mismatch")

    def test_unanchored_until_display(self):
        tr = SensorTracker()
        tr.on_poll(STEP0_WORDS, now=1000)
        self.assertEqual(tr.snapshot(now=1001)["room"]["status"], "unanchored")

    def test_pending_anchor_pairs_with_next_poll(self):
        tr = SensorTracker()
        tr.on_display("room", 23, at=1000)
        self.assertEqual(tr.snapshot(now=1000)["room"]["status"], "unanchored")
        tr.on_poll(STEP0_WORDS, now=1008)
        self.assertEqual(tr.snapshot(now=1008)["room"]["status"], "ok")

    def test_display_too_old_to_pair(self):
        tr = SensorTracker()
        tr.on_display("room", 23, at=1000)
        tr.on_poll(STEP0_WORDS, now=1000 + sensor_regs.ANCHOR_PAIRING_SECONDS + 5)
        self.assertEqual(tr.snapshot(now=1100)["room"]["status"], "unanchored")

    def test_tracks_through_rollover(self):
        # Room 22.89 -> 23.60 with the heater on, as logged 2026-09-15: 241 249 6 16 28 35 56.
        tr = SensorTracker()
        tr.on_poll(words_with(room=241), now=0)
        tr.on_display("room", 23, at=1)
        for i, low in enumerate((249, 6, 16, 28, 35, 56), start=1):
            tr.on_poll(words_with(room=low), now=10 * i)
        room = tr.snapshot(now=61)["room"]
        self.assertEqual(room["value"], 23.6)
        self.assertEqual(room["status"], "ok")

    def test_tracks_down_through_rollover(self):
        tr = SensorTracker()
        tr.on_poll(words_with(outdoor=5), now=0)
        tr.on_display("outdoor", 13, at=1)          # 0x0505 = 12.85
        tr.on_poll(words_with(outdoor=250), now=10)  # 0x04FA = 12.74, not 0x05FA = 15.30
        outdoor = tr.snapshot(now=11)["outdoor"]
        self.assertEqual((outdoor["value"], outdoor["status"]), (12.7, "ok"))

    def test_big_step_is_uncertain_until_reanchored(self):
        tr = self.anchored()
        # Condenser 14.24 -> low byte 20: nearest is 13.00 (a 1.24 °C step).
        tr.on_poll(words_with(condenser=20), now=1010)
        self.assertEqual(tr.snapshot(now=1011)["condenser"]["status"], "uncertain")
        tr.on_poll(words_with(condenser=20), now=1020)
        self.assertEqual(tr.snapshot(now=1021)["condenser"]["status"], "uncertain")
        tr.on_display("condenser", 16, at=1022)       # really 15.56
        snap = tr.snapshot(now=1023)["condenser"]
        self.assertEqual((snap["status"], snap["value"]), ("ok", 15.6))

    def test_long_gap_is_uncertain(self):
        tr = self.anchored()
        tr.on_poll(STEP0_WORDS, now=1000 + 60)  # condenser: 58 s * 0.08 > 1 °C
        snap = tr.snapshot(now=1061)
        self.assertEqual(snap["condenser"]["status"], "uncertain")
        self.assertEqual(snap["room"]["status"], "ok")

    def test_stale_without_polls(self):
        tr = self.anchored()
        self.assertEqual(tr.snapshot(now=1000 + sensor_regs.STALE_SECONDS + 1)["room"]["status"], "stale")

    def test_on_poll_reports_changes_only(self):
        tr = self.anchored()
        self.assertFalse(tr.on_poll(STEP0_WORDS, now=1010))
        self.assertTrue(tr.on_poll(words_with(room=40), now=1020))

    def test_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sensors.json"
            tr = SensorTracker(path)
            tr.on_poll(STEP0_WORDS, now=1000)
            tr.on_display("room", 23, at=1002)
            again = SensorTracker(path)
            self.assertEqual(again.snapshot(now=1003)["room"]["status"], "stale")  # no poll yet
            again.on_poll(STEP0_WORDS, now=1040)
            self.assertEqual(again.snapshot(now=1041)["room"]["status"], "ok")


class SwingTests(unittest.TestCase):
    def tracker(self) -> SensorTracker:
        tr = SensorTracker()
        tr.on_poll(STEP0_WORDS, now=0)
        tr.on_display("condenser", 14, at=1)  # 14.24
        return tr

    def test_idle_drift_is_not_a_swing(self):
        tr = self.tracker()
        low = 144
        for i in range(1, 13):  # +0.01 °C every 2.5 s = 0.004 °C/s
            low += 1
            tr.on_poll(words_with(condenser=low), now=2.5 * i)
        self.assertEqual(tr.swinging(now=30.5), [])

    def test_fast_drop_is_a_swing_and_ends(self):
        tr = self.tracker()
        value = 1424
        for i in range(1, 9):  # -0.3 °C per 2.5 s
            value -= 30
            tr.on_poll(words_with(condenser=value & 0xFF), now=2.5 * i)
        self.assertEqual(tr.swinging(now=20.5), ["condenser"])
        for i in range(9, 15):  # then steady
            tr.on_poll(words_with(condenser=value & 0xFF), now=2.5 * i)
        self.assertEqual(tr.swinging(now=35.5), [])
        self.assertEqual(tr.snapshot(now=36)["condenser"]["status"], "ok")
        self.assertEqual(tr.snapshot(now=36)["condenser"]["value"], 11.8)  # 14.24 - 2.40, across a rollover

    def test_needs_a_full_window(self):
        tr = self.tracker()
        tr.on_poll(words_with(condenser=100), now=2.5)  # big step, but only 2.5 s of history
        self.assertEqual(tr.swinging(now=3), [])

    def test_lost_tracking_is_not_useful_for_fast_polls(self):
        tr = self.tracker()
        self.assertTrue(tr.fast_poll_useful())
        self.assertIsNone(tr.uncertain_since("condenser"))
        for i, value in enumerate((1424, 1394, 1364, 1334, 1200), start=1):  # last step 1.34 °C
            tr.on_poll(words_with(condenser=value & 0xFF), now=2.5 * i)
        self.assertEqual(tr.uncertain_since("condenser"), 12.5)
        self.assertFalse(tr.fast_poll_useful())
        self.assertIn("condenser", tr.swinging(now=13))
        self.assertEqual(tr.swinging(now=13, only_ok=True), [])
        tr.on_display("condenser", 12, at=14)  # re-anchored (really 12.00)
        self.assertIsNone(tr.uncertain_since("condenser"))
        self.assertTrue(tr.fast_poll_useful())

    def test_uncertain_since_survives_polls_for_old_saved_records(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sensors.json"
            path.write_text('{"condenser": {"hundredths": 2150, "status": "uncertain", "at": 900, "low": 102}}')
            tr = SensorTracker(path)
            tr.on_poll(words_with(condenser=102), now=1000)
            tr.on_poll(words_with(condenser=102), now=1010)
            tr.on_poll(words_with(condenser=102), now=1020)
            self.assertEqual(tr.uncertain_since("condenser"), 1000)

    def test_reanchor_is_not_a_swing(self):
        tr = self.tracker()
        for i in range(1, 6):
            tr.on_poll(STEP0_WORDS, now=2.5 * i)
        tr.on_display("condenser", 17, at=13)  # jumps the value a whole window
        for i in range(6, 8):
            tr.on_poll(STEP0_WORDS, now=2.5 * i)
        self.assertEqual(tr.swinging(now=18), [])


class DeviceStateTests(unittest.TestCase):
    def show(self, st, line1, line2):
        for reg, text in ((display_data.LINE1_REG, line1), (display_data.LINE2_REG, line2)) * 2:
            st.note_reg_block(3, 0x42, reg_block(reg, encode_line(text)))

    def test_harvested_screen_anchors_and_status_carries_sensors(self):
        st = state_mod.DeviceState()
        st.note_sensor_block(STEP0_WORDS)
        self.show(st, "HUONE", "T15 23°C")
        sensors = st.status()["sensors"]
        self.assertEqual(sensors["room"]["status"], "ok")
        self.assertEqual(sensors["outdoor"]["status"], "unanchored")
        self.assertIn("hundredths", st.snapshot()["sensors"]["room"])


if __name__ == "__main__":
    unittest.main()
