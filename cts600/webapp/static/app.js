const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const brandVersion = document.getElementById("brand-version");
const busHealthWarning = document.getElementById("bus-health-warning");
const lcd = document.getElementById("lcd");
const metaNode = document.getElementById("meta-node");
const metaMode = document.getElementById("meta-mode");
const metaFrames = document.getElementById("meta-frames");
const metaCrc = document.getElementById("meta-crc");
const metaScreen = document.getElementById("meta-screen");
const regTableBody = document.querySelector("#reg-table tbody");
const bitTableBody = document.querySelector("#bit-table tbody");
const slaveIdTableBody = document.querySelector("#slaveid-table tbody");
const ledDot = document.getElementById("led-dot");
const ledEl = document.getElementById("led");
const lcdNotes = document.getElementById("lcd-notes");
const controlsFrame = document.getElementById("controls-frame");
const controlsDisabled = document.getElementById("controls-disabled");
const pressError = document.getElementById("press-error");
const updateBtn = document.getElementById("update-btn");
const cancelBtn = document.getElementById("cancel-btn");
let controlEnabled = false;
const walkStatusEl = document.getElementById("walk-status");
const readingsGrid = document.getElementById("readings-grid");
const logEl = document.getElementById("log");
const settingsFrame = document.getElementById("settings-frame");
const modeButtons = document.getElementById("mode-buttons");
const setpointValueEl = document.getElementById("setpoint-value");
const setpointDownBtn = document.getElementById("setpoint-down");
const setpointUpBtn = document.getElementById("setpoint-up");
const fanValueEl = document.getElementById("fan-value");
const fanDownBtn = document.getElementById("fan-down");
const fanUpBtn = document.getElementById("fan-up");
const editStatusEl = document.getElementById("edit-status");

// A reading older than this is shown grayed out.
const STALE_SECONDS = 30 * 60;

// Order and labels follow the panel's NÄYTÄ DATA list (display_data.py).
const READINGS = [
  { key: "room", label: "Room", sub: "HUONE · T15" },
  { key: "outdoor", label: "Outdoor", sub: "ULKOILMA · T1" },
  { key: "tank_top", label: "Tank top", sub: "VESI-YLÄ · T11" },
  { key: "tank_bottom", label: "Tank bottom", sub: "VESI-ALA · T12" },
  { key: "supply", label: "Heating supply", sub: "MENO · T14" },
  { key: "condenser", label: "Condenser", sub: "LAUHDUT · T5" },
];

// --- tabs ------------------------------------------------------------

document.querySelectorAll("#tabs .tab-btn[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(name) {
  document.querySelectorAll("#tabs .tab-btn[data-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== `tab-${name}`;
  });
  try { localStorage.setItem("cts600-tab", name); } catch (e) { /* storage blocked */ }
}

try {
  const saved = localStorage.getItem("cts600-tab");
  if (saved === "diagnostics") switchTab(saved);
} catch (e) { /* storage blocked */ }

// --- readings cards ------------------------------------------------------

const cardEls = {};
for (const r of READINGS) {
  const card = document.createElement("div");
  card.className = "reading-card";
  const label = document.createElement("div");
  label.className = "reading-label";
  label.textContent = r.label;
  const sub = document.createElement("div");
  sub.className = "reading-sub";
  sub.textContent = r.sub;
  const value = document.createElement("div");
  value.className = "reading-value";
  value.textContent = "—";
  const age = document.createElement("div");
  age.className = "reading-age";
  age.textContent = "not seen yet";
  card.append(label, sub, value, age);
  readingsGrid.appendChild(card);
  cardEls[r.key] = { card, value, age };
}

// Ages come from the server, plus the time since that snapshot arrived, so
// the browser clock never has to agree with the host's.
let lastReadings = {};
let lastSensors = {};
let lastWalk = { running: false };
let lastPanel = null;
let lastEdit = { running: false };

