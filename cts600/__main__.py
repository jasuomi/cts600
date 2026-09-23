"""Entry point: `python -m cts600 [--port /dev/ttyUSB0] [--http-port 8600]`

Starts the passive bus listener on a background thread and serves the
web dashboard with uvicorn on the main thread.
"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

import uvicorn

from . import __version__
from .config import Config
from .capture_log import CaptureLog
from .passive_listener import PassiveListener
from .state import DeviceState
from .webapp.server import create_app

log = logging.getLogger(__name__)


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
    p.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        type=str.upper,
        help="Log verbosity (default: INFO). DEBUG adds per-poll/per-press "
             "bus timing detail that's noisy for long-running use; INFO is "
             "state changes, warnings and errors only.",
    )
    p.add_argument(
        "--log-file", default=None,
        help="Log to this file instead of stdout, size-rotated so it can't "
             "grow without bound (see --log-max-bytes/--log-backup-count) -- "
             "meant for a 24/7 install (e.g. a Raspberry Pi's SD card) where "
             "shell-redirecting stdout to a plain file would otherwise grow "
             "forever. Without this, logs go to stdout as before.",
    )
    p.add_argument(
        "--log-max-bytes", type=int, default=2_000_000,
        help="Rotate --log-file once it reaches this size (default: 2000000).",
    )
    p.add_argument(
        "--log-backup-count", type=int, default=3,
        help="Keep this many rotated-out --log-file copies (default: 3, "
             "so about 4x --log-max-bytes total on disk).",
    )
    p.add_argument(
        "--capture-max-bytes", type=int, default=20_000_000,
        help="Rotate --capture-file once it reaches this size, 0 for never "
             "(default: 20000000). A --capture-file is meant for a short "
             "recording session (see README's Capture and analysis), but "
             "this bounds it in case one is left running long-term.",
    )
    p.add_argument(
        "--capture-backup-count", type=int, default=2,
        help="Keep this many rotated-out --capture-file copies (default: 2).",
    )
    args = p.parse_args(argv)
    return Config(
        port=args.port, mode=args.mode,
        http_host=args.http_host, http_port=args.http_port,
        capture_file=args.capture_file, readings_file=args.readings_file,
        sensors_file=args.sensors_file, sensor_poll_seconds=args.sensor_poll_seconds,
        auto_reanchor=not args.no_auto_reanchor,
        enable_control=args.enable_control, rts_direction=args.rts_direction,
        log_level=args.log_level, log_file=args.log_file,
        log_max_bytes=args.log_max_bytes, log_backup_count=args.log_backup_count,
        capture_max_bytes=args.capture_max_bytes, capture_backup_count=args.capture_backup_count,
    )


def _configure_logging(config: Config) -> None:
    """stdout by default (as before); with --log-file, a size-rotated file
    instead -- so a 24/7 install doesn't grow an unbounded log on disk (see
    --log-file's help). --log-level controls verbosity either way: DEBUG
    adds per-poll/per-press bus timing detail that's noisy long-term."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers = [RotatingFileHandler(
        config.log_file, maxBytes=config.log_max_bytes, backupCount=config.log_backup_count,
    )] if config.log_file else None
    logging.basicConfig(level=getattr(logging, config.log_level), format=fmt, handlers=handlers)


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv if argv is not None else sys.argv[1:])
    _configure_logging(config)
    log.info("cts600 v%s starting", __version__)

    state = DeviceState(readings_path=config.readings_file, sensors_path=config.sensors_file)
    state.mode = config.mode

    capture_log = None
    if config.capture_file:
        capture_log = CaptureLog(
            config.capture_file,
            max_bytes=config.capture_max_bytes, backup_count=config.capture_backup_count,
        )
        state.attach_capture_log(capture_log)
        log.info("Capturing to: %s", config.capture_file)

    listener = PassiveListener(state, port=config.port)
    try:
        listener.start()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    app = create_app(
        state,
        listener=listener if config.enable_control else None,
        rts_direction=config.rts_direction,
        sensor_poll_seconds=config.sensor_poll_seconds,
        auto_reanchor=config.auto_reanchor,
    )
    log.info("Dashboard: http://%s:%s/", config.http_host, config.http_port)
    if config.enable_control:
        log.info("Control buttons and data refresh ENABLED -- these write to the bus.")
    try:
        uvicorn.run(app, host=config.http_host, port=config.http_port, log_level="warning")
    finally:
        listener.stop()
        if capture_log:
            capture_log.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
