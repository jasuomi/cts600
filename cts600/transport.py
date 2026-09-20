"""Low-level serial transport for the CTS600 bus.

Handles port autodiscovery, opening the port with the right settings,
and turning a stream of bytes into discrete frames using the
"silence >= 3.5 character times" rule from the protocol doc. This is
a direct refactor of cts600_sniffer.py's read loop, split out so both
the passive listener and (later) an active master can share it.
"""

from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import serial
from serial.tools import list_ports

BAUD = 19200

# Substrings used to recognize likely USB serial adapters on Linux.
USB_SERIAL_HINTS = ("usb", "ttyusb", "ttyacm", "ftdi", "ch340", "cp210", "pl2303")

# Idle time (seconds) after which we consider a frame complete. The doc
# specifies >=3.5 character times (~2 ms at 19200 baud); we use a larger
# margin, same as the original sniffer, to be robust to USB-serial jitter.
FRAME_IDLE_SECONDS = 0.01

# A chunk failing CRC is joined with the next one if that starts within
# this long after it (see FrameReader). Split pieces follow each other
# within a few ms of reading; the next real frame starts >=25 ms after the
# previous one ends, and a join is only kept if the CRC then passes, so
# two real frames are never merged.
FRAME_JOIN_SECONDS = 0.02
MAX_FRAME_BYTES = 256  # Modbus RTU maximum


class BusBusyError(RuntimeError):
    """No safe transmit window opened on the bus in time; the write was not sent."""


def is_usable_serial_port(path: str) -> bool:
    return os.path.exists(path) and os.access(path, os.R_OK | os.W_OK)


def find_serial_port() -> str:
    """Auto-discover a likely serial adapter on Linux.

    Preference order:
      1. /dev/serial/by-id/* (stable, descriptive symlinks udev creates)
      2. Ports reported by pyserial that look like USB serial adapters
      3. Any remaining /dev/ttyUSB* or /dev/ttyACM* device nodes
      4. Usable RS232 /dev/ttyS* devices
    """
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    for candidate in by_id:
        if is_usable_serial_port(candidate):
            return candidate

    candidates = []
    for port in list_ports.comports():
        haystack = f"{port.device} {port.description} {port.hwid}".lower()
        looks_usb = any(hint in haystack for hint in USB_SERIAL_HINTS)
        if looks_usb and is_usable_serial_port(port.device):
            candidates.append(port.device)
    if candidates:
        return sorted(candidates)[0]

    fallback = [
        dev
        for dev in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        if is_usable_serial_port(dev)
    ]
    if fallback:
        return fallback[0]

    rs232_ports = [dev for dev in sorted(glob.glob("/dev/ttyS*")) if is_usable_serial_port(dev)]
    if rs232_ports:
        return rs232_ports[0]

    raise RuntimeError(
        "No usable serial adapter found. Plug in the RS232/RS485 adapter, "
        "then ensure your user can access the device (usually group dialout), "
        "or pass the device path explicitly."
    )


def open_serial(port: Optional[str] = None) -> serial.Serial:
    port = port or find_serial_port()
    try:
        ser = serial.Serial(
            port,
            BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0.001,
            # Two processes on this port split incoming bytes between them
            # (every frame chopped -> CRC storm) and both transmit on the
            # shared bus. Happened 2026-09-15: a duplicate instance produced
            # 260+ consecutive bad frames for hours. flock makes a second
            # opener fail immediately, before it touches the bus.
            exclusive=True,
        )
    except serial.SerialException as exc:
        raise RuntimeError(
            f"Could not open serial port {port}: {exc}\n"
            "If another cts600 process (dashboard, master.py, planner.py,\n"
            "regscan.py) is already using the port, stop it first.\n"
            "If this is a permission issue, run:\n"
            "  sudo usermod -aG dialout $USER\n"
            "then log out and back in, or choose another port explicitly."
        ) from exc
    # CRITICAL: pyserial's SerialBase.__init__ defaults self._rts_state to
    # True, and serial.Serial(...) applies that to the hardware line as
    # part of opening -- so without this, the RS232-to-RS485 converter
    # comes up in TRANSMIT-ENABLE mode (see write_with_rts_direction()'s
    # docstring: RTS=True asserts transmit and gates the local receiver
    # off) from the instant the port opens, and stays there until the
    # first write happens to lower it. write_with_rts_direction() only
    # ever LOWERS rts at the end of a write -- nothing ever explicitly
    # set it low on open. In pure passive mode (no --enable-control) RTS
    # is never touched again after this, so the converter would sit
    # transmit-enabled -- contending with/potentially jamming the real
    # bus -- for the entire session. This almost certainly explains a
    # 2026-09-15 incident where LINK ERR only ever occurred while this
    # software was running: every open_serial() call (initial startup,
    # and every PassiveListener._reopen() after a transient
    # SerialException) left RTS asserted until whatever the next write
    # happened to be, sometimes tens of seconds later. Force it low here,
    # unconditionally, so every caller (passive listener startup AND
    # every reopen, since both funnel through this one function) starts
    # in receive-enabled state by default instead of relying on
    # pyserial's opposite default.
    ser.rts = False
    return ser


@dataclass
class RawFrame:
    """A raw byte frame as seen on the bus, with capture timestamp."""

    timestamp: float
    data: bytes


