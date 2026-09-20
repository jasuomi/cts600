"""Web layer: a small FastAPI app serving the CTS600 dashboard.

- GET  /api/state               -> full current snapshot (JSON)
- POST /api/press               -> emulate a physical key press (only when the
                                   app was started with --enable-control --
                                   see __main__.py / passive_listener.py::send_key)
- POST /api/display_data/refresh -> walk the panel's NÄYTÄ DATA menu once to
                                   refresh every reading (--enable-control
                                   only; see data_walk.py for the guards)
- POST /api/display_data/cancel  -> stop a running refresh; it Escs back to idle
- POST /api/settings            -> change mode/setpoint/fan through the panel's
                                   own menu (--enable-control only; see
                                   panel_settings.py for the guards)
- POST /api/read                -> one read-only FC1-4 query, for exploring
                                   registers and bits (--enable-control only)
- POST /api/note                -> add a note to the frame log / capture file
- GET  /api/status              -> compact parsed status (for Home Assistant)
- WS   /ws                      -> live event stream (JSON messages)
- WS   /ws/status               -> /api/status, pushed whenever it changes
- GET  /                        -> the dashboard page

With --enable-control, one thing transmits on its own: the sensor poll,
an atomic FC4 read of the temperature block every sensor_poll_seconds
(sensor_regs.py), every SENSOR_FAST_POLL_SECONDS while the condenser swings
or just after the compressor switches, paused while the bus looks unhealthy.
With auto_reanchor it also presses keys on its own: a short walk to the
condenser's data screen once that value has lost tracking and settled
(reanchor_due() has the conditions).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__, master, panel_settings
from ..data_walk import DataWalker
from ..panel_ops import OperationBusyError
from ..panel_settings import SettingsEditor
from ..state import DeviceState
from ..transport import BusBusyError

if TYPE_CHECKING:
    from ..passive_listener import PassiveListener

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger(__name__)


class NoteBody(BaseModel):
    text: str


class PressBody(BaseModel):
    key: str
    hold: float = 0.3


class SettingsBody(BaseModel):
    mode: str | None = None
    setpoint: int | None = None
    fan: int | None = None


class ReadBody(BaseModel):
    function: int  # 1 output bits, 2 input bits, 3 output registers, 4 input registers
    start: int
    count: int = 1


# POST /api/read limits. Register replies are cut at ~50 bytes by this
# controller, so at most 22 words; bits are small either way.
READ_MAX_WORDS = 22
READ_MAX_BITS = 128
READ_MIN_SPACING_SECONDS = 1.0


# /ws/status: event types that can change the status, how long to gather a
# burst of them before sending, and how often to send regardless (so
# last_frame_at and a lapsed bus-health hold still arrive).
STATUS_EVENTS = {"reg_block", "bit_block", "reading", "walk_status", "edit_status", "slave_id", "sensors"}
STATUS_COALESCE_SECONDS = 0.5
STATUS_HEARTBEAT_SECONDS = 30.0
STATUS_VOLATILE = ("server_time", "last_frame_at")

# The first sensor read waits this long after startup, so the listener has
# learned the panel's cycle and a quick series of restarts can't fire a burst
# of queries.
SENSOR_POLL_STARTUP_DELAY_SECONDS = 30.0
# After this many failed reads in a row, poll at the slow interval until one works.
SENSOR_POLL_MAX_FAILURES = 3
SENSOR_POLL_SLOW_SECONDS = 60.0

# Fast polling while the condenser swings (sensor_regs.py: tracking the low
# byte breaks if a value moves >1.28 °C between polls). 2.5 s is about one
# query per 2.4 panel cycles; 330 reads at ~2.3 s ran clean on 2026-09-16
# (wrap_watch.py). Triggered by a detected swing, or by the compressor
# switching, since the drop can start before a 10 s poll sees it. Capped so
# a long swing can't keep the bus at the fast rate indefinitely.
SENSOR_FAST_POLL_SECONDS = 2.5
SENSOR_FAST_HOLD_SECONDS = 90.0      # stay fast this long after the last trigger
SENSOR_FAST_MAX_SECONDS = 600.0      # longest continuous fast stretch...
SENSOR_FAST_COOLDOWN_SECONDS = 120.0  # ...then this long at the normal rate
SENSOR_POLL_TICK_SECONDS = 0.5


class SensorPollPacer:
    """Picks the next poll interval: normal, fast (during swings, with a cap
    and cooldown) or slow (after repeated failures). Pure; times are
    monotonic seconds."""

    def __init__(self, normal_seconds: float) -> None:
        self.normal = normal_seconds
        self.fast_until = 0.0
        self.fast_since: float | None = None
        self.cooldown_until = 0.0
        self.last_compressor: bool | None = None
        self.failures = 0

    def note(self, now: float, compressor_on: bool | None, swinging: list[str], useful: bool = True) -> None:
        """useful: False once every swing-watched sensor has lost tracking
        (sensor_regs.fast_poll_useful); fast polls can't recover it, so fast
        mode ends and nothing triggers it until a re-anchor."""
        if compressor_on is not None:
            if useful and self.last_compressor is not None and compressor_on != self.last_compressor:
                self._trigger(now, f"compressor {'on' if compressor_on else 'off'}")
            self.last_compressor = compressor_on
        if swinging and useful:
            self._trigger(now, "swing: " + ", ".join(swinging))
        if not useful and self.fast_until > now:
            log.info("Sensor poll: tracking already lost, fast polling can't help")
            self.fast_until = 0.0

        fast = now < self.fast_until
        if fast and self.fast_since is None:
            self.fast_since = now
            log.info("Sensor poll: fast (every %.1fs)", SENSOR_FAST_POLL_SECONDS)
        elif not fast and self.fast_since is not None:
            self.fast_since = None
            log.info("Sensor poll: back to every %.0fs", self.normal)
        if self.fast_since is not None and now - self.fast_since >= SENSOR_FAST_MAX_SECONDS:
            log.warning("Sensor poll: fast for %.0fs, pausing fast polling for %.0fs",
                        SENSOR_FAST_MAX_SECONDS, SENSOR_FAST_COOLDOWN_SECONDS)
            self.fast_since = None
            self.fast_until = 0.0
            self.cooldown_until = now + SENSOR_FAST_COOLDOWN_SECONDS

    def _trigger(self, now: float, why: str) -> None:
        if now < self.cooldown_until:
            return
        if self.fast_since is None:
            log.info("Sensor poll trigger: %s", why)
        self.fast_until = max(self.fast_until, now + SENSOR_FAST_HOLD_SECONDS)

    @property
    def fast(self) -> bool:
        return self.fast_since is not None

    def interval(self) -> float:
        if self.failures >= SENSOR_POLL_MAX_FAILURES:
            return SENSOR_POLL_SLOW_SECONDS
        return min(self.normal, SENSOR_FAST_POLL_SECONDS) if self.fast else self.normal


# Automatic re-anchor: when a swing-prone register temperature has lost
# tracking (e.g. the condenser 2 -> 33 °C within minutes after a mode change,
# 2026-09-17 13:05), walk the panel only down to its data screen and back once
# it has settled. Presses keys on its own, so it is conservative: the idle
# screen, nobody at the panel for REANCHOR_KEY_QUIET_SECONDS, no other panel
# operation, and at most one attempt per REANCHOR_MIN_INTERVAL_SECONDS.
REANCHOR_SCREENS = {"condenser": "LAUHDUT"}   # sensor key -> display_data screen key
REANCHOR_SETTLE_SECONDS = 60.0     # no swing for this long before walking
REANCHOR_MAX_WAIT_SECONDS = 600.0  # uncertain this long: walk even if still drifting
REANCHOR_MIN_INTERVAL_SECONDS = 600.0
REANCHOR_KEY_QUIET_SECONDS = 120.0


def reanchor_due(
    now: float, uncertain_since: float | None, last_swing_at: float | None,
    last_attempt_at: float | None, last_key_at: float | None,
    screen: str | None, operation_running: bool,
) -> bool:
    """Whether to start an automatic re-anchor walk now (wall-clock times)."""
    if uncertain_since is None or operation_running or screen != "idle":
        return False
    if last_attempt_at is not None and now - last_attempt_at < REANCHOR_MIN_INTERVAL_SECONDS:
        return False
    if last_key_at is not None and now - last_key_at < REANCHOR_KEY_QUIET_SECONDS:
        return False
    settled = last_swing_at is None or now - last_swing_at >= REANCHOR_SETTLE_SECONDS
    waited = now - uncertain_since >= REANCHOR_MAX_WAIT_SECONDS
    return (settled and now - uncertain_since >= REANCHOR_SETTLE_SECONDS) or waited


def create_app(
    state: DeviceState, listener: "PassiveListener | None" = None,
    rts_direction: bool = False, sensor_poll_seconds: float = 0.0,
    auto_reanchor: bool = False,
) -> FastAPI:
    async def reanchor(sensor_key: str) -> None:
        screen_key = REANCHOR_SCREENS[sensor_key]
        state.add_note(f"automatic {sensor_key} re-anchor started (walk to {screen_key})")
        try:
            result = await asyncio.to_thread(
                walker.run, target=screen_key, purpose=f"{sensor_key} re-anchor (automatic)",
            )
            state.add_note(f"automatic {sensor_key} re-anchor {result['outcome']}: {result['message']}")
        except OperationBusyError:
            log.info("Automatic %s re-anchor skipped: a panel operation is running", sensor_key)

    async def poll_sensors() -> None:
        loop = asyncio.get_running_loop()
        pacer = SensorPollPacer(sensor_poll_seconds)
        start_at = loop.time() + SENSOR_POLL_STARTUP_DELAY_SECONDS
        last_read = None
        last_swing_at: dict[str, float] = {}
        last_attempt_at: dict[str, float] = {}
        while True:
            now = loop.time()
            wall = time.time()
            sensors = state.sensors
            swinging_any = sensors.swinging(wall)
            for key in swinging_any:
                last_swing_at[key] = wall
            pacer.note(now, state.compressor_on(), sensors.swinging(wall, only_ok=True),
                       useful=sensors.fast_poll_useful())

            if auto_reanchor and walker is not None and last_read is not None:
                screen = state.screen()
                for key in REANCHOR_SCREENS:
                    if reanchor_due(wall, sensors.uncertain_since(key), last_swing_at.get(key),
                                    last_attempt_at.get(key), state.last_physical_key_at,
                                    screen[0].key if screen else None, walker.running):
                        last_attempt_at[key] = wall
                        log.info("Automatic re-anchor of %s: uncertain for %.0fs", key,
                                 wall - sensors.uncertain_since(key))
                        _background(reanchor(key))
                        break

            due = start_at if last_read is None else max(start_at, last_read + pacer.interval())
            if now < due:
                await asyncio.sleep(min(SENSOR_POLL_TICK_SECONDS, due - now))
                continue
            last_read = now
            if not listener._bus_healthy():
                log.info("Sensor poll skipped: bus health degraded")
                continue
            try:
                words = await asyncio.to_thread(listener.read_sensor_block, rts_direction)
                pacer.failures = 0 if words is not None else pacer.failures + 1
            except BusBusyError as exc:
                log.info("Sensor poll skipped: %s", exc)
            except Exception:
                pacer.failures += 1
                log.exception("Sensor poll failed")
            if pacer.failures == SENSOR_POLL_MAX_FAILURES:
                log.warning("Sensor poll: %d failed reads in a row, slowing to every %.0fs",
                            pacer.failures, SENSOR_POLL_SLOW_SECONDS)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if listener is not None and sensor_poll_seconds > 0:
            task = asyncio.create_task(poll_sensors())
            log.info("Sensor poll every %.0fs (first read in %.0fs)",
                     sensor_poll_seconds, SENSOR_POLL_STARTUP_DELAY_SECONDS)
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="CTS600", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    walker = editor = None
    if listener is not None:
        # One lock: a walk and a settings change never press keys at once.
        op_lock = threading.Lock()
        panel_kwargs = dict(
            press=lambda key: listener.send_key(key, 0.3, rts_direction),
            bus_healthy=listener._bus_healthy,
            lock=op_lock,
        )
        walker = DataWalker(state, **panel_kwargs)
        editor = SettingsEditor(state, **panel_kwargs)

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    def _full_snapshot() -> dict:
        snapshot = state.snapshot()
        snapshot["version"] = __version__
        snapshot["control_enabled"] = listener is not None
        snapshot["control_keys"] = sorted(master.AID_CODES) if listener is not None else []
        return snapshot

    def _status() -> dict:
        status = state.status()
        status["version"] = __version__
        status["control_enabled"] = listener is not None
        return status

    @app.get("/api/state")
    async def get_state():
        return JSONResponse(_full_snapshot())

    @app.get("/api/status")
    async def get_status():
        return JSONResponse(_status())

    @app.post("/api/note")
    async def post_note(body: NoteBody):
        state.add_note(body.text)
        return JSONResponse({"ok": True})

    @app.post("/api/press")
    async def post_press(body: PressBody):
        if listener is None:
            return JSONResponse(
                {"error": "Control is disabled -- start with --enable-control to allow writes."},
                status_code=503,
            )
        if body.key not in master.AID_CODES:
            return JSONResponse(
                {"error": f"Unknown key {body.key!r}. Valid: {sorted(master.AID_CODES)}"},
                status_code=400,
            )
        if walker is not None and walker.running:
            return JSONResponse({"error": "a panel operation is in progress, try again when it finishes"}, status_code=409)
        # send_key() blocks for ~hold+0.3s (RTS toggling, sleeps) -- run it
        # off the event loop so it doesn't stall other requests/the
        # websocket while a press is in flight.
        try:
            await asyncio.to_thread(listener.send_key, body.key, body.hold, rts_direction)
        except BusBusyError as exc:
            return JSONResponse({"error": f"{body.key} not sent: {exc}"}, status_code=503)
        state.add_note(f"pressed {body.key} (via dashboard)")
        return JSONResponse({"ok": True})

    @app.post("/api/display_data/refresh")
    async def post_refresh():
        if walker is None:
            return JSONResponse(
                {"error": "Control is disabled -- start with --enable-control to refresh readings."},
                status_code=503,
            )
        if walker.running:
            return JSONResponse({"error": "a panel operation is already running"}, status_code=409)

        async def _run() -> None:
            try:
                result = await asyncio.to_thread(walker.run)
                state.add_note(f"NÄYTÄ DATA refresh {result['outcome']}: {result['message']}")
            except OperationBusyError:
                pass

        state.add_note("NÄYTÄ DATA refresh started")
        _background(_run())
        return JSONResponse({"ok": True}, status_code=202)

    @app.post("/api/display_data/cancel")
    async def post_refresh_cancel():
        if walker is None:
            return JSONResponse(
                {"error": "Control is disabled -- start with --enable-control to refresh readings."},
                status_code=503,
            )
        if not walker.cancel():
            return JSONResponse({"error": "no data refresh is running"}, status_code=409)
        state.add_note("NÄYTÄ DATA refresh cancel requested")
        return JSONResponse({"ok": True}, status_code=202)

    @app.post("/api/settings")
    async def post_settings(body: SettingsBody):
        if editor is None:
            return JSONResponse(
                {"error": "Control is disabled -- start with --enable-control to change settings."},
                status_code=503,
            )
        try:
            panel_settings.validate(mode=body.mode, setpoint=body.setpoint, fan=body.fan)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if editor.running:
            return JSONResponse({"error": "a panel operation is already running"}, status_code=409)

        async def _run() -> None:
            try:
                result = await asyncio.to_thread(
                    editor.apply, mode=body.mode, setpoint=body.setpoint, fan=body.fan,
                )
                state.add_note(f"settings change {result['outcome']}: {result['message']}")
            except OperationBusyError:
                pass

        state.add_note(f"settings change started: {body.model_dump(exclude_none=True)}")
        _background(_run())
        return JSONResponse({"ok": True}, status_code=202)

    read_lock = asyncio.Lock()
    last_read_at = [0.0]

    @app.post("/api/read")
    async def post_read(body: ReadBody):
        """One guarded read-only query, for exploring registers and bits.
        Serialized, and spaced at least READ_MIN_SPACING_SECONDS apart."""
        if listener is None:
            return JSONResponse({"error": "Control is disabled -- start with --enable-control to query the bus."},
                                status_code=503)
        limit = READ_MAX_BITS if body.function in (1, 2) else READ_MAX_WORDS
        if body.function not in (1, 2, 3, 4) or not (1 <= body.count <= limit) or not (0 <= body.start <= 0xFFFF):
            return JSONResponse({"error": f"function must be 1-4, start 0-65535, count 1-{limit}"}, status_code=400)
        async with read_lock:
            wait = last_read_at[0] + READ_MIN_SPACING_SECONDS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if not listener._bus_healthy():
                return JSONResponse({"error": "bus health degraded"}, status_code=503)
            try:
                frame, values = await asyncio.to_thread(
                    listener.read_block, body.function, body.start, body.count, rts_direction)
            except BusBusyError as exc:
                return JSONResponse({"error": f"not sent: {exc}"}, status_code=503)
            finally:
                last_read_at[0] = time.monotonic()
        if frame is None:
            return JSONResponse({"status": "timeout"})
        if values is None:
            return JSONResponse({"status": "exception", "code": frame.data[0] if frame.data else None,
                                 "raw": frame.raw.hex(" ")})
        return JSONResponse({"status": "ok", "values": values, "raw": frame.raw.hex(" ")})

    tasks: set[asyncio.Task] = set()

    def _background(coro) -> None:
        # Keep a reference, or the task can be garbage-collected mid-run.
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        loop = asyncio.get_event_loop()
        queue = state.subscribe(loop)
        try:
            # Send an initial full snapshot so the UI has something
            # to show before the first live event arrives.
            await websocket.send_json({"type": "snapshot", "data": _full_snapshot()})
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            state.unsubscribe(queue)

    @app.websocket("/ws/status")
    async def ws_status(websocket: WebSocket):
        await websocket.accept()
        loop = asyncio.get_event_loop()
        queue = state.subscribe(loop)
        sent = None
        try:
            while True:
                status = _status()
                comparable = {k: v for k, v in status.items() if k not in STATUS_VOLATILE}
                if comparable != sent:
                    await websocket.send_json(status)
                    sent = comparable
                # Wait for a relevant event, or send a heartbeat after a while.
                deadline = loop.time() + STATUS_HEARTBEAT_SECONDS
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), max(0.0, deadline - loop.time()))
                    except asyncio.TimeoutError:
                        sent = None
                        break
                    if event.get("type") in STATUS_EVENTS:
                        await asyncio.sleep(STATUS_COALESCE_SECONDS)
                        while not queue.empty():
                            queue.get_nowait()
                        break
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # the client went away mid-send
            log.debug("status websocket closed: %s", exc)
        finally:
            state.unsubscribe(queue)
            with contextlib.suppress(Exception):
                await websocket.close()

    return app
