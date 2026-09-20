"""Print every register/bit value *change* within a time window of a
capture file, across all addresses -- not just ones already labelled
in regmap.py.

Unlike correlate_notes.py's before/after diff, this shows the full
sequence of intermediate values, which matters for a note like
"changed setpoint to 24 and back to 20": you want to see it climb and
come back down, not just the net (no-op) diff between the endpoints.

Usage:
    python scripts/dump_window.py sessions/keymap.jsonl 1789325018.91 1789325112.49
    python scripts/dump_window.py sessions/keymap.jsonl --note "changing temperature" --pad 90
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
    p.add_argument("start", type=float, nargs="?", help="Window start (unix timestamp)")
    p.add_argument("end", type=float, nargs="?", help="Window end (unix timestamp)")
    p.add_argument("--note", help="Instead of start/end, center the window on the first note containing this text")
    p.add_argument("--pad", type=float, default=30.0, help="With --note: seconds after the note to include (default: 30)")
    args = p.parse_args()

    events = load_events(args.capture_file)

    start, end = args.start, args.end
    if args.note is not None:
        matches = [e for e in events if e["type"] == "note" and args.note.lower() in e["text"].lower()]
        if not matches:
            print(f"No note containing {args.note!r} found.")
            return
        start = matches[0]["timestamp"]
        end = start + args.pad
        print(f"Window from note {matches[0]['text']!r}: {start:.2f} .. {end:.2f}\n")
    elif start is None or end is None:
        p.error("either give start/end timestamps, or --note")

    current: dict[tuple, tuple] = {}
    lines: list[tuple[float, str]] = []
    for e in events:
        if e["timestamp"] > end:
            break
        if e["type"] == "note":
            if start <= e["timestamp"] <= end:
                lines.append((e["timestamp"], f"NOTE: {e['text']!r}"))
            continue
        key = state_key(e)
        if key is None:
            continue
        val = state_value(e)
        if e["timestamp"] < start:
            current[key] = val  # establish baseline before the window
            continue
        if current.get(key) != val:
            label = e.get("label", "")
            addr = e.get("start_reg") or e.get("start_bit")
            lines.append((e["timestamp"], f"{key[0]:3s} {addr}  {current.get(key)} -> {val}  [{label}]"))
            current[key] = val

    for ts, text in sorted(lines):
        print(f"  {ts:.2f}  {text}")


if __name__ == "__main__":
    main()
