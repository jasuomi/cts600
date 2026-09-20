"""Read-only exploration of standard Modbus registers (FC1-4).

Everything else in this codebase has only ever passively observed the
Lodam-specific cyclic traffic (FC65/66) or actively written it
(master.py). This module instead actively QUERIES registers the panel
doesn't cyclically report -- confirmed via the Lodam document's own
"Lodam Modbus adresser" table and its "_FC_READ_OUTPUT_REGS" row
("ved initialisering / efter behov" -- queried at startup or on-demand
only) and independently confirmed empirically: none of the documented-
but-unmapped addresses (0x0000, 0x0101, 0x0102, 0x0103, 0x0104, 0x0110)
ever appeared in two separate real capture sessions.

Read-only in the sense that FC1-4 are Modbus *read* function codes --
nothing here ever calls FC5/6/15/16 (write). It still has to transmit
query frames to get a response, the same way master.py does (same RTS-
direction requirement on this hardware), which is why this needs
--rts-direction too. This does not control the unit in any way; it
only asks it questions.

FINDINGS (2026-09-14 exploration session):

- Both FC3 and FC4 work reliably for reads -- every address in
  KNOWN_OUTPUT_REGS/KNOWN_INPUT_REGS responded normally on the first
  real scan. This contradicts the "doesn't query" half of the
  frodef/nilan-cts600-homeassistant finding quoted in regmap.py -- that
  finding is about FC3/16 *writes* not achieving control, not about
  reads being meaningless. Reads work fine.
- Real-time clock found and decoded: FC4 0x0110, 8 words, BCD (each
  word's low byte is two decimal digits): [sec, min, hour, weekday,
  day, month, year, century]. Confirmed twice, ~55s apart, against the
  real date (day=14, month=09, year=26 matched exactly) with seconds/
  minutes correctly advancing between reads.
- FC3 0x0000=88, 0x0001=100 ("Digitale udgangsregistre, D/A/PWM"):
  present and stable, but confirmed NOT linked to temperature, fan
  speed, or mode -- tested changing each one and re-reading with
  retries=3 (fully reliable, 0 timeouts); these two values never
  moved. Likely a fixed config/calibration pair, or tied to some other
  subsystem not tested (water heater, on/off power state).
- FC3 0x0102 (display codepage) = 1, FC3 0x0104 (remote version) = 101
  -- both stable. Remote version (1.01) is distinct from the firmware
  SW version (1.22) reported by REPORT_SLAVE_ID -- a different
  version number for a different thing.
- FC4 sensor-like registers (0x0000, 0x0004, 0x0007, 0x000A, 0x000B):
  confirmed genuinely live -- values drift continuously between any
  two reads seconds apart, REGARDLESS of which UI action (or none) ran
  in between, including "before" vs "after reverting" a change. This
  is consistent with real analog sensor noise/slow drift (matches the
  doc's "Måleværdier fra A/D kanaler"), not an immediate actuator
  response -- a setpoint change wouldn't move a physical sensor
  reading within seconds anyway (thermal inertia), so this correlation
  test was never going to show a link even if these are the "real"
  temperature sensors. Which physical quantity each one is (room temp?
  outdoor? water tank?) was NOT established -- would need comparison
  against a known physical reference reading.
- FC4 0x000D, 0x000E: stayed perfectly constant (222, 227) across
  every test in this session. Purpose unknown -- possibly an unused
  channel or a fixed reference value. **CORRECTED below (2026-09-14,
  second session)** -- this was wrong, just too little sampling.
- FC3 addresses roughly 0x0108-0x011C did not respond in either wide
  scan (not retried, so treat as "probably unpopulated" rather than
  certain -- see the incident note about false timeouts under panel
  contention).
- INCIDENT: rapid, high-volume querying (the correlation test scripts)
  disrupted the panel<->controller sync badly enough to trigger the
  protocol's documented error-recovery state ("LINK ERR" on the
  physical display, confirmed by the operator) -- and it did NOT
  self-clear even after several minutes of leaving the bus alone.
  Recovered only after a full power-cycle of the controller. A
  same-session theory that our own RTS pin was left stuck asserted
  (jamming the bus) was investigated and NOT conclusively confirmed --
  ser.rts's value right after opening a fresh connection turned out to
  be unreliable as a diagnostic (uncertain whether it reflects real
  hardware state or just a software-side default), so this remains
  somewhat unexplained. Lesson applied: scan() now supports retries
  (query_registers() is a single attempt with no retry logic of its
  own), and any future heavy scanning should keep --settle generous
  and avoid long unattended bursts against a live installation.

SECOND SESSION (2026-09-14, later the same day) -- correlating against
the panel's own "NÄYTÄ DATA" (Show Data) menu, which shows human-
readable labeled sensor values (see regmap.py's comment above
DISPLAY_REG_START for the full menu walk-through and labels):

- The earlier "0x000D/0x000E perfectly constant" finding was WRONG --
  a fresh scan showed both very much alive (199/210 and 115/130 across
  two samples minutes apart). That "constant" reading was an artifact
  of too little sampling in the first session, not a real property of
  either register. Lesson: don't conclude "constant" from a handful of
  samples taken close together in time.
- Found the actual decode formula on GitHub
  (github.com/frodef/nilan-cts600-homeassistant, same protocol
  family): raw A/D value -> Celsius via a linear fit,
  `T = x - raw * (22/160)`, calibrated in that project as two points
  (raw=168 -> 34°C, raw=328 -> 12°C, giving slope -0.1375 -- see
  ad_to_celsius() below), with a device-specific offset `x` (that
  project's default is 56.25, NOT applicable to this unit as-is).
- **CONFIRMED (high confidence): FC4 0x000D is the outdoor air
  temperature sensor (T1 / ULKOILMA)**. Fitted this unit's own offset
  from one sample, then verified against a SECOND, independent live
  sample taken minutes later where the real outdoor temp had measurably
  changed (14°C -> 13°C on the panel) -- the register moved the
  correct direction (199 -> 210) and both samples landed within 0.5°C
  of the panel's own reading using one consistent fitted offset
  (~41.6-41.9). Two independent samples agreeing this tightly, with
  the raw value changing in the physically-correct direction between
  them, is strong evidence -- not a coincidence.
- **PLAUSIBLE, not yet confirmed to the same confidence: FC4 0x000E
  may be the room temperature sensor (T15 / HUONE)**. Matched tightly
  on the first sample (raw 115, computed ~26.1°C vs. displayed 24°C --
  off by ~2°C) but the spread was wider on the second sample. Room
  temperature changes slowly, so a couple of degrees of spread across
  a few minutes is at least as consistent with sensor
  noise/self-heating as with a wrong register -- genuinely uncertain,
  needs another independent sample (ideally hours apart, when the room
  temp has actually moved more) to be sure either way.
- **TRIED AND RETRACTED: FC4 0x000B vs. MENO (supply air)**. Matched
  within 0.04°C on the first sample (raw 145 -> 21.96°C vs. displayed
  22°C -- looked like a great match) but was off by ~7°C on the second,
  independent sample (raw 185 -> 16.2°C vs. displayed 23°C). Did NOT
  replicate. Recorded here explicitly as a caution: a single close
  match, even a very close one, isn't confirmation on its own --
  always check a second independent sample before believing a
  correlation. This register's real meaning is still unidentified.
- Water tank (VESI-YLÄ/VESI-ALA, 58°C/50°C) and condenser (LAUHDUTIN,
  0°C) readings from the same menu were also tried against the
  remaining live registers (0x0000, 0x0004, 0x0007, 0x000A -- all
  still genuinely live/drifting) with no fit found under this same
  formula/offset. Plausible explanation: different sensor types (e.g.
  water tank NTC vs. air NTC) commonly use different resistor
  networks/calibration on real HVAC hardware, so a shared offset
  across all channels was never a safe assumption to begin with --
  each channel may need its own offset fitted separately, which needs
  a reliable simultaneous (register, real-temp) pair for each one.

THIRD SESSION (2026-09-14, same day again) -- asked specifically to
extend the water-tank/condenser/central-heating-supply (MENO)
correlation. Collected a THIRD independent correlated sample (walk
"NÄYTÄ DATA" + immediate scan, same method as before) and combined it
with the two samples from the second session for a 3-way comparison:

- **RULED OUT (not just "no fit found" -- actively contradicted by the
  data): none of 0x0000, 0x0004, 0x0007, 0x000A, or 0x000B can be
  VESI-YLÄ, VESI-ALA, LAUHDUTIN, or MENO.** Across the 3 samples
  (spanning several minutes, with panel button presses and scans in
  between), these registers' raw values swung by up to 96 raw units
  (0x0000: 60->48->144) -- roughly a 13°C-equivalent swing under
  ad_to_celsius()'s slope. But the menu's own water-tank/condenser/
  supply readings barely moved at all in the same windows: VESI-YLÄ
  stayed exactly 58°C every time, VESI-ALA moved at most 1°C (50->51),
  MENO moved at most 1°C (22->23), LAUHDUTIN stayed exactly 0°C every
  time. A register that's supposed to represent a large-thermal-mass
  water tank or an idle condenser physically cannot swing 13°C in a
  couple of minutes while the panel's own reading of that exact
  quantity doesn't move -- this isn't an unlucky miss, it's a
  contradiction. (Smallest swings were 0x000A at ~2.3°C-equivalent and
  0x0004 at ~2.5°C-equivalent -- still bigger than anything these four
  readings actually did.)
  **CORRECTION (TWELFTH SESSION): the "0x000A ruled out" part of this
  verdict was wrong**, for the same reason the room-sensor detour was
  wrong (TENTH SESSION) -- this used ad_to_celsius()'s borrowed slope
  to estimate an "equivalent °C" swing, but 0x000A's real slope turns
  out to be much steeper (and non-constant) than that borrowed value.
  Direct, formula-independent evidence (five real samples, all moving
  the same direction as LAUHDUTIN/T5 across a 33°C range) now strongly
  supports 0x000A AS the condenser sensor. See KNOWN_INPUT_REGS above
  and the TWELFTH SESSION notes below for the corrected verdict --
  0x0000/0x0004/0x0007/0x000B remain ruled out.
- This actually makes 0x000D's confirmed outdoor-sensor mapping more
  plausible in hindsight, not less: outdoor air is the one reading on
  this menu that's genuinely supposed to be volatile, so a "live,
  swings noticeably" register fitting it and NOT fitting the
  thermally-stable water tank/condenser readings is the expected
  pattern, not a coincidence.
- What 0x0000/0x0004/0x0007/0x000A/0x000B actually measure remains
  unidentified. Given how much they move, candidates worth considering
  later: a PWM/duty-cycle or valve-position value, an internal
  control-loop variable, or a genuinely different (faster-reacting)
  physical sensor not shown anywhere on the "NÄYTÄ DATA" menu at all.
  Finding the real water-tank/condenser/central-heating registers, if
  they're exposed via FC3/FC4 at all, would need a wider address scan
  than the ~20 addresses tried so far (only a narrow, doc-cued
  slice of the full 0x0000-0x0110+ space has ever actually been
  queried) -- deliberately NOT attempted automatically here, since a
  broad unscanned-address sweep is exactly what triggered the earlier
  LINK ERR incident (see the first FINDINGS section above). Worth
  doing as its own careful, deliberate session if wanted.

FOURTH SESSION (2026-09-14, same day again) -- explicitly asked to do
that wider scan, "keep pace low to avoid LINK ERR". Added a real,
reusable capability for this rather than a one-off script:
PassiveListener.scan_addresses() (arbitrary address range, reused
connection/reader like scan_standard_registers()) + a dedicated
POST /api/wide_scan endpoint (--enable-control only, capped at 80
addresses/call, settle floor of 1.0s enforced server-side). Ran it
with settle=2.0s -- chosen from the Lodam document's stated behavior:
the panel repeats its init sequence (showing "LINK ERR") after 10
CONSECUTIVE comm failures, and its own cyclic exchange runs about
once a second, so spacing our queries at roughly double that gives it
a clean window between every one of ours.

Densely filled the gaps around every already-known cluster (FC3 and
FC4, 0x0000-0x0020 and 0x0100-0x0120), then extended FC4 a bit further
(0x0021-0x0060). ~165 queries total across ~20 minutes. Result:
completely uneventful -- CRC error count on node 3 crept from 2 to 7
over the whole campaign (background-noise rate, same as normal idle
operation), frame count climbed steadily throughout with no gaps, the
idle display never moved, and no LINK ERR. Confirms the pacing theory
in practice, not just on paper.

Findings from the sweep itself:
- Almost the entire swept space is genuinely unpopulated (reads back
  as a clean 0, not a timeout/exception -- these addresses ARE
  implemented, they just hold zero). This also corrects an earlier
  note that FC3 0x0108-0x011C "did not respond" -- with a properly
  generous settle they respond fine, just with value 0. Not
  "unpopulated", just currently zero.
- FC4 0x0110-0x0117 is exactly the 8-word RTC block as already
  documented -- 0x0118 onward reads 0, confirming the RTC's upper
  boundary precisely.
- One genuine new find: **FC4 0x0005 = 160**, live and nonzero, in a
  gap right inside the already-known "A/D kanaler" block. Tried
  correlating it against the "NÄYTÄ DATA" menu the same way as
  0x000D/0x000E -- across THREE independent samples spanning about 9
  minutes (including one where VESI-YLÄ moved 58->59°C and LAUHDUTIN
  moved 0->1°C), **0x0005 read exactly 160 every single time, with
  zero drift.** RULED OUT as any of the dynamic sensor readings on
  that menu for the mirror-image reason of the 0x0000/0x0004/0x0007/
  0x000A/0x000B group above: those were too volatile to match a
  stable reading, this one is too static to match anything that
  visibly moved on the panel in the same window. Most likely a static
  config/calibration value, like 0x0000/0x0001 in STANDARD_OUTPUT_REGS.
  Purpose still unidentified.
- Net result: the water-tank/condenser/central-heating-supply
  registers were NOT found in this sweep. The swept region (roughly
  0x0000-0x0060 and 0x0100-0x0120 for both function codes) is now
  fairly thoroughly covered and mostly empty; if these sensors are
  exposed via FC3/FC4 at all, they're most likely somewhere in the
  large untested middle (~0x0060-0x0100) or beyond 0x0120 -- a
  reasonable next step with the new scan_addresses()/wide_scan
  capability, at the same careful pace, if wanted.

FIFTH SESSION (2026-09-14, same day, continuing) -- asked to scan
exactly that untested middle, 0x0060-0x0100, for both function codes.
Split into 8 batches (the /api/wide_scan endpoint caps at 80
addresses/call) of ~40 addresses each, same 2.0s settle, with a short
breather and a health check between every batch.

- **Result: completely empty.** Every single address in 0x0060-0x0100
  for both FC3 and FC4 -- 322 queries total -- read back either a
  clean 0 or one of a small handful of harmless timeouts (no
  exceptions at all). Not one nonzero value anywhere in this range.
- Bus health across the whole campaign: node 3's CRC error count moved
  from 0 to 1 (background-noise rate), one single stray one-off
  garbled frame on an unrelated node (same harmless pattern as always
  -- one bad byte sequence, correctly discarded, never repeated), the
  idle display never moved, no LINK ERR. The 2.0s pacing continues to
  hold up under real, sustained use across two separate wide-scan
  sessions now, not just the first one.
- **Combined with the fourth session, essentially all of
  0x0000-0x0100 has now been swept for both function codes** (the
  narrow ~20 originally-documented addresses, the gap-fills around
  them, and this middle region). Nothing beyond the already-known
  small A/D block near 0x0000 (0x0000, 0x0004, 0x0005, 0x0007, 0x000A,
  0x000B, 0x000D, 0x000E) ever returns anything but zero.
- **Working conclusion, not yet certain: the water-tank/condenser/
  central-heating-supply values probably aren't exposed as standalone
  FC3/FC4 registers at all**, at least not below 0x0100. They're only
  known to exist as formatted text on the "NÄYTÄ DATA" -> "NYKYTILA"
  display screen (see regmap.py's DISPLAY_REG_START comment). It's
  entirely possible those values are computed/formatted for display
  only, from internal firmware state that was never mapped to a
  standard register in the first place -- in which case the only
  reliable way to get them programmatically would be parsing the
  display text while navigating to that screen (the same technique
  planner.py already uses for temperature/mode/fan on the idle
  screen), not register decoding. Still unconfirmed either way; the
  only way to fully rule out FC3/FC4 entirely would be sweeping
  0x0100-0xFFFF too, which is a much bigger, lower-probability
  undertaking (nothing found so far suggests registers are scattered
  far from node 3's small active cluster) -- not attempted here.

SIXTH SESSION (2026-09-14, same day, continuing) -- filled the one
remaining cheap gap: 0x0120-0x01FF (the space between the fifth
session's swept region and the documented display-data range,
0x0200-0x0FFF, which is already characterized via passive FC65/66
traffic and wasn't re-scanned here). 8 batches of ~55 addresses each
(the /api/wide_scan endpoint caps at 80/call), same 2.0s settle,
health-checked between every batch, one client-side tool timeout on
the very first FC4 batch (its own 120s cap was just barely too tight
for ~56 addresses at this pace -- confirmed via the server log that
the scan itself completed normally and every address got a real
response; not a bus issue, just my own harness's timeout).

- **Result: completely empty**, same as the fifth session -- 448
  queries across both function codes, every one either a clean 0 or a
  harmless timeout, zero exceptions, zero nonzero values.
- Bus health across the whole campaign: node 3's CRC error count moved
  from 3 to 9 (still background-noise rate, ~0.09% of 9700 frames),
  a few more stray one-off garbled frames on unrelated node addresses
  (same harmless single-bad-byte-sequence pattern as every previous
  session, none repeating), idle display never moved, no LINK ERR.
- **0x0000-0x0200 is now essentially fully mapped for both function
  codes** across four separate sessions (this one, the fourth, and the
  fifth), and the small A/D cluster near 0x0000 plus the RTC block
  near 0x0110 remain the only populated addresses found anywhere in
  that entire range. This meaningfully strengthens the fifth session's
  working conclusion that the water-tank/condenser/central-heating
  values aren't exposed as FC3/FC4 registers below the display-data
  range at all -- display-text parsing (like planner.py already does)
  is the more promising path forward, not further register scanning.

SEVENTH SESSION (2026-09-14, same day, continuing) -- the user pointed
at github.com/veista/nilan, a Home Assistant integration for the
Nilan CTS602 (a newer controller in the same product family, NOT
CTS600 -- its own README explicitly limits support to CTS602 and
asks for help with anything else). Its registers.py has a large,
modern, fully-documented decimal Modbus register map -- genuinely
useful as a cross-check even though it's a different generation:

- **Confirms the T-number sensor convention is shared across Nilan's
  whole product line, even though the register ADDRESSES aren't.**
  CTS602's input_t15_room=215, input_t11_top=211, input_t12_bottom=212,
  input_t14_supply=214, and input_t5_cond=205 use the exact same T15/
  T11/T12/T14/T5 labels this project independently read straight off
  the CTS600 panel's own "NÄYTÄ DATA" screen (HUONE/T15, VESI-YLÄ/T11,
  VESI-ALA/T12, MENO/T14, LAUHDUTIN/T5) -- strong, independent
  validation that those menu labels were read/transcribed correctly.
  (Outdoor is the one exception: CTS602 calls it T8, CTS600's panel
  calls it T1 -- sensor numbering isn't perfectly identical across
  generations either.)
- **The actual register addresses don't transfer.** All of CTS602's
  low temperature-sensor addresses (205, 208, 211, 212, 214, 215) fall
  inside the 0x00B0-0x00D7 range this project already swept to all-
  zero in the fifth session. Tried the 6 genuinely untested higher
  "hps_" (heat-pump-specific) candidates too -- 1900, 1902, 1904, 1905,
  6167, 6169 decimal (0x076C, 0x076E, 0x0770, 0x0771, 0x1817, 0x1819)
  -- all read a clean 0, no exceptions, no bus disruption (6 queries,
  essentially free). Confirms CTS602 runs a fundamentally different,
  much larger, modern Modbus register scheme -- not a superset or
  extension of CTS600's minimal Lodam-era layout that any address
  reuse could exploit. Not a useful register-hunting lead after all,
  but a genuine confirmation that the CTS600's official doc's own
  framing ("betjeningsenhed skal kun implementere funktionskode 3, 16,
  17, 65 og 66" -- a deliberately minimal panel-side implementation)
  reflects a real, older, much smaller design, not just an
  under-documented corner of a bigger one.

EIGHTH SESSION (2026-09-14, same day, continuing) -- the physical unit
was switched off (confirmed via the idle display, "AUTO" -> "OFF", and
the compressor LED bit 0x0100 going [1,0] -> [0,0]). Asked to focus on
the "drifting" registers specifically while off, since a real on/off
transition is a much stronger natural experiment than anything tried
so far. Scanned twice, ~3.5 minutes apart, and compared against every
ON-state value collected across all earlier sessions:

- **CONFIRMED, high confidence: STANDARD_OUTPUT_REGS 0x0000 and 0x0001
  are tied to the unit's on/off power state.** Both were rock-stable
  at 88 and 100 respectively across every single ON-state test this
  whole project (many samples, multiple sessions, explicitly "NOT
  linked to temp/fan/mode" per earlier testing) -- and both read
  exactly 0 on BOTH independent OFF samples. Given the label
  ("Digitale udgangsregistre (D/A, PWM etc.)"), the natural
  explanation is a PWM/D-A duty-cycle output for an actuator (most
  likely fan motor speed) that's simply zero when nothing is running.
  This resolves the "or tied to an untested subsystem... on/off power
  state" speculation in KNOWN_OUTPUT_REGS/regmap.py from the very
  first session -- it was right.
- **New observation, not previously flagged: FC4 0x0000 and 0x0007
  consistently mirror each other almost exactly, in every single
  sample ever taken (ON and OFF alike)** -- e.g. 60/60, 48/49, 144/143,
  106/107, timeout/timeout, 6/6. Likely the same underlying value
  exposed at two addresses, or two near-identical/redundant channels.
  Doesn't identify what they measure, but is a real, reproducible
  structural fact about this register block worth knowing.
- **FC4 0x000A: consistent, dramatic on/off-correlated shift.** ON
  range across many samples: 177-228. Both independent OFF samples:
  33-34. A gap that clean and that consistent, with no overlap at all
  between the ON and OFF ranges, is a real signal -- this register
  reacts to something that changes with power state, even though what
  it physically represents is still unknown.
- **FC4 0x0004: also shifted, but noisier.** ON range: 28-52. OFF
  samples: 241, then 180 -- clearly a different (higher) regime than
  ON, but still moving within it rather than settling on one fixed
  value, unlike 0x000A's tighter OFF-state clustering.
- **FC4 0x000B: no clear on/off correlation.** OFF samples (178, 175)
  landed inside the existing ON range (145-236) -- doesn't obviously
  react to power state the way 0x0000/0x0004/0x0007/0x000A do.
- **FC4 0x000E ("possibly room/T15") -- this new data argues AGAINST
  that hypothesis, not for it.** If this were true ambient room air
  temperature, it should barely move over ~3.5 minutes with the unit
  off (rooms have real thermal mass). Instead it rose from an
  ad_to_celsius()-computed ~31.1°C to ~33.5°C in that window --
  physically implausible for room air, but very plausible for a
  sensor mounted near recently-running electronics/motor components,
  which can show a *rising* temperature right after shutdown (heat
  from the stopped motor/compressor conducting outward with no more
  airflow to carry it away). Downgrading confidence on the "room
  (T15)" label accordingly -- an internal/near-component sensor (e.g.
  something like CTS602's T0 controller-board or T9 heater sensors --
  see the SEVENTH SESSION notes) is now at least as plausible.
- **FC4 0x000D (outdoor, confirmed) also swung more than expected**
  (~11.1°C -> ~15.5°C in ~3.5 minutes) -- larger than ideal for true
  outdoor air, but not implausible depending on sensor placement
  (direct sun, a wind gust) and doesn't undermine the original
  confirmation, which rested on two ON-state samples where the
  register moved the correct direction with a real, independently-
  observed temperature change on the panel, not on short-term
  stability. Noted here as an open observation, not a contradiction.
- No incident: CRC error rate stayed low (climbed from ~9 to 25 over
  this whole round, ~0.2% of frames -- still every individual bad
  frame an isolated one-off on some node, never repeating), the OFF
  display never changed unexpectedly, no LINK ERR.

NINTH SESSION (2026-09-14, same day, continuing) -- asked to turn the
unit back on and confirm the on/off-correlated registers return to
normal, completing the round trip. Pressed "on" via the dashboard
(PassiveListener.send_key), confirmed via the display ("OFF" -> "AUTO").

- **Immediately after pressing "on" (~3s later): 0x0000/0x0001 output
  were nonzero, but NOT their usual 88/100** -- read 6 and 206 instead.
  Compressor LED still off at this point (expected -- the ~98s anti-
  short-cycle delay documented back in the project's "Status" section
  applies here too). This looks like a genuine startup transient, not
  noise: plausible if these really are PWM/duty-cycle values, since
  actuators (a fan motor especially) commonly ramp rather than jump
  straight to a steady-state duty cycle.
- **~2 minutes later: 0x0000/0x0001 read EXACTLY 88 and 100 again** --
  the same steady-state values seen in literally every prior ON-state
  sample this whole project, across every earlier session. Clean,
  reproducible three-phase pattern now confirmed: OFF=0, STARTUP=
  variable/transient, STEADY-STATE-ON=88/100 every time. This is the
  strongest evidence yet for these being genuine PWM/actuator duty-
  cycle outputs rather than arbitrary config values.
- 0x0000/0x0007 (input) mirrored each other again (90/88) -- the
  pairing held through the whole on/off/on round trip.
- 0x000A read 19 at this second ON-state check -- notably BELOW both
  the previously-established ON range (177-228) and the OFF range
  (33-34) seen earlier this session. Whether this is a third,
  transient-startup regime (paralleling 0x0000/0x0001's own startup
  transient) or just this register's normal noise floor extending
  lower than previously sampled isn't established -- flagged as an
  open observation, not a new claim.
- **0x000E kept rising, a fourth consecutive sample in the same
  direction**: ~31.1C -> ~33.5C -> ~35.8C -> ~37.8C, now spanning both
  the OFF period AND the first couple minutes back ON (compressor
  still hadn't started by this point). Continuing to rise even after
  power-on further undercuts the "room temperature" hypothesis (a
  monotonic ~7C rise over roughly 15-20 minutes, unbroken by the
  power state change, is not how room air or a simple "residual heat
  dissipating" story would behave) -- genuinely still unexplained, but
  clearly NOT tracking anything as slow-moving as room temperature.
  Worth another look once the compressor actually kicks in and airflow
  resumes, to see if/when this trend finally reverses.
- No incident: bus stayed healthy through the whole on/off/on cycle,
  CRC rate stayed in the same low background range throughout (never
  spiked), no LINK ERR at any point.

TENTH SESSION (2026-09-14, same day, continuing) -- the unit was now
actively running in cooling mode (confirmed by the user), and the
compressor had started (LED bit confirmed on). Asked "check if the
compressor turned on" -- confirmed yes via output_bits 0x0100 bit0.
While watching 0x000E continue its confusing "rise" from the eighth/
ninth sessions, the user mentioned they'd physically navigated the
panel to the "NÄYTÄ DATA" -> "NYKYTILA" -> HUONE screen -- which,
cross-checked against the exact same-moment register scan, gave a
DIRECT, user-verified ground-truth (raw register, real display value)
pair. Asked for and got a second one a few minutes later:

  raw=158 -> displayed HUONE/T15 = 22C
  raw=212 -> displayed HUONE/T15 = 23C  (~a few minutes later)

- **This resolves the whole 0x000E mystery from the eighth/ninth
  sessions.** ad_to_celsius() (0x000D's outdoor formula) does NOT
  apply to 0x000E -- fitting a line through these two real points
  gives a completely different transfer function: POSITIVE slope
  (~0.0185 C/raw, higher raw = warmer) vs. outdoor's negative slope
  (~0.1375 C/raw, higher raw = colder) -- opposite sign, ~7.4x smaller
  magnitude. Now regscan.room_ad_to_celsius(), with its own fitted
  ROOM_AD_OFFSET/ROOM_AD_SLOPE constants.
- Applying this NEW, correct formula retroactively to every earlier
  0x000E raw value from this session turns a nonsensical, unbroken
  "rising forever regardless of power/compressor state" story into a
  coherent one: room ~20.5C while off, cooling to ~19.3-19.6C as the
  compressor started -- consistent with both the idle screen's own
  displayed number (21C -> 19C over the same window) and the cooling
  mode the user confirmed was running. The apparent "anomaly" flagged
  in the eighth/ninth sessions was entirely an artifact of applying
  the wrong sensor's formula, not a real behavioral mystery.
- **CONFIRMS FC4 0x000E as the room temperature sensor (T15/HUONE)**
  -- upgraded from the earlier "downgraded, likely not room" verdict.
  Confidence note: this rests on 2 live, directly user-verified
  samples (about as good ground truth as this project gets -- a human
  reading the actual panel screen, not an inference from menu text
  captured via the dashboard), but is still only 2 points, which
  trivially fit ANY straight line regardless of whether the underlying
  model is truly linear. Not yet cross-validated the same way 0x000D
  was (two samples confirming both value AND a real, independently-
  observed directional change matching). A third live sample would
  raise this to that same standard; the user declined for now (fine --
  the fit is already strong and physically coherent, not just
  numerically exact).
- Reinforces this project's now-repeated lesson: different A/D
  channels on this unit genuinely need independently-fitted transfer
  functions (confirmed twice now, for two different sensors with very
  different slopes and even opposite signs) -- never assume one
  confirmed formula applies elsewhere without its own real calibration
  points.

ELEVENTH SESSION (2026-09-14, same day, continuing) -- tried the same
live-correlation technique on the water tank (VESI-YLÄ/T11), first
opportunistically (one live VESI-YLÄ=59C reading and one live
VESI-ALA=53C reading, different sensors at different moments -- too
weak to fit anything, see the analysis this produced: implausible
offsets like 131C/256C at raw=0 for some candidates), then properly:
the user changed the water heater target temperature specifically to
trigger active heating and a real, controlled rise on T11. Got a
genuine baseline (57C) and tracked it as heating progressed, using
PassiveListener.send_key() via the dashboard to navigate back to
VESI-YLÄ myself between checks rather than repeatedly asking the user.

- T11 rose 57C -> 59C over ~7 minutes (heating confirmed working --
  the idle screen also started showing the "W" water-heater-active
  flag, matching regmap.py's documented DISPLAY_REG_START comment).
  2-point fits for 0x0000, 0x0004, 0x000A, 0x000B all "worked"
  (trivially -- 2 points fit any line), with wildly inconsistent
  implied offsets (65C, 148C, 47C, 18C at raw=0) -- already a bad
  sign, unlike room's clean, consistent 2-point fit.
- **Decisive result on a THIRD check, ~6 minutes later: T11 held
  flat at 59C (no change at all) while 0x0000 moved 85->116, 0x000A
  moved 23->41, and 0x000B moved 183->207.** This is a direct
  contradiction, not a failed-to-fit: a real sensor reading the same
  physical temperature twice can't swing that much. RULES OUT
  0x0000 (and 0x0007, which mirrors it), 0x000A, and 0x000B as the
  water tank upper sensor -- and does so WITHOUT depending on any
  assumed slope/formula, unlike the weaker THIRD SESSION analysis
  (which used the outdoor sensor's slope and could in principle have
  been wrong the same way it was for 0x000E). This is the stronger,
  cleaner version of that same conclusion. 0x0004 timed out on the
  decisive check, so it's untested by this specific method -- still
  genuinely unknown, not ruled out the same way.
- **No water tank register identified.** Combined with the
  fourth/fifth/sixth sessions' exhaustive address-space sweep (all of
  0x0000-0x0200 mapped, nothing found beyond the small known cluster)
  and this session's decisive same-sensor contradiction test, the
  water tank temperature is very likely simply not exposed via FC3/FC4
  on this unit at all -- consistent with the working conclusion from
  the fifth session. Display-text parsing (reading the "NÄYTÄ DATA"
  menu directly, the same way this session got its ground-truth
  values) remains the only known way to get this value
  programmatically.
- Minor operational note: CRC error rate on node 3 crept up to ~0.8%
  over this session (vs. the usual ~0.1-0.3% background), all still
  isolated one-off frames on node 3's own traffic, no LINK ERR, no
  pattern -- flagged as worth keeping an eye on in future sessions,
  not yet a concern.

TWELFTH SESSION (2026-09-14, same day, continuing) -- same technique
on the condenser (LAUHDUTIN/T5). First tried it passively (compressor
running in cooling mode already had T5 moving on its own, 1C -> 14C
over a few minutes just from normal operation) then the user switched
the unit from cooling to heating mode specifically to force a bigger,
faster, more controllable change. Used
PassiveListener.send_key() via the dashboard to navigate back to the
LAUHDUTIN screen between checks, same as the eleventh session. Five
real, time-separated (raw, real T5) pairs collected as the mode switch
drove T5 up:

  T5=1C:  0x0000=137 0x0004=128 0x000A=99  0x000B=233
  T5=14C: 0x0000=122 0x0004=87  0x000A=111 0x000B=238
  T5=27C: 0x0000=200 0x0004=236 0x000A=133 0x000B=239
  T5=32C: 0x0000=189 0x0004=170 0x000A=149 0x000B=245
  T5=34C: 0x0000=148 0x0004=122 0x000A=167 0x000B=247

- **0x0000 and 0x0004 RULED OUT again**, decisively: their slope sign
  flips between segments (e.g. 0x0000 goes -0.867, +0.167, -0.455,
  -0.049 across the four segments) -- impossible for a register
  representing a single physical quantity moving monotonically in one
  direction the whole time. Consistent with the ELEVENTH SESSION's
  water-tank finding -- these two (which, remember, mirror 0x0007 and
  each other almost exactly) are not tracking any of the temperature
  sensors on this menu.
- **0x000B not trustworthy**: same-sign across all segments, but the
  raw value barely moves at all (233->247, only 14 raw units across
  the WHOLE 33C real swing) and the implied per-segment slopes are
  wildly inconsistent (2.6, 13.0, 0.83, 1.0) -- a register genuinely
  tracking a 33C swing that tightly should show more than 14 raw units
  of movement, and slope shouldn't swing 15x between segments. Reads
  as coincidental monotonic drift, not a real signal.
- **0x000A: STRONG, well-supported identification as the condenser
  temperature sensor (T5/LAUHDUTIN).** Every one of the five samples
  moved the SAME direction as T5, with zero exceptions, across a full
  33C real range (99->111->133->149->167 as T5 went 1->14->27->32->
  34C) -- the cleanest, most consistent multi-point result this whole
  project has produced, stronger evidence than what confirmed either
  0x000D (outdoor) or 0x000E (room), both of which only had 2 points.
  NOT a simple linear relationship, though: the segment slopes taper
  off noticeably at higher temperatures (1.083 -> 0.591 -> 0.312 ->
  0.111 C per raw unit) rather than staying constant. This is
  consistent with a genuinely non-linear sensor response (common for
  NTC thermistors -- resistance vs. temperature is naturally
  logarithmic, not linear; 0x000D/0x000E's linear fits may just be
  reasonable local approximations over the narrower ranges they were
  tested across, while 0x000A's much wider 33C test range exposed the
  curve). Deliberately NOT adding a decode_input_value() formula for
  this yet -- a linear fit would misrepresent the confidence level,
  and there isn't enough data across a wide-enough range yet to fit a
  non-linear (e.g. Steinhart-Hart-style) curve properly. The
  IDENTIFICATION (this is the condenser register) is well-supported;
  the precise FORMULA is not, and shouldn't be guessed at.
- Bus health: CRC rate on node 3 continued its gradual climb from the
  eleventh session, reaching ~0.9% by the end of this one -- still no
  burst pattern, no LINK ERR, display remained correct and responsive
  throughout (including correctly showing the "LÄMMITYS" mode-name
  change after the cooling->heating switch). Given the steady (not
  sudden) climb across a long, heavily-navigated multi-hour session,
  most likely cumulative background noise rather than a developing
  problem -- but worth starting a fresh session/restart before pushing
  much further live exploration, out of caution.

THIRTEENTH SESSION (2026-09-14, same day, continuing) -- restarted the
listener process first (clean 0-CRC-error baseline confirmed), then
asked to check MENO (T14, central-heating supply) the same way. The
user correctly predicted floor heating wouldn't actually engage (high
outside temperature), so unlike the water-tank/condenser tests there
was no real forced change available -- MENO held flat at 22C across
all four checks over ~10 minutes. Used the flat period itself as a
test instead: any candidate that moved substantially while MENO didn't
move at all can be ruled out, the same logic as the ELEVENTH SESSION's
water-tank contradiction test, just without a rise to also confirm a
positive match.

  0x0000: [76, 124, 136, 115]  -- range 60 while MENO flat
  0x0004: [139, 138, 136, 139] -- range 3 while MENO flat
  0x0007: [69, 125, 138, 118]  -- range 69 while MENO flat
  0x000A: [197, 240, 250, 44]  -- range 206 while MENO flat (expected --
                                   this is the condenser register, TWELFTH
                                   SESSION, driven by unrelated compressor
                                   activity, not MENO)
  0x000B: [--, 49, 50, 56]     -- range 7 while MENO flat

- 0x0000/0x0007 (the mirror pair) ruled out again, consistent with
  every prior test.
- **0x0004 and 0x000B were NOT contradicted this time** -- both stayed
  close to flat alongside MENO. This is much weaker evidence than the
  water-tank/condenser tests, though: a register can coincidentally
  sit still for 10 minutes without actually tracking the flat value in
  question. NEITHER is confirmed. 0x000B in particular already has a
  "matched once, then failed on a second sample" history for MENO
  specifically (SECOND SESSION) -- exactly the kind of one-off
  coincidence this project has repeatedly learned not to trust. 0x0004
  also has documented sign-flip contradictions against water tank AND
  condenser (ELEVENTH/TWELFTH SESSIONS) in OTHER tests, so even if it
  isn't contradicted by MENO specifically, its overall behavior (flat
  sometimes, wildly swinging other times for reasons unrelated to any
  single confirmed real quantity) doesn't inspire confidence that it's
  a genuine temperature sensor at all.
- **No identification for MENO this session.** A real determination
  needs an actual observed change to correlate against, the same way
  every other sensor here was confirmed -- not available today given
  the outside temperature. Worth revisiting if/when floor heating
  actually engages (colder weather, or another way to force a real
  supply-temperature change), testing 0x0004 and 0x000B specifically
  since they're the only two not already ruled out.
- **Bus health note, more pronounced than before**: despite the fresh
  restart at the start of this session, node 3's CRC error rate
  climbed to ~1.7% within about 10 minutes and ~1300 frames -- faster
  than the gradual climb seen across the entire multi-hour eleventh/
  twelfth sessions combined. Still no burst pattern (errors spread
  across the window, not clustered) and no LINK ERR, display remained
  correct throughout. Given this is now the SECOND session in a row to
  show climbing CRC noise, and this one climbed noticeably faster
  right after a clean restart, this is worth taking more seriously
  than a one-off -- possibly an environmental factor (something new
  generating RS485 noise nearby) or a developing physical-layer issue
  (connector, cable, termination) rather than pure incidental buildup.
  Recommend checking physical connections if this pattern continues in
  future sessions, and pacing back live exploration for now.

FOLLOW-UP FIX (CRC-error investigation, same project): the 2.0s pace
proven safe above was never actually applied to
PassiveListener.scan_standard_registers() (the dashboard's "Rescan now"
button) -- it silently used scan()'s own faster 0.3s default instead.
Now fixed, and centralized as SAFE_QUERY_SETTLE_SECONDS (see below) so
every active-query path uses the same proven-safe pace. scan() also
gained an optional `health_check` callback so a scan can abort itself
mid-batch if the bus is already showing the consecutive-CRC-error
pattern that precedes LINK ERR -- see state.py::NodeStats and
passive_listener.py::PassiveListener._bus_healthy().

FOLLOW-UP FIX (outdoor-temp mismatch, 2026-09-15): the dashboard's live
outdoor reading (T1, FC4 0x000D) stopped matching the physical panel at
all -- showing ~30-36C while the raw register had drifted to 43-80, far
outside the ~190-222 raw window ad_to_celsius()'s 2-point linear fit
was ever actually validated against (SECOND/EIGHTH SESSIONS above).
Same root cause already documented for the condenser sensor
(0x000A) -- a narrow-window linear fit extrapolated far past its
calibration range produces confident-looking nonsense -- but the guard
that was already added for condenser (CONDENSER_AD_FIT_RAW_RANGE) was
never applied to outdoor. Fixed the same way: added
OUTDOOR_AD_FIT_RAW_RANGE and made decode_input_value() return None
(raw value shown only, no decoded C) outside it, instead of silently
extrapolating. The range itself is a provisional guess pending a fresh
ground-truth check against the panel at whatever raw value 0x000D is
actually visiting now -- see OUTDOOR_AD_FIT_RAW_RANGE's comment.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from typing import Callable

from . import protocol, transport

log = logging.getLogger(__name__)

# The pace proven safe in practice (FOURTH/FIFTH/SIXTH SESSIONS above):
# Lodam states the panel repeats its own init sequence ("LINK ERR")
# after 10 CONSECUTIVE communication failures, and its own cyclic exchange
# with the controller runs about once a second. Spacing our own queries at
# roughly double that period gives it a clean, uncontested window between
# every single one of ours. Every active-query path in this codebase
# should use this (or slower), not a locally-chosen literal -- the
# scan_standard_registers() settle bug (2026 CRC-incident investigation)
# was exactly a caller silently falling back to scan()'s much faster
# default instead of this value.
SAFE_QUERY_SETTLE_SECONDS = 2.0

# FC3/FC4 replies do NOT echo the address they answer (see
# protocol.parse_read_regs_response) -- so if a reply to an earlier query
# arrives late, nothing in it says "this is not what you just asked for".
# Measured 2026-09-15: the controller starts replying ~28 ms after our
# frame ends and completes 50-75 ms after write() begins, so a matching
# frame that shows up sooner than this cannot be an answer to the query
# we just sent -- it's a leftover already in flight (or queued by the
# listener) when we wrote. 15 ms is comfortably below the fastest real
# reply and well above "already buffered".
MIN_REPLY_SECONDS = 0.015

# How long scan() waits before RE-asking an address that timed out.
# Without this, the retry registers its response waiter immediately and a
# late reply to the timed-out attempt satisfies it -- the value of one
# address credited to another. Found 2026-09-16: 37 reply pairs ~1.0 s
# apart in one session, the signature of exactly that. Harmless for block
# reads (same block either way), but it silently corrupted the earlier
# one-register-at-a-time correlation data. Waiting here means the late
# reply lands with no waiter registered, so passive_listener's
# _deliver_to_waiter() drops it (and the standalone path's
# reset_input_buffer() flushes it) instead of misattributing it.
RETRY_GUARD_SECONDS = 0.5

# From the Lodam document's "Lodam Modbus adresser" table -- addresses
# documented but never seen in passive captures, so worth actively
# querying. Kept here as the starting point for a scan, not a
# guarantee any of them respond (FC4 in particular isn't in the doc's
# list of function codes the *panel* must implement, which may or may
# not say anything about what the *controller* accepts).
KNOWN_OUTPUT_REGS = {  # Modbus ref 4x, read via FC3
    0x0000: "Digitale udgangsregistre (D/A, PWM etc.) -- confirmed present & stable (=88), NOT linked to temp/fan/mode",
    0x0001: "(undocumented, adjacent to 0x0000) -- confirmed present & stable (=100), NOT linked to temp/fan/mode",
    0x0100: "Aktionskode master/betjening (_AID_xxx) -- confirmed, regmap.py; also readable here (0 when idle)",
    0x0101: "Aktionskode slave/styring (_AID_xxx) -- the controller's own; confirmed readable, 0 when idle",
    0x0102: "Display codepage 16 bits (_CP_xxx fra DrvLCD modul) -- confirmed, value 1",
    0x0104: "Remote version -- confirmed, value 101 (i.e. 1.01) -- distinct from firmware SW version (1.22)",
}
KNOWN_INPUT_REGS = {  # Modbus ref 3x, read via FC4
    0x0000: "Måleværdier fra A/D kanaler og lignende (2 bytes pr. stk.) -- confirmed live/drifting, RULED OUT as water tank/condenser/MENO (swings ~13C-equivalent while those stayed flat, see THIRD SESSION notes), quantity still unidentified",
    0x0005: "(found in FOURTH SESSION's gap-fill scan, part of the same A/D block) -- reads exactly 160 across 3 independent samples spanning ~9 minutes while several menu readings visibly moved; RULED OUT as a live sensor, likely a static config/calibration value",
    0x0004: "(undocumented, part of the same A/D block) -- confirmed live/drifting, RULED OUT as water tank/condenser (sign-flip contradictions, ELEVENTH/TWELFTH SESSIONS), NOT contradicted (but not confirmed either) as MENO -- see THIRTEENTH SESSION notes",
    0x0007: "(undocumented, part of the same A/D block) -- confirmed live/drifting, RULED OUT as water tank/condenser/MENO, see THIRD SESSION notes",
    0x000A: "Condenser temp sensor (T5/LAUHDUTIN) -- STRONGLY SUPPORTED (not yet a fitted formula) via 5 real samples across a 33C range, all same direction, see TWELFTH SESSION notes. CORRECTS the earlier THIRD SESSION 'ruled out' verdict, which used a flawed shared-slope assumption -- same mistake pattern as the room-sensor detour, see TENTH SESSION notes",
    0x000B: "(undocumented, part of the same A/D block) -- live/drifting; a match against NAYTA DATA's MENO reading did NOT replicate on a second sample (SECOND SESSION); RULED OUT as water tank (ELEVENTH SESSION) and as tracking the condenser rise (TWELFTH SESSION, too noisy); NOT contradicted (but not confirmed, and has a prior false-positive history) as MENO -- see THIRTEENTH SESSION notes",
    0x000D: "Outdoor air temp sensor (T1/ULKOILMA) -- CONFIRMED via ad_to_celsius() against 2 independent NAYTA DATA samples, see SECOND SESSION notes above",
    0x000E: "Room temp sensor (T15/HUONE) -- CONFIRMED via a DIFFERENT (positive-slope) formula than 0x000D, fitted from 2 live user-verified samples, see TENTH SESSION notes",
    0x002A: "AD count fra betjening -- confirmed heartbeat, regmap.py",
    0x0100: "Tastekode 16 bits (bit 0-7=værdi, bit 8-15=funktionsflag) -- confirmed readable, 0 when idle",
    0x0102: "Reset årsag -- confirmed, value 1 (per typeSLAVEID's enum: 1=Power-on reset)",
    0x0103: "Initialiseringsstatus -- confirmed, value 1",
    0x0110: "RTC register -- CONFIRMED & DECODED: 8 words from here, BCD, "
             "[sec,min,hour,weekday,day,month,year,century]. Verified against "
             "real date/time twice, ~55s apart.",
}


# Slope from github.com/frodef/nilan-cts600-homeassistant's
# nilanADToCelsius(): a linear fit between two calibration points
# (raw=168 -> 34C, raw=328 -> 12C) for the same protocol family's A/D
# temperature sensor encoding. That project's default offset (56.25)
# is specific to their own unit's wiring/calibration, not this one --
# OUTDOOR_AD_OFFSET below is this unit's own, fitted from FC4 0x000D
# against the panel's "NAYTA DATA" -> "NYKYTILA" -> ULKOILMA reading
# and cross-validated against a second independent live sample (see
# this module's "SECOND SESSION" docstring notes for the numbers).
AD_TO_CELSIUS_SLOPE = (34 - 12) / (328 - 168)  # = 0.1375 C per raw unit
OUTDOOR_AD_OFFSET = 41.6  # fitted for FC4 0x000D specifically -- confirmed

# The only raw values this 2-point linear fit was ever actually
# validated against, +/- a margin: SECOND SESSION's pair (raw 199/210,
# real 14C/13C) and EIGHTH SESSION's observed swing (real 11.1C-15.5C,
# which this offset/slope back out to raw ~190-222). FOLLOW-UP BUG
# (2026-09-15): the live dashboard showed ~30-36C, not matching the
# panel at all, while the real raw value had drifted to 43-80 -- ~110-
# 160 raw units outside this calibrated window. A 2-point linear fit
# extrapolated that far is exactly the "confident-looking nonsense"
# already learned about for the condenser sensor (0x000A, NTC
# thermistors are naturally logarithmic, not linear -- see
# condenser_ad_to_celsius()'s docstring) and never guarded against
# here the way it was for condenser. Guarded the same way now: only
# decode within this range, show the raw value only outside it (see
# decode_input_value()). This band is a provisional guess (the
# confirmed points padded by ~35-40 raw units for ordinary day-to-day
# drift), not itself re-validated -- tighten it once a fresh
# (raw, real ULKOILMA reading) ground-truth pair is checked against
# the panel at whatever raw value the register is actually visiting
# now.
OUTDOOR_AD_FIT_RAW_RANGE = (150, 260)


def ad_to_celsius(raw: int, offset: float = OUTDOOR_AD_OFFSET) -> float:
    """Decode a raw A/D register value into degrees Celsius, outdoor-
    sensor formula (negative slope -- higher raw = colder).

    Reference only, NOT called live: on 2026-09-15 the panel read 11C
    both at raw 222 and at raw 119, so 0x000D has no stable mapping. NOT a general A/D formula -- FC4 0x000E (room) needs a
    completely different slope AND sign (see room_ad_to_celsius()
    below); other sensor channels (water tank, condenser, ...) may
    need their own fit too, or may not follow a linear formula like
    this at all (no fit was found for the ones tried so far -- see
    this module's THIRD SESSION notes).
    """
    return offset - raw * AD_TO_CELSIUS_SLOPE


# Room sensor (FC4 0x000E) formula -- CONFIRMED 2026-09-14 (TENTH
# SESSION), but via a fit that looks nothing like the outdoor one:
# fitted from 2 live, user-verified samples (the user physically
# reading the panel's "NÄYTÄ DATA" -> "NYKYTILA" -> HUONE screen while
# a register scan ran at the same moment): raw 158 -> 22C, raw 212 ->
# 23C. Slope is POSITIVE (higher raw = warmer, opposite sign from
# outdoor) and roughly 7.4x smaller in magnitude than the outdoor
# sensor's -- confirms different A/D channels on this unit really do
# need independently-fitted transfer functions, not a shared formula.
# Applying the outdoor formula to 0x000E's raw values (which earlier
# sessions did, for lack of anything better) produced a nonsensical
# "room temperature" that seemed to rise continuously for 40+ minutes
# regardless of the unit's power/compressor state -- that confusion is
# now resolved: it was the wrong formula, not a real anomaly. Applying
# THIS formula to that same historical data instead gives a coherent
# story (room cooling from ~20.5C toward ~19.3-19.6C as the compressor
# started), consistent with the cooling mode the user reported running
# during this test. Only 2 data points, though (a straight line always
# fits exactly 2 points, model-correct or not) -- treat as
# well-supported, not to outdoor's confirmation standard, until a
# third independent live sample is checked.
ROOM_AD_OFFSET = 19.074
ROOM_AD_SLOPE = 1 / 54  # = (23 - 22) / (212 - 158), fitted from the 2 live samples above


def room_ad_to_celsius(raw: int) -> float:
    """2-point fit for FC4 0x000E -- reference only, NOT called live.
    Slope 1/54 squeezes raw 0-255 into 19.1-23.8C, and the register
    wraps at 255, so a wrap reads as a ~4.7C drop with no real change.
    """
    return ROOM_AD_OFFSET + raw * ROOM_AD_SLOPE


# Condenser sensor (FC4 0x000A) formula -- FOURTEENTH SESSION
# (2026-09-14), fitted from the 5 real (raw, real-T5) points the
# TWELFTH SESSION collected while the user forced the unit from
# cooling to heating mode. The segment slopes in that data clearly
# tapered off at higher temperatures (1.08 -> 0.59 -> 0.31 -> 0.11
# C/raw) rather than staying constant, so a straight line was always
# going to be a poor fit -- tried it anyway plus a few standard
# non-linear forms (log, inverse-log/thermistor-B-style, power) via
# ordinary least squares on all 5 points. Log came out best:
#
#   raw   real(C)  linear-pred  log-pred
#   99    1.0      5.9  (+4.9)  4.6  (+3.6)
#   111   14.0     11.7 (-2.3)  11.9 (-2.1)
#   133   27.0     22.2 (-4.8)  23.3 (-3.7)
#   149   32.0     29.8 (-2.2)  30.5 (-1.5)
#   167   34.0     38.4 (+4.4)  37.7 (+3.7)
#
# R^2=0.94 for the log fit vs. 0.90 for linear -- log wins, but the
# residuals (up to ~3.7C) are much worse than outdoor's/room's fitted
# formulas (well under 1C). Also notable: the SAME points on both ends
# over-predict and the middle under-predicts for every 2-parameter
# candidate tried -- consistent with either genuine higher-order
# non-linearity (a proper 3-parameter Steinhart-Hart curve might fit
# better, but 5 points isn't enough to fit 3 parameters reliably), or
# some timing skew between reading the display (T5) and scanning the
# register a few seconds later while T5 was changing fast during that
# forced test (unlike the slower, more static conditions outdoor/room
# were fitted under). Use this as a rough approximation, not a
# confirmed formula to outdoor's/room's standard.
#
# Deliberately NOT applied outside roughly the tested range (raw
# 99-167, padded a bit to 90-180 below): this register has read
# anywhere from 17 to 250 across other sessions (different fan/
# compressor states), and extrapolating a 2-parameter log fit that far
# past its calibration range would produce confident-looking nonsense
# rather than an honest "don't know".
#
# DECODE DISABLED, FIFTEENTH SESSION (2026-09-14, same day): the user
# switched the unit from heating back to cooling and reported the
# dashboard's condenser reading changing "too quick and too much" to
# be believable. Checked the background poller's own log across that
# window and confirmed it wasn't just a bad feeling -- the DECODED
# value went 29.6C -> 16.8C -> 4.0C across two consecutive ~66s poll
# cycles (~13C/minute apparent rate). That's not purely a formula
# artifact either: the raw register itself jumped -27 and -22 units in
# those same two cycles, vs. typical -1 to -6 elsewhere, so something
# in the underlying signal genuinely did accelerate. But this formula
# is steepest in exactly that low-raw region (the log derivative
# grows as raw shrinks), so a real-but-modest acceleration got turned
# into a swing that looked like sensor failure rather than plausible
# condenser behavior. decode_input_value() no longer calls this
# function for 0x000A -- the dashboard/Registers tab show the raw
# value only now, full stop. The function and its fitted constants are
# kept here for reference/offline analysis, not deleted, since the
# underlying IDENTIFICATION (this register tracks the condenser) is
# still well-supported -- only the FORMULA turned out too fragile for
# live, unsmoothed display. A steadier estimate would need either
# averaging several reads before decoding, or refitting with a lot
# more calibration data across the full raw range this register
# actually visits (17-250, not just the 99-167 this was fitted on).
CONDENSER_AD_LOG_A = -286.42
CONDENSER_AD_LOG_B = 63.334
CONDENSER_AD_FIT_RAW_RANGE = (90, 180)


def condenser_ad_to_celsius(raw: float) -> float:
    """Fit for FC4 0x000A against LAUHDUTIN -- kept for reference/
    offline analysis only. **0x000A'S IDENTIFICATION AS THE CONDENSER
    SENSOR IS NOW DOUBTED, SIXTEENTH SESSION (2026-09-15)** -- see the
    comment above IDENTIFICATION REOPENED below. Do not call this live.
    """
    return CONDENSER_AD_LOG_A + CONDENSER_AD_LOG_B * math.log(raw)


# IDENTIFICATION REOPENED, SIXTEENTH SESSION (2026-09-15): re-enabling
# 0x000A's decode with smoothing (see git history for that brief
# attempt) surfaced a live reading that flatly didn't match the
# panel. Investigated properly with 5 fresh (raw, real-LAUHDUTIN) pairs
# across a genuine 27C forced swing (10C -> 37C, switching cooling then
# heating, same method as the TWELFTH SESSION that originally
# identified 0x000A):
#
#   real(C)  0x0000  0x0004  0x0007  0x000A  0x000B
#   10       116     201     107     192     187
#   19       114     208     108     179     194
#   32       94      85      89      178     196
#   35       89      18      113     175     200
#   37       127     131     116     173     209
#
# 0x000A moved DOWN as real T5 rose (192->173) -- the OPPOSITE sign
# from the positive slope that originally identified it in the TWELFTH
# SESSION (raw 99->167 as T5 rose 1C->34C). Not just outside its fitted
# range this time (see the FIFTEENTH/current-session raw-range-guard
# saga) -- actively contradicting its own identifying relationship.
# 0x0000/0x0004/0x0007 all flip sign across segments (already-known
# erratic candidates, re-confirmed erratic here too).
#
# **0x000B is the new leading candidate**: monotonically INCREASING
# across all 5 points (187->209, correct sign, ~0.81C/raw average --
# comparable magnitude to 0x000A's original low-end slope), and never
# broke sign once, unlike every other candidate including 0x0000 (which
# looked promising for the first 4 points, then flipped on the 5th).
# 0x000B was previously dismissed for condenser in the TWELFTH SESSION,
# but that dismissal used the same flawed method (borrowing another
# sensor's slope to estimate an equivalent-C swing) that ALSO initially
# mis-ruled-out 0x000A before its own correction -- so that old verdict
# carries little weight against this session's direct evidence.
#
# NOT yet fitting a formula for 0x000B: the per-segment implied slope
# is wildly inconsistent (0.78, 0.15, 1.33, 4.5 C/raw across the 4
# segments) -- the same "wildly inconsistent implied slopes... reads as
# coincidental drift" pattern this project has learned to distrust
# before adding a number. Identification only for now; see
# regmap.py's STANDARD_INPUT_REGS[0x000B] for the full write-up.
#
# **0x000B RETIRED TOO, same session, minutes later**: continuing to
# collect points to probe that inconsistent slope, two independent
# reads showed this register crash from 234 to 17-18 (not a fluke --
# reproduced) while the real panel read LAUHDUTIN=39C, near its
# session-high and still rising. A register tracking a temperature
# that's staying high cannot crash to near-zero; bus health was normal
# throughout (rules out a noise explanation). Also checked whether
# 0x000A might correlate with an INVERTED sign instead -- overlaying
# both sessions' data at comparable real temperatures shows a
# consistent ~30-90 unit OFFSET between sessions, not just a flipped
# sign, so that doesn't reconcile either (full numbers in regmap.py's
# STANDARD_INPUT_REGS[0x000A] follow-up note).
#
# **CONCLUSION: no known FC4 register in 0x0000-0x000B reliably tracks
# LAUHDUTIN.** Same conclusion this project already reached for the
# water tank/MENO sensors -- most likely not exposed as a standalone
# FC3/FC4 register in this range at all. decode_input_value() has no
# branch for 0x000A OR 0x000B -- both raw-only if queried manually
# (Registers tab). passive_listener.py's LIVE_SENSOR_ADDRS no longer
# has a "T5" entry at all; the dashboard's front-page condenser card
# was removed. Display-text parsing (walking "NÄYTÄ DATA" -> LAUHDUTIN,
# same technique planner.py uses for the idle screen) is the only known
# way to get this value now.
#
# CONDENSER_SMOOTHING_WINDOW/decode_condenser_smoothed() below are kept
# as a general-purpose, address-agnostic SMOOTHING MECHANISM (not tied
# to which register condenser turns out to be) -- reusable once/if a
# real formula gets fitted for 0x000B, without needing to reinvent the
# averaging approach. Not currently wired into anything live.
CONDENSER_SMOOTHING_WINDOW = 5


def decode_condenser_smoothed(smoothed_raw: float) -> str | None:
    """Decode an ALREADY-SMOOTHED raw value (the average of several
    recent reads, not a single instantaneous one -- see
    CONDENSER_SMOOTHING_WINDOW) using condenser_ad_to_celsius()'s
    formula. NOT CURRENTLY CALLED LIVE -- see the "IDENTIFICATION
    REOPENED" comment above: this formula/range was fitted for 0x000A,
    which is no longer believed to be the condenser register at all,
    and there's no fitted formula yet for 0x000B (the current leading
    candidate). Kept as a ready-to-reuse smoothing mechanism for
    whichever register eventually gets a real fit -- do not wire this
    back to 0x000A.
    """
    if not (CONDENSER_AD_FIT_RAW_RANGE[0] <= smoothed_raw <= CONDENSER_AD_FIT_RAW_RANGE[1]):
        return None
    return (
        f"~{condenser_ad_to_celsius(smoothed_raw):.1f}°C "
        f"(rough fit, {CONDENSER_SMOOTHING_WINDOW}-read average)"
    )


# FC4 0x0110: real-time clock, 8 words, BCD (see KNOWN_INPUT_REGS above
# and regmap.py) -- decoding it needs all 8 words in one read, unlike
# every other address here which is a single register.
RTC_REG = 0x0110
RTC_WORD_COUNT = 8


def _bcd_byte(word: int) -> int:
    """Decode one RTC word's low byte: high nibble = tens, low = units."""
    b = word & 0xFF
    return (b >> 4) * 10 + (b & 0x0F)