// Mirrors panel_settings.py: SETPOINT_RANGE, FAN_RANGE, MODES (minus off/on,
// which are folded into these four buttons -- picking auto/cool/heat while
// off switches the unit on first, same as the physical panel's Enter x2).
const SETPOINT_RANGE = [5, 30];
const FAN_RANGE = [1, 4];

// Register temperature statuses (sensor_regs.py): what the card says under
// the value, and the longer explanation in its tooltip. Anything other than
// "ok" means the live value can't be trusted and an Update is what fixes it.
const SENSOR_NOTES = {
  uncertain: ["press Update", "the register value lost track of the panel — an Update re-anchors it"],
  mismatch: ["press Update", "the register value doesn't match the panel's own reading"],
  stale: ["press Update", "register reads are failing"],
  unanchored: ["press Update", "this sensor has never been anchored to a panel reading"],
};
let snapshotReceivedAt = 0;

function elapsed() {
  return Date.now() / 1000 - snapshotReceivedAt;
}

function formatAge(seconds) {
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

function paintReadings() {
  for (const r of READINGS) {
    const els = cardEls[r.key];
    const reading = lastReadings[r.key];   // the panel's own data screen
    const sensor = lastSensors[r.key];     // the tracked register value
    const live = !!(sensor && sensor.status === "ok" && sensor.value !== null && sensor.value !== undefined);
    const where = sensor ? `T${sensor.t_number}, input register ${sensor.register}` : "";
    const age = reading ? reading.age_s + elapsed() : null;

    // Anything but "ok" means the live value can't be trusted and an Update
    // is what fixes it -- say so on the card instead of showing a number.
    els.card.classList.toggle("sensor-doubt", !live && !!sensor && controlEnabled);

    if (live) {
      els.value.textContent = `${sensor.value.toFixed(1)}°C`;
      els.age.textContent = "live";
      els.card.classList.remove("stale");
      els.card.title = `Live from ${where}`
        + (reading ? ` · panel showed ${reading.value} ${formatAge(age)}` : "");
      continue;
    }

    els.value.textContent = reading ? reading.value : "—";
    els.card.classList.toggle("stale", age === null || age > STALE_SECONDS);
    if (sensor && controlEnabled) {
      const [note, why] = SENSOR_NOTES[sensor.status] || ["press Update", sensor.status];
      const reg = sensor.value === null || sensor.value === undefined
        ? "no value yet" : `${sensor.value.toFixed(1)}°C`;
      els.age.textContent = note;
      els.card.title = `${why} (${where}, currently ${reg})`
        + (reading ? ` · showing the panel's ${reading.value} from ${formatAge(age)}` : "");
    } else {
      els.age.textContent = age === null ? "not seen yet" : formatAge(age);
      els.card.title = reading ? `From the panel's data screen: ${reading.text}` : "";
    }
  }
}

// Any panel operation (an Update walk or a settings change) holds the same
// lock on the server (panel_ops.py): only one runs at a time. Both busy
// states disable *all* the panel-touching controls, not just their own, so
// a click doesn't just earn a 409 while the panel's mid-sequence.
function operationRunning() {
  return !!(lastWalk && lastWalk.running) || !!(lastEdit && lastEdit.running);
}

function paintWalk() {
  const w = lastWalk || {};
  updateBtn.disabled = operationRunning();
  updateBtn.textContent = w.running ? "Updating…" : "Update";
  cancelBtn.hidden = !(controlEnabled && w.running);
  cancelBtn.disabled = !!w.cancelling;
  cancelBtn.textContent = w.cancelling ? "Cancelling…" : "Cancel";
  walkStatusEl.classList.toggle("warn", !w.running && w.outcome === "stopped");
  if (w.running) {
    const step = w.total_steps ? ` · step ${w.steps}/${w.total_steps}` : "";
    walkStatusEl.textContent = `${capitalize(w.message || "working")}${step}`;
    walkStatusEl.hidden = false;
  } else if (w.outcome && w.finished_age_s !== undefined) {
    const age = formatAge(w.finished_age_s + elapsed());
    const verb = { done: "", cancelled: " cancelled", stopped: " stopped" }[w.outcome] ?? ` ${w.outcome}`;
    walkStatusEl.textContent = `Last update${verb} ${age}: ${w.message}`;
    walkStatusEl.hidden = false;
  } else {
    walkStatusEl.hidden = true;
  }
  document.querySelectorAll(".ctrl-btn").forEach((btn) => { btn.disabled = operationRunning(); });
}

// Settings: mode/setpoint/fan, applied through POST /api/settings
// (panel_settings.py), which presses Enter/Up/Down/Esc the same way a
// person would and checks the screen after every press. This replaces
// blindly counting Enter presses on the raw Up/Down/Enter buttons -- the
// panel gives no reliable "this field is selected" signal to read back
// (display_decoder.py has the detail: setpoint blinks, fan shows a static
// marker, mode shows nothing at all), so edit-status text from the server
// is the only trustworthy indicator of what's being changed.
function paintSettings() {
  const p = lastPanel;
  const busy = operationRunning();
  const isOff = !p || p.mode === "off";
  modeButtons.querySelectorAll(".setting-btn").forEach((btn) => {
    btn.classList.toggle("active", !!p && p.mode === btn.dataset.mode);
    btn.disabled = busy || !p;
  });
  setpointValueEl.textContent = p && p.setpoint != null ? `${p.setpoint}°C` : "—";
  fanValueEl.textContent = p && p.fan != null ? p.fan : "—";
  const setpointKnown = !!p && p.setpoint != null;
  const fanKnown = !!p && p.fan != null;
  setpointDownBtn.disabled = busy || isOff || !setpointKnown || p.setpoint <= SETPOINT_RANGE[0];
  setpointUpBtn.disabled = busy || isOff || !setpointKnown || p.setpoint >= SETPOINT_RANGE[1];
  fanDownBtn.disabled = busy || isOff || !fanKnown || p.fan <= FAN_RANGE[0];
  fanUpBtn.disabled = busy || isOff || !fanKnown || p.fan >= FAN_RANGE[1];
}

function paintEditStatus() {
  const e = lastEdit || {};
  editStatusEl.classList.toggle("warn", !e.running && e.outcome === "stopped");
  if (e.running) {
    const step = e.steps ? ` · step ${e.steps}` : "";
    editStatusEl.textContent = `${capitalize(e.message || e.phase || "working")}${step}`;
    editStatusEl.hidden = false;
  } else if (e.outcome && e.finished_age_s !== undefined) {
    const age = formatAge(e.finished_age_s + elapsed());
    const verb = { done: "", stopped: " stopped" }[e.outcome] ?? ` ${e.outcome}`;
    editStatusEl.textContent = `Last setting change${verb} ${age}: ${e.message}`;
    editStatusEl.hidden = false;
  } else {
    editStatusEl.hidden = true;
  }
}

async function applySetting(payload, trigger) {
  if (trigger) trigger.disabled = true;
  hidePressError();
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showPressError(`Setting change not started: ${body.error || res.status}`);
      paintSettings();  // didn't actually start: re-sync disabled state now, don't wait for the next tick
    }
    // On success the edit_status push (over /ws) repaints and re-enables
    // controls once the operation finishes.
  } catch (e) {
    showPressError(`Setting change not started: ${e}`);
    paintSettings();
  }
}

