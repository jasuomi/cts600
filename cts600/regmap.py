"""The growing CTS600 register/bit data dictionary.

This is deliberately a plain data structure (not code) so it's easy
to extend as we correlate more panel button-presses / display screens
against bus traffic. Anything not listed here is simply shown as a
raw value in the UI -- unknown is a normal, expected state for most
of this map right now.

Ranges are documented in the Lodam protocol document ("Lodam Modbus
adresser") and
cross-checked against cts600_capture.txt.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RegInfo:
    label: str
    note: str = ""


# --- Output registers (Modbus ref. 4x), written by the master (panel) ------
OUTPUT_REGS: dict[int, RegInfo] = {
    0x0100: RegInfo(
        "Aktionskode master/betjening (_AID_xxx)",
        "One-hot bitmask of the key currently held on the panel (0x0000 "
        "= idle). Mapped against a capture with known button presses "
        "(sessions/keymap-20260913-212233.jsonl, cross-checked against "
        "every occurrence in the file, not just noted ones -- no "
        "exceptions): 0x0001=Esc, 0x0002=Up, 0x0004=Down, 0x0008=Enter, "
        "0x0010=Off, 0x0020=On. Unconfirmed: any bits above 0x0020 "
        "(e.g. a numeric keypad, if the physical panel has one).",
    ),
}

# NOTE on the overall control model (confirmed against an independent
# CTS600 Home Assistant integration -- frodef/nilan-cts600-homeassistant,
# same bus, VPL-15-class unit): standard Modbus read/write-register
# function codes (3/16) are accepted by the controller but do not
# actually control or query it. The only reliable way to operate the
# unit is panel emulation: report button state via FC65, and parse the
# scrolling display text from FC66 -- the same thing a human reading
# the physical panel would do. That project also switches the
# controller's menu language to English during initialization to make
# display parsing reliable, rather than matching against the
# device's default language (Finnish, in our capture). We should do
# the same once we get to the active-master phase.

# --- Input registers (Modbus ref. 3x), reported by the controller ----------
INPUT_REGS: dict[int, RegInfo] = {
    0x002A: RegInfo(
        "AD count fra betjening",
        "Free-running counter/heartbeat, not device state.",
    ),
}

# --- Display data (0x0200-0x0FFF) ------------------------------------------
DISPLAY_REG_START = 0x0200
DISPLAY_REG_END = 0x0FFF

# Like all display registers, 0x0200 and 0x020A hold whatever text is
# currently on that LCD line -- their meaning depends entirely on
# which screen is showing, same as every other display register. The
# two meanings below are what they show on the *idle/home* screen
# specifically (confirmed via sessions/keymap-20260913-212233.jsonl,
# the four "changing X" notes near the end of that file):
#
#   0x0200 (line 1, idle): operating mode name -- "AUTO", "VIILEN"
#       (cooling), or a word that decodes as "L.MP." (almost certainly
#       "LAMPO"/"heat" with Nordic letters -- see display_decoder.py's
#       _to_text note). Flags follow the mode name within the 8
#       visible characters (e.g. "AUTO  W "), per the panel operator
#       (2026-09-16): "W" = water heating active, "*" = ventilation
#       boost active. Not to be confused with a "*" in the 2 trailing
#       attribute bytes, which marks a selected field (display_decoder.py).
#   0x020A (line 2, idle): ">N< MM.C" where N = fan speed digit,
#       MM = room temperature setpoint in Celsius. In the water-heater
#       submenu the same register instead shows "T1 NN.C" -- almost
#       certainly the water tank sensor/setpoint, not yet confirmed
#       against a dedicated capture the way the idle screen is.
#
# UI model, confirmed against the real panel by its operator: this
# idle screen is read-only display, not an editable one. Up/Down don't
# adjust anything on it directly -- Enter cycles which field is
# selected/editable (temperature -> mode -> fan; confirmed 3 Enter
# presses from plain idle reaches fan), and only then do Up/Down
# adjust the selected field. Selection is shown on this same idle
# screen for temperature and fan (two different byte-level markers --
# see display_decoder.py's comment above _EXTENDED_CHARSET for the
# full write-up, including why mode has no visible marker at all and
# why a dashboard indicator for it isn't straightforward). Pressing
# Up/Down straight from this idle screen with no Enter first does
# something else entirely -- confirmed landing on a "Näytä data"
# ("show data") screen instead of touching the setpoint. See
# planner.py, which presses Enter before adjusting for exactly this
# reason.
#
# Also confirmed: Esc DISCARDS an edit instead of just backing out of
# it -- Enter is what confirms/saves. Watched live: the setpoint
# visibly changed on the panel during Up presses, then reverted back
# to its original value the moment Esc was pressed instead of Enter.
# planner.py's set_temperature() presses Enter to confirm, then Esc to
# back out of whatever field that confirm-and-advance action selects
# next (believed "mode") -- confirmed end-to-end durable (a 20->22°C
# run held after the script exited, didn't revert).
#
# "NÄYTÄ DATA" (Show Data) menu, walked 2026-09-14 with Up -> Enter ->
# repeated Down (the user's own button sequence). CORRECTED 2026-09-16
# (first live Update walk, then checked by the panel operator): one Up
# from idle shows "NÄYTÄ" / "DATA" directly; Enter opens the list.
# data_walk.py relies on this and stops if one Up lands anywhere else.
# (How the "HÄLYT" item seen on another visit is reached is not mapped.) Entering "DATA" lands on "NYKYTILA"
# (current status), whose line 2 shows the current operating mode
# (e.g. "VIILENN.." = VIILENNYS/cooling, matching the idle screen's
# 0x0200 mode name). Repeated Down from there scrolls a real sensor
# readout -- same two display registers (0x0200/0x020A) as everywhere
# else, just different content per screen, confirmed via
# recent_log/reg_block: no other display register address was ever
# touched. Confirmed labels (line 1 -> line 2 content):
#
#   HUONE     T15  NN°C   room temperature sensor
#   ULKOILMA  T1   NN°C   outdoor air temperature sensor
#   VESI-YLÄ  T11  NN°C   water tank, upper sensor
#   VESI-ALA  T12  NN°C   water tank, lower sensor
#   MENO      T14  NN°C   supply ("meno" = outgoing) air temperature
#   LAUHDUTIN T5   NN°C   condenser temperature sensor
#   TULOPUH   --          supply fan (reading not clearly captured --
#                         screen was mid-transition when read; revisit)
#   POISTPUH  TEHO N      extract fan power/speed level (integer, not °C)
#   SOFTA     1  N.NN     software version 1 -- matches the firmware SW
#                         version (1.22) REPORT_SLAVE_ID already reports
#   SOFTA     2  N.NN     software version 2 -- CONFIRMED via live cross-
#                         check to be the same value as OUTPUT_REGS/
#                         STANDARD_OUTPUT_REGS 0x0104 ("remote version"):
#                         both read 1.01 (101) at the same moment. This
#                         is the one register-to-menu-value correlation
#                         found so far.
#   TYYPPI    VP 18cek    unit type/model designation (static, end of
#                         list -- bounded, non-wrapping, like the mode
#                         field; Down does nothing past it)
#
# Follow-up (same day): found the actual A/D-to-Celsius decode formula
# on GitHub (frodef/nilan-cts600-homeassistant's nilanADToCelsius(),
# now regscan.ad_to_celsius()) and used it to test the other live
# "sensor-like" STANDARD_INPUT_REGS (0x0000, 0x0004, 0x0007, 0x000A,
# 0x000B) against these six readings across THREE independent
# correlated samples taken minutes apart:
#   - FC4 0x000D = outdoor air (T1/ULKOILMA) -- CONFIRMED, matched
#     within 0.3-0.5C on two independent samples where the real
#     reading had actually moved (14C -> 13C) and the register moved
#     the correct direction with it.
#   - FC4 0x000E = possibly room (T15/HUONE) -- plausible, not
#     confirmed to the same confidence (spread widened on later
#     samples).
#   - FC4 0x000B vs. MENO (supply) -- tried, matched within 0.04C once,
#     then missed by ~7C on a second sample. Retracted.
#   - FC4 0x0000/0x0004/0x0007/0x000A, and 0x000B again, vs. water
#     tank (VESI-YLÄ/VESI-ALA), condenser (LAUHDUTIN), and MENO --
#     RULED OUT, not just unmatched: these registers swing by up to
#     ~13C-equivalent across samples spanning a few minutes, while the
#     menu's own water-tank/condenser/supply readings barely moved
#     (well under 1C in the same windows). A register representing a
#     large-thermal-mass water tank or an idle condenser can't
#     physically swing that much that fast while the panel's own
#     reading of the same quantity doesn't move -- this rules the
#     mapping out rather than just failing to confirm it.
# Full numbers and reasoning in regscan.py's module docstring
# ("SECOND SESSION"/"THIRD SESSION" sections). What these five
# registers actually measure is still unidentified -- they're clearly
# too volatile to be the water tank/condenser/supply sensors, which
# rules out the natural hypothesis without replacing it with a better
# one yet. Finding the real water-tank/condenser registers, if they're
# exposed via FC3/FC4 at all, would need scanning addresses beyond the
# narrow ~20 already tried -- deliberately not attempted here, since
# broad unscanned-address sweeps are what caused the LINK ERR incident
# (see regscan.py's first FINDINGS section).
#
# Follow-up (same day, once more): did that wider scan, paced
# carefully (regscan.RTC_REG's neighborhood and the rest of the known
# clusters densely gap-filled, ~165 queries over ~20 minutes at a 2s
# settle -- see PassiveListener.scan_addresses() and regscan.py's
# "FOURTH SESSION" notes for the safety reasoning and full results).
# Completely uneventful: no LINK ERR, background-noise-level CRC
# errors only. Found one new live register (FC4 0x0005) right in the
# gaps of the already-known A/D block -- tried correlating it the same
# way, and it turned out to be the mirror image of the group above:
# RULED OUT for being too STATIC (exactly 160 across 3 samples where
# other menu readings visibly moved), rather than too volatile. Still
# no water-tank/condenser/MENO register found -- the swept region is
# now fairly thoroughly covered and mostly empty, so if those exist
# via FC3/FC4 at all, they're likely in the large untested middle
# (~0x0060-0x0100) or beyond 0x0120.
#
# CORRECTION, later the same day: the condenser (LAUHDUTIN/T5) WAS
# found after all -- FC4 0x000A. The negative verdict above used
# ad_to_celsius()'s (outdoor's) borrowed slope to estimate an
# equivalent °C swing, the same mistake that initially hid the room
# sensor (see the comment above regarding 0x000E). Corrected in
# regscan.py's TWELFTH SESSION notes with direct, formula-independent
# evidence: the user switched the unit from cooling to heating mode to
# force a real 33°C rise on T5, and 0x000A tracked it in the same
# direction across all 5 samples taken, no exceptions. See
# STANDARD_INPUT_REGS[0x000A] below for the full account, including
# why this ISN'T a simple linear relationship the way outdoor/room
# were. Water tank (confirmed genuinely absent from FC3/FC4 via the
# ELEVENTH SESSION's direct contradiction test) and MENO remain
# unlocated.

# --- Output bits (Modbus ref. 0x) ------------------------------------------
OUTPUT_BITS: dict[int, RegInfo] = {
    0x0000: RegInfo("Diskrete digitale udgange (relæer etc.)"),
    0x0100: RegInfo(
        "LED + blink bits",
        "2 bits per LED: bit0=on/off, bit1=blink.",
    ),
}

# --- Input bits (Modbus ref. 1x) --------------------------------------------
INPUT_BITS: dict[int, RegInfo] = {
    0x0000: RegInfo("Diskrete digitale indgange"),
    0x0100: RegInfo(
        "Mislabeled -- actually the panel's status LED (OUTPUT_BITS 0x0100)",
        "Per the panel operator (2026-09-16): the one LED is a STATUS LED -- "
        "on while the compressor runs, BLINKING on an alarm. The dashboard "
        "labels it just \"Status\". "
        "CONFIRMED, not just hypothesis: bit0 was watched live side by "
        "side with the physical panel's compressor indicator LED (via "
        "the dashboard's Compressor LED indicator, app.js::renderLed) "
        "and it tracks it exactly. This is genuinely OUTPUT_BITS "
        "0x0100 ('LED + blink bits: bit0=on/off, bit1=blink'), not "
        "INPUT_BITS/keyboard -- it's filed under INPUT_BITS here only "
        "because state.py::note_bit_block doesn't distinguish query/"
        "response direction on the bus and always applies the "
        "INPUT_BITS label to every bit_block event, regardless of "
        "which one it actually is. Also explains the ~98s delay "
        "observed between pressing On and this bit lighting up in "
        "sessions/keymap-20260913-212233.jsonl: compressor start "
        "delay/anti-short-cycle protection, not a button-press echo. "
        "bit1 ('blink', meant to indicate an alarm) has been 0 in "
        "every capture so far -- still unconfirmed, needs a capture "
        "during a real/test alarm condition. The real keyboard-bits "
        "register (if the controller even reports discrete keyboard "
        "state at all, as opposed to only the _AID_xxx action code) "
        "remains unidentified.",
    ),
}


# --- Standard Modbus registers (FC3/FC4), actively queried -----------------
#
# Everything above this point describes the Lodam-specific cyclic
# traffic (FC65/66) -- either passively observed (master.py/state.py)
# or actively written (master.py's key-press emulation). The two dicts
# below are a DIFFERENT namespace/access mechanism entirely: standard
# Modbus function codes 3 (Read Output/Holding Registers, "Modbus ref
# 4x") and 4 (Read Input Registers, "Modbus ref 3x"), actively queried
# read-only by regscan.py -- addresses documented by Lodam but
# never seen in passive captures, since the panel doesn't cyclically
# report them.
#
# Address numbers here can collide with OUTPUT_REGS/INPUT_REGS above
# (e.g. 0x0100 exists in both) without meaning the same thing -- they
# are different registers reached via a different function code, not
# the same register under two names. Keep them in separate dicts
# rather than merging, precisely to avoid that confusion.
#
# All entries below are confirmed live against real hardware during
# the 2026-09-14 exploration session -- see regscan.py's module
# docstring for the full findings write-up (methodology, the LINK ERR
# incident, and what's still unconfirmed).
STANDARD_OUTPUT_REGS: dict[int, RegInfo] = {  # Modbus ref 4x, via FC3
    0x0000: RegInfo(
        "Extract fan drive (PWM_Return per Nilan's T1.33 register list -- "
        "name not confirmed on 1.22), INVERTED",
        "**CONFIRMED against the fan level 2026-09-17 21:05-21:18** "
        "(fan_test.py / fan4_test.py on the host: set the level through "
        "the panel's own menu, read FC3 0-1 after each step). Higher fan "
        "level -> LOWER value, with 0x0001 alongside it: "
        "fan 2 = 148/160, fan 3 = 88/100, fan 4 = 16/16, unit off = 0/0. "
        "It ramps rather than jumping (fan 4: 72/84 then 16/16 fifteen "
        "seconds later; fan 3 after fan 4: 182/182 -> 88/100 within 5 s), "
        "so read it at least ~20 s after a change. Settled values are "
        "rock-steady (2.5 min at fan 4). The inversion plus 0-when-off "
        "suggests the stored value is the PWM's off time, not its on "
        "time. This CORRECTS the note below, which concluded it was not "
        "linked to the fan speed setting -- those early tests read the "
        "value too soon after a change, or with the unit off. "
        "Nilan's T1.33 register list names FC3 0/1 PWM_Return/PWM_Supply, i.e. "
        "0x0000 is the extract (return) fan; which of the two physical "
        "fans is which has not been checked independently here.\n\n"
        "Earlier, still-valid observations: "
        "CONFIRMED three-phase pattern (2026-09-14, EIGHTH/NINTH "
        "SESSIONS), a full off->on round trip: exactly 0 with the unit "
        "off (two independent samples); a transient non-88 value (6) "
        "for a few seconds right after pressing \"on\" -- plausible "
        "startup ramp, not noise; then exactly 88 again once settled "
        "(~2 minutes later), matching every ON-state sample this whole "
        "project has ever taken. NOT linked to temperature, fan SPEED "
        "SETTING, or mode (tested changing those directly early on, "
        "this value never moved from 88 while genuinely at steady "
        "state). Given the \"D/A, PWM etc.\" label, most likely an "
        "actuator duty-cycle output -- fan motor is the obvious "
        "candidate, since motors commonly ramp rather than jump "
        "straight to a steady-state duty cycle.",
    ),
    0x0001: RegInfo(
        "Supply fan drive (PWM_Supply per Nilan's T1.33 register list -- "
        "name not confirmed on 1.22), INVERTED",
        "Tracks the fan level exactly like 0x0000 -- see its entry for the "
        "2026-09-17 measurements (fan 2 = 160, fan 3 = 100, fan 4 = 16, "
        "off = 0). It sits 12 above 0x0000 at fan 2 and 3, and equals it "
        "at fan 4.\n\n"
        "Earlier, still-valid observations: "
        "Same confirmed three-phase pattern as 0x0000: 0 when off, a "
        "transient (206) right after power-on, settling to exactly 100 "
        "once steady state is reached. See regscan.py's EIGHTH/NINTH "
        "SESSION notes.",
    ),
    0x0100: RegInfo(
        "Aktionskode master/betjening (_AID_xxx)",
        "The same action code documented in OUTPUT_REGS above, also "
        "readable via FC3 -- confirmed 0 when idle.",
    ),
    0x0101: RegInfo(
        "Aktionskode slave/styring (_AID_xxx)",
        "The controller's own action code (as opposed to the panel's, "
        "at 0x0100). Confirmed readable, 0 when idle.",
    ),
    0x0102: RegInfo(
        "Display codepage (_CP_xxx fra DrvLCD modul)",
        "Confirmed, value 1.",
    ),
    0x0104: RegInfo(
        "Remote version",
        "Confirmed, value 101 (i.e. 1.01) -- distinct from the "
        "firmware SW version (1.22) reported by REPORT_SLAVE_ID. "
        "CROSS-CONFIRMED 2026-09-14 against the panel's own \"NÄYTÄ "
        "DATA\" -> \"NYKYTILA\" menu, which shows the same 1.01 as "
        "\"SOFTA 2\" -- see the DISPLAY_REG_START comment block above "
        "for the full menu walk-through.",
    ),
}

STANDARD_INPUT_REGS: dict[int, RegInfo] = {  # Modbus ref 3x, via FC4
    0x0000: RegInfo(
        "Måleværdi fra A/D kanal -- mirrors 0x0007 almost exactly",
        "Confirmed genuinely live -- drifts continuously between reads "
        "seconds apart, regardless of UI action. RULED OUT as water "
        "tank/condenser/MENO (central heating supply): swung ~13°C-"
        "equivalent across 3 samples spanning minutes while those "
        "menu readings barely moved -- see regscan.py's THIRD SESSION "
        "notes. CONFIRMED on/off-correlated (2026-09-14, EIGHTH "
        "SESSION): reads well outside its normal ON-state range when "
        "the unit is switched off, on both independent OFF samples "
        "taken. Also noticed: this register and 0x0007 track each "
        "other almost exactly in literally every sample ever taken "
        "(ON and OFF alike) -- likely the same value exposed twice, or "
        "two near-identical channels. RULED OUT more decisively as "
        "the water tank upper sensor (T11/VESI-YLÄ) in the ELEVENTH "
        "SESSION: moved 85->116 while T11 itself held flat at 59°C "
        "(a real, user-verified live reading) across the same window "
        "-- a direct contradiction, not just a failed correlation, and "
        "one that doesn't depend on any assumed formula. Physical "
        "quantity still unidentified.",
    ),
    0x0004: RegInfo(
        "(undocumented, part of the same A/D block as 0x0000)",
        "Confirmed live/drifting. RULED OUT as water tank (ELEVENTH "
        "SESSION) and condenser (TWELFTH SESSION) via direct sign-flip "
        "contradictions against real, actively-changing readings. NOT "
        "contradicted as MENO (THIRTEENTH SESSION), but that's much "
        "weaker evidence -- MENO happened to sit flat the whole test "
        "window (floor heating wasn't engaging), so this register "
        "coincidentally sitting still too doesn't confirm anything; a "
        "register that's sometimes flat and sometimes swings wildly "
        "for reasons unrelated to any confirmed real quantity isn't "
        "inspiring confidence it's a genuine sensor at all. Also shows "
        "a real but noisier on/off-correlated shift (EIGHTH SESSION): "
        "clearly higher when off than the entire ON-state range seen "
        "so far, though it keeps moving within that higher regime "
        "rather than settling on one fixed OFF value.",
    ),
    0x0005: RegInfo(
        "(found scanning the gaps in this A/D block, purpose unidentified)",
        "Found 2026-09-14 in a deliberate gap-fill scan (regscan.py's "
        "FOURTH SESSION). Reads exactly 160 across 3 independent "
        "samples spanning ~9 minutes, including one where VESI-YLÄ "
        "moved 58°C->59°C and LAUHDUTIN moved 0°C->1°C on the same "
        "menu -- RULED OUT as any of those (too static to track "
        "something that visibly moved). Most likely a static config/"
        "calibration value, similar to STANDARD_OUTPUT_REGS 0x0000/"
        "0x0001.",
    ),
    0x0007: RegInfo(
        "Måleværdi fra A/D kanal -- mirrors 0x0000 almost exactly",
        "Confirmed live/drifting. Same RULED OUT status and caveats as "
        "0x0000 -- see regscan.py's THIRD SESSION notes. Tracks "
        "0x0000's value almost exactly in every sample ever taken; "
        "also shows the same on/off correlation -- see EIGHTH SESSION "
        "notes. Same water-tank RULED OUT status as 0x0000 (they "
        "mirror, so this follows automatically) -- see ELEVENTH "
        "SESSION notes.",
    ),
    0x000A: RegInfo(
        "Formerly believed condenser temp sensor (T5 / LAUHDUTIN) -- RETIRED (SIXTEENTH SESSION, 2026-09-15); 0x000B also retired, no known FC4 register found",
        "**SUPERSEDED, SIXTEENTH SESSION (2026-09-15)**: re-enabling a "
        "live decode (with smoothing, to address the FIFTEENTH SESSION "
        "formula-fragility issue below) surfaced a reading that didn't "
        "match the panel at all. Investigated with 5 fresh (raw, "
        "real-LAUHDUTIN) pairs across a genuine 27°C forced swing (10°C "
        "-> 37°C, cooling then heating, same method as the TWELFTH "
        "SESSION below): this register moved DOWN as real T5 rose "
        "(192->173) -- the OPPOSITE sign from the positive slope that "
        "originally identified it. Not just outside its fitted range "
        "this time -- actively contradicting its own identifying "
        "relationship. 0x000B moved consistently the CORRECT direction "
        "across all 5 points instead (187->209) and is now the leading "
        "candidate -- see its own entry below and regscan.py's "
        "\"IDENTIFICATION REOPENED\" comment for the full data table. "
        "decode_input_value() has no branch for this address any more; "
        "LIVE_SENSOR_ADDRS in passive_listener.py no longer includes a "
        "\"T5\" entry at all (see below -- 0x000B failed too). "
        "Everything below is the now-superseded prior identification, "
        "kept for the historical record and because the METHODOLOGY "
        "lesson (a flawed borrowed-slope comparison can both wrongly "
        "rule IN and wrongly rule OUT a register) still applies.\n\n"
        "**Follow-up, same session**: asked whether this register might "
        "still track LAUHDUTIN with simply an INVERTED sign (today's "
        "5 points, taken alone, ARE internally monotonic -- just "
        "decreasing instead of increasing). Checked directly: overlaying "
        "today's data against the original TWELFTH SESSION calibration "
        "at comparable real temperatures shows they don't reconcile "
        "under either sign -- at ~1-14°C the original session read "
        "raw 99-111, while today's ~10-19°C read raw 179-192, a "
        "70-90 unit gap; at ~27-34°C the original read raw 133-167 vs. "
        "today's ~32-37°C at raw 173-178, still a consistent ~30-40 "
        "unit gap. Not just a sign flip -- the entire session's curve "
        "sits offset from the other's, at every comparable point. "
        "Something besides T5 itself (compressor duty-cycle phase, fan "
        "speed, time since mode change, ...) most likely also drives "
        "this register, or the two sessions' apparent correlations were "
        "each coincidental in their own way. Ruled out either way as a "
        "usable single-valued proxy for LAUHDUTIN.\n\n"
        "Went through the same wrong-conclusion detour as 0x000E "
        "(room) before landing here: the THIRD SESSION called this "
        "\"ruled out\" for water tank/condenser using an estimated "
        "°C-equivalent swing based on ad_to_celsius()'s (outdoor's) "
        "borrowed slope -- wrong, for the same reason that formula "
        "didn't apply to 0x000E either. CORRECTED in the TWELFTH "
        "SESSION with direct, formula-independent evidence: the user "
        "switched the unit from cooling to heating mode specifically "
        "to force a large, controllable rise on LAUHDUTIN's real "
        "reading (T5), and this register moved the SAME direction as "
        "T5 in all 5 real, time-separated samples collected, with zero "
        "exceptions, across a full 33°C range (raw 99->111->133->149->"
        "167 as T5 went 1°C->14°C->27°C->32°C->34°C). That's a cleaner, "
        "more consistent multi-point result than either 0x000D or "
        "0x000E got (both confirmed from only 2 points). NOT a simple "
        "linear relationship though -- the implied slope tapers off "
        "noticeably at higher temperatures (1.08 -> 0.59 -> 0.31 -> "
        "0.11 °C/raw across the four segments) rather than staying "
        "constant, consistent with a genuinely non-linear sensor curve "
        "(common for NTC thermistors). FOURTEENTH SESSION: fitted a "
        "logarithmic curve (regscan.condenser_ad_to_celsius()) via "
        "least squares across all 5 points -- best of several forms "
        "tried (linear, log, thermistor-B-style, power), but still "
        "only R²=0.94 with residuals up to ~3.7°C, well short of "
        "outdoor's/room's sub-1°C fits. Deliberately only applied "
        "within roughly the tested range (raw 90-180) -- this register "
        "has read anywhere from 17 to 250 across other sessions, and "
        "extrapolating a 2-parameter fit that far past its calibration "
        "range would produce confident-looking nonsense. Outside that "
        "range the dashboard shows the raw value only, no decoded °C. "
        "The IDENTIFICATION is well-supported; the FORMULA is a rough "
        "approximation, not a confirmed fit -- treat any single reading "
        "as ±4°C, not exact. **DECODE DISABLED, FIFTEENTH SESSION**: "
        "the user reported the dashboard's condenser reading changing "
        "\"too quick and too much\" to be believable after switching "
        "from heating back to cooling. Confirmed against the background "
        "poller's own log: the decoded value went 29.6°C -> 16.8°C -> "
        "4.0°C across two consecutive ~66s polls (~13°C/minute "
        "apparent). Not purely a formula illusion -- the raw register "
        "itself jumped -27 and -22 units in those same cycles (vs. "
        "typical -1 to -6), so the underlying signal genuinely "
        "accelerated -- but the formula is steepest in exactly that "
        "low-raw region, turning a real-but-modest acceleration into "
        "an implausible-looking swing. decode_input_value() no longer "
        "calls the formula for this address; the dashboard/Registers "
        "tab show the raw value only now. Function and constants kept "
        "in regscan.py for reference, not deleted -- the "
        "IDENTIFICATION still stands, only the FORMULA proved too "
        "fragile for live, unsmoothed display. Also confirmed on/off-correlated (EIGHTH SESSION): ON-state "
        "range across many samples was 177-228; both independent OFF "
        "samples landed at 33-34, a clean gap with no overlap -- "
        "consistent with a condenser that's naturally near-ambient "
        "when the compressor isn't running. RULED OUT as the water "
        "tank upper sensor (T11/VESI-YLÄ) specifically in the ELEVENTH "
        "SESSION: moved 23->41 while T11 held flat at 59°C across the "
        "same window -- correctly ruled out for THAT sensor, just not "
        "for the condenser.",
    ),
    0x000B: RegInfo(
        "RETIRED as condenser candidate (T5 / LAUHDUTIN) -- failed a live reality check, SIXTEENTH SESSION 2026-09-15",
        "**PROMOTED, SIXTEENTH SESSION (2026-09-15)**: after 0x000A's "
        "condenser identification broke down (see its entry above), "
        "collected 5 fresh (raw, real-LAUHDUTIN) pairs across a genuine "
        "27°C forced swing (10°C->19°C->32°C->35°C->37°C, switching "
        "cooling then heating): this register moved monotonically "
        "UP in every single segment (187->194->196->200->209, correct "
        "sign, ~0.81°C/raw average -- comparable magnitude to 0x000A's "
        "original low-end slope) and never broke sign once, unlike "
        "every other live FC4 candidate in this block (0x0000 looked "
        "promising for 4 points then flipped on the 5th; 0x0004/0x0007 "
        "flipped repeatedly; 0x000A moved backwards throughout). This "
        "session's dismissal below (TWELFTH SESSION) used the same "
        "flawed borrowed-slope method that also briefly mis-ruled-out "
        "0x000A before ITS correction -- carries little weight against "
        "this direct evidence. NOT yet a confirmed identification to "
        "0x000D's/0x000E's standard (needs independent replication, "
        "same caution this project applies everywhere), and NO formula "
        "fitted -- the per-segment implied slope is wildly inconsistent "
        "(0.78, 0.15, 1.33, 4.5 °C/raw across the 4 segments), the same "
        "\"coincidental drift\" red flag that's burned this project "
        "before. Shown as raw-only on the dashboard/Registers tab. See "
        "regscan.py's \"IDENTIFICATION REOPENED\" comment for the full "
        "data table.\n\n"
        "**RETIRED, same session, minutes later**: continued collecting "
        "points to check the inconsistent slope above. Two independent "
        "reads (not a single fluke) suddenly showed this register crash "
        "from 234 to 17-18 -- a ~217-unit jump, equivalent to hundreds "
        "of degrees at the established ~0.81 raw/°C rate, so obviously "
        "not a real temperature swing. Checked the real panel at the "
        "same moment: LAUHDUTIN read 39°C, near the HIGHEST it had been "
        "all session and still rising -- directly contradicting the "
        "register's crash toward its lowest-ever value. A register "
        "genuinely tracking a temperature that's staying high cannot "
        "crash to near-zero. Bus health was normal throughout (no CRC "
        "issues), ruling out a noise/corruption explanation. RETIRED as "
        "a condenser candidate on this direct evidence. Combined with "
        "0x000A's retirement above, NO known FC4 register in "
        "0x0000-0x000B reliably tracks LAUHDUTIN -- same conclusion "
        "already reached for the water tank/MENO sensors (see "
        "regscan.py's THIRD/ELEVENTH SESSION notes): these values most "
        "likely aren't exposed as standalone FC3/FC4 registers at all, "
        "at least not in this address range. passive_listener.py's "
        "LIVE_SENSOR_ADDRS no longer has a \"T5\" entry; the dashboard's "
        "front-page condenser card was removed rather than show a "
        "known-unreliable number. Display-text parsing (walking "
        "\"NÄYTÄ DATA\" -> LAUHDUTIN, the same technique planner.py "
        "already uses for the idle screen) is the only known way to get "
        "this value now, not register decoding.\n\n"
        "Everything below is prior history, from when this register was "
        "still just one of several undifferentiated live/drifting "
        "candidates.\n\n"
        "Confirmed live/drifting. Tried correlating against the "
        "\"NÄYTÄ DATA\" menu's MENO (supply air) reading via "
        "regscan.ad_to_celsius() -- matched within 0.04°C on one "
        "sample (raw 145 -> 21.96°C vs. displayed 22°C) but was off by "
        "~7°C on a second, independent sample minutes later (raw 185 "
        "-> 16.2°C vs. displayed 23°C). Did NOT replicate -- retracted "
        "as a confirmed mapping, most likely a coincidental first "
        "match. Also RULED OUT more broadly against water tank/"
        "condenser (too volatile -- see regscan.py's THIRD SESSION "
        "notes). Unlike 0x0000/0x0004/0x0007/0x000A, did NOT show a "
        "clear on/off correlation either (EIGHTH SESSION) -- both OFF "
        "samples landed inside the existing ON-state range. RULED OUT "
        "as the water tank upper sensor (T11/VESI-YLÄ) in the ELEVENTH "
        "SESSION too: moved 183->207 while T11 itself held flat at "
        "59°C (real, user-verified reading) across the same window -- "
        "a direct contradiction, formula-independent. Also RULED OUT "
        "as tracking the condenser's forced rise (TWELFTH SESSION) -- "
        "same-sign across segments but raw barely moved (14 units "
        "total across a 33°C real swing) with wildly inconsistent "
        "implied slopes; read as coincidental drift, not a real "
        "signal. NOT contradicted as MENO (THIRTEENTH SESSION) -- "
        "stayed nearly flat (49->50->56) while MENO also sat flat --  "
        "but given this register's prior track record here (one close "
        "MENO match that failed to replicate, ruled out for two other "
        "sensors already), treat this as weak, not confirming, "
        "evidence. Physical quantity still unidentified.",
    ),
    0x000D: RegInfo(
        "Outdoor air temperature sensor (T1 / ULKOILMA) -- DOUBTED 2026-09-15: panel 11C at both raw 222 and raw 119; decode disabled",
        "CONFIRMED 2026-09-14, cross-validated against the \"NÄYTÄ "
        "DATA\" -> \"NYKYTILA\" menu's ULKOILMA reading on two "
        "independent live samples taken minutes apart (the real "
        "reading had genuinely changed, 14°C -> 13°C, and this "
        "register moved with it, 199 -> 210 -- the correct direction "
        "for regscan.ad_to_celsius()'s negative slope). Both samples "
        "landed within 0.5°C of the displayed value using the same "
        "fitted device offset (~41.6-41.9). See regscan.py's "
        "ad_to_celsius() for the formula, sourced from "
        "github.com/frodef/nilan-cts600-homeassistant's "
        "nilanADToCelsius() (same protocol family) and re-calibrated "
        "for this specific unit's offset. This module previously "
        "claimed this register was \"PERFECTLY CONSTANT (222)\" -- "
        "that was wrong, based on too little early-session sampling; "
        "it's a live sensor like the others.",
    ),
    0x000E: RegInfo(
        "Room temperature sensor (T15 / HUONE) -- DOUBTED 2026-09-15: formula wrong, register wraps at 255; decode disabled",
        "Went through a confusing detour before landing here, worth "
        "keeping for the lesson: applying 0x000D's outdoor formula "
        "(ad_to_celsius()) to this register's raw values -- the only "
        "formula available at the time -- produced a nonsensical "
        "\"room temperature\" that appeared to rise continuously for "
        "40+ minutes regardless of the unit's power or compressor "
        "state (EIGHTH/NINTH SESSIONS), which correctly triggered "
        "skepticism that this register was room temperature at all. "
        "RESOLVED in the TENTH SESSION: while the user was physically "
        "navigating the panel's \"NÄYTÄ DATA\" -> \"NYKYTILA\" -> HUONE "
        "screen (confirmed live, not inferred), two register scans at "
        "the exact same moments gave direct ground-truth pairs (raw "
        "158 -> displayed 22°C, raw 212 -> displayed 23°C, a few "
        "minutes apart). Fitting a line through these two real points "
        "gives a COMPLETELY DIFFERENT transfer function than 0x000D's: "
        "positive slope (~0.0185°C/raw, higher raw = warmer) vs. "
        "outdoor's negative slope (~0.1375°C/raw) -- opposite sign, "
        "~7.4x smaller magnitude. Applying THIS correct formula "
        "retroactively to the whole session's earlier 0x000E readings "
        "turns the \"impossible rising temperature\" into a coherent "
        "story instead: room ~20.5°C while the unit was off, cooling "
        "to ~19.3-19.6°C as the compressor started -- consistent with "
        "both the idle screen's own setpoint-area reading (21°C->19°C "
        "over the same window) and the cooling mode the user confirmed "
        "was running. The earlier \"anomaly\" was entirely an artifact "
        "of using the wrong sensor's formula, not a real behavioral "
        "mystery, and not evidence this wasn't room temperature after "
        "all. Confidence note: 2 live, directly user-verified samples "
        "(as good as ground truth gets in this project) but still only "
        "2 points, which trivially fit any straight line -- not yet "
        "cross-validated to 0x000D's standard (a real, independently-"
        "observed directional change confirmed twice). A third live "
        "sample would close that gap; not collected yet. Also "
        "previously (wrongly) documented as \"PERFECTLY CONSTANT "
        "(227)\" in the very first session -- same correction as "
        "0x000D. See regscan.py's TENTH SESSION notes for the full "
        "account, including why this is a good example of why "
        "different A/D channels need independently-fitted formulas, "
        "not a shared one.",
    ),
    0x002A: RegInfo(
        "AD count fra betjening",
        "Same free-running heartbeat documented in INPUT_REGS above, "
        "also readable via FC4.",
    ),
    0x0100: RegInfo(
        "Tastekode 16 bits (bit 0-7=værdi, bit 8-15=funktionsflag)",
        "Confirmed readable, 0 when idle.",
    ),
    0x0102: RegInfo(
        "Reset årsag",
        "Confirmed, value 1 (per typeSLAVEID's enum: 1=Power-on reset).",
    ),
    0x0103: RegInfo(
        "Initialiseringsstatus",
        "Confirmed, value 1.",
    ),
    0x0110: RegInfo(
        "Real-time clock",
        "CONFIRMED & DECODED: 8 words starting here, BCD-encoded (each "
        "word's low byte holds two decimal digits): [sec, min, hour, "
        "weekday, day, month, year, century]. Verified twice, ~55s "
        "apart, against the real date/time (day=14, month=09, year=26 "
        "matched exactly, seconds/minutes advanced correctly between "
        "reads).",
    ),
}

# FC3 addresses roughly 0x0108-0x011C did not respond in either wide
# scan this session -- not retried, so treat as "probably unpopulated"
# rather than certain (a timeout under panel contention isn't always a
# genuinely invalid address; see regscan.py's incident note).


# FC4 input registers that hold live data but whose purpose is still
# unknown -- shown with a history graph on the dashboard's Unidentified
# tab. Notes summarise observed behaviour (2026-09-15 noise-signature
# tests), not a meaning.
OUTDOOR_REG = 0x000D  # shown raw on the dashboard's Outdoor card

UNIDENTIFIED_INPUT_REGS: dict[int, str] = {
    0x0000: "Smooth oscillation, period ~25-30 s. That's faster than the 60 s poll, so this graph is aliased. Identical to 0x0007.",
    0x0004: "Spans almost the full 0-255 range over an hour, with frequent large jumps (14 of 51 steps >100 at the 60 s poll) -- fast or wrapping, so the graph is likely aliased. Looked like slow drift over 3 minutes.",
    0x0005: "Always exactly 160. Reference channel or a stored setting? Possibly MENO -- untestable until floor heating runs.",
    0x0007: "Identical to 0x0000 in every atomic read -- the same value exposed twice.",
    0x000A: "Ramps smoothly across the full 0-249 range over tens of minutes, then wraps. Looked tight (53-58) in a 3-minute test that was too short to show the ramp.",
    0x000B: "Counter: rises +1/+2 every few seconds, never falls, wraps at 255. Too fast for the 60 s poll, so the graph looks random.",
    0x000D: "Was outdoor temperature (T1). The panel read 11 C both at raw 222 (morning) and raw 119 (evening), so no formula can fit it.",
    0x000E: "Was room temperature (T15). Wraps at 255 and its formula read ~4 C low, so the decode is disabled.",
}


def describe_reg(table: dict[int, RegInfo], addr: int) -> str:
    info = table.get(addr)
    if info:
        return info.label
    if DISPLAY_REG_START <= addr <= DISPLAY_REG_END:
        return "Display data"
    return "unknown"
