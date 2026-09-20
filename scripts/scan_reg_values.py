"""List every distinct value seen at one register/bit address in a
capture file, with how many times each was seen and every transition
between values, timestamped.

Useful once `correlate_notes.py` points at a register worth digging
into further: shows the full picture across the whole file, including
button presses/clicks that never got a note.

Usage:
    python scripts/scan_reg_values.py sessions/keymap.jsonl 0x0100
    python scripts/scan_reg_values.py sessions/keymap.jsonl 0x0100 --bits
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("capture_file", type=Path)
    p.add_argument("addr", help="Register/bit address, e.g. 0x0100")
    p.add_argument("--bits", action="store_true", help="Look at a bit_block (start_bit) instead of a reg_block (start_reg)")
    args = p.parse_args()

    event_type = "bit_block" if args.bits else "reg_block"
    addr_field = "start_bit" if args.bits else "start_reg"
    value_field = "bits" if args.bits else "words"

    counts: dict[tuple, int] = {}
    transitions: list[tuple[float, tuple | None, tuple]] = []
    prev = None

    with args.capture_file.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") != event_type or obj.get(addr_field) != args.addr:
                continue
            val = tuple(obj[value_field]) if args.bits else obj[value_field][0]
            counts[val] = counts.get(val, 0) + 1
            if val != prev:
                transitions.append((obj["timestamp"], prev, val))
                prev = val

    if not counts:
        print(f"No {event_type} events found for {addr_field}={args.addr!r}.")
        return

    print(f"Distinct values seen at {addr_field}={args.addr}:")
    for val, n in sorted(counts.items(), key=str):
        print(f"  {val}: seen {n} times")

    print("\nTransitions (timestamp, from, to):")
    for ts, a, b in transitions:
        print(f"  {ts:.2f}: {a} -> {b}")


if __name__ == "__main__":
    main()
