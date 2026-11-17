// AgriScout phone dashboard. Plain JavaScript, no libraries (works with no internet).
// Same safety rules as the desktop GUI (desktop/gui.py):
//   - hold a pad button to drive: the command is resent every 0.15 s, release = stop
//   - status is polled every 0.3 s; this is also the auto-mode heartbeat on the robot
//   - the light only goes red after 3 failed polls in a row (one slow Wi-Fi reply is normal)
"use strict";

const DRIVE_REPEAT_MS = 150;
const STATUS_PERIOD_MS = 300;
const TIMEOUT_MS = 500;
const SCAN_TIMEOUT_MS = 6000;     // a scan takes ~2.5 s on the Pi
const FAILS_BEFORE_RED = 3;
const VIDEO_RETRY_MS = 2000;
const VIDEO_CHECK_MS = 500;
const SLIDER_DEBOUNCE_MS = 100;
const SLIDER_SYNC_PAUSE_MS = 1000; // don't move a slider the user touched less than 1 s ago
const SCAN_BAR_MAX_CM = 200;

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- HTTP

// One helper for every request. Never throws: returns {ok, status, data, error},
// like desktop/api_client.py's Result. status is null when no reply came.
async function api(method, path, body, timeoutMs = TIMEOUT_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const opts = { method, signal: ctrl.signal, cache: "no-store" };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const reply = await fetch(path, opts);
    let data;
    try {
      data = await reply.json();
    } catch (e) {
      return { ok: false, status: reply.status, data: {}, error: "bad reply (not JSON)" };
    }
    if (reply.ok) return { ok: true, status: reply.status, data, error: null };
    return { ok: false, status: reply.status, data, error: data.error || "HTTP " + reply.status };
  } catch (e) {
    const error = e.name === "AbortError" ? "robot did not answer in time" : "cannot reach robot";
    return { ok: false, status: null, data: {}, error };
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------- messages

let toastTimer = null;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3000);
}

let obstacleMsg = "";
function showObstacle(text) {
  obstacleMsg = text;
  $("banner").textContent = text;
  $("banner").hidden = false;
}

// ---------------------------------------------------------------- driving

// held: pointerId -> command, for every finger/mouse button on a pad button.
// A Map keeps insertion order, so the LAST pressed one wins (like the desktop keys).
const held = new Map();
let sentCmd = null;       // what we are sending now ("stop" or a move)
let blockedCmd = null;    // got a 409 obstacle for this: don't resend until released
let drivesPending = 0;    // drive requests queued or on the way

// Browsers send requests in parallel, so a "stop" could overtake a "forward" that is still on
// its way, and the forward would then win. This chain sends drive commands strictly in order
// (the desktop GUI does the same with its CommandThread).
let driveChain = Promise.resolve();

function heldCmd() {
  let cmd = null;
  for (const c of held.values()) cmd = c;
  return cmd;
}

function speed() {
  return Number($("speed").value);
}

function sendDrive(cmd) {
  sentCmd = cmd;
  drivesPending += 1;
  driveChain = driveChain.then(async () => {
    const r = await api("POST", "/api/drive", { cmd, speed: speed() });
    drivesPending -= 1;
    if (r.status === 409 && /obstacle/.test(r.error || "")) {
      blockedCmd = cmd;       // server refused forward: stop asking until the finger lifts
      sentCmd = "stop";
      showObstacle(r.error);
    } else if (!r.ok) {
      toast("drive: " + r.error);
    } else if (r.data.recentred) {
      toast("Ultrasonic re-centred: driving forward needs it looking ahead");
    }
  });
}

// Called whenever what is held changes: react now, don't wait for the next repeat.
function updateDrive() {
  const cmd = heldCmd();
  if (cmd !== blockedCmd) blockedCmd = null;
  if (cmd === null) {
    if (sentCmd !== "stop") sendDrive("stop");
  } else if (cmd !== sentCmd && cmd !== blockedCmd) {
    sendDrive(cmd);
  }
}

