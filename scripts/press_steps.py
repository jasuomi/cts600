"""For every Down the service pressed itself (Update walks, automatic
re-anchors, dashboard presses), how many NÄYTÄ DATA screens it moved and
how soon our own next transmission (a sensor read or the next key press)
followed the release.

A Down should move exactly one screen. On 2026-09-23 one moved four
(ULKOILMA -> LAUHDUT), right after a sensor read went out ~0.1 s behind
the release; passive_listener.POST_RELEASE_QUIET_SECONDS keeps that gap
at least 1 s since. This checks that jumps stay gone.

Needs the service's own log at INFO or lower (the "down: press" lines,
from --log-file) and the --capture-file of the same run, for the display
and the sensor-read replies. Only presses whose screen before and after
are both NÄYTÄ DATA screens are counted.

Usage (from the project root):
    python scripts/press_steps.py cts600.log sessions/live.jsonl
    python scripts/press_steps.py cts600.log sessions/live.jsonl --since 23:33
    python scripts/press_steps.py cts600.log sessions/live.jsonl --since "2026-09-23 23:33:00"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cts600.display_data import DATA_SCREENS, clean  # noqa: E402

PRESS_RE = re.compile(r"(\S+ \S+) INFO cts600\.master: (\w+): (press|release)")
SCREEN_WAIT_SECONDS = 6.5  # a press's screen change must show within this


def parse_since(text: str | None) -> float:
    if not text:
        return 0.0
    if " " not in text:
        text = time.strftime("%Y-%m-%d ") + text
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            pass
    raise SystemExit(f"--since: can't parse {text!r} (use HH:MM[:SS] or 'YYYY-MM-DD HH:MM[:SS]')")


def log_time(stamp: str) -> float:
    """'2026-09-23 23:27:51,175' (logging's default asctime, local time)."""
    return time.mktime(time.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")) + int(stamp[20:23]) / 1000


def screen_order(line1: str, line2: str) -> tuple[int | None, str]:
    """(position in the NÄYTÄ DATA list or None, a name for it). Line 1
    alone names the screen, except SOFTA1/SOFTA2: both show "SOFTA", and
    line 2 starts with the 1 or 2."""
    text = clean(line1)
    for i, (key, prefix, _reading, _t) in enumerate(DATA_SCREENS):
        if text.startswith(prefix):
            if prefix == "SOFTA" and clean(line2).startswith("2"):
                return i + 1, "SOFTA2"
            return i, key
    return None, text


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("log", type=Path, help="the service's --log-file")
    p.add_argument("capture", type=Path, help="the same run's --capture-file")
    p.add_argument("--since", help="only presses from this time on: HH:MM[:SS] today, or 'YYYY-MM-DD HH:MM[:SS]'")
    args = p.parse_args(argv)
    since = parse_since(args.since)

    presses, releases = [], []
    with args.log.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = PRESS_RE.match(line)
            if m and log_time(m.group(1)) >= since:
                (presses if m.group(3) == "press" else releases).append((log_time(m.group(1)), m.group(2)))

    # (time, position, name) each time a display line arrives; the two
    # lines come in separate frames, in either order.
    screens, reads = [], []
    lines = {"0x0200": "", "0x020A": ""}
    with args.capture.open(encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e["timestamp"] < since - 10:
                continue
            if e["type"] == "reg_block" and e.get("start_reg") in lines:
                lines[e["start_reg"]] = e["text"] or ""
                screens.append((e["timestamp"], *screen_order(lines["0x0200"], lines["0x020A"])))
            elif e["type"] == "raw" and e["fc"] == "0x04" and e["crc_ok"]:
                reads.append(e["timestamp"])  # the reply; our query went out a few tens of ms before

    total = jumps = 0
    for t, key in presses:
        if key != "down":
            continue
        before = [s for s in screens if s[0] < t]
        if not before or before[-1][1] is None:
            continue
        o0, name0 = before[-1][1], before[-1][2]
        after = [s for s in screens if t < s[0] < t + SCREEN_WAIT_SECONDS and s[1] != o0]
        o1 = after[0][1] if after else None
        if after and o1 is None:
            continue  # left the list (e.g. an Esc raced it): not a Down step
        release = next((r for r, _k in releases if r > t), None)
        own_next = [r for r in reads if release and r > release] + [q for q, _k in presses if release and q > release]
        steps = o1 - o0 if o1 is not None else None
        total += 1
        jumped = steps is not None and steps > 1
        jumps += jumped
        print(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
            "%-9s -> %-11s" % (name0, after[0][2] if after else "(no change)"),
            f"steps={steps}",
            f"hold={release - t:.2f}s" if release else "",
            f"next own TX {min(own_next) - release:.2f}s after release" if own_next else "",
            "  <-- JUMP" if jumped else "",
        )
    print(f"{total} Down presses, {jumps} jumps")
    return 1 if jumps else 0


if __name__ == "__main__":
    raise SystemExit(main())