modeButtons.querySelectorAll(".setting-btn").forEach((btn) => {
  btn.addEventListener("click", () => applySetting({ mode: btn.dataset.mode }, btn));
});
setpointDownBtn.addEventListener("click", () => {
  if (lastPanel && lastPanel.setpoint != null) applySetting({ setpoint: lastPanel.setpoint - 1 }, setpointDownBtn);
});
setpointUpBtn.addEventListener("click", () => {
  if (lastPanel && lastPanel.setpoint != null) applySetting({ setpoint: lastPanel.setpoint + 1 }, setpointUpBtn);
});
fanDownBtn.addEventListener("click", () => {
  if (lastPanel && lastPanel.fan != null) applySetting({ fan: lastPanel.fan - 1 }, fanDownBtn);
});
fanUpBtn.addEventListener("click", () => {
  if (lastPanel && lastPanel.fan != null) applySetting({ fan: lastPanel.fan + 1 }, fanUpBtn);
});

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

setInterval(() => { paintReadings(); paintWalk(); paintSettings(); paintEditStatus(); }, 5000);

updateBtn.addEventListener("click", async () => {
  updateBtn.disabled = true;
  hidePressError();
  try {
    const res = await fetch("/api/display_data/refresh", { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showPressError(`Update not started: ${body.error || res.status}`);
      updateBtn.disabled = false;
    }
  } catch (e) {
    showPressError(`Update not started: ${e}`);
    updateBtn.disabled = false;
  }
});

cancelBtn.addEventListener("click", async () => {
  cancelBtn.disabled = true;
  cancelBtn.textContent = "Cancelling…";
  hidePressError();
  try {
    const res = await fetch("/api/display_data/cancel", { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showPressError(`Cancel failed: ${body.error || res.status}`);
      cancelBtn.disabled = false;
      cancelBtn.textContent = "Cancel";
    }
  } catch (e) {
    showPressError(`Cancel failed: ${e}`);
    cancelBtn.disabled = false;
    cancelBtn.textContent = "Cancel";
  }
});

// --- controls ------------------------------------------------------------

document.querySelectorAll(".ctrl-btn").forEach((btn) => {
  btn.addEventListener("click", () => pressKey(btn.dataset.key, btn));
});

let pressErrorTimer = null;

function showPressError(text) {
  pressError.textContent = text;
  pressError.hidden = false;
  clearTimeout(pressErrorTimer);
  pressErrorTimer = setTimeout(hidePressError, 8000);
}

function hidePressError() {
  pressError.hidden = true;
}

async function pressKey(key, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/press", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const msg = `press ${key} failed: ${body.error || res.status}`;
      showPressError(msg);
      appendLogRow({ type: "note", timestamp: Date.now() / 1000, text: msg });
    }
  } catch (e) {
    showPressError(`press ${key} failed: ${e}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// --- diagnostics -----------------------------------------------------------

document.getElementById("clear-log").addEventListener("click", () => {
  logEl.innerHTML = "";
});

document.getElementById("note-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("note-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  try {
    await fetch("/api/note", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  } catch (e) {
    // the note will simply be missing from the log; not fatal
  }
});

// --- snapshot ----------------------------------------------------------------

let refreshPending = false;

function scheduleSnapshotRefresh() {
  if (refreshPending) return;
  refreshPending = true;
  setTimeout(async () => {
    refreshPending = false;
    try {
      const res = await fetch("/api/state");
      renderSnapshot(await res.json());
    } catch (e) {
      // transient; next event will retry
    }
  }, 150);
}

function renderSnapshot(snap) {
  snapshotReceivedAt = Date.now() / 1000;
  if (snap.version) brandVersion.textContent = `v${snap.version}`;

  // LCD: the reassembled display segments.
  const segments = snap.display && snap.display.segments ? snap.display.segments : [];
  if (segments.length === 0) {
    lcd.innerHTML = '<div class="lcd-line lcd-placeholder">no display data yet</div>';
  } else {
    lcd.innerHTML = segments
      .map((s) => `<div class="lcd-line">${escapeHtml(s.text)}</div>`)
      .join("");
  }
  renderLcdNotes(snap, segments);

  const nodeIds = Object.keys(snap.node_stats || {});
  metaNode.textContent = nodeIds.length ? nodeIds.join(", ") : "—";
  metaMode.textContent = snap.mode || "—";
  metaScreen.textContent = snap.screen || "—";
  let frames = 0, crc = 0;
  for (const id of nodeIds) {
    frames += snap.node_stats[id].frame_count || 0;
    crc += snap.node_stats[id].crc_error_count || 0;
  }
  metaFrames.textContent = frames;
  metaCrc.textContent = crc;

  // Bus-health warning: per-node and bus-wide (address-independent)
  // signals from state.py -- a corrupted frame's address can't be trusted,
  // so a noise burst can scatter across many bogus addresses.
  const atRiskNodes = nodeIds.filter((id) => snap.node_stats[id].link_error_risk);
  const busAtRisk = !!(snap.bus_health && snap.bus_health.at_risk);
  if (atRiskNodes.length || busAtRisk) {
    const parts = atRiskNodes
      .map((id) => `node ${id}: ${snap.node_stats[id].consecutive_crc_errors} consecutive CRC errors`);
    if (busAtRisk) {
      parts.push(`bus-wide: ${snap.bus_health.consecutive_crc_errors} consecutive (any address)`);
    }
    busHealthWarning.textContent =
      `⚠ bus health degraded (${parts.join(", ")}) — data updates pause automatically. ` +
      `This only pauses our own traffic — it can't stop a LINK ERR caused by a real wiring/noise problem.`;
    busHealthWarning.hidden = false;
  } else {
    busHealthWarning.hidden = true;
  }

  statusDot.classList.toggle("ok", !!snap.connected);
  statusText.textContent = snap.connected
    ? `listening on ${snap.port || "?"}`
    : "not connected";

  renderKV(regTableBody, mergeRegs(snap.output_regs, snap.input_regs));
  renderKV(bitTableBody, snap.output_bits || {}, formatBits);
  renderSlaveId(snap.slave_id);
  renderLed((snap.output_bits || {})["0x0100"]);

  controlsFrame.hidden = !snap.control_enabled;
  settingsFrame.hidden = !snap.control_enabled;
  controlsDisabled.hidden = !!snap.control_enabled;
  controlEnabled = !!snap.control_enabled;
  updateBtn.hidden = !controlEnabled;
  updateBtn.parentElement.hidden = !controlEnabled;

  lastReadings = snap.readings || {};
  lastSensors = snap.sensors || {};
  lastWalk = snap.walk || { running: false };
  lastPanel = snap.panel || null;
  lastEdit = snap.edit || { running: false };
  paintReadings();
  paintWalk();
  paintSettings();
  paintEditStatus();

  // Backfill the log with recent history on first load only.
  if (logEl.childElementCount === 0 && snap.recent_log) {
    for (const event of snap.recent_log) {
      if (event.type === "raw" || event.type === "note") appendLogRow(event);
    }
  }
}

function mergeRegs(a, b) {
  return Object.assign({}, b || {}, a || {});
}

function formatBits(bits) {
  return Array.isArray(bits) ? bits.join("") : String(bits);
}

function renderKV(tbody, obj, formatter) {
  const entries = Object.entries(obj || {});
  if (entries.length === 0) {
    tbody.innerHTML = '<tr><td class="unknown" colspan="2">none observed yet</td></tr>';
    return;
  }
  tbody.innerHTML = entries
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `<tr><td>${k}</td><td>${escapeHtml(formatter ? formatter(v) : String(v))}</td></tr>`)
    .join("");
}

