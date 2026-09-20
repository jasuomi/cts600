# CTS600 dashboard

A web dashboard and Home Assistant integration for the Nilan CTS600 heat
pump controller, connected to its RS485 bus alongside the original
control panel. It shows the panel's display, the status LED and the
"NÄYTÄ DATA" ("SHOW DATA") readings, and (with `--enable-control`) can operate the
panel's six buttons itself.

This is the Python prototype/validation phase of a longer-term project to
replace the physical CTS600 panel with a custom ESP32-based one,
controllable locally and over the network. It grew out of an earlier
passive-only research prototype used to reverse-engineer the bus
protocol (a vendor-specific Modbus RTU variant) before any control code
was written.

**Requires the panel's menu language to be set to Finnish.** Every
screen this service recognises — the idle screen's mode name, the
NÄYTÄ DATA ("SHOW DATA") menu used by Update and the automatic condenser re-anchor,
and the settings editor's verification — is matched against Finnish
menu text (`display_data.py`). In another language, mode detection
returns "unknown" and the guarded key-press sequences (Update, settings
changes, re-anchor) stop immediately on an unrecognised screen rather
than press blindly. There's no language-switch support.

**Linux only.** Serial port autodiscovery (`transport.find_serial_port`)
looks for `/dev/serial/by-id/*`, `/dev/ttyUSB*`/`/dev/ttyACM*`, and
`/dev/ttyS*`, and the "no adapter found" error suggests `usermod -aG
dialout`. On another OS, pass `--port` explicitly with a path pyserial
accepts there (e.g. a Windows `COM` port) — untested, not the
supported path.

## Install

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```
python -m cts600 --port /dev/ttyS0 --http-host 0.0.0.0 --enable-control --rts-direction
```

Then open http://<host>:8600/.

Options:

- `--port DEVICE` — serial device (default: autodiscover, Linux only — see above)
- `--http-host HOST` (default `127.0.0.1`), `--http-port PORT` (default `8600`)
- `--enable-control` — show the buttons and the Update button, start the
  sensor poll and automatic condenser re-anchor (below), and accept
  `/api/settings` changes. Without it the dashboard only listens: none
  of these transmit anything.
- `--sensor-poll-seconds N` — how often the temperature registers are read
  (default 10; 0 turns the poll off)
- `--sensors-file PATH` — where the register temperatures are kept across
  restarts (default `data/sensor_regs.json`)
- `--no-auto-reanchor` — don't walk to the condenser screen automatically
  (see Live temperatures)
