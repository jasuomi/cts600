"""Diff bus state around each note in a capture JSONL file.

Manually grepping reg_block/bit_block events near a note timestamp
(as the README suggests) only surfaces registers you already thought
to look at. This instead diffs *every* register/bit address seen
anywhere in the file between "just before the note" and "a few
seconds after it", so it also catches changes in addresses nobody
has labelled in regmap.py yet.

Usage:
    python scripts/correlate_notes.py sessions/live.jsonl
    python scripts/correlate_notes.py sessions/live.jsonl --post 5
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_events(path: Path) -> list[dict]:
    events = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    events.sort(key=lambda e: e["timestamp"])
    return events


def state_key(event: dict) -> tuple | None:
    if event["type"] == "reg_block":
        return ("reg", event["start_reg"])
    if event["type"] == "bit_block":
        return ("bit", event["start_bit"])
    return None


def state_value(event: dict):
    if event["type"] == "reg_block":
        return tuple(event["words"])
    if event["type"] == "bit_block":
        return tuple(event["bits"])
    return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("capture_file", type=Path)
    p.add_argument("--post", type=float, default=4.0, help="Seconds after the note to sample the 'after' state (default: 4)")
    args = p.parse_args()

    events = load_events(args.capture_file)
    notes = [e for e in events if e["type"] == "note"]
    if not notes:
        print("No notes found in this capture file.")
        return

    # Build probe list: for each note, snapshot state at note.timestamp
    # ("before") and at note.timestamp + post ("after").
    probes = []
    for i, note in enumerate(notes):
        probes.append((note["timestamp"], i, "before"))
        probes.append((note["timestamp"] + args.post, i, "after"))
    probes.sort(key=lambda pr: pr[0])

    current: dict[tuple, tuple] = {}
    snapshots: dict[tuple[int, str], dict[tuple, tuple]] = {}

    ei = 0
    for probe_time, note_idx, which in probes:
        while ei < len(events) and events[ei]["timestamp"] <= probe_time:
            key = state_key(events[ei])
            if key is not None:
                current[key] = state_value(events[ei])
            ei += 1
        snapshots[(note_idx, which)] = dict(current)

    for i, note in enumerate(notes):
        before = snapshots[(i, "before")]
        after = snapshots[(i, "after")]
        changed = {
            k for k in (set(before) | set(after))
            if before.get(k) != after.get(k)
        }
        print(f"\n=== note[{i}] @ {note['timestamp']:.2f}: {note['text']!r} ===")
        if not changed:
            print("  (no reg/bit changes in the window)")
            continue
        for kind, addr in sorted(changed):
            print(f"  {kind:3s} {addr}: {before.get((kind, addr))} -> {after.get((kind, addr))}")


if __name__ == "__main__":
    main()
