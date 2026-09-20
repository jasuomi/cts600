"""Active master mode: writes to the bus.

Deliberately separate from passive_listener.py, and never imported by
it -- writing to the bus is an explicit, opt-in action, not something
that happens as a side effect of running the dashboard. That convention
is why this module stayed unwritten while the project was purely
passive research.

Currently implements the bare minimum: emulate a single physical key
press by writing the _AID_xxx action code to output register 0x0100
via FC66 (the same thing the physical panel does to report a key
press), then releasing it back to 0x0000. Codes are the confirmed
mapping from regmap.py (sessions/keymap-20260913-212233.jsonl).

CAUTION: the physical panel is normally the bus's only master. If it's
still connected while this runs, both will be transmitting -- that's
useful for understanding what a two-master conflict looks like, but
it's a deliberate test condition, not how this is meant to run
long-term (the eventual goal is this replaces the panel, not races
it).

Needs --rts-direction on this hardware: a plain write() with no RTS
handling reached the OS/pyserial layer fine but never showed up on the
bus at all (no corruption, no echo -- nothing), because /dev/ttyS0
here is a native onboard RS232 port through an external RS232-to-RS485
converter that apparently needs an explicit transmit-enable signal and
doesn't auto-sense TX activity. pyserial's kernel-assisted rs485_mode
doesn't work either on this port's driver (TIOCSRS485 -> "Inappropriate
ioctl for device"), hence the manual RTS toggling in
transport.write_with_rts_direction (promoted there from here so
regscan.py can reuse it too).

CONFIRMED WORKING, both ways:
1. With the physical panel still connected, a live test produced
   exactly one corrupted, CRC-failing frame at the moment of the write
   -- garbage address/function bytes, crc_ok=False, tracked under
   state.py's node_stats as a one-off "phantom" node -- while the
   panel's own node-3 traffic stayed clean throughout. Consistent with
   a real electrical collision: overlapping transmissions get bit-mixed
   into garbage, not gracefully merged, and a corrupted frame is simply
   discarded per the protocol's own bad-CRC rule -- so a colliding
   key-press write is most likely just lost, not (mis)received.
2. With the panel disconnected and the bus fully quiesced (confirmed
   silent: 0 frames in 8s), a --self-check run of `up` got a direct,
   unambiguous response: right after the press, the controller sent an
   FC65 bit-block frame (03 41 01 00 00 02 00 01 01 D4 0A) -- the same
   shape that follows every genuine AID-write throughout every capture,
   i.e. a real protocol acknowledgment, not coincidence. Right after
   the release, the display (reg 0x0200) changed to "NÄYTÄ" (Finnish,
   "show") -- a real, visible state change with nothing else on the
   bus that could have caused it. The controller only speaks when
   written to (confirmed: went silent again immediately after
   responding) -- it is NOT an autonomous broadcaster, despite
   appearances right after a panel disconnect (that was residual/
   decaying activity, not ongoing independent broadcasting; earlier
   notes here suggesting otherwise were wrong).

Net result: the write path works. The earlier confusion (several
Esc-write attempts against a not-yet-fully-quiesced bus showing no
visible effect) came from testing with a no-op key (Esc does nothing
on an already-idle screen, so even a successfully-received Esc looks
identical to a lost one) combined with residual controller chatter
muddying cause and effect -- not from the write itself being broken.

CONTINUOUS REPORTING (send_key_continuous): a real panel re-transmits
its AID state cyclically (~1/sec, idle included), not just once, so
this re-asserts the press code every cycle_seconds while held, then
re-asserts idle for release_cycles after. Tested live with the panel
connected: 3 cycles of "down" (1s apart) drove the display through a
real sequence of menu screens (ETÄKYTKIN -> KESKUS -> VIILENNYS),
exactly in step with the press cycles -- continuous navigation, not
just a single nudge. Settled back to the normal idle screen afterward
on its own (no setting was actually changed; Down was just scrolling
a list). One important side-note this test surfaced: passive_listener.py's
capture of this window never shows our AID code (0x0100 stayed 0x0000
throughout, per the log) -- that's expected, not a bug. RTS is a
hardware pin on the one physical UART both processes share; asserting
it to transmit gates the *local receiver circuit* off entirely while
high (correct half-duplex behavior), so nothing on the bus is visible
to anyone sharing this hardware -- not even our own self-check --
during our own TX window. Our writes are therefore fundamentally
invisible to local capture by design; only the controller's *reaction*,
arriving after we've dropped RTS, is ever observable.
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Callable

from . import protocol, transport

log = logging.getLogger(__name__)

# Confirmed against sessions/keymap-20260913-212233.jsonl -- see
# regmap.py's OUTPUT_REGS[0x0100] entry.
AID_CODES = {
    "esc": 0x0001,
    "up": 0x0002,
    "down": 0x0004,
    "enter": 0x0008,
    "off": 0x0010,
    "on": 0x0020,
}

_AID_REG = 0x0100


def send_key_press(
    ser, key: str, hold_seconds: float = 0.3, use_rts: bool = False,
    self_check: bool = False, self_check_seconds: float = 0.15,
    before_write: "Callable[[str], None] | None" = None,
) -> None:
    """Emulate a physical key press: write the AID code, hold, release.

    before_write, if given, is called with "press" or "release" right
    before that write -- the dashboard uses it to wait for a quiet moment
    between panel<->controller exchanges. It may raise to cancel the write.

    self_check reads back whatever appears on this same port/fd right
    after each write, for self_check_seconds. This exists because
    inferring "did the write reach the bus" secondhand -- from a
    *separate* passive_listener.py process's capture log -- turned out
    to be ambiguous: two processes with the same tty device open can
    each only see some of the incoming bytes (the kernel hands each
    byte to whichever process's read() call is waiting, not to both),
    so a listener process seeing nothing doesn't prove nothing arrived.
    Reading back from the very fd that wrote removes that ambiguity.
    For the cleanest result, stop any other process reading this port
    before using this (otherwise the two are still splitting bytes).
    """
    code = AID_CODES[key]
    press = protocol.encode_frame(
        protocol.NODE_STYREENHED_1,
        protocol.FunctionCode.WI_RO_REGS,
        protocol.encode_reg_block(_AID_REG, [code]),
    )
    release = protocol.encode_frame(
        protocol.NODE_STYREENHED_1,
        protocol.FunctionCode.WI_RO_REGS,
        protocol.encode_reg_block(_AID_REG, [0x0000]),
    )
    write = transport.write_with_rts_direction if use_rts else transport.write_frame

    if before_write:
        before_write("press")
    if self_check:
        ser.reset_input_buffer()
    log.info("%s press:   %s", key, protocol.hexdump(press))
    write(ser, press)
    if self_check:
        echoed = transport.read_for(ser, self_check_seconds)
        log.info(
            "  read-back after press   (%d bytes): %s",
            len(echoed), protocol.hexdump(echoed) if echoed else "(nothing)",
        )

    time.sleep(hold_seconds)

    if before_write:
        before_write("release")
    if self_check:
        ser.reset_input_buffer()
    log.info("%s release: %s", key, protocol.hexdump(release))
    write(ser, release)
    if self_check:
        echoed = transport.read_for(ser, self_check_seconds)
        log.info(
            "  read-back after release (%d bytes): %s",
            len(echoed), protocol.hexdump(echoed) if echoed else "(nothing)",
        )


def _send_frame(
    ser, frame: bytes, label: str, write, self_check: bool, self_check_seconds: float,
) -> None:
    if self_check:
        ser.reset_input_buffer()
    log.info("%s: %s", label, protocol.hexdump(frame))
    write(ser, frame)
    if self_check:
        echoed = transport.read_for(ser, self_check_seconds)
        log.info(
            "  read-back (%d bytes): %s",
            len(echoed), protocol.hexdump(echoed) if echoed else "(nothing)",
        )


def send_key_continuous(
    ser, key: str, hold_seconds: float = 2.0, cycle_seconds: float = 1.0,
    release_cycles: int = 2, use_rts: bool = False,
    self_check: bool = False, self_check_seconds: float = 0.15,
) -> None:
    """Emulate a physical key held down, the way the real panel reports
    it: by repeatedly re-asserting the AID code every cycle_seconds for
    as long as it's held, not just once.

    send_key_press() only writes the code a single time -- but every
    capture so far shows the panel cyclically re-transmitting its
    current AID state cyclically, roughly once a second regardless,
    idle (0x0000) included. A
    single press+release doesn't match that; a controller expecting the
    normal cyclic pattern may not treat one lone write the same way as
    a held key. This instead re-sends the press code every
    cycle_seconds for hold_seconds, then re-sends idle (0x0000) for
    release_cycles more cycles, to signal the release the same
    (repeated) way the press was signaled.
    """
    code = AID_CODES[key]
    press = protocol.encode_frame(
        protocol.NODE_STYREENHED_1,
        protocol.FunctionCode.WI_RO_REGS,
        protocol.encode_reg_block(_AID_REG, [code]),
    )
    release = protocol.encode_frame(
        protocol.NODE_STYREENHED_1,
        protocol.FunctionCode.WI_RO_REGS,
        protocol.encode_reg_block(_AID_REG, [0x0000]),
    )
    write = transport.write_with_rts_direction if use_rts else transport.write_frame

    press_cycles = max(1, round(hold_seconds / cycle_seconds))
    for i in range(press_cycles):
        _send_frame(
            ser, press, f"{key} press   [{i + 1}/{press_cycles}]",
            write, self_check, self_check_seconds,
        )
        time.sleep(cycle_seconds)

    for i in range(release_cycles):
        _send_frame(
            ser, release, f"{key} release [{i + 1}/{release_cycles}]",
            write, self_check, self_check_seconds,
        )
        time.sleep(cycle_seconds)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="Emulate a single physical key press on the CTS600 bus.")
    p.add_argument("key", choices=sorted(AID_CODES))
    p.add_argument("--port", default=None, help="Serial device (default: autodiscover)")
    p.add_argument("--hold", type=float, default=0.3, help="Seconds to hold the key down (default: 0.3)")
    p.add_argument(
        "--continuous", action="store_true",
        help="Repeatedly re-assert the AID code every --cycle seconds "
             "while held, then repeatedly report idle for --release-"
             "cycles more cycles, matching the panel's own observed "
             "cyclic (~1/sec) reporting -- instead of writing the code "
             "just once like the default single press+release does.",
    )
    p.add_argument("--cycle", type=float, default=1.0, help="Seconds between re-asserts in --continuous mode (default: 1.0)")
    p.add_argument("--release-cycles", type=int, default=2, help="How many idle cycles to send after releasing in --continuous mode (default: 2)")
    p.add_argument(
        "--rts-direction", action="store_true",
        help="Toggle RTS by hand around each write for half-duplex RS485 "
             "direction control (needed by many RS232-to-RS485 converters "
             "that don't auto-sense TX activity; try this if a plain write "
             "shows no effect on the bus at all). Uses manual ser.rts "
             "toggling, not pyserial's rs485_mode -- this port's driver "
             "doesn't support the TIOCSRS485 ioctl that relies on.",
    )
    p.add_argument(
        "--self-check", action="store_true",
        help="Read back from this same port right after each write, to "
             "directly confirm whether the bytes reached the bus, instead "
             "of relying on a separate passive_listener.py process's "
             "capture (which can miss bytes another reader on the same "
             "port already consumed). For a clean result, stop any other "
             "process reading this port first.",
    )
    args = p.parse_args(argv)

    ser = transport.open_serial(args.port)
    if args.rts_direction:
        log.info("Using manual RTS toggling for TX direction control")
    try:
        if args.continuous:
            send_key_continuous(
                ser, args.key, hold_seconds=args.hold, cycle_seconds=args.cycle,
                release_cycles=args.release_cycles,
                use_rts=args.rts_direction, self_check=args.self_check,
            )
        else:
            send_key_press(
                ser, args.key, args.hold,
                use_rts=args.rts_direction, self_check=args.self_check,
            )
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
