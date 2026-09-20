"""Display-text parsing + goal-directed key picking: closes the loop
between "read the current screen" and "press the right key", instead
of a human picking the key by hand for every test.

Deliberately narrow in scope. We only have a solid, confirmed read on
one screen -- the idle/home screen (see regmap.py's comment above
DISPLAY_REG_START). We do NOT have the rest of the menu tree mapped,
so this makes no attempt to navigate anywhere else. If the display
doesn't match the known idle format, every function here refuses to
guess a key rather than pressing something blindly into an unmapped
menu.

CONFIRMED AGAINST THE REAL PANEL (from its operator, watching live --
not just captures, twice now, each correcting the previous guess):
1. Up/Down on the plain idle screen do NOT adjust anything directly.
   Enter cycles which field is selected/editable -- temperature ->
   mode -> fan -- and Up/Down only adjusts whichever field is
   currently selected. An early attempt here pressed Up/Down straight
   from idle with no Enter first and didn't touch the setpoint -- it
   landed on a "Näytä data" ("show data") screen instead.
2. Esc DISCARDS an edit rather than just "backing out" of it. The
   first fix for (1) added Enter-to-select then Up/Down then Esc-to-
   exit -- the operator watched the setpoint visibly change on the
   panel during the Up presses, then revert back to its original value
   the moment Esc was pressed. Enter is what confirms/saves the change.
set_temperature() now: Enter once to select temperature, the observe/
decide/press loop, Enter to confirm (not Esc), then Esc to back out of
whatever field Enter's confirm-and-advance behavior selects next.
CONFIRMED WORKING end-to-end, including durably: ran a full 20->22°C
sequence this way and the operator confirmed on the physical panel
that it showed 22°C and stayed there (didn't revert), unlike the
Esc-based version.

IMPORTANT DISTINCTION, confirmed by testing (see master.py's
docstring): single un-held taps (send_key_press) adjust the selected
field by one step. Continuous/held reporting (send_key_continuous)
instead seems to enter a (different) settings menu and scroll through
it. This module only ever uses single taps for exactly that reason --
holding Up/Down to "get there faster" would very likely navigate
somewhere else entirely instead of just adjusting faster.
"""

from __future__ import annotations

import argparse
import logging
import re
import time

from . import master, protocol, transport
from .display_decoder import DisplayBuffer

log = logging.getLogger(__name__)

# ">3< 20°C.." -> fan speed 3, setpoint 20. See regmap.py's comment
# above DISPLAY_REG_START for how this was confirmed.
_IDLE_LINE2_RE = re.compile(r"^>(\d)< *(\d+)°C")

_LINE1_REG = 0x0200
_LINE2_REG = 0x020A

# Loose sanity bounds on a settable temperature -- not the device's
# actual documented range (unknown), just a guard against an obvious
# typo running away with repeated presses.
_MIN_SANE_TEMP_C = 5
_MAX_SANE_TEMP_C = 35


def parse_idle_status(line1: str, line2: str) -> dict | None:
    """Parse the idle screen's two display lines into structured
    values. Returns None if line2 doesn't match the known idle-screen
    shape (e.g. we're in a menu, or on the water-heater submenu's
    "T1 NN°C" screen instead) -- deliberately strict, since a false
    positive here would mean pressing Up/Down somewhere we don't
    understand.
    """
    m = _IDLE_LINE2_RE.match(line2)
    if not m:
        return None
    mode_raw = line1.rstrip(". ")
    water_heater = mode_raw.endswith("W")
    mode = mode_raw[:-1].rstrip() if water_heater else mode_raw
    return {
        "mode": mode,
        "water_heater": water_heater,
        "fan_speed": int(m.group(1)),
        "setpoint_c": int(m.group(2)),
    }


def pick_key_for_target_temp(status: dict, target_c: int) -> str | None:
    """Return "up" or "down" to move one step toward target_c, or None
    if already there."""
    if status["setpoint_c"] == target_c:
        return None
    return "up" if target_c > status["setpoint_c"] else "down"


def read_idle_status(ser, timeout_seconds: float = 5.0) -> dict | None:
    """Passively read this same port until both idle-screen display
    lines have been seen (or timeout_seconds elapses), then parse
    them. Returns None on timeout, or if what we received doesn't
    parse as the idle screen.
    """
    reader = transport.FrameReader(ser)
    buf = DisplayBuffer()
    deadline = time.monotonic() + timeout_seconds
    have_line1 = have_line2 = False

    while time.monotonic() < deadline and not (have_line1 and have_line2):
        frame = reader.poll_once()
        if frame is None:
            continue
        f = protocol.decode_frame(frame.data)
        if f is None or not f.crc_ok or f.base_function != protocol.FunctionCode.WI_RO_REGS:
            continue
        block = protocol.parse_reg_block(f.data)
        if block is None or not buf.is_display_reg(block.start_reg):
            continue
        words = block.words()
        word_bytes = [block.data[2 * i:2 * i + 2] for i in range(len(words))]
        buf.update(block.start_reg, word_bytes)
        if block.start_reg == _LINE1_REG:
            have_line1 = True
        elif block.start_reg == _LINE2_REG:
            have_line2 = True

    if not (have_line1 and have_line2):
        return None

    segments = {s.start_reg: s.text for s in buf.segments()}
    return parse_idle_status(segments.get(_LINE1_REG, ""), segments.get(_LINE2_REG, ""))


