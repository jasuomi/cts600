"""Offline tests for the sensor block read (passive_listener.read_sensor_block)
and the server's periodic poll.

Run from the project root: python -m unittest discover -s tests
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from test_sensor_regs import STEP0_WORDS

from cts600 import passive_listener, protocol, state as state_mod, transport

try:
    from fastapi.testclient import TestClient

    from cts600.webapp import server
except (ImportError, RuntimeError):
    TestClient = None


def reply_frame(words: list[int]) -> bytes:
    data = bytes([2 * len(words)]) + b"".join(w.to_bytes(2, "big") for w in words)
    return protocol.encode_frame(protocol.NODE_STYREENHED_1, protocol.FunctionCode.READ_INPUT_REGS, data)


class ReadSensorBlockTests(unittest.TestCase):
    def setUp(self):
        self.state = state_mod.DeviceState()
        self.listener = passive_listener.PassiveListener(self.state)
        self.listener._ser = object()
        self.listener._reader = mock.Mock(last_byte_time=0.0)  # bus long silent: window open
        self.writes: list[bytes] = []

    def fake_write(self, reply: bytes | None):
        def write(_ser, data):
            self.writes.append(data)
            if reply is not None:
                # The controller answers ~50 ms later, through the read thread.
                def deliver():
                    time.sleep(0.05)
                    self.listener._on_raw_frame(transport.RawFrame(time.time(), reply))
                threading.Thread(target=deliver).start()
        return write

    def test_reads_block_and_feeds_state(self):
        with mock.patch.object(transport, "write_with_rts_direction", self.fake_write(reply_frame(STEP0_WORDS))):
            words = self.listener.read_sensor_block(use_rts=True)
        self.assertEqual(words, STEP0_WORDS)
        self.assertEqual(self.writes, [protocol.encode_read_query(3, 4, 0, 16)])
        self.state.sensors.on_display("room", 23)
        self.assertEqual(self.state.status()["sensors"]["room"]["status"], "ok")

    def test_timeout_returns_none(self):
        with mock.patch.object(transport, "write_with_rts_direction", self.fake_write(None)):
            self.assertIsNone(self.listener.read_sensor_block(use_rts=True))

    def test_wrong_size_reply_is_not_ours(self):
        with mock.patch.object(transport, "write_with_rts_direction", self.fake_write(reply_frame([1] * 8))):
            self.assertIsNone(self.listener.read_sensor_block(use_rts=True))

    def test_reads_bits_as_sent(self):
        # Seen live: FC2 count 16 -> byte count 3 but only two data bytes.
        reply = protocol.encode_frame(3, 2, bytes([3, 0xED, 0xE0]))
        with mock.patch.object(transport, "write_with_rts_direction", self.fake_write(reply)):
            frame, bits = self.listener.read_block(2, 0, 16)
        self.assertEqual(len(bits), 16)
        self.assertEqual([i for i, b in enumerate(bits) if b], [0, 2, 3, 5, 6, 7, 13, 14, 15])
        self.assertEqual(self.writes, [protocol.encode_read_query(3, 2, 0, 16)])

    def test_exception_reply(self):
        reply = protocol.encode_frame(3, 0x82, bytes([2]))
        with mock.patch.object(transport, "write_with_rts_direction", self.fake_write(reply)):
            frame, bits = self.listener.read_block(2, 0, 15)
        self.assertIsNone(bits)
        self.assertTrue(frame.is_exception)

    def test_refuses_write_function_codes(self):
        for fc in (5, 6, 15, 16, 0x41, 0x42):
            with self.assertRaises(ValueError):
                self.listener.read_block(fc, 0, 1)
        self.assertEqual(self.writes, [])

    def test_busy_bus_sends_nothing(self):
        self.listener._reader.last_byte_time = time.time() + 10  # bus "active" throughout
        with mock.patch.object(passive_listener, "TX_MAX_WAIT_SECONDS", 0.05), \
                mock.patch.object(transport, "write_with_rts_direction", self.fake_write(None)):
            with self.assertRaises(transport.BusBusyError):
                self.listener.read_sensor_block(use_rts=True)
        self.assertEqual(self.writes, [])


class CaptureRawTests(unittest.TestCase):
    """Raw frames reach the capture file only when bad or not decoded into
    another event; the live frame log (recent_log / websocket) gets all."""

    def setUp(self):
        self.state = state_mod.DeviceState()
        self.captured: list[dict] = []
        self.state.attach_capture_log(mock.Mock(write=self.captured.append))
        self.listener = passive_listener.PassiveListener(self.state)

    def feed(self, data: bytes):
        self.listener._on_raw_frame(transport.RawFrame(time.time(), data))

    def captured_types(self):
        return [e["type"] for e in self.captured]

    def live_raw(self):
        return [e for e in self.state.snapshot()["recent_log"] if e["type"] == "raw"]

    def test_decoded_frame_is_captured_once(self):
        self.feed(protocol.encode_frame(3, 0x42, protocol.encode_reg_block(0x0100, [0])))
        self.assertEqual(self.captured_types(), ["reg_block"])
        self.assertEqual(len(self.live_raw()), 1)

    def test_bad_crc_frame_is_captured_raw(self):
        good = protocol.encode_frame(3, 0x42, protocol.encode_reg_block(0x0100, [0]))
        self.feed(good[:-1] + bytes([good[-1] ^ 0xFF]))
        self.assertEqual(self.captured_types(), ["raw"])
        self.assertFalse(self.captured[0]["crc_ok"])

    def test_undecoded_sensor_reply_is_captured_raw(self):
        self.feed(reply_frame(STEP0_WORDS))  # FC4: no decoded event of its own
        self.assertEqual(self.captured_types(), ["raw"])
        self.assertEqual(self.captured[0]["fc"], "0x04")


@unittest.skipIf(TestClient is None, "fastapi not available")
class PacerTests(unittest.TestCase):
    def test_normal_by_default(self):
        p = server.SensorPollPacer(10)
        p.note(0, False, [])
        self.assertEqual(p.interval(), 10)

    def test_compressor_edge_and_swing_trigger_fast_then_hold_expires(self):
        p = server.SensorPollPacer(10)
        p.note(0, False, [])
        p.note(1, True, [])  # compressor starts
        self.assertEqual(p.interval(), server.SENSOR_FAST_POLL_SECONDS)
        p.note(60, True, ["condenser"])  # swing renews the hold
        p.note(60 + server.SENSOR_FAST_HOLD_SECONDS - 1, True, [])
        self.assertTrue(p.fast)
        p.note(60 + server.SENSOR_FAST_HOLD_SECONDS + 1, True, [])
        self.assertEqual(p.interval(), 10)

    def test_first_led_state_is_not_an_edge(self):
        p = server.SensorPollPacer(10)
        p.note(0, True, [])
        self.assertFalse(p.fast)

    def test_fast_is_capped_then_cools_down(self):
        p = server.SensorPollPacer(10)
        t = 0.0
        while t < server.SENSOR_FAST_MAX_SECONDS + 1:
            p.note(t, None, ["condenser"])
            t += 5
        self.assertFalse(p.fast)
        p.note(t + 5, None, ["condenser"])  # still swinging, but cooling down
        self.assertEqual(p.interval(), 10)
        later = t + server.SENSOR_FAST_COOLDOWN_SECONDS + 5
        p.note(later, None, ["condenser"])
        self.assertTrue(p.fast)

    def test_not_useful_ends_fast_and_blocks_triggers(self):
        p = server.SensorPollPacer(10)
        p.note(0, False, ["condenser"])
        self.assertTrue(p.fast)
        p.note(3, False, [], useful=False)  # condenser just went uncertain
        self.assertFalse(p.fast)
        p.note(5, True, ["condenser"], useful=False)  # compressor edge while lost
        self.assertEqual(p.interval(), 10)

    def test_failures_win(self):
        p = server.SensorPollPacer(10)
        p.note(0, False, ["condenser"])
        p.failures = server.SENSOR_POLL_MAX_FAILURES
        self.assertEqual(p.interval(), server.SENSOR_POLL_SLOW_SECONDS)


@unittest.skipIf(TestClient is None, "fastapi not available")
class ReanchorDueTests(unittest.TestCase):
    # 13:05 case: uncertain at t=0, swinging until t=400.
    BASE = dict(uncertain_since=0.0, last_swing_at=400.0, last_attempt_at=None,
                last_key_at=None, screen="idle", operation_running=False)

    def due(self, now, **kw):
        return server.reanchor_due(now, **{**self.BASE, **kw})

    def test_waits_for_settle(self):
        self.assertFalse(self.due(420))
        self.assertTrue(self.due(400 + server.REANCHOR_SETTLE_SECONDS))

    def test_max_wait_even_if_drifting(self):
        self.assertTrue(self.due(server.REANCHOR_MAX_WAIT_SECONDS, last_swing_at=599.0))

    def test_not_uncertain(self):
        self.assertFalse(self.due(1000, uncertain_since=None))

    def test_guards(self):
        now = 1000.0
        self.assertTrue(self.due(now))
        self.assertFalse(self.due(now, screen="NAYTA_DATA"))
        self.assertFalse(self.due(now, screen=None))
        self.assertFalse(self.due(now, operation_running=True))
        self.assertFalse(self.due(now, last_key_at=now - 30))
        self.assertFalse(self.due(now, last_attempt_at=now - 60))
        self.assertTrue(self.due(now, last_attempt_at=now - server.REANCHOR_MIN_INTERVAL_SECONDS))


@unittest.skipIf(TestClient is None, "fastapi not available")
class ReanchorPlanTests(unittest.TestCase):
    def test_nothing_due(self):
        # Uncertain and settled isn't enough on its own: only a due sensor starts a walk.
        self.assertIsNone(server.reanchor_plan([], ["tank_top"], ["tank_top"]))

    def test_all_due_is_one_walk_to_the_deepest(self):
        # After a restart (2026-09-23 23:07): all four uncertain and due.
        keys = ["tank_top", "tank_bottom", "supply", "condenser"]
        target, covered = server.reanchor_plan(keys, keys, keys)
        self.assertEqual(target, "condenser")
        self.assertEqual(covered, keys)

    def test_passed_uncertain_sensors_are_covered_even_if_not_due_yet(self):
        target, covered = server.reanchor_plan(["supply"], ["tank_top", "supply"], ["tank_top", "supply"])
        self.assertEqual(target, "supply")
        self.assertEqual(covered, ["tank_top", "supply"])

    def test_settled_condenser_extends_a_walk_that_is_happening_anyway(self):
        # 2026-09-23 23:33: supply due from before a restart, the rest just
        # went uncertain -- this was two walks (to MENO, then LAUHDUT a
        # minute later); now it's one.
        uncertain = ["tank_top", "tank_bottom", "supply", "condenser"]
        target, covered = server.reanchor_plan(["supply"], uncertain, uncertain)
        self.assertEqual(target, "condenser")
        self.assertEqual(covered, uncertain)

    def test_swinging_condenser_does_not_set_the_depth(self):
        target, covered = server.reanchor_plan(["tank_bottom"], ["tank_bottom", "condenser"], ["tank_bottom"])
        self.assertEqual(target, "tank_bottom")
        self.assertEqual(covered, ["tank_bottom"])


@unittest.skipIf(TestClient is None, "fastapi TestClient not available")
class ReadEndpointTests(unittest.TestCase):
    def test_validation_and_read(self):
        listener = FakePollListener()
        client = TestClient(server.create_app(state_mod.DeviceState(), listener=listener))
        for body in ({"function": 6, "start": 0, "count": 1}, {"function": 3, "start": 0, "count": 23},
                     {"function": 1, "start": -1, "count": 1}, {"function": 2, "start": 0, "count": 0}):
            self.assertEqual(client.post("/api/read", json=body).status_code, 400, body)
        r = client.post("/api/read", json={"function": 1, "start": 0, "count": 24}).json()
        self.assertEqual((r["status"], r["values"]), ("ok", [0] * 24))
        self.assertEqual(listener.blocks, [(1, 0, 24)])

    def test_passive_refuses(self):
        client = TestClient(server.create_app(state_mod.DeviceState()))
        self.assertEqual(client.post("/api/read", json={"function": 4, "start": 0, "count": 1}).status_code, 503)


class FakePollListener:
    def __init__(self, healthy=True):
        self.healthy = healthy
        self.reads = 0
        self.blocks = []

    def read_block(self, function, start, count, use_rts=True):
        self.blocks.append((function, start, count))
        return mock.Mock(raw=b"\x03"), [0] * count

    def _bus_healthy(self):
        return self.healthy

    def read_sensor_block(self, use_rts=True):
        self.reads += 1
        return STEP0_WORDS


@unittest.skipIf(TestClient is None, "fastapi TestClient not available")
class PollTaskTests(unittest.TestCase):
    def run_app(self, listener, poll_seconds, wait=0.35):
        with mock.patch.object(server, "SENSOR_POLL_STARTUP_DELAY_SECONDS", 0.05):
            app = server.create_app(state_mod.DeviceState(), listener=listener, sensor_poll_seconds=poll_seconds)
            with TestClient(app):
                time.sleep(wait)

    def test_polls_repeatedly(self):
        listener = FakePollListener()
        self.run_app(listener, 0.1)
        self.assertGreaterEqual(listener.reads, 2)

    def test_no_poll_when_disabled_or_unhealthy(self):
        off = FakePollListener()
        self.run_app(off, 0)
        self.assertEqual(off.reads, 0)
        sick = FakePollListener(healthy=False)
        self.run_app(sick, 0.1)
        self.assertEqual(sick.reads, 0)


if __name__ == "__main__":
    unittest.main()
