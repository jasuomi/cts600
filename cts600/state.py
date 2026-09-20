"""Canonical in-memory model of CTS600 bus state.

Frames get decoded (by passive_listener.py, and later master.py) and
fed into a single DeviceState instance. This module owns thread
safety: decoding happens on a background serial-reading thread, while
the web layer reads/subscribes from the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import display_data, protocol, sensor_regs
from .display_decoder import DisplayBuffer
from .regmap import INPUT_BITS, OUTPUT_REGS, describe_reg

log = logging.getLogger(__name__)

RAW_LOG_MAXLEN = 500

# Output register the panel writes its held key into (one-hot _AID_xxx
# code, 0 = no key). Our own writes are never seen by the listener (RTS
# gates the local receiver while we transmit), so a non-zero value here
# means someone pressed a key on the physical panel.
AID_REG = 0x0100

# A reading unchanged on screen is re-published at most this often, so
# "seen N min ago" stays current without flooding the websocket.
READING_REPUBLISH_SECONDS = 30.0

# How many CONSECUTIVE bad-CRC frames on one node counts as "at risk of
# LINK ERR". Lodam documents the panel's own real trigger as 10
# consecutive communication failures -- this is deliberately set to half
# that, so software notices (and, via PassiveListener's health_check
# wiring, stops transmitting) well before the panel itself would fault.
LINK_ERR_RISK_THRESHOLD = 5

# Window (seconds) used for the "recent" CRC error rate, kept separate
# from the lifetime crc_error_count/frame_count -- a lifetime average
# hides a fast recent spike (or a slow creep) behind a long clean
# session's history. "CRC rate climbing" showed up repeatedly during
# bus testing as an early warning.
RECENT_WINDOW_SECONDS = 60.0

# --- Bus-wide health (address-independent) ----------------------------
#
# 2026-09-15 incident: the per-node guard above DID fire (5 consecutive
# bad-CRC frames seen from node 3, scans correctly paused) but the panel
# still hit a real LINK ERR shortly after, needing a manual power-cycle.
# Two gaps, not one bug:
#
# 1. protocol.decode_frame() reads the address byte from the frame
#    unconditionally, BEFORE the CRC is checked -- so a corrupted frame's
#    claimed address is exactly as untrustworthy as any other byte in it
#    (already observed in practice: a garbled frame once landed under a
#    bogus node "39", not 3). A real corruption burst can scatter across
#    many different bogus addresses, each only accumulating 1-2 bad
#    frames -- never enough to trip any single node's consecutive
#    counter, even while the bus overall is clearly unwell. Fixed by
#    ALSO tracking a bus-wide, address-independent BusHealth below --
#    every decoded frame counts toward it no matter what (possibly
#    corrupted) address it claims.
# 2. consecutive_crc_errors resets to 0 the instant a single good frame
#    arrives, so link_error_risk can clear mid-recovery, one good frame
#    after a bad streak, even while the underlying noise/physical-layer
#    problem is still actively ongoing (which is exactly what a real
#    noise burst looks like -- not every frame in it is corrupted, node
#    3's own cyclic traffic keeps mixing in occasional clean frames).
#    Fixed with a hold/cooldown: once tripped, "at risk" stays true for
#    RISK_HOLD_SECONDS after the last qualifying bad frame, not just
#    until the next good one.
#
# Also added a RATE-based trigger (not just a consecutive-run one): a
# sustained elevated bad-frame rate over the recent window is exactly
# the "CRC rate climbing" pattern seen repeatedly on this bus as a
# precursor -- it can be a real warning sign even when
# no single run of consecutive failures ever gets long enough to trip
# the streak-based check on its own.
#
# IMPORTANT CAVEAT, learned from the same incident: none of this can
# PREVENT a LINK ERR caused by a genuine physical-layer/noise event --
# pausing our own transmissions only removes OUR contribution to bus
# contention. The 2026-09-15 incident's own log shows the guard paused
# scanning appropriately *before* the LINK ERR, with no query traffic
# from us during the window it happened in -- strong evidence that
# particular incident was environmental (cabling/termination/grounding/
# EMI), not caused by this software's pacing. This module can only
# detect and react faster; a repeat despite a fast, correct reaction is
# a hardware/physical-layer signal, not a software bug to chase further.
BUS_LINK_ERR_RISK_THRESHOLD = 4  # bus-wide consecutive bad frames, ANY address
BUS_RECENT_RATE_THRESHOLD = 0.15  # 15% bad frames in the recent window
BUS_RECENT_RATE_MIN_SAMPLES = 15  # don't trust the rate signal on a tiny sample
RISK_HOLD_SECONDS = 20.0  # stay "at risk" this long after the last qualifying bad frame


@dataclass
class NodeStats:
    frame_count: int = 0
    crc_error_count: int = 0
    last_frame_time: float | None = None
    consecutive_crc_errors: int = 0
    # (timestamp, crc_ok) for frames within the last RECENT_WINDOW_SECONDS,
    # used to compute recent_crc_rate() without keeping unbounded history.
    _recent: deque[tuple[float, bool]] = field(default_factory=deque)

    def note(self, crc_ok: bool, now: float) -> None:
        self.frame_count += 1
        self.last_frame_time = now
        if crc_ok:
            self.consecutive_crc_errors = 0
        else:
            self.crc_error_count += 1
            self.consecutive_crc_errors += 1
        self._recent.append((now, crc_ok))
        cutoff = now - RECENT_WINDOW_SECONDS
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()

    @property
    def recent_crc_rate(self) -> float | None:
        """Fraction of bad-CRC frames within the last RECENT_WINDOW_SECONDS,
        or None if no frames have been seen in that window yet."""
        if not self._recent:
            return None
        bad = sum(1 for _, ok in self._recent if not ok)
        return bad / len(self._recent)

    @property
    def link_error_risk(self) -> bool:
        return self.consecutive_crc_errors >= LINK_ERR_RISK_THRESHOLD


@dataclass
class BusHealth:
    """Address-independent bus health -- every decoded frame counts
    toward this regardless of what (possibly corrupted, possibly bogus)
    address it claims. See the "Bus-wide health" comment above for why
    this exists alongside the per-node NodeStats: a real corruption
    burst can scatter across many different bogus addresses, none of
    which individually accumulates enough consecutive failures to trip
    NodeStats.link_error_risk, even while the bus overall is unwell.
    """
    frame_count: int = 0
    crc_error_count: int = 0
    consecutive_crc_errors: int = 0
    _recent: deque[tuple[float, bool]] = field(default_factory=deque)
    _last_risk_time: float | None = None

    def note(self, crc_ok: bool, now: float) -> bool:
        """Update from one decoded frame. Returns True if this frame's
        arrival is a NEW transition into "at risk" (edge-triggered, for
        logging a warning once rather than on every subsequent frame)."""
        self.frame_count += 1
        if crc_ok:
            self.consecutive_crc_errors = 0
        else:
            self.crc_error_count += 1
            self.consecutive_crc_errors += 1
        self._recent.append((now, crc_ok))
        cutoff = now - RECENT_WINDOW_SECONDS
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()

        was_at_risk = self.at_risk(now)
        if self._trip_condition():
            self._last_risk_time = now
        return self.at_risk(now) and not was_at_risk

    def _trip_condition(self) -> bool:
        if self.consecutive_crc_errors >= BUS_LINK_ERR_RISK_THRESHOLD:
            return True
        if len(self._recent) >= BUS_RECENT_RATE_MIN_SAMPLES:
            bad = sum(1 for _, ok in self._recent if not ok)
            if bad / len(self._recent) >= BUS_RECENT_RATE_THRESHOLD:
                return True
        return False

    def at_risk(self, now: float | None = None) -> bool:
        """True if a trip condition fired within the last
        RISK_HOLD_SECONDS -- not just "is a trip condition true right
        this instant", so a single good frame arriving mid-noise-burst
        doesn't immediately wave the bus healthy again. See the
        "Bus-wide health" comment above for why this hold/cooldown was
        added."""
        if self._last_risk_time is None:
            return False
        now = time.time() if now is None else now
        return (now - self._last_risk_time) < RISK_HOLD_SECONDS


def _operation(status: dict[str, Any]) -> dict[str, Any]:
    """Walk/edit status without its per-publish timestamp."""
    return {k: v for k, v in status.items() if k != "updated_at"}


class DeviceState:
    def __init__(self, readings_path: str | None = None, sensors_path: str | None = None) -> None:
        self._lock = threading.Lock()
        self.connected = False
        self.port: str | None = None
        self.mode = "passive"
        self.started_at = time.time()

        self.node_stats: dict[int, NodeStats] = {}
        self.bus_health = BusHealth()
        self.output_regs: dict[int, int] = {}
        self.input_regs: dict[int, int] = {}
        self.output_bits: dict[int, list[int]] = {}
        self.input_bits: dict[int, list[int]] = {}
        self.display = DisplayBuffer()
        self.slave_id: protocol.SlaveId | None = None

        # Per LCD line: {"text", "changed_at", "received_at"}. The panel
        # retransmits the current screen every cycle, one line per block,
        # so right after a screen change one line can still hold the
        # previous screen's text. A screen counts as consistent only once
        # both lines have been received again after the latest change of
        # either -- see screen().
        self._lines: dict[int, dict] = {}

        # "NÄYTÄ DATA" readings, harvested whenever a data screen is shown
        # (by a person at the panel, the dashboard buttons, or data_walk.py).
        self.readings = display_data.ReadingsStore(readings_path)
        self._reading_published_at: dict[str, float] = {}

        # Temperatures reconstructed from FC4 0x0000-0x000F (sensor_regs.py),
        # anchored by the readings above.
        self.sensors = sensor_regs.SensorTracker(sensors_path)

        # Last time a non-zero key code was seen in the panel's own traffic.
        self.last_physical_key_at: float | None = None

        # Progress of the NÄYTÄ DATA menu walk (data_walk.py), for the UI.
        self.walk_status: dict[str, Any] = {"running": False}
        # Progress of a settings change (panel_settings.py).
        self.edit_status: dict[str, Any] = {"running": False}

        # Last idle screen seen, parsed (display_data.parse_idle) plus "at".
        # Kept while the panel shows a menu, so the settings don't blank.
        self.idle_status: dict[str, Any] | None = None

        self._raw_log: deque[dict[str, Any]] = deque(maxlen=RAW_LOG_MAXLEN)
        self._capture_log = None  # set via attach_capture_log()

        # subscribers: (event_loop, asyncio.Queue) pairs, for pushing
        # live updates out over websockets. Registered from the web layer.
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, "asyncio.Queue[dict]"]] = []

    # -- subscription ---------------------------------------------------

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> "asyncio.Queue[dict]":
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[dict]") -> None:
        with self._lock:
            self._subscribers = [(l, q) for (l, q) in self._subscribers if q is not queue]

    def _publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for loop, queue in subs:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass  # loop closed; subscriber will be cleaned up on unsubscribe

    # -- ingest (called from the serial-reading thread) ------------------

    def note_frame(self, frame: protocol.Frame) -> None:
        now = time.time()
        with self._lock:
            # A bad frame's address byte is as untrustworthy as the rest of
            # it: only nodes that have sent a valid frame get per-node stats.
            # Otherwise every garbled address becomes a "node" whose error
            # streak can never be cleared by a good frame, and the warning
            # sticks forever (seen 2026-09-17: bogus nodes 0, 62, 65).
            # Bad frames from unknown addresses still count bus-wide below.
            stats = self.node_stats.get(frame.address)
            if stats is None and frame.crc_ok:
                stats = self.node_stats[frame.address] = NodeStats()
            was_at_risk = stats is not None and stats.link_error_risk
            if stats is not None:
                stats.note(frame.crc_ok, now)
            newly_at_risk = stats is not None and stats.link_error_risk and not was_at_risk
            # Bus-wide, address-independent -- updated from EVERY frame
            # regardless of what (possibly corrupted) address it claims.
            # See BusHealth's docstring for why this exists alongside the
            # per-node check above.
            bus_newly_at_risk = self.bus_health.note(frame.crc_ok, now)
        # Logged outside the lock, edge-triggered (only on the transition
        # into risk, not once per frame after) -- see LINK_ERR_RISK_THRESHOLD.
        if newly_at_risk:
            log.warning(
                "Node 0x%02X: %d consecutive CRC errors -- bus health "
                "degraded, at risk of the panel's own LINK ERR state "
                "(triggers at 10 consecutive failures per Lodam's spec). "
                "Active query paths will pause until this clears.",
                frame.address, stats.consecutive_crc_errors,
            )
        if bus_newly_at_risk:
            log.warning(
                "Bus-wide: %d consecutive bad-CRC frames (any address) "
                "and/or a sustained elevated error rate -- bus health "
                "degraded. Active query paths will pause for at least "
                "%.0fs. Note: this only stops OUR OWN transmissions -- "
                "it cannot prevent a LINK ERR caused by a genuine "
                "physical-layer/noise problem on the bus itself.",
                self.bus_health.consecutive_crc_errors, RISK_HOLD_SECONDS,
            )

    def note_reg_block(self, address: int, fc: int, block: protocol.RegBlock) -> None:
        now = time.time()
        screen = None
        with self._lock:
            is_display = self.display.is_display_reg(block.start_reg)
            words = block.words()

            if is_display:
                word_bytes = [
                    block.data[2 * i:2 * i + 2] for i in range(len(words))
                ]
                self.display.update(block.start_reg, word_bytes)
                self._note_lines(block.start_reg, len(words), now)
                screen = self._consistent_screen_locked()
            else:
                # Heuristic: FC66 carries both "write input"/"read output"
                # halves in one exchange, per the doc. We don't yet
                # distinguish query vs response direction at this layer,
                # so we record into both maps and let the UI show both.
                for i, w in enumerate(words):
                    reg = block.start_reg + i
                    self.output_regs[reg] = w
                    self.input_regs[reg] = w
                    if reg == AID_REG and w != 0:
                        self.last_physical_key_at = now

        if screen is not None:
            self._harvest(screen, now)
            idle = display_data.parse_idle(screen[0])
            if idle is not None:
                with self._lock:
                    self.idle_status = {**idle, "at": screen[1]}

        event = {
            "type": "reg_block",
            "address": address,
            "fc": fc,
            "start_reg": f"0x{block.start_reg:04X}",
            "is_display": is_display,
            "text": block.as_text() if is_display else None,
            "words": [f"0x{w:04X}" for w in words],
            "label": describe_reg(OUTPUT_REGS, block.start_reg) if not is_display else "Display data",
        }
        self._log_and_publish(event)

    def note_bit_block(self, address: int, fc: int, block: protocol.BitBlock) -> None:
        with self._lock:
            self.output_bits[block.start_bit] = block.bits()
            self.input_bits[block.start_bit] = block.bits()

        event = {
            "type": "bit_block",
            "address": address,
            "fc": fc,
            "start_bit": f"0x{block.start_bit:04X}",
            "bits": block.bits(),
            "label": describe_reg(INPUT_BITS, block.start_bit),
        }
        self._log_and_publish(event)

    # -- display screens and NÄYTÄ DATA readings ---------------------------

    def _note_lines(self, start_reg: int, n_words: int, now: float) -> None:
        for line_reg, end_reg in (
            (display_data.LINE1_REG, display_data.LINE2_REG),
            (display_data.LINE2_REG, display_data.LINE_END_REG),
        ):
            if not (start_reg <= line_reg < start_reg + n_words):
                continue
            text = self.display.line_text(line_reg, end_reg)
            info = self._lines.get(line_reg)
            if info is None or info["text"] != text:
                self._lines[line_reg] = {"text": text, "changed_at": now, "received_at": now}
            else:
                info["received_at"] = now

    def _consistent_screen_locked(self) -> "tuple[display_data.Screen, float] | None":
        l1 = self._lines.get(display_data.LINE1_REG)
        l2 = self._lines.get(display_data.LINE2_REG)
        if l1 is None or l2 is None:
            return None
        last_change = max(l1["changed_at"], l2["changed_at"])
        if min(l1["received_at"], l2["received_at"]) <= last_change:
            return None
        return display_data.classify(l1["text"], l2["text"]), min(l1["received_at"], l2["received_at"])

    def screen(self) -> "tuple[display_data.Screen, float] | None":
        """The current screen and the time both of its lines were confirmed
        (the older of the two receive times), or None while the two lines
        may still belong to different screens."""
        with self._lock:
            return self._consistent_screen_locked()

    def _harvest(self, screen_at: "tuple[display_data.Screen, float]", now: float) -> None:
        screen, _ = screen_at
        if screen.reading is None:
            return
        changed = self.readings.update(screen, now)
        if self.sensors.on_display(screen.reading, screen.number, now):
            self._log_and_publish({"type": "sensors", "cause": "anchor", "key": screen.reading})
        last = self._reading_published_at.get(screen.reading, 0.0)
        if changed or now - last >= READING_REPUBLISH_SECONDS:
            self._reading_published_at[screen.reading] = now
            if changed:
                log.info("NÄYTÄ DATA %s = %s", screen.reading, screen.value)
            self._log_and_publish({
                "type": "reading", "key": screen.reading,
                "value": screen.value, "changed": changed,
            })

    def set_walk_status(self, **status: Any) -> None:
        with self._lock:
            self.walk_status = {**status, "updated_at": time.time()}
            payload = dict(self.walk_status)
        self._log_and_publish({"type": "walk_status", "status": payload})

    def set_edit_status(self, **status: Any) -> None:
        with self._lock:
            self.edit_status = {**status, "updated_at": time.time()}
            payload = dict(self.edit_status)
        self._log_and_publish({"type": "edit_status", "status": payload})

    def compressor_on(self) -> bool | None:
        """The status LED's on bit (on while the compressor runs), or None
        before the first LED frame."""
        with self._lock:
            led = self.output_bits.get(0x0100)
            return bool(led[0]) if led else None

    def note_sensor_block(self, words: list[int]) -> None:
        """One atomic FC4 read of the sensor block (see sensor_regs.py)."""
        if self.sensors.on_poll(words):
            self._log_and_publish({"type": "sensors", "cause": "poll"})

    def note_slave_id(self, slave_id: protocol.SlaveId) -> None:
        with self._lock:
            self.slave_id = slave_id
        self._log_and_publish({"type": "slave_id", "slave_id": vars(slave_id)})

    def note_raw(self, address: int, fc: int, raw: bytes, crc_ok: bool) -> None:
        event = {
            "type": "raw",
            "timestamp": time.time(),
            "address": address,
            "fc": f"0x{fc:02X}",
            "crc_ok": crc_ok,
            "hex": protocol.hexdump(raw),
        }
        self._log_and_publish(event, log_only=True)

    # -- persistent capture logging ---------------------------------------

    def attach_capture_log(self, capture_log) -> None:
        """Enable durable JSONL logging of every event to disk.

        Safe to call before or after the listener starts; not safe to
        call from multiple threads concurrently (call it once, at
        startup, before starting the listener thread).
        """
        self._capture_log = capture_log

    def add_note(self, text: str) -> None:
        """Record a user-supplied marker, e.g. "pressed UP button now".

        Only meaningful (and only persisted) when a capture log is
        attached -- notes are also published live so the dashboard
        can show them in the log pane even without one.
        """
        event = {"type": "note", "text": text}
        self._log_and_publish(event)

    def _log_and_publish(self, event: dict[str, Any], log_only: bool = False) -> None:
        event.setdefault("timestamp", time.time())
        with self._lock:
            self._raw_log.append(event)
            capture_log = self._capture_log
        if capture_log is not None:
            capture_log.write(event)
        self._publish(event)

    def is_link_error_risk(self, address: int) -> bool:
        """True if `address` currently shows enough consecutive CRC
        errors to be at risk of the panel's own LINK ERR trigger --
        see NodeStats.link_error_risk/LINK_ERR_RISK_THRESHOLD. Used by
        PassiveListener._bus_healthy() to gate active query bursts. No
        stats yet for this address (e.g. right after startup) counts
        as healthy, not at risk.
        """
        with self._lock:
            stats = self.node_stats.get(address)
            return stats is not None and stats.link_error_risk

    def is_bus_at_risk(self) -> bool:
        """True if the bus-wide, address-independent health check (see
        BusHealth) currently reports risk -- the authoritative signal
        PassiveListener._bus_healthy() gates active query bursts on,
        alongside (not instead of) the per-node check above. Catches a
        real corruption burst that scatters across many different
        (possibly bogus) addresses rather than accumulating under one.
        """
        with self._lock:
            return self.bus_health.at_risk()

    # -- snapshot (called from the web layer) -----------------------------

    def status(self) -> dict[str, Any]:
        """Compact, parsed state for integrations (Home Assistant): the
        idle screen's settings, the status LED, the readings and operation
        progress. Times are the host's epoch seconds; "server_time" lets a
        client correct for clock skew. Everything but server_time and
        last_frame_at only changes when something on the panel does."""
        with self._lock:
            screen_at = self._consistent_screen_locked()
            led = self.output_bits.get(0x0100)
            last_frame = max(
                (s.last_frame_time for s in self.node_stats.values() if s.last_frame_time),
                default=None,
            )
            slave = self.slave_id
            return {
                "server_time": time.time(),
                "connected": self.connected,
                "port": self.port,
                "last_frame_at": last_frame,
                "screen": screen_at[0].key if screen_at else None,
                "panel": dict(self.idle_status) if self.idle_status else None,
                "led": {"on": bool(led[0]), "blink": len(led) > 1 and bool(led[1])} if led else None,
                "readings": {
                    k: {kk: v[kk] for kk in ("value", "number", "text", "at")}
                    for k, v in self.readings.snapshot().items()
                },
                "sensors": self.sensors.snapshot(),
                "walk": _operation(self.walk_status),
                "edit": _operation(self.edit_status),
                "device": {
                    "product": slave.product, "sw_version": slave.sw_version,
                    "protocol_version": slave.prot_ver,
                } if slave else None,
                "bus_at_risk": self.bus_health.at_risk(),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            screen_at = self._consistent_screen_locked()
            walk_status = dict(self.walk_status)
            if "started_at" in walk_status:
                walk_status["started_age_s"] = now - walk_status["started_at"]
            if "finished_at" in walk_status:
                walk_status["finished_age_s"] = now - walk_status["finished_at"]
            return {
                "connected": self.connected,
                "port": self.port,
                "mode": self.mode,
                "uptime_seconds": time.time() - self.started_at,
                "node_stats": {
                    addr: {
                        "frame_count": s.frame_count,
                        "crc_error_count": s.crc_error_count,
                        "last_frame_age": (
                            time.time() - s.last_frame_time if s.last_frame_time else None
                        ),
                        "consecutive_crc_errors": s.consecutive_crc_errors,
                        "recent_crc_rate": s.recent_crc_rate,
                        "link_error_risk": s.link_error_risk,
                    }
                    for addr, s in self.node_stats.items()
                },
                "bus_health": {
                    "frame_count": self.bus_health.frame_count,
                    "crc_error_count": self.bus_health.crc_error_count,
                    "consecutive_crc_errors": self.bus_health.consecutive_crc_errors,
                    "at_risk": self.bus_health.at_risk(),
                },
                "output_regs": {f"0x{k:04X}": v for k, v in self.output_regs.items()},
                "input_regs": {f"0x{k:04X}": v for k, v in self.input_regs.items()},
                "output_bits": {f"0x{k:04X}": v for k, v in self.output_bits.items()},
                "input_bits": {f"0x{k:04X}": v for k, v in self.input_bits.items()},
                "display": self.display.snapshot(),
                "slave_id": vars(self.slave_id) if self.slave_id else None,
                "screen": screen_at[0].key if screen_at else None,
                # Ages rather than timestamps, so the browser doesn't
                # depend on its clock agreeing with the host's.
                "readings": self.readings.snapshot(now),
                "sensors": self.sensors.snapshot(now, detail=True),
                "walk": walk_status,
                "edit": dict(self.edit_status),
                "recent_log": list(self._raw_log)[-100:],
            }
