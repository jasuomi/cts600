"""Persistent capture logging.

The in-memory DeviceState only keeps a short rolling buffer for the
live dashboard. For actually reverse-engineering the AID bitmask and
keyboard bit layout, we need durable, timestamped, greppable capture
sessions -- ideally with notes like "pressed UP" dropped in at the
right moment. CaptureLog writes one JSON object per line (JSONL) so
it's easy to `grep`/`jq` afterwards, and easy to append to without
re-writing the whole file.

Enabled with `--capture-file PATH`; notes arrive via POST /api/note (the
dashboard's "add note" box). The files this writes are what scripts/
reads -- see "Capture and analysis" in the README for the whole loop.

A capture session is meant to be short (see the README), but a
--capture-file left running long-term (as happened on the Pi -- one grew
past 280 MB) would otherwise grow forever, which is exactly the SD-card
risk logging.py's rotation exists to avoid. So this rotates the same way a
RotatingFileHandler would: once the file passes max_bytes, it's renamed
.1 (bumping any existing .1..N-1 up first) and a fresh file started, up to
backup_count old files. max_bytes=0 disables rotation.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, TextIO


class CaptureLog:
    def __init__(self, path: str | Path, max_bytes: int = 0, backup_count: int = 2):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()
        self._fh: TextIO = self.path.open("a", buffering=1)  # line-buffered

    def write(self, event: dict[str, Any]) -> None:
        record = dict(event)
        record.setdefault("timestamp", time.time())
        line = json.dumps(record, default=str) + "\n"
        with self._lock:
            self._fh.write(line)
            if self.max_bytes and self._fh.tell() >= self.max_bytes:
                self._rotate_locked()

    def _rotate_locked(self) -> None:
        self._fh.close()
        for i in range(self.backup_count - 1, 0, -1):
            src, dst = self.path.with_suffix(f"{self.path.suffix}.{i}"), self.path.with_suffix(f"{self.path.suffix}.{i + 1}")
            if src.exists():
                src.replace(dst)
        if self.backup_count > 0:
            self.path.replace(self.path.with_suffix(f"{self.path.suffix}.1"))
        else:
            self.path.unlink(missing_ok=True)
        self._fh = self.path.open("a", buffering=1)

    def note(self, text: str) -> dict[str, Any]:
        """Insert a user-supplied marker (e.g. "pressed UP") into the log."""
        event = {"type": "note", "timestamp": time.time(), "text": text}
        self.write(event)
        return event

    def close(self) -> None:
        with self._lock:
            self._fh.close()