// Every 0.15 s: repeat the held move (the robot stops after 0.8 s without one).
// Only if the previous drive request is finished, so requests never pile up.
setInterval(() => {
  const cmd = heldCmd();
  if (cmd === null || cmd === blockedCmd) return;
  if (drivesPending === 0) sendDrive(cmd);
}, DRIVE_REPEAT_MS);

function stopAll() {
  held.clear();
  document.querySelectorAll("#pad .down").forEach((b) => b.classList.remove("down"));
  sendDrive("stop");
}

function setupPad() {
  for (const btn of document.querySelectorAll("#pad .drive")) {
    const cmd = btn.dataset.cmd;
    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      // Touch screens "capture" the finger to the button it went down on, and then
      // pointerleave never fires. Release that capture, so sliding off the button stops.
      if (btn.hasPointerCapture(e.pointerId)) btn.releasePointerCapture(e.pointerId);
      held.set(e.pointerId, cmd);
      btn.classList.add("down");
      updateDrive();
    });
    const release = (e) => {
      if (held.get(e.pointerId) !== cmd) return;
      held.delete(e.pointerId);
      btn.classList.remove("down");
      updateDrive();
    };
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
      btn.addEventListener(type, release);
    }
  }
  const stop = $("stop");
  stop.addEventListener("pointerdown", (e) => { e.preventDefault(); stopAll(); });
  stop.addEventListener("click", stopAll);  // keyboard / accessibility
  $("pad").addEventListener("contextmenu", (e) => e.preventDefault());  // no long-press menu
}

// ---------------------------------------------------------------- keyboard (laptop browser)

// Same keys as the desktop GUI (desktop/gui.py KEY_TO_CMD; tests/test_web.py checks they match).
const KEY_TO_CMD = {
  w: "forward", s: "back", a: "left", d: "right", q: "spin_left", e: "spin_right",
};

// While a slider or text box has focus, keys belong to it (arrow keys move a slider, etc.).
function typingInField() {
  const el = document.activeElement;
  return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT");
}

function setupKeyboard() {
  document.addEventListener("keydown", (e) => {
    if (typingInField() || e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === " ") {
      e.preventDefault();          // Space must not "click" a focused button or scroll the page
      if (!e.repeat) stopAll();
      return;
    }
    const cmd = KEY_TO_CMD[e.key.toLowerCase()];
    if (!cmd) return;
    e.preventDefault();
    if (e.repeat) return;          // the OS repeats keydown while a key is held: ignore those
    held.set("key:" + cmd, cmd);   // keys share the "held" map with fingers: last pressed wins
    updateDrive();
  });
  document.addEventListener("keyup", (e) => {
    const cmd = KEY_TO_CMD[(e.key || "").toLowerCase()];
    if (!cmd || !held.has("key:" + cmd)) return;
    held.delete("key:" + cmd);
    updateDrive();
  });
}

// Phone locked, app switched, tab hidden: the finger-up event may never arrive. Stop.
// (Stop also ends a running auto mode: nobody is watching the screen any more.)
function stopIfActive() {
  if (held.size > 0 || sentCmd !== "stop" || lastMode !== "manual") stopAll();
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopIfActive();
    stopVideo();
  } else {
    startVideo();
  }
});
window.addEventListener("blur", stopIfActive);
window.addEventListener("pagehide", stopIfActive);

// ---------------------------------------------------------------- video

let config = null;
let videoRetry = null;

function startVideo() {
  if (!config || document.hidden) return;
  clearTimeout(videoRetry);
  const url = config.camera_stream_url;
  // A new query string forces a fresh connection instead of a cached broken one.
  $("video").src = url + (url.includes("?") ? "&" : "?") + "t=" + Date.now();
}

function stopVideo() {
  clearTimeout(videoRetry);
  $("video").removeAttribute("src");
  showVideo(false);
}

let videoShown = false;

function showVideo(on) {
  videoShown = on;
  $("video").classList.toggle("off", !on);
  $("no-video").hidden = on;
}

function videoLost() {
  showVideo(false);
  $("no-video").textContent = "NO VIDEO (retrying)";
  clearTimeout(videoRetry);
  videoRetry = setTimeout(startVideo, VIDEO_RETRY_MS);
}

