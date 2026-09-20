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
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, TextIO


class CaptureLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._fh: TextIO = self.path.open("a", buffering=1)  # line-buffered

    def write(self, event: dict[str, Any]) -> None:
        record = dict(event)
        record.setdefault("timestamp", time.time())
        with self._lock:
            self._fh.write(json.dumps(record, default=str) + "\n")

    def note(self, text: str) -> dict[str, Any]:
        """Insert a user-supplied marker (e.g. "pressed UP") into the log."""
        event = {"type": "note", "timestamp": time.time(), "text": text}
        self.write(event)
        return event

    def close(self) -> None:
        with self._lock:
            self._fh.close()