def set_temperature(
    ser, target_c: int, use_rts: bool = False, max_steps: int = 15,
    settle_seconds: float = 1.2,
) -> bool:
    """Closed-loop: observe the idle screen, press one Up/Down tap
    toward target_c, re-observe, repeat -- rather than blindly pressing
    N times open-loop with no feedback. Refuses to act if the display
    doesn't match the known idle screen at any point (e.g. something
    else navigated us into a menu), rather than guessing. Returns True
    if target_c was reached, False otherwise.
    """
    if not (_MIN_SANE_TEMP_C <= target_c <= _MAX_SANE_TEMP_C):
        log.error(
            "target_c=%d is outside the sanity range %d-%d -- refusing "
            "(this isn't the device's documented range, just a guard "
            "against repeatedly pressing toward an obvious typo).",
            target_c, _MIN_SANE_TEMP_C, _MAX_SANE_TEMP_C,
        )
        return False

    status = read_idle_status(ser)
    if status is None:
        log.error(
            "Display doesn't match the known idle screen (or nothing was "
            "received) -- refusing to guess a key rather than press into "
            "an unmapped menu.",
        )
        return False
    log.info("Current: %s", status)

    if pick_key_for_target_temp(status, target_c) is None:
        log.info("Already at target (%d°C), nothing to do.", target_c)
        return True

    # Confirmed against the real panel: Up/Down on the plain idle screen
    # does NOT adjust anything directly. Enter cycles which field is
    # selected/editable (temperature -> mode -> fan); Up/Down only
    # adjusts whichever one is currently selected. So select temperature
    # first -- one Enter press from plain idle.
    log.info("Pressing Enter once to select the temperature field")
    master.send_key_press(ser, "enter", hold_seconds=0.3, use_rts=use_rts)
    time.sleep(settle_seconds)

    reached = False
    lost_track = False
    for step in range(max_steps):
        status = read_idle_status(ser)
        if status is None:
            log.error(
                "Display doesn't match the known idle-screen shape anymore "
                "(or nothing was received) -- stopping rather than "
                "continuing to press blindly.",
            )
            lost_track = True
            break
        log.info("Current: %s", status)

        key = pick_key_for_target_temp(status, target_c)
        if key is None:
            log.info("Reached target (%d°C).", target_c)
            reached = True
            break

        log.info("Step %d/%d: pressing %s", step + 1, max_steps, key)
        master.send_key_press(ser, key, hold_seconds=0.3, use_rts=use_rts)
        time.sleep(settle_seconds)  # let the controller process + re-broadcast
    else:
        log.error("Gave up after %d steps without reaching %d°C.", max_steps, target_c)

    if lost_track:
        # We don't know what screen we're actually on anymore -- pressing
        # anything here would be exactly the kind of blind guess this
        # module exists to avoid. Leave it for a human to check instead.
        log.error(
            "Not pressing Enter or Esc -- screen state is unknown, check "
            "the panel manually.",
        )
        return False

    # CONFIRMED against the real panel (by its operator, watching live):
    # Esc here *discards* the edit back to whatever it was before this
    # run, it does not just "back out" -- an earlier version of this
    # function used Esc and silently lost a successfully-applied change.
    # Enter is what confirms/saves it.
    log.info("Pressing Enter to confirm the new value")
    master.send_key_press(ser, "enter", hold_seconds=0.3, use_rts=use_rts)
    time.sleep(settle_seconds)

    # Confirming with Enter is also believed to advance selection to the
    # next field (temperature -> mode), per the same cycling behavior
    # that picks which field Enter selects in the first place -- so back
    # out of *that* with Esc. Nothing should be unconfirmed there (we
    # never touched mode/fan), so this Esc shouldn't discard anything.
    log.info("Pressing Esc to back out of field selection")
    master.send_key_press(ser, "esc", hold_seconds=0.3, use_rts=use_rts)

    return reached


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    p = argparse.ArgumentParser(
        description="Set the room temperature setpoint by reading the idle "
                     "screen and tapping Up/Down toward the target, one "
                     "step at a time.",
    )
    p.add_argument("target_c", type=int, help="Target setpoint in Celsius")
    p.add_argument("--port", default=None, help="Serial device (default: autodiscover)")
    p.add_argument("--max-steps", type=int, default=15, help="Give up after this many taps (default: 15)")
    p.add_argument(
        "--rts-direction", action="store_true",
        help="Toggle RTS by hand around each write -- see master.py --help.",
    )
    args = p.parse_args(argv)

    ser = transport.open_serial(args.port)
    try:
        ok = set_temperature(
            ser, args.target_c, use_rts=args.rts_direction, max_steps=args.max_steps,
        )
    finally:
        ser.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