def decode_output_value(addr: int, words: "list[int] | None") -> str | None:
    """Human-readable interpretation of a STANDARD_OUTPUT_REGS (FC3)
    reading, for the handful of addresses with a confirmed meaning.
    Returns None (not "unknown") when nothing's confirmed for this
    address -- the caller decides how to present that.
    """
    if not words:
        return None
    v = words[0]
    if addr == 0x0104:
        return f"v{v / 100:.2f}"
    if addr == 0x0102:
        return f"codepage {v}"
    if addr in (0x0000, 0x0001):
        # CONFIRMED 2026-09-14 (EIGHTH SESSION): 0 when the unit is
        # off, a stable nonzero value (88/100 respectively) when on.
        # Likely a PWM/D-A actuator duty cycle -- what the specific
        # nonzero value MEANS beyond "on" isn't established, so only
        # decode the confirmed part.
        return "0 (off)" if v == 0 else f"{v} (on)"
    return None


def decode_input_value(addr: int, words: "list[int] | None") -> str | None:
    """Human-readable interpretation of a STANDARD_INPUT_REGS (FC4)
    reading. See decode_output_value() for the None convention.

    Confidence varies by address -- matches regmap.py's own caveats:
    No temperature channel is decoded: 0x000D (outdoor) and 0x000E (room)
    were both shown not to map stably to the panel's own reading (see the
    comments at their former branches).
    0x000A's identification as the condenser sensor is DOUBTED as of
    the SIXTEENTH SESSION (2026-09-15) -- it moved the wrong direction
    in a fresh forced-swing test, contradicting its own original
    identification. 0x000B is the current leading candidate instead,
    but has no fitted formula yet. Neither is decoded here -- both show
    raw-only, like every other unconfirmed channel (see the
    "IDENTIFICATION REOPENED" comment above condenser_ad_to_celsius()).
    0x0110 (RTC) is confirmed but needs RTC_WORD_COUNT words, not just
    words[0].
    """
    if not words:
        return None
    if addr == RTC_REG:
        if len(words) < RTC_WORD_COUNT:
            return None  # partial read (e.g. a plain single-word scan) -- can't decode
        sec, minute, hour, weekday, day, month, year, century = (
            _bcd_byte(w) for w in words[:RTC_WORD_COUNT]
        )
        return f"{century:02d}{year:02d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{sec:02d} (weekday {weekday})"
    v = words[0]
    # 0x000D (outdoor/T1): no decode branch. The panel read 11C both when
    # this register was 222 (formula 11.1C) and when it was 119 (formula
    # 25.2C), 2026-09-15 -- no stable mapping for any formula to fix.
    # 0x000E (room/T15): no decode branch. room_ad_to_celsius() maps the
    # whole 8-bit range into 19.1-23.8C, so it can never look wrong, and
    # the register wraps at 255 -- raw 14 displayed 19.3C while HUONE
    # read 23C (2026-09-15). Raw-only until re-identified.
    # 0x000A / 0x000B (condenser/T5 candidates): no decode branch for
    # either. 0x000A's own fitted formula (condenser_ad_to_celsius())
    # is kept for reference only -- its IDENTIFICATION as the condenser
    # sensor is now doubted (SIXTEENTH SESSION, 2026-09-15), not just
    # its formula (which was already known fragile, FIFTEENTH SESSION).
    # 0x000B is the current leading candidate but has no fitted formula
    # yet. See the "IDENTIFICATION REOPENED" comment above
    # condenser_ad_to_celsius() for the full account.
    if addr == 0x0102:
        return "power-on reset" if v == 1 else f"reset cause {v}"
    if addr == 0x0103:
        return "initialized" if v == 1 else f"status {v}"
    return None