function setupVideo() {
  const img = $("video");
  showVideo(false);
  // Desktop Chrome does not reliably fire "load" for an MJPEG stream (it is one endless
  // response), so check every 500 ms whether a frame has arrived: naturalWidth > 0.
  // (Checking that the picture CHANGES is not possible: the stream comes from port 8080,
  // another origin, so the browser does not let JavaScript read its pixels.)
  setInterval(() => {
    const hasFrame = img.hasAttribute("src") && img.naturalWidth > 0;
    if (hasFrame && !videoShown) showVideo(true);
    else if (!hasFrame && videoShown) videoLost();
  }, VIDEO_CHECK_MS);
  img.addEventListener("load", () => showVideo(true));  // fast path where it works
  img.addEventListener("error", videoLost);
}

// ---------------------------------------------------------------- config + sliders

// A slider's left end must be the direction called "left" (front servo is mirrored vs pan).
// direction: rtl puts the maximum on the left.
function setupRange(el, lo, hi, leftValue, rightValue) {
  el.min = lo;
  el.max = hi;
  el.style.direction = leftValue > rightValue ? "rtl" : "ltr";
}

const lastTouched = {};   // slider id -> time the user last moved it
function debounced(fn) {
  let timer = null;
  return () => { clearTimeout(timer); timer = setTimeout(fn, SLIDER_DEBOUNCE_MS); };
}

function applyConfig(c) {
  config = c;
  const s = c.servo;
  const l = c.limits;
  setupRange($("speed"), l.speed[0], l.speed[1], 0, 1);
  $("speed").value = c.default_speed;
  $("speed-out").textContent = c.default_speed;
  setupRange($("pan"), l.pan[0], l.pan[1], s.PAN_LEFT, s.PAN_RIGHT);
  setupRange($("tilt"), l.tilt[0], l.tilt[1], s.TILT_DOWN, s.TILT_UP);  // left end = down
  setupRange($("front"), l.front[0], l.front[1], s.FRONT_LEFT, s.FRONT_RIGHT);
  buildScanRows(c.scan_angles || [], s);
  syncServos = true;
  startVideo();
}

async function loadConfig() {
  const r = await api("GET", "/api/config");
  if (r.ok) applyConfig(r.data);
  else setTimeout(loadConfig, 1000);
}

function setupSliders() {
  $("speed").addEventListener("input", () => { $("speed-out").textContent = $("speed").value; });

  const sendCamera = debounced(async () => {
    const r = await api("POST", "/api/camera",
      { pan: Number($("pan").value), tilt: Number($("tilt").value) });
    if (!r.ok) toast("camera: " + r.error);
  });
  const sendFront = debounced(async () => {
    const r = await api("POST", "/api/front_servo", { angle: Number($("front").value) });
    if (!r.ok) toast("ultrasonic: " + r.error);
  });
  for (const [id, send] of [["pan", sendCamera], ["tilt", sendCamera], ["front", sendFront]]) {
    $(id).addEventListener("input", () => { lastTouched[id] = Date.now(); send(); });
  }
  // A focused slider keeps the keyboard (W/A/S/D would be ignored). Let go when the drag ends.
  for (const el of document.querySelectorAll("input[type=range]")) {
    el.addEventListener("change", () => el.blur());
  }
  $("cam-centre").addEventListener("click", () => {
    if (!config) return;
    $("pan").value = config.servo.PAN_CENTER;
    $("tilt").value = config.servo.TILT_LEVEL;
    lastTouched.pan = lastTouched.tilt = Date.now();
    sendCamera();
  });
  $("front-centre").addEventListener("click", () => {
    if (!config) return;
    $("front").value = config.servo.FRONT_CENTER;
    lastTouched.front = Date.now();
    sendFront();
  });
}

// Move a slider to the robot's value, unless the user is using it right now.
function syncSlider(id, value) {
  if (value === undefined || value === null) return;
  if (Date.now() - (lastTouched[id] || 0) < SLIDER_SYNC_PAUSE_MS) return;
  $(id).value = value;
}

// ---------------------------------------------------------------- scan

