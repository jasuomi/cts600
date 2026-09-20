"""Offline tests for FrameReader's reassembly of frames split by a late read,
and for per-node stats ignoring bogus addresses.

Run from the project root: python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cts600 import protocol, state as state_mod, transport  # noqa: E402

# Real frames from sessions/capture-20260916-ha.jsonl, and how they were split.
LINE2 = bytes.fromhex("03 42 02 0A 00 05 00 0A 3E 33 3C 20 32 32 DF 43 00 00 01 C3")
LINE1 = bytes.fromhex("03 42 02 00 00 05 00 0A 41 55 54 4F 20 20 20 20 00 00 4C 1B")
HEARTBEAT = bytes.fromhex("03 42 00 2A 00 01 00 02 00 EF E5 FE")
BITS = bytes.fromhex("03 41 01 00 00 02 00 01 01 D4 0A")


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        return self.t


class FakeSerial:
    """Delivers (at_time, bytes) chunks; read() returns what has 'arrived'."""

    def __init__(self, clock: FakeClock, chunks: list[tuple[float, bytes]]) -> None:
        self.clock = clock
        self.chunks = sorted(chunks)

    @property
    def in_waiting(self) -> int:
        return sum(len(b) for t, b in self.chunks if t <= self.clock.t)

    def read(self, n: int) -> bytes:
        out = bytearray()
        while self.chunks and self.chunks[0][0] <= self.clock.t and len(out) < n:
            out += self.chunks.pop(0)[1]
        self.clock.t += 0.001  # the 1 ms read timeout
        return bytes(out)


def run(chunks: list[tuple[float, bytes]], until: float = 1.0) -> tuple[list[bytes], transport.FrameReader]:
    clock = FakeClock()
    ser = FakeSerial(clock, [(clock.t + t, b) for t, b in chunks])
    with mock.patch.object(transport, "time", clock):
        reader = transport.FrameReader(ser, is_valid=protocol.verify_crc)
        reader._last_byte_time = clock.t
        frames = []
        end = clock.t + until
        while clock.t < end:
            f = reader.poll_once()
            if f is not None:
                frames.append(f.data)
    return frames, reader


class FramingTests(unittest.TestCase):
    def test_whole_frames_pass_through(self):
        frames, reader = run([(0.0, HEARTBEAT), (0.04, LINE2), (1.0 - 0.1, BITS)])
        self.assertEqual(frames, [HEARTBEAT, LINE2, BITS])
        self.assertEqual(reader.joined_count, 0)

    def test_split_after_16_bytes_is_joined(self):
        # as captured: 16 bytes, then "00 00 01 C3" read 11-13 ms later
        frames, reader = run([(0.0, LINE2[:16]), (0.012, LINE2[16:]), (0.05, HEARTBEAT)])
        self.assertEqual(frames, [LINE2, HEARTBEAT])
        self.assertEqual(reader.joined_count, 1)

    def test_split_after_8_bytes_and_in_three_pieces(self):
        frames, _ = run([(0.0, LINE1[:8]), (0.012, LINE1[8:])])
        self.assertEqual(frames, [LINE1])
        frames, _ = run([(0.0, LINE1[:8]), (0.012, LINE1[8:16]), (0.024, LINE1[16:])])
        self.assertEqual(frames, [LINE1])

    def test_short_first_piece_is_joined(self):
        frames, _ = run([(0.0, BITS[:2]), (0.012, BITS[2:])])
        self.assertEqual(frames, [BITS])

    def test_corrupt_frame_is_still_emitted(self):
        bad = bytearray(LINE2)
        bad[10] ^= 0x01
        frames, _ = run([(0.0, bytes(bad)), (0.05, HEARTBEAT)])
        self.assertEqual(frames, [bytes(bad), HEARTBEAT])

    def test_corrupt_frame_then_valid_frame_soon_after(self):
        bad = LINE2[:-1] + b"\x00"
        frames, _ = run([(0.0, bad), (0.013, HEARTBEAT)])
        self.assertEqual(frames, [bad, HEARTBEAT])

    def test_without_validator_behaves_as_before(self):
        clock = FakeClock()
        ser = FakeSerial(clock, [(clock.t, LINE2[:16]), (clock.t + 0.012, LINE2[16:])])
        with mock.patch.object(transport, "time", clock):
            reader = transport.FrameReader(ser)
            frames = []
            while clock.t < 1000.2:
                f = reader.poll_once()
                if f:
                    frames.append(f.data)
        self.assertEqual(frames, [LINE2[:16], LINE2[16:]])


class BogusNodeTests(unittest.TestCase):
    def test_bad_frames_from_unknown_addresses_dont_create_nodes(self):
        state = state_mod.DeviceState(readings_path=None)
        state.note_frame(protocol.decode_frame(HEARTBEAT))
        for _ in range(30):
            state.note_frame(protocol.decode_frame(bytes.fromhex("00 00 01 C3")))
            state.note_frame(protocol.decode_frame(HEARTBEAT))
        self.assertEqual(set(state.node_stats), {3})
        self.assertFalse(state.is_link_error_risk(0))
        self.assertEqual(state.bus_health.crc_error_count, 30)

    def test_bad_frames_from_a_known_node_still_count(self):
        state = state_mod.DeviceState(readings_path=None)
        state.note_frame(protocol.decode_frame(HEARTBEAT))
        for _ in range(6):
            state.note_frame(protocol.decode_frame(HEARTBEAT[:-1] + b"\x00"))
        self.assertTrue(state.is_link_error_risk(3))


if __name__ == "__main__":
    unittest.main()