class FrameReader:
    """Reads a serial port and yields RawFrame objects.

    Groups bytes by the silence-based boundary rule, with one repair:
    the silence is measured when *this process* reads the bytes, not when
    they crossed the wire. If the read thread is late by more than
    idle_seconds (scheduling, the GIL), a frame arrives in two chunks and
    looks like two frames. Seen 2026-09-17: ~0.45% of frames, always split
    at 4/8/12/16 bytes (the UART FIFO), gap 11-13 ms, the tail's first
    byte showing up as bogus "nodes" 0/62/65 with sticky CRC-error streaks.
    So a chunk failing is_valid() is held for up to join_seconds, and
    joined with the next chunk if the joined bytes are valid. A genuinely
    corrupted frame is still emitted as-is once the window passes.
    """

    def __init__(
        self, ser: serial.Serial, idle_seconds: float = FRAME_IDLE_SECONDS,
        is_valid: Optional[Callable[[bytes], bool]] = None,
        join_seconds: float = FRAME_JOIN_SECONDS,
    ):
        self._ser = ser
        self._idle_seconds = idle_seconds
        self._is_valid = is_valid
        self._join_seconds = join_seconds
        self._buf = bytearray()
        self._last_byte_time = time.time()
        self._stop = False
        self._pending: Optional[bytes] = None  # invalid chunk waiting for its tail
        self._pending_end = 0.0                # when its last byte was read
        self._chunk_start = 0.0                # when the current chunk's first byte was read
        self._ready: list[RawFrame] = []
        self.joined_count = 0

    def stop(self) -> None:
        self._stop = True

    @property
    def last_byte_time(self) -> float:
        """time.time() of the most recently received byte -- how long the
        bus has been silent, for timing our own writes."""
        return self._last_byte_time

    def poll_once(self) -> Optional[RawFrame]:
        """Read whatever is available; return a completed frame, if any."""
        if self._ready:
            return self._ready.pop(0)
        # Everything already buffered in one call, not byte by byte: less
        # Python work per byte keeps the read thread on time.
        b = self._ser.read(max(1, self._ser.in_waiting))
        now = time.time()
        if b:
            if not self._buf:
                self._chunk_start = now
            self._buf.extend(b)
            self._last_byte_time = now
            return None

        if self._buf and now - self._last_byte_time > self._idle_seconds:
            chunk = bytes(self._buf)
            self._buf.clear()
            self._chunk(chunk, now)
        elif self._pending is not None and not self._buf and now - self._pending_end > self._join_seconds:
            self._emit_pending(now)
        return self._ready.pop(0) if self._ready else None

    def _chunk(self, chunk: bytes, now: float) -> None:
        valid = self._is_valid
        if valid is None:
            self._ready.append(RawFrame(now, chunk))
            return
        if self._pending is not None:
            joined = self._pending + chunk
            if valid(joined):
                self._pending = None
                self.joined_count += 1
                self._ready.append(RawFrame(now, joined))
                return
            if not valid(chunk) and self._chunk_start - self._pending_end <= self._join_seconds \
                    and len(joined) <= MAX_FRAME_BYTES:
                self._pending, self._pending_end = joined, self._last_byte_time  # maybe a third piece
                return
            self._emit_pending(now)
        if valid(chunk):
            self._ready.append(RawFrame(now, chunk))
        else:
            self._pending, self._pending_end = chunk, self._last_byte_time

    def _emit_pending(self, now: float) -> None:
        self._ready.append(RawFrame(now, self._pending))
        self._pending = None

    def read_forever(self, on_frame: Callable[[RawFrame], None]) -> None:
        """Blocking loop; calls on_frame(frame) for each completed frame.

        Intended to be run in a background thread. Call stop() from
        another thread to exit the loop cleanly.
        """
        while not self._stop:
            frame = self.poll_once()
            if frame is not None:
                on_frame(frame)


def write_frame(ser: serial.Serial, data: bytes) -> None:
    """Send a raw frame. Used by an active master; unused in passive mode."""
    ser.write(data)
    ser.flush()


def write_with_rts_direction(ser: serial.Serial, data: bytes, margin_seconds: float = 0.002) -> None:
    """Toggle RTS around a write, for RS232-to-RS485 converters that need
    an explicit transmit-enable signal and don't auto-sense TX activity
    (confirmed needed on this specific hardware -- see master.py's
    docstring for how that was diagnosed).

    Not using pyserial's rs485_mode/TIOCSRS485 here -- confirmed this
    port's driver doesn't support that kernel ioctl ("OSError: [Errno 25]
    Inappropriate ioctl for device"), so this toggles RTS by hand instead.
    RTS is released as soon as the frame has physically left: at least
    the frame's on-wire time after write() starts, plus margin_seconds.
    flush() (tcdrain) normally already guarantees the bytes are out; the
    explicit floor covers drivers where it returns early. Do NOT hold RTS
    longer: the controller starts replying ~20 ms after our frame ends
    (measured 2026-09-15, replies complete 50-75 ms after write()), and
    the previous fixed 20 ms hold kept our driver on the bus into its
    fastest replies -- 1 of 26 guarded key presses had its reply garbled
    that way. RTS is dropped (RX re-enabled, on transceivers that gate RX
    off during TX) before this returns, so a caller doing a read right
    after this should already be able to see anything that comes back.
    """
    bits_per_char = 1 + ser.bytesize + (0 if ser.parity == serial.PARITY_NONE else 1) + ser.stopbits
    on_wire = len(data) * bits_per_char / ser.baudrate
    ser.rts = True
    start = time.monotonic()
    ser.write(data)
    ser.flush()
    remaining = start + on_wire + margin_seconds - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    ser.rts = False


def read_for(ser: serial.Serial, duration_seconds: float) -> bytes:
    """Read whatever appears on the port for the given duration."""
    deadline = time.monotonic() + duration_seconds
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = ser.read(64)
        if chunk:
            buf.extend(chunk)
    return bytes(buf)