function renderSlaveId(slaveId) {
  if (!slaveId) {
    slaveIdTableBody.innerHTML = '<tr><td class="unknown" colspan="2">none observed yet</td></tr>';
    return;
  }
  const swVersion = (slaveId.sw_ver_raw / 100).toFixed(2);
  const rows = [
    ["product", slaveId.product],
    ["slave id", slaveId.slave_id],
    ["protocol version (raw)", slaveId.prot_ver],
    ["software version", swVersion],
    ["run / error / reset status", `${slaveId.run_status} / ${slaveId.error_status} / ${slaveId.reset_status}`],
    ["display", `${slaveId.nb_display_cols} cols × ${slaveId.nb_display_rows} rows`],
    ["display type / data type", `${slaveId.display_type} / ${slaveId.display_data_type}`],
    ["outputs / leds / inputs / keys", `${slaveId.nb_outputs} / ${slaveId.nb_leds} / ${slaveId.nb_inputs} / ${slaveId.nb_keys}`],
    ["output regs / input regs / actions", `${slaveId.nb_output_regs} / ${slaveId.nb_input_regs} / ${slaveId.nb_actions}`],
  ];
  slaveIdTableBody.innerHTML = rows
    .map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(String(v))}</td></tr>`)
    .join("");
}

function renderLed(bits) {
  if (!Array.isArray(bits) || bits.length < 1) {
    ledDot.className = "led-dot";
    ledEl.title = "Status LED: no data yet";
    return;
  }
  const on = bits[0] === 1;
  const blinking = bits[1] === 1;
  ledDot.className = "led-dot" + (on ? " on" : "") + (blinking ? " blink" : "");
  ledEl.title = blinking ? "Status LED blinking: alarm" : `Status LED ${on ? "on" : "off"}`;
}

// Flags the idle screen shows after the mode name on line 1 ("AUTO  W*").
const IDLE_FLAGS = { W: "Water heating", "*": "Ventilation boost" };

function renderLcdNotes(snap, segments) {
  const notes = [];
  const line1 = segments.find((s) => s.start_reg === "0x0200");
  // Only the 8 visible characters; the trailing attribute bytes can also
  // render as "*" (a field-selection marker), which is not a flag.
  if (snap.screen === "idle" && line1) {
    const flags = line1.text.slice(0, 8).trim().split(/\s+/).slice(1).join("");
    for (const ch of flags) {
      if (IDLE_FLAGS[ch] && !notes.includes(IDLE_FLAGS[ch])) notes.push(IDLE_FLAGS[ch]);
    }
  }
  lcdNotes.replaceChildren(...notes.map((text) => {
    const li = document.createElement("li");
    li.textContent = text;
    return li;
  }));
  lcdNotes.hidden = notes.length === 0;
}

function appendLogRow(event) {
  const row = document.createElement("div");
  row.className = "log-row";
  const time = new Date(event.timestamp * 1000).toLocaleTimeString();
  if (event.type === "note") {
    row.innerHTML = `<span class="t">${time}</span> <span class="note">NOTE: ${escapeHtml(event.text)}</span>`;
  } else {
    const crcClass = event.crc_ok === false ? "crc-bad" : "";
    row.innerHTML =
      `<span class="t">${time}</span> ` +
      `node ${event.address} <span class="fc ${crcClass}">${event.fc}</span> ` +
      `${event.crc_ok === false ? "CRC-FAIL " : ""}` +
      `${escapeHtml(event.hex || "")}`;
  }
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  logEl.appendChild(row);
  while (logEl.childElementCount > 1000) logEl.firstElementChild.remove();
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    statusText.textContent = "connected, waiting for data…";
  };

  ws.onmessage = (msg) => {
    const event = JSON.parse(msg.data);
    if (event.type === "snapshot") {
      renderSnapshot(event.data);
      return;
    }
    if (event.type === "raw" || event.type === "note") {
      appendLogRow(event);
      return;
    }
    if (event.type === "walk_status") {
      lastWalk = { ...event.status, finished_age_s: event.status.running ? undefined : 0 };
      snapshotReceivedAt = Date.now() / 1000;
      paintWalk();
    }
    if (event.type === "edit_status") {
      lastEdit = { ...event.status, finished_age_s: event.status.running ? undefined : 0 };
      snapshotReceivedAt = Date.now() / 1000;
      paintEditStatus();
      paintSettings();
    }
    // reg_block / bit_block / slave_id / reading: refresh the derived views.
    scheduleSnapshotRefresh();
  };

  ws.onclose = () => {
    statusDot.classList.remove("ok");
    statusText.textContent = "disconnected — retrying…";
    setTimeout(connect, 1500);
  };

  ws.onerror = () => ws.close();
}

connect();
