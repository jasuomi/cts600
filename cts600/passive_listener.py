"""Passive/shadow-mode bus listener.

Runs on a background thread, reads raw frames off the serial port
(via transport.FrameReader) and decodes them into the shared
DeviceState. Does not transmit anything on its own -- safe to run
alongside the physical control panel.

The one exception is send_key() -- see its docstring. It is opt-in (the
caller, webapp/server.py, only exposes it when started with
--enable-control) and reuses this same already-open connection rather
than a second process opening the port separately, which is what made
master.py's/regscan.py's CLIs unreliable to run *alongside* the
dashboard: two processes reading the same tty split incoming bytes
between them unpredictably. The NÄYTÄ DATA
walk (data_walk.py) presses keys through send_key() too.

v1.0 removed the FC3/FC4 register scans. read_sensor_block() brings back
one read: the temperature block FC4 0x0000-0x000F, whose values turned out
to be hundredths of a °C with only the low byte sent (sensor_regs.py). Also
opt-in (--enable-control), and placed in the bus's quiet window like a key
press.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque

from . import master, protocol, regscan, sensor_regs, transport
from .state import DeviceState

log = logging.getLogger(__name__)

# Bus timing measured from 63k captured frames on 2026-09-15 (clean,
# single-instance data): the panel<->controller exchange repeats every
# 1035-1037 ms, each a query and response ~39 ms apart (max 56 ms),
# leaving ~990 ms of silence. A key press written at a random moment can
# land inside an exchange and corrupt both frames; these keep it in the
# silence.
EXCHANGE_GAP_SECONDS = 0.15   # a longer gap than this starts a new exchange
TX_QUIET_SECONDS = 0.10       # bus silent this long: the current exchange has finished
TX_WINDOW_SECONDS = 0.35      # our frame + RTS settle + controller reply must fit before the next exchange
TX_MAX_WAIT_SECONDS = 2.5     # ~2.4 cycles; refuse rather than transmit into a busy bus
SILENT_BUS_SECONDS = 1.5      # silent for longer than a cycle: nothing to collide with
OWN_REPLY_SECONDS = 0.4       # frames this soon after our own write are replies to us, not the cycle


def cycle_period(starts: "list[float]") -> "float | None":
    """Median panel cycle period from recent exchange start times, ignoring
    intervals outside the plausible 0.9-1.2 s band (skipped or extra
    exchanges). None until there are enough samples to trust."""
    diffs = sorted(b - a for a, b in zip(starts, starts[1:]) if 0.9 <= b - a <= 1.2)
    if len(diffs) < 3:
        return None
    return diffs[len(diffs) // 2]


def tx_window_open(now: float, last_byte_time: float, starts: "list[float]") -> "tuple[bool, str]":
    """Whether transmitting right now is unlikely to collide with the
    panel<->controller cycle, plus a short reason for logs/errors.

    Requires the bus to be quiet (the exchange in progress has finished)
    and, when the cycle period is known, the next predicted exchange to be
    far enough away for our frame and the controller's reply. The phase
    check also rejects the first TX_QUIET_SECONDS of a cycle, so an
    exchange that is merely a few ms late isn't mistaken for a skipped one.
    """
    quiet = now - last_byte_time
    if quiet >= SILENT_BUS_SECONDS:
        return True, f"bus silent for {quiet:.2f}s"
    if quiet < TX_QUIET_SECONDS:
        return False, "bus active"
    period = cycle_period(starts)
    if period is None:
        return True, f"quiet {quiet:.2f}s, no cycle estimate yet"
    since = now - starts[-1]
    if since > 3 * period:
        return True, f"quiet {quiet:.2f}s, cycle estimate stale"
    phase = since % period
    to_next = period - phase
    if phase < TX_QUIET_SECONDS or to_next < TX_WINDOW_SECONDS:
        return False, f"next exchange in {to_next:.2f}s"
    return True, f"quiet {quiet:.2f}s, next exchange in {to_next:.2f}s"


class PassiveListener:
    def __init__(self, state: DeviceState, port: str | None = None):
        self.state = state
        self._port_arg = port
        self._thread: threading.Thread | None = None
        self._reader: transport.FrameReader | None = None
        self._ser = None
        self._write_lock = threading.Lock()

        # Recent bus timing, for placing key-press writes in the silence
        # between panel<->controller exchanges (see tx_window_open()).
        self._timing_lock = threading.Lock()
        self._exchange_starts: "deque[float]" = deque(maxlen=12)
        self._last_frame_at: "float | None" = None
        self._own_tx_at = 0.0

        # Replies for read_block(), handed over by the read thread (one reader
        # on the fd; see regscan.query_registers' wait_response). Holds the
        # function code being waited for, or None.
        self._read_replies: "queue.Queue[transport.RawFrame]" = queue.Queue()
        self._read_waiting: "int | None" = None

    def start(self) -> None:
        self._ser = transport.open_serial(self._port_arg)
        self.state.port = self._ser.port
        self.state.mode = "passive"
        self.state.connected = True
        self._reader = transport.FrameReader(self._ser, is_valid=protocol.verify_crc)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Passive listener started on %s", self._ser.port)

    def stop(self) -> None:
        if self._reader:
            self._reader.stop()
        if self._thread:
            self._thread.join(timeout=2)
        if self._ser:
            self._ser.close()
        self.state.connected = False

    def send_key(self, key: str, hold_seconds: float = 0.3, use_rts: bool = True) -> None:
        """Write a single key press+release to the bus (see master.py
        for the protocol-level details: AID codes, RTS-direction
        toggling), reusing this listener's own serial connection.

        _write_lock serializes concurrent calls -- e.g. two dashboard
        button clicks in quick succession -- so their RTS toggling
        can't interleave and corrupt each other. It does not lock
        against the background read thread; reads and writes on a
        POSIX serial fd don't interfere with each other the way two
        writers would.

        Each write waits for a quiet window between panel<->controller
        exchanges. Raises transport.BusBusyError if none opens for the press (nothing
        is sent); the release is sent regardless, so a key is never left
        held.
        """
        if self._ser is None:
            raise RuntimeError("Listener not started -- call start() first")
        with self._write_lock:
            master.send_key_press(
                self._ser, key, hold_seconds=hold_seconds, use_rts=use_rts,
                before_write=lambda stage: self._wait_for_tx_window(strict=stage == "press"),
            )

    def read_sensor_block(self, use_rts: bool = True) -> "list[int] | None":
        """One atomic FC4 read of the sensor block, fed into state.sensors.
        Returns the 16 words, or None on a timeout or exception reply.
        Raises transport.BusBusyError if no quiet window opens (nothing sent).

        Holds _write_lock for the query and its reply, so it never
        interleaves with a key press (a walk's presses just wait ~1 s).
        """
        frame, words = self.read_block(
            protocol.FunctionCode.READ_INPUT_REGS, sensor_regs.BLOCK_START, sensor_regs.BLOCK_COUNT, use_rts,
        )
        if words is None:
            log.info("Sensor block read: %s", "exception reply" if frame is not None else "timeout")
            return None
        self.state.note_sensor_block(words)
        return words

    READ_FUNCTIONS = (1, 2, 3, 4)  # Modbus READ codes only; this never sends a write code

    def read_block(self, function: int, start: int, count: int, use_rts: bool = True,
                   ) -> "tuple[protocol.Frame | None, list[int] | None]":
        """One guarded read of `count` registers (FC3/FC4) or bits (FC1/FC2)
        from the controller. Returns (frame, values): values is None on a
        timeout (frame None) or an exception reply (frame.is_exception).
        Raises transport.BusBusyError if no quiet window opens (nothing sent),
        ValueError for anything but a read function code."""
        if function not in self.READ_FUNCTIONS:
            raise ValueError(f"function {function} is not a Modbus read code")
        if self._ser is None:
            raise RuntimeError("Listener not started -- call start() first")

        def wait_response(timeout: float) -> "transport.RawFrame | None":
            try:
                return self._read_replies.get(timeout=timeout)
            except queue.Empty:
                return None

        before_write = lambda: self._wait_for_tx_window(strict=True)  # noqa: E731
        with self._write_lock:
            while not self._read_replies.empty():
                self._read_replies.get_nowait()
            self._read_waiting = function
            try:
                if function in (3, 4):
                    return regscan.query_registers(
                        self._ser, protocol.NODE_STYREENHED_1, function, start, count,
                        use_rts=use_rts, timeout=1.0, wait_response=wait_response,
                        before_write=before_write,
                    )
                return self._query_bits(function, start, count, use_rts, wait_response, before_write)
            finally:
                self._read_waiting = None

    def _query_bits(self, function, start, count, use_rts, wait_response, before_write):
        """FC1/FC2 counterpart of regscan.query_registers, with its too-early
        guard. Firmware 1.22's bit replies are NOT standard (2026-09-17):
        always two data bytes, whatever the count, a byte-count field of
        count // 8 + 1 that doesn't match them, and the start address ignored
        (`03 01 01 ED 00`, `03 02 03 ED E0`). So the size isn't checked;
        this returns the bits of the data bytes actually present (bit 0 of
        the first byte first), with no truncation to count."""
        query = protocol.encode_read_query(protocol.NODE_STYREENHED_1, function, start, count)
        write = transport.write_with_rts_direction if use_rts else transport.write_frame
        before_write()
        write(self._ser, query)
        write_at = time.time()
        deadline = time.monotonic() + 1.0
        while (remaining := deadline - time.monotonic()) > 0:
            raw = wait_response(remaining)
            if raw is None:
                break
            if raw.timestamp < write_at + regscan.MIN_REPLY_SECONDS:
                continue
            frame = protocol.decode_frame(raw.data)
            if frame is None or not frame.crc_ok or frame.base_function != function:
                continue
            if frame.is_exception:
                return frame, None
            data = frame.data[1:]  # after the (unreliable) byte count
            return frame, [(byte >> i) & 1 for byte in data for i in range(8)]
        return None, None

    def _wait_for_tx_window(self, strict: bool = True) -> None:
        """Block until tx_window_open() allows a write, then mark the write
        so the controller's reply isn't mistaken for a cycle exchange. After
        TX_MAX_WAIT_SECONDS: raise BusBusyError if strict, otherwise log and
        return so the caller sends anyway."""
        t0 = time.monotonic()
        while True:
            with self._timing_lock:
                starts = list(self._exchange_starts)
            ok, why = tx_window_open(time.time(), self._reader.last_byte_time, starts)
            if ok or time.monotonic() - t0 >= TX_MAX_WAIT_SECONDS:
                if not ok:
                    if strict:
                        raise transport.BusBusyError(f"no safe transmit window within {TX_MAX_WAIT_SECONDS:.1f}s ({why})")
                    log.warning("No safe TX window within %.1fs (%s); sending anyway", TX_MAX_WAIT_SECONDS, why)
                else:
                    log.info("TX window after %.2fs: %s", time.monotonic() - t0, why)
                with self._timing_lock:
                    self._own_tx_at = time.time()
                return
            time.sleep(0.005)

    def _note_bus_timing(self, t: float, frame: "protocol.Frame | None") -> None:
        """Record exchange start times of the panel's cyclic traffic only:
        good-CRC FC65/FC66 frames after a gap, excluding replies to our own
        writes and our polls' FC3/FC4 responses, which would skew the
        predicted cycle phase."""
        cyclic = (
            frame is not None and frame.crc_ok
            and frame.base_function in (protocol.FunctionCode.WI_RO_BITS, protocol.FunctionCode.WI_RO_REGS)
        )
        with self._timing_lock:
            after_gap = self._last_frame_at is None or t - self._last_frame_at >= EXCHANGE_GAP_SECONDS
            if cyclic and after_gap and t - self._own_tx_at >= OWN_REPLY_SECONDS:
                self._exchange_starts.append(t)
            self._last_frame_at = t

    def _bus_healthy(self, node: int = protocol.NODE_STYREENHED_1) -> bool:
        """False if EITHER `node` specifically, OR the bus as a whole
        (address-independent -- see state.py::BusHealth), currently
        looks at risk of the panel's own LINK ERR trigger.

        The bus-wide check matters because a corrupted frame's claimed
        address is exactly as untrustworthy as any other byte in it
        (protocol.decode_frame() reads it before the CRC is checked) --
        a real corruption burst can scatter across many different bogus
        addresses, none of which individually accumulates enough
        consecutive failures to trip the per-node check alone (see
        BusHealth's docstring, added after a 2026-09-15 incident where
        the per-node check fired and paused scans correctly, but a real
        LINK ERR still happened).

        Used by data_walk.py, so a NÄYTÄ DATA walk stops pressing keys
        on an already-struggling bus. No node_stats entry yet (e.g. right
        after startup) counts as healthy for the per-node half.

        Caveat, learned from that same incident: this can only stop OUR
        OWN transmissions -- it cannot prevent a LINK ERR caused by a
        genuine physical-layer/noise problem on the bus itself.
        """
        return not (self.state.is_link_error_risk(node) or self.state.is_bus_at_risk())

    def _run(self) -> None:
        """Drive the read loop, and recover from it dying instead of
        letting the whole background thread exit silently.

        Found the hard way: a transient serial read glitch right after
        reopening the port (SerialException: "device reports readiness
        to read but returned no data") killed this thread outright on
        one restart, with no crash and no visible symptom other than
        the dashboard's frame counter silently freezing forever while
        state.connected stayed True -- indistinguishable from a healthy
        idle bus without checking last_frame_age by hand. Now it
        reopens the port and keeps going instead.
        """
        assert self._reader is not None
        while True:
            try:
                self._reader.read_forever(self._on_raw_frame)
                return  # reader.stop() was called -- normal shutdown
            except Exception:
                log.exception(
                    "Passive read loop crashed (serial glitch?) -- "
                    "reopening the port and retrying"
                )
                self.state.connected = False
                self._reopen()

    def _reopen(self, delay_seconds: float = 1.0) -> None:
        time.sleep(delay_seconds)
        with self._write_lock:
            try:
                self._ser.close()
            except Exception:
                pass
            try:
                self._ser = transport.open_serial(self._port_arg)
            except Exception:
                log.exception("Reopening the port failed; will retry")
                return
            self.state.port = self._ser.port
            self._reader = transport.FrameReader(self._ser, is_valid=protocol.verify_crc)
        self.state.connected = True

    def _on_raw_frame(self, raw_frame: transport.RawFrame) -> None:
        frame = protocol.decode_frame(raw_frame.data)
        self._note_bus_timing(raw_frame.timestamp, frame)
        if frame is None:
            return

        self.state.note_frame(frame)
        self.state.note_raw(frame.address, frame.function, frame.raw, frame.crc_ok)

        if not frame.crc_ok:
            return  # per the doc: bad-CRC frames are simply discarded

        waiting = self._read_waiting
        if waiting is not None and frame.base_function == waiting and frame.address == protocol.NODE_STYREENHED_1:
            self._read_replies.put(raw_frame)

        try:
            self._decode_payload(frame)
        except Exception:  # keep the listener alive even on a parse bug
            log.exception("Failed to decode frame: %s", protocol.hexdump(frame.raw))

    def _decode_payload(self, frame: protocol.Frame) -> None:
        fc = frame.base_function

        if fc == protocol.FunctionCode.WI_RO_REGS:
            block = protocol.parse_reg_block(frame.data)
            if block:
                self.state.note_reg_block(frame.address, frame.function, block)

        elif fc == protocol.FunctionCode.WI_RO_BITS:
            block = protocol.parse_bit_block(frame.data)
            if block:
                self.state.note_bit_block(frame.address, frame.function, block)

        elif fc == protocol.FunctionCode.REPORT_SLAVE_ID:
            slave_id = protocol.parse_slave_id(frame.data)
            if slave_id:
                self.state.note_slave_id(slave_id)

        # FC 3/16 (plain read/write output regs) and others: not yet
        # given dedicated handling. They still show up in the raw log
        # via note_raw() above.