def query_registers(
    ser, address: int, function: int, start: int, count: int,
    use_rts: bool = True, timeout: float = 1.0,
    wait_response: "Callable[[float], transport.RawFrame | None] | None" = None,
    before_write: "Callable[[], None] | None" = None,
) -> tuple[protocol.Frame | None, list[int] | None]:
    """Send one FC3/4 read query and wait up to timeout seconds for a
    response. Returns (frame, words) on a normal response, (frame,
    None) on an exception response (frame.is_exception is True -- the
    exception code is frame.data[0]), or (None, None) on timeout.

    wait_response: optional callable(timeout_seconds) -> RawFrame|None,
    used instead of this function's own FrameReader-based polling loop.
    Needed when the caller already has a background thread reading this
    same connection (see passive_listener.py::PassiveListener --
    scan_standard_registers() passes one in): two independent readers
    polling the same fd race each other and intermittently raise
    "SerialException: device reports readiness to read but returned no
    data (device disconnected or multiple access on port?)" -- confirmed
    in practice, not just a theoretical concern. Without one (e.g. this
    module's own standalone CLI, which owns the port exclusively), falls
    back to reading via a local FrameReader as before.

    before_write: optional callable run right before the query is sent --
    the dashboard uses it to wait for a quiet moment between panel<->
    controller exchanges. It may raise transport.BusBusyError to cancel.

    Drains the input buffer first (standalone-reader path only -- would
    race the caller's background thread otherwise) so a stray leftover
    byte from earlier traffic can't be mistaken for this query's response.

    Rejects two kinds of impostor reply, since FC3/FC4 replies don't echo
    the address they answer and a match on (node, function) alone is not
    proof: one that arrives sooner than MIN_REPLY_SECONDS after our write
    (physically too early to be ours), and one carrying a different number
    of registers than we asked for. Both are what a late reply to an
    EARLIER query looks like -- see RETRY_GUARD_SECONDS for how that
    situation arises.
    """
    if wait_response is None:
        ser.reset_input_buffer()
        reader = transport.FrameReader(ser)

        def wait_response(t: float) -> "transport.RawFrame | None":
            deadline = time.monotonic() + t
            while time.monotonic() < deadline:
                raw = reader.poll_once()
                if raw is not None:
                    return raw
            return None

    query = protocol.encode_read_query(address, function, start, count)
    write = transport.write_with_rts_direction if use_rts else transport.write_frame
    if before_write:
        before_write()
    write(ser, query)
    write_at = time.time()  # same clock as transport.RawFrame.timestamp

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None
        raw = wait_response(remaining)
        if raw is None:
            return None, None
        age_ms = (raw.timestamp - write_at) * 1000
        if raw.timestamp < write_at + MIN_REPLY_SECONDS:
            log.debug(
                "0x%04X: ignoring a frame %.0f ms after our write -- too early "
                "to be this query's reply", start, age_ms,
            )
            continue
        frame = protocol.decode_frame(raw.data)
        if frame is None or not frame.crc_ok:
            continue
        if frame.address != address or frame.base_function != function:
            continue  # not our response (could be unrelated bus traffic)
        if frame.is_exception:
            return frame, None
        words = protocol.parse_read_regs_response(frame.data)
        if words is not None and len(words) != count:
            log.debug(
                "0x%04X: ignoring a %d-word reply (asked for %d) -- not ours",
                start, len(words), count,
            )
            continue
        return frame, words


