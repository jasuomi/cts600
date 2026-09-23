"""Offline tests for passive_listener._wait_for_tx_window's max_wait bound.

Covers the fix for the raw Up/Down "jumps too many steps" bug: a key
release used to wait up to TX_MAX_WAIT_SECONDS (2.5s) for a quiet bus
window, on top of the intended hold_seconds -- long enough that the panel
could see it as a held (auto-repeating) key rather than one tap. Releases
now use a much shorter RELEASE_MAX_WAIT_SECONDS bound instead (see
passive_listener.py's comment above that constant, and master.py's
send_key_continuous docstring for the auto-repeat this avoids).

Run from the project root: python -m unittest discover -s tests
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from cts600 import passive_listener, state as state_mod, transport


class WaitForTxWindowTests(unittest.TestCase):
    def setUp(self):
        self.listener = passive_listener.PassiveListener(state_mod.DeviceState())
        # A last_byte_time in the future keeps tx_window_open() reporting
        # "busy" for as long as the test runs, regardless of its own
        # duration (the same trick test_sensor_poll.py's
        # test_busy_bus_sends_nothing uses).
        self.listener._reader = mock.Mock(last_byte_time=time.time() + 1000)

    def test_non_strict_returns_after_its_own_max_wait(self):
        t0 = time.monotonic()
        self.listener._wait_for_tx_window(strict=False, max_wait=0.08)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.08)
        self.assertLess(elapsed, 0.08 + 0.15)  # slack for the 5ms poll loop + scheduling

    def test_strict_raises_after_its_own_max_wait(self):
        t0 = time.monotonic()
        with self.assertRaises(transport.BusBusyError):
            self.listener._wait_for_tx_window(strict=True, max_wait=0.05)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 0.05 + 0.15)

    def test_release_bound_is_shorter_than_press_bound_by_default(self):
        # The bound a real key release should use (RELEASE_MAX_WAIT_SECONDS)
        # must stay well under the press's (TX_MAX_WAIT_SECONDS), or a
        # delayed release can still hold the AID code long enough to look
        # like a held key to the panel.
        self.assertLess(passive_listener.RELEASE_MAX_WAIT_SECONDS, passive_listener.TX_MAX_WAIT_SECONDS)


class SendKeyUsesShorterReleaseBoundTests(unittest.TestCase):
    """send_key() itself: press must open the bus (strict), then the bus
    turns busy again right before release -- release must still be sent
    (never raises), and must not wait anywhere near as long as press's
    bound would allow."""

    def setUp(self):
        self.state = state_mod.DeviceState()
        self.listener = passive_listener.PassiveListener(self.state)
        self.listener._ser = object()
        self.writes: list[bytes] = []
        self._busy_from = None

        class FakeReader:
            last_byte_time = 0.0  # bus long silent at first: press's window is open at once

        self.listener._reader = FakeReader()

    def fake_write(self, _ser, data):
        self.writes.append(data)
        if len(self.writes) == 1:
            # Right after the press write, make the bus look busy again so
            # the release write has something to wait out.
            self.listener._reader.last_byte_time = time.time() + 1000

    def test_release_sent_promptly_even_if_bus_turns_busy(self):
        with mock.patch.object(passive_listener, "TX_MAX_WAIT_SECONDS", 2.5), \
                mock.patch.object(passive_listener, "RELEASE_MAX_WAIT_SECONDS", 0.05), \
                mock.patch.object(transport, "write_with_rts_direction", self.fake_write):
            t0 = time.monotonic()
            self.listener.send_key("up", hold_seconds=0.0, use_rts=True)
            elapsed = time.monotonic() - t0
        self.assertEqual(len(self.writes), 2)  # press, then release -- never skipped
        # Well under TX_MAX_WAIT_SECONDS: proves release used its own short bound.
        self.assertLess(elapsed, 1.0)


class PressWindowTests(unittest.TestCase):
    """A press starts only with room for hold + release before the next
    exchange (2026-09-23 23:49-23:50: presses with <=0.64 s to go were held
    0.52-0.78 s and 4 of 5 moved two screens; >=0.72 s to go, all one)."""

    PERIOD = 1.035

    def open_at(self, phase: float, need: float) -> bool:
        now = 1000.0
        starts = [now - phase - self.PERIOD * i for i in range(5, -1, -1)]
        return passive_listener.tx_window_open(now, now - 0.3, starts, need)[0]

    def test_press_needs_room_for_hold_and_release(self):
        need = passive_listener.press_window(0.3)
        self.assertTrue(self.open_at(0.40, passive_listener.TX_WINDOW_SECONDS))  # fine for a sensor read...
        self.assertFalse(self.open_at(0.40, need))  # ...but the release wouldn't fit before the exchange
        self.assertTrue(self.open_at(0.20, need))

    def test_long_hold_is_capped_so_a_window_still_exists(self):
        need = passive_listener.press_window(5.0)
        self.assertEqual(need, passive_listener.PRESS_WINDOW_MAX_SECONDS)
        self.assertTrue(self.open_at(passive_listener.TX_QUIET_SECONDS + 0.01, need))

    def test_send_key_asks_press_for_the_bigger_window(self):
        listener = passive_listener.PassiveListener(state_mod.DeviceState())
        listener._ser = object()
        calls = []
        with mock.patch.object(listener, "_wait_for_tx_window", lambda **kw: calls.append(kw)), \
                mock.patch.object(transport, "write_with_rts_direction", lambda _s, _d: None):
            listener.send_key("up", hold_seconds=0.3, use_rts=True)
        self.assertEqual(calls[0]["need"], passive_listener.press_window(0.3))
        self.assertEqual(calls[1]["need"], passive_listener.TX_WINDOW_SECONDS)


class PostReleaseQuietTests(unittest.TestCase):
    """Nothing of ours goes out within POST_RELEASE_QUIET_SECONDS of a key
    release (2026-09-23 23:27:51: a sensor read ~0.1 s behind a release was
    followed by a 4-screen jump)."""

    def setUp(self):
        self.listener = passive_listener.PassiveListener(state_mod.DeviceState())
        self.listener._ser = object()

        class SilentBus:
            last_byte_time = 0.0  # long silent: tx_window_open() alone would say go

        self.listener._reader = SilentBus()
        self.write_times: list[float] = []

    def fake_write(self, _ser, _data):
        self.write_times.append(time.monotonic())

    def test_next_transmission_waits_out_the_quiet_period(self):
        with mock.patch.object(passive_listener, "POST_RELEASE_QUIET_SECONDS", 0.2), \
                mock.patch.object(transport, "write_with_rts_direction", self.fake_write):
            self.listener.send_key("down", hold_seconds=0.0, use_rts=True)
            released = self.write_times[-1]
            self.listener._wait_for_tx_window(strict=True)  # e.g. the sensor read right behind it
            self.assertGreaterEqual(time.monotonic() - released, 0.2)

    def test_no_wait_without_a_recent_release(self):
        with mock.patch.object(passive_listener, "POST_RELEASE_QUIET_SECONDS", 0.5):
            t0 = time.monotonic()
            self.listener._wait_for_tx_window(strict=True)
            self.assertLess(time.monotonic() - t0, 0.1)

    def test_back_to_back_presses_are_spaced(self):
        with mock.patch.object(passive_listener, "POST_RELEASE_QUIET_SECONDS", 0.2), \
                mock.patch.object(transport, "write_with_rts_direction", self.fake_write):
            self.listener.send_key("down", hold_seconds=0.0, use_rts=True)
            self.listener.send_key("down", hold_seconds=0.0, use_rts=True)
        first_release, second_press = self.write_times[1], self.write_times[2]
        self.assertGreaterEqual(second_press - first_release, 0.2)


if __name__ == "__main__":
    unittest.main()