- `--rts-direction` — manual RS485 transmit-enable via RTS (needed on this
  installation's converter)
- `--readings-file PATH` — where the last readings are kept across
  restarts (default `data/display_readings.json`)
- `--capture-file PATH` — append every decoded event to a JSONL file
  (see Capture and analysis)

## Pages

**Panel** (entry page)

- Display: the panel's two LCD lines. On the idle screen the flags after
  the mode name are spelled out next to them: W = water heating,
  * = ventilation boost.
- Status LED: on while the compressor runs, blinks on an alarm.
- Buttons: On, Off, Up, Down, Enter, Esc. Each press is timed into the
  quiet gap between panel and controller exchanges.
- Näytä data ("Show data"): room (T15), outdoor (T1), tank top (T11), tank bottom (T12),
  heating supply (T14) and condenser (T5). A live card says "live"; one
  that needs an Update says so (see below). The fan levels, unit type and
  software versions were dropped: they have no register, so they could only
  ever repeat the last walk's value.

Readings update whenever one of these screens is on the display: when
someone walks the menu at the panel, uses the dashboard buttons, or
clicks **Update**.

**Live temperatures.** The six temperature cards show a live value to
0.1 °C, read every 10 s from the controller's input registers 0–15
(T1–T16, per Nilan's T1.33 register list). This controller sends only
the low byte of each value (hundredths of a °C), so a value is known only
to within steps of 2.56 °C until the panel has shown that sensor once. Each
panel reading "anchors" the matching register value, and polls track it from
there (`sensor_regs.py`). Until the first Update after install, the cards
show panel readings only. If a sensor jumps more than 1 °C between polls,
or the polls stop for a while, its tracking can no longer be trusted. The
card then shows the panel's last value with **press Update** under it, so
it's clear which cards need one; hovering says why.

The condenser can swing that fast, so the poll speeds up to every 2.5 s
while it changes faster than 0.2 °C per 10 s, and for 90 s after the
compressor switches on or off. A fast stretch lasts at most 10 minutes,
followed by 2 minutes at the normal rate. Fast polling stops as soon as the
condenser has lost tracking anyway. After a mode change it can climb from
2 to 33 °C in minutes, faster than any poll rate can follow.

**Automatic condenser re-anchor** (default with `--enable-control`; turn
off with `--no-auto-reanchor`). When the condenser value has lost tracking,
the service walks the panel to the LAUHDUT screen and back by itself: Up,
Enter, six Downs, two Escs, about 40 s. It shows up like an Update
("condenser re-anchor (automatic)"), with the same step checks, and only
when:

- the condenser has stopped swinging for 60 s, or has been uncertain for 10 minutes
- the panel is on the idle screen, nobody has pressed a key there for 2 minutes,
  and no other panel operation is running
- at least 10 minutes have passed since the last automatic attempt

**Update** walks the menu once (about a minute; each screen takes ~4 s to confirm): Up, Enter, Down
through the list, then Esc back to idle. It checks every screen before
the next press and stops, pressing nothing more, if:

- the panel isn't on the idle screen when it starts, or a key was
  pressed on the panel in the last 10 seconds
- a press lands on an unexpected screen, or doesn't change the screen
- a key is pressed on the physical panel during the walk
- bus health degrades

If it stops inside the menu, the panel returns to idle by itself after
about 2 min 45 s. Dashboard buttons are refused while a walk runs.

**Cancel** (shown while an update runs) stops it after the current step
and presses Esc back to the idle screen, checking each step as usual.

**Diagnostics**: frame and CRC counters, current screen, the raw
register and bit tables, the device info from the power-up handshake,
and the live frame log with a note box.

## Home Assistant

`homeassistant/custom_components/cts600` is a Home Assistant integration
for this service. Unlike the reference integration
(github.com/Produce2727/nilan-cts600-homeassistant), which replaces the
panel and opens the serial port from Home Assistant, it leaves the
physical panel in place: this service stays the only program on the bus,
and Home Assistant talks to it over the network. All bus safety (quiet-window
timing, bus-health checks, stopping when someone uses the panel) stays in
one place.

### Install

1. Run the service reachable from Home Assistant, with control enabled:
   `python -m cts600 --port /dev/ttyS0 --http-host 0.0.0.0 --enable-control --rts-direction`.
   The service has no authentication, so keep it on a trusted network.
2. Copy `homeassistant/custom_components/cts600` to Home Assistant's
   `config/custom_components/` and restart Home Assistant.
3. Settings → Devices & services → Add integration → "Nilan CTS600 panel
   bridge", enter the service's host and port (8600).

### Entities

- **Climate**: mode (off / auto / cool / heat), room setpoint (5–30 °C, a
  guessed guard rail, not a confirmed panel limit), fan speed
  1–4, all read from the idle screen. Current temperature is the live room
  (T15) register value, or the last panel reading when that isn't trusted. The action is idle / heating / cooling from the
  status LED and the mode (in auto, from a fresh "Operating state" reading).
- **Sensors**: room, outdoor, tank top, tank bottom, heating supply and
  condenser temperatures (live from the registers when the service trusts
  the value, otherwise the last panel reading; attributes `source` and
  `register_status`), operating state (with `read_at`), "Readings updated",
  and "Last panel operation" (diagnostic).
- **Binary sensors**: compressor (status LED), alarm (LED blinking), water
  heating and ventilation boost (idle-screen flags), bus problem and panel
  operation running (diagnostic).
- **Buttons**: Update readings, Cancel readings update (available while
  one runs); the six raw panel keys (disabled by
  default, since a raw press isn't checked).

The integration's icon is `custom_components/cts600/brand/icon.png` (and
`icon@2x.png`), which Home Assistant 2026.x serves directly for custom
integrations. The source is `homeassistant/brand_src/icon.svg`; re-render
with `brand_src/render_brand.py` (instructions in its docstring).

Updates are pushed over `/ws/status`, with a 60 s poll as fallback. When
a settings change or readings update finishes, the integration fires a
`cts600_operation_finished` event (`operation`, `outcome`, `message`) and
logs a warning if it stopped.

Options: **update readings every N minutes** (0 = never, else at least
15; changes what the panel shows for about a minute each time, skipped
while the panel is in use) and **show readings as unavailable after N
minutes** (0 = never).

### How settings are changed

`POST /api/settings` with any of `mode` (`off`, `on`, `auto`, `cool`,
`heat`), `setpoint` (5–30 °C — a loose guard, not the panel's actual
documented range, which is unknown) and `fan` (1–4) presses keys the way a person
would (`panel_settings.py`): Enter to select the field (once = setpoint,
twice = mode, three times = fan), Up/Down, Enter to confirm, Esc to leave
the selection, then it reads the idle screen again to check the value
stuck. The panel can't show which field is selected, so every Up/Down must
change exactly that field by one step; anything else is undone with Esc
(which discards an unconfirmed edit) and the change stops. The same guards
as Update apply: idle screen and no physical key in the last 10 s to
start, stop on a physical key press or bus-health risk, and only one
operation (Update, settings change) at a time. Each step takes a few
seconds, since both display lines must be re-sent.

## Capture and analysis

None of this is needed to run the dashboard. It's the toolchain used to
work out what the registers mean, kept because the map is far from
complete — most of `regmap.py` is still "unknown", which is a normal
state for it.

Record a session, annotating it as you go:

```
python -m cts600 --capture-file sessions/live.jsonl
```

Every decoded event is appended as one JSON object per line. While it
runs, type into the **add note** box on the Panel page (or `POST
/api/note`) at the moment you do something — "pressed UP", "set fan to
3". Dashboard button presses, Update walks and settings changes note
themselves automatically. Those notes are what make the capture
readable afterwards: they're timestamps you can anchor an analysis to.

Then work through the file with the scripts:

```
python scripts/correlate_notes.py sessions/live.jsonl
python scripts/dump_window.py    sessions/live.jsonl --note "pressed UP" --pad 90
python scripts/scan_reg_values.py sessions/live.jsonl 0x0100 --bits
```

`correlate_notes.py` diffs every address seen in the file from just
before each note to just after it — including addresses nothing has
labelled yet, which is the point. `dump_window.py` shows the full
sequence of intermediate values in a window rather than just the
endpoints, which matters when a change goes out and comes back.
`scan_reg_values.py` takes one address and shows every distinct value
and transition across the whole file. Each takes `--help`.

Captures stay local: `sessions/` and `*.jsonl` are gitignored.

## Tests

```
python -m unittest discover -s tests
```

Covers screen parsing, the readings store, the walk against a simulated
panel (normal run, not idle, physical key press, unexpected screen, bus
health), settings changes against a simulated panel with field editing
(lost and doubled presses, limits, mode order, off/on), register
temperature reconstruction/anchoring/tracking (rollover, gaps, swings,
persistence across restarts), the sensor poll pacing, and the status
endpoints (these need `httpx`; skipped without it).

Home Assistant integration tests (mocked service; Linux, Python 3.14):

```
docker run --rm -v "$PWD/homeassistant:/src:ro" python:3.14 sh /src/tests/run_in_docker.sh
```

## Layout

```
cts600/
  protocol.py          CRC, frame encode/decode, payload structures
  transport.py         serial port, frame boundaries, RTS-direction writes
  regmap.py            register/bit dictionary and research notes
  display_decoder.py   display registers -> text
  display_data.py      screen parsing (data screens, idle screen) + readings store
  sensor_regs.py       register temperatures: low-byte reconstruction, anchoring, tracking
  panel_ops.py         shared guards for key-press sequences
  data_walk.py         guarded Update walk through the menu
  panel_settings.py    guarded mode/setpoint/fan/on-off changes
  state.py             device state, bus health, pub/sub for the web layer
  passive_listener.py  read thread; send_key() and read_sensor_block() (opt-in)
  master.py            key-press encoding (also a CLI)
  planner.py           setpoint CLI (idle-screen parsing)
  regscan.py           FC3/FC4 research CLI (not used by the dashboard)
  capture_log.py       JSONL capture writer (--capture-file)
  webapp/server.py     FastAPI: /api/state, /api/status, /api/press, /api/settings,
                       /api/display_data/refresh, /ws, /ws/status
  webapp/static/       dashboard page, docs page
homeassistant/custom_components/cts600/   Home Assistant integration
scripts/               offline capture analysis (see Capture and analysis)
tests/                 offline tests
```

## Referenced documents

The source comments cite two third-party documents. Neither is
redistributed here, and neither is needed to run or modify this project
— everything they were used for is recorded in the code and its
comments.

- **The Lodam protocol document** — Lodam, "517200JE Nilan CTS 600
  Betjeningsprotokol". The panel-to-controller protocol: framing, CRC,
  the `typeSLAVEID` payload layout, and the "Lodam Modbus adresser"
  register table. Cited throughout `protocol.py`, `regmap.py` and
  `regscan.py`.
- **Nilan's T1.33 register list** — Nilan's CTS 600 Modbus register list
  for firmware T1.33. Names the T1–T16 temperature inputs and the
  `PWM_Supply`/`PWM_Return` fan registers. Cited in `regmap.py` and
  `sensor_regs.py`.

**On firmware versions.** This unit runs firmware 1.22, so the T1.33
list is a lead, not a match — a comment saying a register is named
something "per Nilan's T1.33 register list" means exactly that and
nothing more. Where a claim was verified on the wire, the comment says
so. The documents are wrong about this unit in at least one known place:
`parse_bit_block` in `protocol.py` deviates from Lodam's own worked
example because the observed bytes don't match it. Earlier and later
revisions of either document differ in naming and numbering; treat a
citation as something to verify on your own bus. Reports from other
firmware versions are welcome.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Nilan or Lodam. "Nilan" and "CTS 600"
are their respective owners' marks, used here only to say what hardware
this talks to.