def scan(
    ser, function: int, addresses: list[int], count: int = 1,
    use_rts: bool = True, timeout: float = 1.0, settle: float = 0.3,
    retries: int = 0,
    wait_response: "Callable[[float], transport.RawFrame | None] | None" = None,
    health_check: "Callable[[], bool] | None" = None,
    before_write: "Callable[[], None] | None" = None,
) -> dict[int, dict]:
    """Query each address in turn, return {address: result_dict}.

    result_dict has "status": "ok" | "exception" | "timeout", plus
    "words" (on ok) or "error_code" (on exception).

    retries: on a timeout, retry up to this many more times before
    giving up. Worth setting >0 when the physical panel is still on
    the bus -- a timeout is often just an unlucky collision with the
    panel's own ~1/sec cyclic traffic, not a genuinely invalid address
    (confirmed: retrying a "timeout" address here often succeeds).

    wait_response: passed straight through to query_registers() -- see
    its docstring. Needed whenever `ser` is shared with a background
    reader thread (e.g. the dashboard's on-demand scan button).

    health_check: optional callable(), checked once before each
    address's query. Returning False aborts the rest of the scan
    immediately -- every remaining address is recorded as
    "skipped_unhealthy" rather than queried -- instead of grinding
    through the whole batch while the bus is already showing signs of
    the same CRC-error pattern that has previously led to a real LINK
    ERR on this bus. This generalizes what was previously only a
    human watching the dashboard and stopping a scan by hand. Caller
    (PassiveListener) wires this to a per-node consecutive-CRC-error
    check -- see passive_listener.py::_bus_healthy().

    before_write: passed to query_registers() for every attempt. If it
    raises transport.BusBusyError (no quiet window on the bus), the scan
    stops the same way a failed health_check does: this and every
    remaining address is recorded as "skipped_busy".
    """
    results: dict[int, dict] = {}
    for i, addr in enumerate(addresses):
        if health_check is not None and not health_check():
            log.warning(
                "Bus health check failed before 0x%04X -- aborting scan, "
                "%d of %d addresses not queried",
                addr, len(addresses) - i, len(addresses),
            )
            for remaining in addresses[i:]:
                results[remaining] = {"status": "skipped_unhealthy"}
            break
        try:
            for attempt in range(retries + 1):
                if attempt:
                    time.sleep(RETRY_GUARD_SECONDS)  # see RETRY_GUARD_SECONDS
                frame, words = query_registers(
                    ser, protocol.NODE_STYREENHED_1, function, addr, count,
                    use_rts=use_rts, timeout=timeout, wait_response=wait_response,
                    before_write=before_write,
                )
                if frame is not None:
                    break
        except transport.BusBusyError as exc:
            log.warning(
                "%s before 0x%04X -- aborting scan, %d of %d addresses not queried",
                exc, addr, len(addresses) - i, len(addresses),
            )
            for remaining in addresses[i:]:
                results[remaining] = {"status": "skipped_busy"}
            break
        if frame is None:
            results[addr] = {"status": "timeout"}
            log.info("0x%04X: timeout (no response in %.1fs)", addr, timeout)
        elif frame.is_exception:
            code = frame.data[0] if frame.data else None
            try:
                name = protocol.ErrorCode(code).name
            except ValueError:
                name = f"unknown(0x{code:02X})" if code is not None else "unknown"
            results[addr] = {"status": "exception", "error_code": code, "error_name": name}
            log.info("0x%04X: exception %s", addr, name)
        else:
            results[addr] = {"status": "ok", "words": words}
            log.info("0x%04X: %s", addr, [f"0x{w:04X}" for w in (words or [])])
        time.sleep(settle)
    return results


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="Read-only scan of standard Modbus registers (FC3/FC4).")
    p.add_argument("--port", default=None, help="Serial device (default: autodiscover)")
    p.add_argument("--function", choices=["3", "4"], default="3", help="3=output regs (default), 4=input regs")
    p.add_argument("--start", type=lambda s: int(s, 0), default=0x0000)
    p.add_argument("--end", type=lambda s: int(s, 0), default=0x0010)
    p.add_argument("--count", type=int, default=1, help="Registers per query (default: 1)")
    p.add_argument("--rts-direction", action="store_true")
    p.add_argument("--out", default=None, help="Write results as JSON to this path")
    args = p.parse_args(argv)

    ser = transport.open_serial(args.port)
    try:
        addrs = list(range(args.start, args.end + 1, args.count))
        results = scan(
            ser, int(args.function), addrs, count=args.count,
            use_rts=args.rts_direction,
        )
    finally:
        ser.close()

    if args.out:
        with open(args.out, "w") as f:
            json.dump({f"0x{k:04X}": v for k, v in results.items()}, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