function buildScanRows(angles, s) {
  const names = {};
  names[s.FRONT_LEFT] = "Left";
  names[s.FRONT_CENTER] = "Centre";
  names[s.FRONT_RIGHT] = "Right";
  const box = $("scan-bars");
  box.textContent = "";
  for (const angle of angles) {
    const row = document.createElement("div");
    row.className = "scan-row";
    row.innerHTML = '<span></span><div class="bar"><div class="fill" style="width:0"></div>' +
      '<span class="txt">--</span></div>';
    row.firstChild.textContent = names[angle] || angle + "°";
    box.appendChild(row);
  }
}

function showScan(distances) {
  const rows = $("scan-bars").children;
  distances.forEach((cm, i) => {
    if (!rows[i]) return;
    const pct = cm === null ? 0 : Math.min(cm, SCAN_BAR_MAX_CM) / SCAN_BAR_MAX_CM * 100;
    rows[i].querySelector(".fill").style.width = pct + "%";
    rows[i].querySelector(".txt").textContent = cm === null ? "no echo" : Math.round(cm) + " cm";
  });
}

async function scan() {
  const btn = $("scan");
  btn.disabled = true;
  btn.textContent = "Scanning...";
  const r = await api("POST", "/api/scan", {}, SCAN_TIMEOUT_MS);
  btn.disabled = false;
  btn.textContent = "Scan";
  if (r.ok) showScan(r.data.distances_cm);
  else toast("scan: " + r.error);
}

// ---------------------------------------------------------------- modes, LED, beep

function setupButtons() {
  for (const btn of document.querySelectorAll(".mode")) {
    btn.addEventListener("click", async () => {
      const r = await api("POST", "/api/mode", { mode: btn.dataset.mode });
      if (!r.ok) toast("mode: " + r.error);
    });
  }
  for (const btn of document.querySelectorAll(".led")) {
    btn.addEventListener("click", async () => {
      const [r, g, b] = btn.dataset.rgb.split(",").map(Number);
      const reply = await api("POST", "/api/led", { r, g, b });
      if (!reply.ok) toast("LED: " + reply.error);
    });
  }
  $("beep").addEventListener("click", async () => {
    const r = await api("POST", "/api/beep", { seconds: 0.2 });
    if (!r.ok) toast("beep: " + r.error);
  });
  $("scan").addEventListener("click", scan);
  $("more-toggle").addEventListener("click", () => {
    const open = $("more-panel").classList.toggle("open");
    $("more-toggle").textContent = open ? "More ▴" : "More ▾";
    $("more-toggle").setAttribute("aria-expanded", String(open));
  });
}

// ---------------------------------------------------------------- status polling

let fails = 0;
let connected = false;
let lastMode = "manual";
let syncServos = true;    // copy pan/tilt from the robot once after (re)connecting
let modeMsgTimer = null;

function setConnected(ok, error) {
  const dot = $("conn");
  dot.className = "dot " + (ok ? "ok" : "bad");
  dot.title = ok ? "connected" : (error || "disconnected");
  if (ok && !connected) {       // (re)connected: fresh config + video
    syncServos = true;
    if (config) startVideo();
    else loadConfig();
  }
  connected = ok;
}

// ---------------------------------------------------------------- climate (DHT11)
// Same rules as climate_text() in desktop/gui.py.
const CLIMATE_FRESH_S = 30;          // = scout/config.py CLIMATE_FRESH_S
const CLIMATE_SENSOR_MAX_PCT = 90;   // the DHT11 tops out at ~90-91 % RH

function ageText(ageS) {
  const s = Math.floor(ageS);
  return s < 120 ? s + " s" : Math.floor(s / 60) + " min";
}

