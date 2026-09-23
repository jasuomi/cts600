"""Offline tests for capture_log.CaptureLog's size rotation (added so a
--capture-file left running long-term, as happened on the Pi -- one grew
past 280 MB -- can't grow the SD card without bound).

Run from the project root: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cts600.capture_log import CaptureLog


class CaptureLogRotationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "capture.jsonl"

    def test_no_rotation_by_default(self):
        log = CaptureLog(self.path)
        for i in range(50):
            log.write({"type": "note", "text": f"event {i}"})
        log.close()
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_suffix(".jsonl.1").exists())

    def test_rotates_once_max_bytes_is_passed(self):
        log = CaptureLog(self.path, max_bytes=200, backup_count=2)
        for i in range(60):
            log.write({"type": "note", "text": f"event {i:03d}"})
        log.close()
        self.assertTrue(self.path.exists())          # the current (newest, smallest) file
        self.assertTrue(self.path.with_suffix(".jsonl.1").exists())
        # No events lost from the two files that must exist; content is
        # still valid JSONL either side of a rotation boundary.
        for p in (self.path, self.path.with_suffix(".jsonl.1")):
            for line in p.read_text(encoding="utf-8").splitlines():
                json.loads(line)

    def test_keeps_at_most_backup_count_old_files(self):
        log = CaptureLog(self.path, max_bytes=120, backup_count=2)
        for i in range(200):
            log.write({"type": "note", "text": f"event {i:04d}"})
        log.close()
        self.assertTrue(self.path.with_suffix(".jsonl.1").exists())
        self.assertTrue(self.path.with_suffix(".jsonl.2").exists())
        self.assertFalse(self.path.with_suffix(".jsonl.3").exists())

    def test_max_bytes_zero_never_rotates(self):
        log = CaptureLog(self.path, max_bytes=0)
        for i in range(500):
            log.write({"type": "note", "text": f"event {i:04d}"})
        log.close()
        self.assertFalse(self.path.with_suffix(".jsonl.1").exists())
        self.assertGreater(self.path.stat().st_size, 500)


if __name__ == "__main__":
    unittest.main()
