"""Constants for the Nilan CTS600 panel bridge."""

from datetime import timedelta

DOMAIN = "cts600"
DEFAULT_PORT = 8600

# Options
CONF_REFRESH_MINUTES = "refresh_minutes"  # 0 = never walk the menu automatically
CONF_MAX_AGE_MINUTES = "max_age_minutes"  # 0 = keep showing old readings
MIN_REFRESH_MINUTES = 15

# Fallback poll; normally everything arrives over the /ws/status push.
POLL_INTERVAL = timedelta(seconds=60)
RECONNECT_MIN_SECONDS = 5
RECONNECT_MAX_SECONDS = 300

# No frame from the bus for this long -> panel-derived entities unavailable.
BUS_SILENT_SECONDS = 30

EVENT_OPERATION_FINISHED = f"{DOMAIN}_operation_finished"

PANEL_KEYS = ("esc", "up", "down", "enter", "on", "off")
