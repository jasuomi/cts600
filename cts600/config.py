"""Prototype configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Config:
    port: str | None = None      # None => autodiscover
    mode: str = "passive"        # "passive" (shadow) or "active" (master) -- active not yet implemented
    http_host: str = "127.0.0.1"
    http_port: int = 8600
    capture_file: str | None = None   # None => no durable capture logging
    readings_file: str | None = "data/display_readings.json"  # last NÄYTÄ DATA readings
    sensors_file: str | None = "data/sensor_regs.json"  # reconstructed register temperatures
    sensor_poll_seconds: float = 10.0  # FC4 temperature block read interval (control only; 0 = off)
    auto_reanchor: bool = True        # short walk to LAUHDUT when the condenser loses tracking (control only)
    enable_control: bool = False      # opt-in: expose POST /api/press (writes to the bus)
    rts_direction: bool = False       # manual RTS toggling for writes -- see master.py's docstring