// Returns [text, state]; state: fresh, stale, waiting or offline.
function climateText(c) {
  if (!c) return ["Sensor offline: robot server has no climate data (deploy the new version)",
                  "offline"];
  if (!c.available) return ["Sensor offline: " + (c.reason || "unknown reason"), "offline"];
  if (c.temp_c === null || c.humidity_pct === null || c.age_s === null) {
    return ["Waiting for the first reading...", "waiting"];
  }
  const humidity = c.humidity_pct >= CLIMATE_SENSOR_MAX_PCT
    ? "≥ " + CLIMATE_SENSOR_MAX_PCT + " % RH (sensor maximum)"
    : Math.round(c.humidity_pct) + " % RH";
  const text = c.temp_c.toFixed(1) + " °C · " + humidity + " · updated " + ageText(c.age_s) + " ago";
  if (c.age_s > CLIMATE_FRESH_S) return [text + " (stale)", "stale"];
  return [text, "fresh"];
}

// ---------------------------------------------------------------- soil (Uno on USB)
// Same rules as soil_text() in desktop/gui.py. Information only.
const SOIL_STATE_TEXT = { dry: "Dry", ok: "OK", wet: "Wet" };

// Returns [text, state]; state: fresh, stale or offline.
function soilText(s) {
  if (!s) return ["Soil: robot server has no soil data (deploy the new version)", "offline"];
  if (s.raw === null || s.moisture_pct === null) {
    return ["Soil: " + (s.reason || "unknown reason"), "offline"];
  }
  let text = s.moisture_pct + " % moisture · " + (SOIL_STATE_TEXT[s.state] || "?") +
    " · raw " + s.raw;
  if (!s.available) return [text + " (" + (s.reason || "stale") + ")", "stale"];
  if (s.age_s !== null) text += " · updated " + ageText(s.age_s) + " ago";
  return [text, "fresh"];
}

function render(s) {
  // climate
  const [climate, climateState] = climateText(s.climate);
  $("climate").textContent = climate;
  $("climate").classList.toggle("stale", climateState !== "fresh");

  // soil
  const [soil, soilState] = soilText(s.soil);
  $("soil").textContent = soil;
  $("soil").classList.toggle("stale", soilState !== "fresh");

  // mode
  $("mode-now").textContent = "mode: " + s.mode;
  for (const btn of document.querySelectorAll(".mode")) {
    btn.classList.toggle("active", btn.dataset.mode === s.mode);
  }
  if (lastMode !== "manual" && s.mode === "manual" && s.mode_reason) {
    $("mode-msg").textContent = "Mode ended: " + s.mode_reason;
    $("mode-msg").hidden = false;
    clearTimeout(modeMsgTimer);
    modeMsgTimer = setTimeout(() => { $("mode-msg").hidden = true; }, 6000);
  }
  lastMode = s.mode;

  // distance + line sensors
  const d = s.distance_cm;
  $("distance").textContent = (d === null ? "no echo" : Math.round(d) + " cm") +
    (s.front_ahead === false ? " (side)" : "");
  if (s.line) {
    $("line").textContent = ["L1", "L2", "R1", "R2"]
      .map((k) => k + " " + (s.line[k] ? "■" : "□")).join(" ");
  }

  // obstacle banner
  if (s.obstacle) {
    showObstacle(obstacleMsg || "OBSTACLE AHEAD: forward blocked");
  } else {
    obstacleMsg = "";
    $("banner").hidden = true;
  }

  // sliders follow the robot
  syncSlider("front", s.front);
  if (syncServos) {
    syncServos = false;
    syncSlider("pan", s.pan);
    syncSlider("tilt", s.tilt);
  }
}

async function pollStatus() {
  const r = await api("GET", "/api/status");
  if (r.ok) {
    fails = 0;
    setConnected(true);
    render(r.data);
  } else {
    fails += 1;
    if (fails >= FAILS_BEFORE_RED) {
      setConnected(false, r.error);
      $("climate").textContent = "Sensor offline: robot not reachable";
      $("climate").classList.add("stale");
      $("soil").textContent = "Soil: robot not reachable";
      $("soil").classList.add("stale");
    }
  }
  // Next poll only after this one finished: slow Wi-Fi never piles up requests.
  setTimeout(pollStatus, STATUS_PERIOD_MS);
}

// ---------------------------------------------------------------- start

setupPad();
setupKeyboard();
setupVideo();
setupSliders();
setupButtons();
loadConfig();
pollStatus();
