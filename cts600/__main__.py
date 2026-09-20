"""Entry point: `python -m cts600 [--port /dev/ttyUSB0] [--http-port 8600]`

Starts the passive bus listener on a background thread and serves the
web dashboard with uvicorn on the main thread.
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from . import __version__
from .config import Config
from .capture_log import CaptureLog
from .passive_listener import PassiveListener
from .state import DeviceState
from .webapp.server import create_app


def parse_args(argv: list[str]) -> Config:
    p = argparse.ArgumentParser(description=f"CTS600 dashboard v{__version__}")
    p.add_argument("--port", default=None, help="Serial device (default: autodiscover)")
    p.add_argument(
        "--mode",
        default="passive",
        choices=["passive"],
        help="Operating mode. Only 'passive' (shadow/listen-only) is implemented so far.",
    )
    p.add_argument("--http-host", default="127.0.0.1")
    p.add_argument("--http-port", type=int, default=8600)
    p.add_argument(
        "--capture-file",
        default=None,
        help="Append every decoded event (and any notes you add from the "
             "dashboard) to this JSONL file for later analysis. "
             "Without this, only the last ~500 events are kept, in memory.",
    )
    p.add_argument(
        "--readings-file", default="data/display_readings.json",
        help="Where the last NÄYTÄ DATA readings are kept across restarts "
             "(default: data/display_readings.json).",
    )
    p.add_argument(
        "--sensors-file", default="data/sensor_regs.json",
        help="Where the register temperatures (sensor_regs.py) are kept across restarts.",
    )
    p.add_argument(
        "--sensor-poll-seconds", type=float, default=10.0,
        help="With --enable-control, read the FC4 temperature block this often "
             "(default 10; 0 turns the poll off).",
    )
    p.add_argument(
        "--no-auto-reanchor", action="store_true",
        help="Don't walk the panel to the condenser screen by itself when the "
             "condenser register value has lost tracking (default: it does, "
             "with --enable-control).",
    )
    p.add_argument(
        "--enable-control", action="store_true",
        help="Expose POST /api/press and the dashboard's control buttons, "
             "which write button-press frames to the bus (see master.py). "
             "Without this flag the dashboard stays purely passive, "
             "matching its behavior before this option existed.",
    )
    p.add_argument(
        "--rts-direction", action="store_true",
        help="Toggle RTS by hand around each write for half-duplex RS485 "
             "direction control -- see master.py --help. Only matters "
             "together with --enable-control.",
    )
    args = p.parse_args(argv)
    return Config(
        port=args.port, mode=args.mode,
        http_host=args.http_host, http_port=args.http_port,
        capture_file=args.capture_file, readings_file=args.readings_file,
        sensors_file=args.sensors_file, sensor_poll_seconds=args.sensor_poll_seconds,
        auto_reanchor=not args.no_auto_reanchor,
        enable_control=args.enable_control, rts_direction=args.rts_direction,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = parse_args(argv if argv is not None else sys.argv[1:])

    state = DeviceState(readings_path=config.readings_file, sensors_path=config.sensors_file)
    state.mode = config.mode

    capture_log = None
    if config.capture_file:
        capture_log = CaptureLog(config.capture_file)
        state.attach_capture_log(capture_log)
        print(f"Capturing to: {config.capture_file}")

    listener = PassiveListener(state, port=config.port)
    try:
        listener.start()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    app = create_app(
        state,
        listener=listener if config.enable_control else None,
        rts_direction=config.rts_direction,
        sensor_poll_seconds=config.sensor_poll_seconds,
        auto_reanchor=config.auto_reanchor,
    )
    print(f"Dashboard: http://{config.http_host}:{config.http_port}/")
    if config.enable_control:
        print("Control buttons and data refresh ENABLED -- these write to the bus.")
    try:
        uvicorn.run(app, host=config.http_host, port=config.http_port, log_level="warning")
    finally:
        listener.stop()
        if capture_log:
            capture_log.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
