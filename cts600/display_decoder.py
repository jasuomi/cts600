"""Reassembles the CTS600 display-data registers (0x0200-0x0FFF) into
readable text.

The controller does not expose "temperature"/"mode" as discrete
registers for most status info -- it streams whatever the physical
LCD would show, as raw display buffer content, addressed by register.
Different frames update different (sometimes overlapping) register
ranges as the panel's menu scrolls/pages. This module keeps a sparse
byte buffer of "last known content per register" and reconstructs
readable contiguous text segments from it.

NOTE: the doc says the actual display geometry (columns/rows, data
type -- plain ASCII vs "8 bytes ASCII + 2 bytes attribute") comes
from the _FC_REPORT_SLAVE_ID response (ucDisplayType/ucDisplayDataType/
uiNbDisplayCols/uiNbDisplayRows). That handshake has now been captured
from a live bus (it is parsed into `state.DeviceState.slave_id`):
nb_display_cols=2, nb_display_rows=8, display_type=1,
display_data_type=1. This module still doesn't use that geometry --
it makes no assumption about row layout and just reports contiguous
printable runs -- since wiring it in is a separate change.
"""

from __future__ import annotations

from dataclasses import dataclass

from .regmap import DISPLAY_REG_END, DISPLAY_REG_START


@dataclass
class DisplaySegment:
    start_reg: int
    text: str


class DisplayBuffer:
    def __init__(self) -> None:
        # register address -> 2-byte word content (as bytes)
        self._words: dict[int, bytes] = {}

    def update(self, start_reg: int, word_bytes: list[bytes]) -> bool:
        """Feed in decoded word bytes for a block starting at start_reg.

        Returns True if this call changed anything (i.e. new content).
        """
        changed = False
        for i, wb in enumerate(word_bytes):
            reg = start_reg + i
            if self._words.get(reg) != wb:
                self._words[reg] = wb
                changed = True
        return changed

    def is_display_reg(self, addr: int) -> bool:
        return DISPLAY_REG_START <= addr <= DISPLAY_REG_END

    def segments(self) -> list[DisplaySegment]:
        """Return contiguous runs of known registers as text segments."""
        if not self._words:
            return []
        regs = sorted(self._words)
        segments: list[DisplaySegment] = []
        run_start = regs[0]
        run_bytes = bytearray(self._words[regs[0]])
        prev = regs[0]

        for reg in regs[1:]:
            if reg == prev + 1:
                run_bytes.extend(self._words[reg])
            else:
                segments.append(DisplaySegment(run_start, _to_text(run_bytes)))
                run_start = reg
                run_bytes = bytearray(self._words[reg])
            prev = reg

        segments.append(DisplaySegment(run_start, _to_text(run_bytes)))
        return segments

    def line_text(self, start_reg: int, end_reg: int) -> str | None:
        """Text of the contiguous known registers from start_reg up to
        (not including) end_reg, or None if start_reg has never been seen.
        Used to read one LCD line (0x0200 / 0x020A) even when a block
        covered both lines at once."""
        if start_reg not in self._words:
            return None
        data = bytearray()
        reg = start_reg
        while reg < end_reg and reg in self._words:
            data.extend(self._words[reg])
            reg += 1
        return _to_text(data)

    def snapshot(self) -> dict:
        return {
            "segments": [
                {"start_reg": f"0x{s.start_reg:04X}", "text": s.text}
                for s in self.segments()
            ],
        }


# Byte codes on the panel's extended (non-ASCII) charset, confirmed by
# cross-referencing ~800 occurrences across two captures against
# recognizable Finnish HVAC menu words -- e.g. 0x0B turns "L.MP." /
# "L.MMITYS" / "K.YTT." into "LÄMPÖ" (heat) / "LÄMMITYS" (heating) /
# "KÄYTTÖ" (use), and 0xDF appears right before every "C" in a
# temperature reading ("20.C" -> "20°C"). Not in this map: 0x0A, 0x80,
# 0xA0, 0xA8, 0xAA, which also show up in display text but look like
# LCD icon glyphs or a field-selection marker rather than letters --
# deliberately left as "." rather than guessed. Å hasn't been seen in
# any capture yet.
#
# A UI-visualization attempt was made and then deliberately reverted
# (2026-09-14) after live testing showed the underlying signal is
# inconsistent across fields -- worth knowing before trying again:
#   0x000A (last word) -- a genuine blink: toggles 0x0000<->0x000A
#     roughly at blink rate for as long as a value stays selected.
#     Confirmed selecting temperature (room setpoint and the
#     water-heater T1 setpoint).
#   0x2A00 (last word) -- static, does not toggle: renders as a
#     literal "*" right after the value (0x2A is printable ASCII) and
#     holds steady the whole time. Confirmed selecting fan speed
#     (Enter x3 from idle -- temperature -> mode -> fan -- turns
#     "0x020A: >3< 22°C.." into "0x020A: >3< 22°C*.").
#   Mode (Enter x2 from idle) has NEITHER: watched 0x0200 live for 40s
#     after selecting mode and it stayed completely unchanged for the
#     first ~11s, only reaching for any indicator at all. So there's
#     no way to distinguish "mode is selected" from "plain idle" using
#     display-register content alone.
#   0xAA0A, separately: NOT a per-field selection marker as first
#     assumed -- confirmed it's a transient flash tied to the panel's
#     ~10-13s inactivity timeout (toggled 0xAA0A<->0x0000 twice right
#     before reverting to idle, nothing beforehand), and also appears
#     briefly right after pressing Enter to advance fields. Reads as a
#     general "state changing" flash, not "this field is selected".
# A reliable indicator (if one gets built later) likely needs to track
# which field is selected by counting Enter/Esc presses structurally,
# not by reading display registers -- fragile against the ~10-13s
# panel-side timeout and against real physical-panel button presses
# happening outside this codebase's view.
_EXTENDED_CHARSET = {
    0x0B: "Ä",
    0x0C: "Ö",
    0xDF: "°",  # degree sign
}


def _to_text(data: bytes) -> str:
    return "".join(
        chr(b) if 32 <= b <= 126 else _EXTENDED_CHARSET.get(b, ".")
        for b in data
    )
