/* Test stand console — no external libraries, works offline. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const CFG = window.TS || { hold_s: 1.5, alarm: 80, max: 100 };
  let lastEventId = 0;
  let state = null;
  let chartWin = 30;
  let countdownEnd = null;    // performance.now() ms at T-0
  let failCount = 0;

  async function api(path, body) {
    const opt = body === undefined ? { method: "POST" } :
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    try {
      const r = await fetch(path, opt);
      const j = await r.json();
      if (!j.ok) toast(j.error || "Refused");
      return j;
    } catch (e) { toast("Server not reachable"); return { ok: false }; }
  }

  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3500);
  }

  // ---------------- polling ----------------
  async function poll() {
    try {
      const r = await fetch(`/api/state?events_after=${lastEventId}`, { cache: "no-store" });
      state = await r.json();
      failCount = 0;
      $("offline").hidden = true;
      render(state);
    } catch (e) {
      if (++failCount > 3) { $("offline").hidden = false; document.body.dataset.state = "OFFLINE"; }
    }
    setTimeout(poll, 100);
  }

  const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? "–" : Number(v).toFixed(d);

  const S = window.STELLAR.status;   // shared helpers from shell.js
  let lastResult = null, firstRender = true, viewChosen = false, armedSinceFire = false;
  const ARC = 414.7, CIRC = 552.9;     // 270-degree gauge on r=88
  function setArc(id, frac) {
    const el = $(id);
    if (el) el.style.strokeDasharray = `${(ARC * Math.max(0, Math.min(1, frac))).toFixed(1)} ${CIRC}`;
  }
  function setStageView(v) {
    $("stage").dataset.view = v;
    document.querySelectorAll(".seg.view").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  }
  try { const v = localStorage.getItem("stageView"); if (v) { setStageView(v); viewChosen = true; } } catch (e) { /* no storage */ }
  document.querySelectorAll(".seg.view").forEach((b) => b.addEventListener("click", () => {
    setStageView(b.dataset.view); viewChosen = true;
    try { localStorage.setItem("stageView", b.dataset.view); } catch (e) { /* no storage */ }
  }));
  setStageView($("stage").dataset.view);
  const PHASES = ["ready", "armed", "countdown", "ignition", "safed"];

  function render(s) {
    const t = s.telemetry, ign = s.ignition, rec = s.recorder;

    // top bar
    const mode = S.mode(ign);
    S.pill($("pill-ign"), ign.physical ? "Ignition" : "Ignition (sim)", ign.state, S.ignClass(ign.state));
    S.pill($("pill-rec"), "Recording", rec.active ? `${rec.elapsed_s}s` : "Off", rec.active ? "rec" : "");
    S.announce(ign.state, ign);

    // pressure
    const p = t.pressure_bar;
    $("p-now").textContent = fmt(p, 2);
    $("p-psi").textContent = p === null ? "–" : fmt(p * 14.5038, 1);
    $("p-peak").textContent = fmt(t.peak_bar, 2);
    $("p-tare").textContent = fmt(t.tare_bar, 3);
    const ro = document.querySelector(".readout");
    const alarm = p !== null && p >= CFG.alarm;
    ro.classList.toggle("alarm", alarm);
    ro.classList.toggle("stale", t.link !== "LIVE");
    document.body.classList.toggle("p-alarm", alarm);
    $("p-alarm-val").textContent = fmt(CFG.alarm, 0);
    setArc("g-now-arc", p === null ? 0 : p / CFG.max);
    setArc("g-peak-arc", (t.peak_bar || 0) / CFG.max);
    ["g-now-alarm", "g-peak-alarm"].forEach((id) => { const el = $(id); if (el) el.style.strokeDashoffset = (-ARC * Math.min(1, CFG.alarm / CFG.max)).toFixed(1); });
    $("p-meta").textContent = t.link === "LIVE" ? `PT-01 · ${t.rate_hz} readings/s` : "Waiting for the ESP32";
    $("p-meta").title = t.firmware ? `${t.firmware} from ${t.remote || "?"}` : "";
    $("link-detail").textContent = t.port_error ? t.port_error
      : t.link === "OFFLINE" ? `No connection from the ESP32 (this PC listens on port ${t.port}).`
      : t.gaps || t.bad_frames ? `${t.gaps} gaps and ${t.bad_frames} damaged messages since start.`
      : "";

    // ignition
    document.body.dataset.state = ign.state;
    $("ign-state").textContent = ign.state;
    $("ign-mode").textContent = mode;
    $("ign-mode").className = "mode-tag " + (ign.physical ? "live" : "sim");
    const hw = ign.hardware;
    $("kv-cd").textContent = `${ign.countdown_s} s`;
    $("kv-hold").textContent = `${fmt(CFG.hold_s, 1)} s`;
    $("kv-pulse").textContent = `${ign.pulse_ms} ms`;
    $("kv-sig").textContent = t.voltage_v !== null && t.link === "LIVE" ? `${fmt(t.voltage_v, 3)} V` : "–";
    $("ign-hw").textContent = ign.physical ? (hw.module ? "" : "The ESP32 relay is not answering.") : "Simulation: no relay switches.";
    $("ign-hw").className = "ign-hw" + (ign.physical && !hw.module ? " bad" : "");
    let phase = { SAFE: "ready", ARMED: "armed", COUNTDOWN: "countdown", FIRING: "ignition" }[ign.state] || "ready";
    if (ign.state === "ARMED" || ign.state === "COUNTDOWN") armedSinceFire = true;
    if (ign.state === "FIRING") armedSinceFire = false;
    if (ign.state === "SAFE" && ign.last_fire && !armedSinceFire) phase = "safed";
    document.body.classList.toggle("safed", phase === "safed");
    const pi = PHASES.indexOf(phase);
    document.querySelectorAll("#timeline li").forEach((li, i) => { li.classList.toggle("done", i < pi); li.classList.toggle("now", i === pi); });
    $("timeline").style.setProperty("--done", `calc((100% - 18px) * ${pi / (PHASES.length - 1)})`);
    $("t-label").textContent = ign.state === "COUNTDOWN" ? "Time to ignition" : ign.state === "FIRING" ? "Ignition" : phase === "safed" ? "Test complete" : `Countdown ${ign.countdown_s} s`;
    if (ign.state === "COUNTDOWN" && ign.t_minus !== null) countdownEnd = performance.now() + ign.t_minus * 1000;
    else if (ign.state !== "FIRING") countdownEnd = null;

    $("btn-arm").textContent = ign.state === "ARMED" ? "DISARM" : "ARM";
    $("btn-arm").disabled = !(ign.state === "SAFE" || ign.state === "ARMED");
    $("btn-start").disabled = ign.state !== "ARMED";
    $("start-lbl").textContent = "Hold to start";
    $("btn-start").title = "Press and hold to start the countdown (or hold Enter)";
    $("btn-relay-arm").disabled = $("btn-relay-fire").disabled = ign.state !== "SAFE";
    let note = "", noteCls = "muted";
    if (ign.state === "ARMED" && ign.arm_remaining_s !== null) { note = `Disarms by itself in ${ign.arm_remaining_s} s if nothing happens.`; noteCls = "warn"; }
    else if (ign.state === "SAFE" && t.link !== "LIVE") { note = "ARM needs live pressure data from the ESP32."; noteCls = "warn"; }
    else if (ign.state === "SAFE" && ign.last_abort_reason) {
      const who = ign.last_abort_reason === "operator ABORT" ? "by the operator" : ign.last_abort_reason;
      const at = ign.last_abort_t ? " at " + new Date(ign.last_abort_t * 1000).toLocaleTimeString([], { hour12: false }) : "";
      note = `Last abort: ${who}${at}.`;
    }
    $("ign-note").textContent = note;
    $("ign-note").className = "small ign-note " + noteCls;
    if (ign.state === "FIRING") S.flash("FIRE", "fire");
    else if (alarm) S.flash(`Pressure alarm ${fmt(p, 1)} bar`, "alarm");
    else S.clearFx();

    // cameras: a camera without a picture collapses to a short notice
    let live = 0, shown = 0;
    for (const c of s.cameras) {
      const fig = $("cam-" + c.id);
      if (!fig) continue;
      shown++;
      const ok = c.status === "LIVE";
      if (ok) live++;
      fig.classList.toggle("off", !ok);
      fig.querySelector(".cam-err").textContent = c.error ? c.error : `Status: ${c.status.toLowerCase()}.`;
      const st = fig.querySelector(".cam-status");
      st.title = c.error || "";
      if (c.recording) { st.textContent = `Rec ${c.record_s} s`; st.className = "cam-status rec"; }
      else if (c.record_failed) { st.textContent = "Recording failed"; st.className = "cam-status bad"; }
      else { st.textContent = ok ? "Live" : c.status.toLowerCase(); st.className = "cam-status " + (ok ? "ok" : "bad"); }
    }
    $("cams-note").textContent = shown ? `${live} of ${shown} camera${shown > 1 ? "s" : ""} live` : "";
    if (!viewChosen) setStageView(live > 0 ? "camera" : "pressure");

    // recorder
    const busy = ign.state === "COUNTDOWN" || ign.state === "FIRING";
    $("btn-rec").textContent = rec.active ? "Stop" : "Start";
    document.body.classList.toggle("recording", rec.active);
    $("btn-rec").disabled = rec.active && busy;   // never cut the data during countdown or fire
    $("btn-rec").title = rec.active && busy ? "Recording cannot be stopped during countdown or fire" : "";
    $("rec-session").textContent = rec.active ? rec.session : "";
    $("rec-detail").textContent = rec.active
      ? `${rec.samples} readings saved` + (rec.auto_stop_pending ? ". Stops by itself shortly." : ".")
      : "Starts by itself at countdown.";
    renderResult(rec.last);

    renderSystems(s);
    if (!armModal.hidden) renderArmFacts(s);
    firstRender = false;

    // events
    if (s.events.length) {
      const ol = $("events");
      for (const e of s.events) {
        lastEventId = Math.max(lastEventId, e.id);
        const li = document.createElement("li");
        li.className = e.level;
        const time = new Date(e.t * 1000).toLocaleTimeString([], { hour12: false });
        li.innerHTML = `<span class="t">${time}</span><span class="s">${e.source}</span><span class="m"></span>`;
        li.lastChild.textContent = String(e.message).replace(/\S*[\\/](\d{8}_\d{6})\b/g, "$1");
        ol.prepend(li);
      }
      while (ol.children.length > 300) ol.lastChild.remove();
    }
  }

  // ---------------- last test result ----------------
  function renderResult(L) {
    const box = $("result");
    if (!L) { box.hidden = true; return; }
    box.hidden = false;
    if (lastResult === L.session) return;
    const isNew = lastResult !== null || (!firstRender && lastResult === null);
    lastResult = L.session;
    const burned = L.fired && L.peak_pressure_bar >= Math.max(0.5, CFG.alarm * 0.01) && !(L.time_to_peak_s < 0);
    $("r-tag").textContent = !L.fired ? "Not fired" : burned ? "Fired" : "No burn";
    $("r-tag").className = "result-tag " + (burned ? "fired" : "idle");
    $("r-session").textContent = new Date(L.stopped || L.started).toLocaleString([], { hour12: false, dateStyle: "medium", timeStyle: "short" });
    $("r-peak").textContent = fmt(L.peak_pressure_bar, 2);
    const unit = (id, v) => { $(id).parentElement.innerHTML = v > 0 ? `<b id="${id}">${fmt(v, 2)}</b> s` : `<b id="${id}">–</b>`; };
    unit("r-burn", burned ? L.burn_time_s_estimate : 0); unit("r-ttp", burned ? L.time_to_peak_s : 0);
    $("r-samples").textContent = L.samples.toLocaleString();
    $("r-files").textContent = `Saved: pressure, events` + (L.cameras && L.cameras.length ? `, ${L.cameras.length} camera video${L.cameras.length > 1 ? "s" : ""}` : "") + ` in ${L.session}`;
    if (isNew) { box.classList.remove("fresh"); void box.offsetWidth; box.classList.add("fresh"); }
  }
  $("r-open").addEventListener("click", () => api("/api/session/open"));

  // ---------------- systems bar ----------------
  const SHORT = { ESP32: "ESP", Sensor: "SNS", Relay: "RLY", Simulation: "SIM" };
  function chip(el, text, cls, title, short) {
    const sh = short || SHORT[text] || text.slice(0, 5);
    el.innerHTML = `<span class="f"></span><span class="s"></span>`;
    el.firstChild.textContent = text; el.lastChild.textContent = sh;
    el.className = "chip " + cls; el.title = title || text;
  }
  function renderSystems(s) {
    const t = s.telemetry, hw = s.ignition.hardware;
    if (t.port_error) chip($("chip-net"), "ESP32", "bad", t.port_error);
    else if (t.link === "LIVE") chip($("chip-net"), "ESP32", "ok", `Connected from ${t.remote}`);
    else if (t.link === "NO DATA" || t.link === "STALE") chip($("chip-net"), "ESP32", "warn", t.link);
    else chip($("chip-net"), "ESP32", "bad", "ESP32 not connected. Run the system check.");
    if (t.link !== "LIVE") chip($("chip-sensor"), "Sensor", t.link === "NO DATA" ? "bad" : "", "No pressure data");
    else if (t.voltage_v !== null && (t.voltage_v < 0.01 || t.voltage_v > 4.9)) chip($("chip-sensor"), "Sensor", "warn", `${t.voltage_v} V`);
    else chip($("chip-sensor"), "Sensor", "ok", `${t.rate_hz} Hz`);
    if (!s.ignition.physical) chip($("chip-ign"), "Simulation", "warn", "Ignition is in simulation: no relay switches");
    else if (hw.module) chip($("chip-ign"), "Relay", "ok", `ESP32 relay reports ${hw.state}`);
    else chip($("chip-ign"), "Relay", "bad", "The ESP32 relay is not answering");
    const box = $("chip-cams");
    const cams = s.cameras.filter((c) => c.status !== "DISABLED");   // only cameras in use
    if (box.childElementCount !== cams.length) {
      box.innerHTML = "";
      for (const c of cams) { const e = document.createElement("button"); e.id = "chip-cam-" + c.id; box.appendChild(e); }
    }
    for (const c of cams) {
      const e = $("chip-cam-" + c.id);
      chip(e, c.name, c.status === "LIVE" ? "ok" : c.status === "DISABLED" ? "" : "bad", c.error || c.status, "CAM");
    }
    const h = s.hardware, he = $("chip-hw");
    he.hidden = !h || !h.report;
    if (h && h.report) {
      if (h.no_code) chip(he, `${h.no_code} new device${h.no_code > 1 ? "s" : ""}`, "warn", `${h.no_code} connected with no code`, `HW ${h.no_code}`);
      else if (h.missing) chip(he, `${h.missing} missing`, "bad", `${h.missing} used in code but not answering`, `HW ${h.missing}`);
      else chip(he, "Hardware", "ok", "Everything connected is used by the code", "HW");
    }
  }
  document.querySelector(".systems").addEventListener("click", (e) => { const c = e.target.closest("button.chip"); if (c) openCheck(); });

  // ---------------- modals: focus in, Tab stays inside, focus back on close ----------------
  let modalReturn = null;
  function openModal(m, focusSel) {
    modalReturn = document.activeElement;
    m.hidden = false;
    const f = (focusSel && m.querySelector(focusSel)) || m.querySelector("button, input, a");
    if (f) f.focus();
  }
  function closeModal(m) { m.hidden = true; if (modalReturn && modalReturn.focus) modalReturn.focus(); }
  const openModalEl = () => document.querySelector(".modal:not([hidden])");
  document.addEventListener("keydown", (e) => {
    const m = openModalEl();
    if (!m || e.key !== "Tab") return;
    const items = [...m.querySelectorAll("button:not([disabled]), input, a[href]")];
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  // ---------------- system check ----------------
  const checkModal = $("check-modal");
  const escH = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  async function openCheck() {
    if (checkModal.hidden) openModal(checkModal, "#check-close");
    $("check-list").innerHTML = '<li class="muted">Checking network, ESP32, firewall, cameras… (a few seconds)</li>';
    $("check-summary").textContent = ""; $("check-fw").hidden = true;
    let j;
    try { j = await (await fetch("/api/check", { cache: "no-store" })).json(); }
    catch (e) { $("check-list").innerHTML = '<li class="muted">Server not reachable.</li>'; return; }
    $("check-summary").textContent = "· " + j.summary;
    $("check-list").innerHTML = j.checks.map((c) =>
      `<li class="${c.status}"><span class="st">${c.status}</span><div><b>${escH(c.name)}</b>` +
      `<div class="d">${escH(c.detail)}</div>${c.fix ? `<div class="fix">${escH(c.fix)}</div>` : ""}</div></li>`).join("");
    $("check-fw").hidden = !j.checks.some((c) => c.id === "firewall" && (c.status === "FAIL" || c.status === "WARN"));
  }
  $("btn-check").addEventListener("click", openCheck);
  $("check-again").addEventListener("click", openCheck);
  $("check-close").addEventListener("click", () => closeModal(checkModal));
  $("check-fw").addEventListener("click", async () => {
    $("check-fw").disabled = true;
    $("check-summary").textContent = "· Accept the Windows prompt…";
    const j = await api("/api/check/fix-firewall");
    $("check-fw").disabled = false;
    if (j.ok) toast(j.message);
    openCheck();
  });

  // ---------------- relay test ----------------
  async function relayTest(which) {
    const j = await api("/api/relay-test/" + which);
    if (j.ok) $("relay-note").textContent = `${which.toUpperCase()} relay clicked for ${j.pulse_ms} ms.`;
  }
  $("btn-relay-arm").addEventListener("click", () => relayTest("arm"));
  $("btn-relay-fire").addEventListener("click", () => relayTest("fire"));

  // ---------------- countdown + clock (smooth, 60 fps) ----------------
  function frame() {
    $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false });
    if (countdownEnd !== null) {
      const left = (countdownEnd - performance.now()) / 1000;
      $("countdown").textContent = left > 0 ? "T−" + mmss(Math.ceil(left)) : "T+00:00";
      $("tminus").textContent = left > 0 ? "T−" + left.toFixed(1) : "T+0";
    } else {
      $("tminus").textContent = state && state.ignition.state === "FIRING" ? "FIRE" : "";
      if (state && state.ignition.state !== "FIRING") $("countdown").textContent = document.body.classList.contains("safed") ? "T+00:00" : "T−" + mmss(state.ignition.countdown_s);
    }
    requestAnimationFrame(frame);
  }

  function mmss(sec) { sec = Math.max(0, sec | 0); return String(Math.floor(sec / 60)).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0"); }

  // ---------------- chart ----------------
  const canvas = $("chart"), ctx = canvas.getContext("2d");
  async function chartLoop() {
    try {
      const r = await fetch(`/api/history?s=${chartWin}`, { cache: "no-store" });
      const j = await r.json();
      drawChart(j.points, j.server_time);
    } catch (e) { /* keep last picture */ }
    setTimeout(chartLoop, 250);
  }

  function drawChart(pts, now) {
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if (canvas.width !== W * dpr || canvas.height !== H * dpr) { canvas.width = W * dpr; canvas.height = H * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const L = 44, R = 10, T = 10, B = 22, pw = W - L - R, ph = H - T - B;
    let ymax = 5;
    for (const p of pts) if (p[1] > ymax) ymax = p[1];
    ymax = niceMax(ymax * 1.15);   // scale follows the data; the alarm is marked at the edge when above it
    const x = (t) => L + pw * (1 - (now - t) / chartWin);
    const y = (v) => T + ph * (1 - v / ymax);
    ctx.font = "400 12px Barlow, system-ui, sans-serif";
    ctx.strokeStyle = "rgba(255,255,255,.09)"; ctx.fillStyle = "rgba(255,255,255,.6)"; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const v = ymax * i / 4, yy = y(v);
      ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(W - R, yy); ctx.stroke();
      ctx.fillText(v.toFixed(v < 10 ? 1 : 0), 4, yy + 4);
    }
    const step = chartWin <= 10 ? 2 : chartWin <= 30 ? 5 : 10;
    for (let s = 0; s <= chartWin; s += step) {
      const xx = L + pw * (1 - s / chartWin);
      ctx.fillText(s === 0 ? "now" : `-${s}s`, xx - 12, H - 6);
    }
    if (CFG.alarm < ymax) {
      ctx.strokeStyle = "rgba(255,77,85,.75)"; ctx.setLineDash([6, 5]);
      ctx.beginPath(); ctx.moveTo(L, y(CFG.alarm)); ctx.lineTo(W - R, y(CFG.alarm)); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#ff9ca1"; ctx.fillText(`alarm ${CFG.alarm}`, W - R - 62, y(CFG.alarm) - 5);
    } else {
      ctx.fillStyle = "#ff9ca1"; ctx.fillText(`Alarm line (${CFG.alarm} bar) is above this scale`, L + 6, T + 12);
    }
    if (!pts.length) return;
    ctx.beginPath();
    pts.forEach((p, i) => { const xx = x(p[0]), yy = y(p[1]); i ? ctx.lineTo(xx, yy) : ctx.moveTo(xx, yy); });
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.stroke();
    const area = ctx.createLinearGradient(0, T, 0, T + ph);
    area.addColorStop(0, "rgba(255,255,255,.14)"); area.addColorStop(1, "rgba(255,255,255,0)");
    ctx.lineTo(x(pts[pts.length - 1][0]), y(0)); ctx.lineTo(x(pts[0][0]), y(0)); ctx.closePath();
    ctx.fillStyle = area; ctx.fill();
  }
  function niceMax(v) {
    const m = Math.pow(10, Math.floor(Math.log10(v)));
    for (const k of [1, 2, 2.5, 5, 10]) if (k * m >= v) return k * m;
    return 10 * m;
  }
  document.querySelectorAll(".seg.win").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".seg.win").forEach((x) => x.classList.remove("on"));
    b.classList.add("on"); chartWin = +b.dataset.win;
  }));

  // ---------------- ignition controls ----------------
  const armModal = $("arm-modal"), checks = armModal.querySelectorAll(".chk");
  function renderArmFacts(s) {
    const t = s.telemetry, ign = s.ignition;
    const cams = s.cameras.filter((c) => c.status !== "DISABLED");
    const live = cams.filter((c) => c.status === "LIVE").length;
    const facts = [
      [t.link === "LIVE", t.link === "LIVE" ? `Pressure data live: ${fmt(t.pressure_bar, 2)} bar` : `Pressure data ${t.link.toLowerCase()}`],
      [live === cams.length && cams.length > 0, cams.length ? `Cameras live: ${live} of ${cams.length}` : "No camera turned on"],
      [true, s.recorder.active ? "Recording is running" : "Recording starts by itself at countdown"],
      [!ign.physical || ign.hardware.module, ign.physical ? (ign.hardware.module ? "ESP32 relay answering" : "ESP32 relay not answering") : "Simulation: no relay will switch"],
    ];
    $("arm-facts").innerHTML = facts.map(([ok, txt]) => `<li class="${ok ? "ok" : "warn"}">${escH(txt)}</li>`).join("");
    $("arm-mode").textContent = S.mode(ign);
    $("arm-mode").className = "mode-tag " + (ign.physical ? "live" : "sim");
    $("arm-confirm").textContent = ign.physical ? "ARM live relay" : "ARM simulation";
  }
  $("btn-arm").addEventListener("click", () => {
    if (state && state.ignition.state === "ARMED") { api("/api/ignition/disarm"); return; }
    checks.forEach((c) => (c.checked = false));
    $("arm-confirm").disabled = true;
    if (state) renderArmFacts(state);
    openModal(armModal, ".chk");
  });
  checks.forEach((c) => c.addEventListener("change", () => {
    $("arm-confirm").disabled = ![...checks].every((x) => x.checked);
  }));
  $("arm-cancel").addEventListener("click", () => closeModal(armModal));
  $("arm-confirm").addEventListener("click", async () => { closeModal(armModal); await api("/api/ignition/arm"); });

  // hold-to-start
  const startBtn = $("btn-start"), holdFill = $("hold-fill");
  let holdStart = null, holdTimer = null;
  function holdTick() {
    if (holdStart === null) return;
    const held = (performance.now() - holdStart) / 1000;
    holdFill.style.width = Math.min(100, (held / CFG.hold_s) * 100) + "%";
    if (held >= CFG.hold_s) { holdStart = null; holdFill.style.width = "0"; api("/api/ignition/start", { held_s: held }); return; }
    holdTimer = requestAnimationFrame(holdTick);
  }
  function holdCancel() { holdStart = null; cancelAnimationFrame(holdTimer); holdFill.style.width = "0"; }
  startBtn.addEventListener("pointerdown", (e) => {
    if (startBtn.disabled) return;
    startBtn.setPointerCapture(e.pointerId);
    holdStart = performance.now(); holdTick();
  });
  ["pointerup", "pointercancel", "lostpointercapture"].forEach((ev) => startBtn.addEventListener(ev, holdCancel));
  // keyboard: hold Enter on the focused button
  startBtn.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.repeat || startBtn.disabled) return;
    e.preventDefault(); holdStart = performance.now(); holdTick();
  });
  startBtn.addEventListener("keyup", (e) => { if (e.key === "Enter") holdCancel(); });
  startBtn.addEventListener("blur", holdCancel);

  const doAbort = () => { holdCancel(); api("/api/ignition/abort"); };
  $("btn-abort").addEventListener("click", doAbort);
  // Esc always aborts (and closes an open window). Space aborts unless a control is selected.
  const CONTROLS = ["BUTTON", "INPUT", "TEXTAREA", "SELECT", "A", "SUMMARY"];
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      const m = openModalEl();
      if (m) closeModal(m);
      if (!m || (state && state.ignition.state !== "SAFE")) doAbort();
    } else if (e.code === "Space" && !CONTROLS.includes(document.activeElement && document.activeElement.tagName)) {
      e.preventDefault(); doAbort();
    }
  });

  // ---------------- other controls ----------------
  $("btn-zero").addEventListener("click", () => api("/api/pressure/zero"));
  $("btn-clearzero").addEventListener("click", () => api("/api/pressure/clear-zero"));
  $("btn-peak").addEventListener("click", () => api("/api/pressure/reset-peak"));
  $("btn-rec").addEventListener("click", () => api(state && state.recorder.active ? "/api/record/stop" : "/api/record/start"));
  $("note-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("note").value.trim();
    if (v) api("/api/note", { text: v });
    $("note").value = "";
  });

  // camera images reconnect by themselves
  document.querySelectorAll(".cam img").forEach((img) => {
    img.addEventListener("error", () => setTimeout(() => (img.src = img.dataset.src + "?t=" + Date.now()), 3000));
  });

  // ---------------- updates + power ----------------
  const updModal = $("update-modal"), updBody = $("upd-body"), updApply = $("upd-apply");
  const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  $("upd-close").addEventListener("click", () => closeModal(updModal));
  $("btn-update").addEventListener("click", async () => {
    openModal(updModal, "#upd-close"); updApply.hidden = true;
    $("upd-title").textContent = "Updates";
    updBody.textContent = "Checking GitHub for a new version…";
    let j;
    try { j = await (await fetch("/api/update/check", { method: "POST" })).json(); }
    catch (e) { updBody.textContent = "Server not reachable."; return; }
    if (!j.ok) { updBody.textContent = j.error; return; }
    const cur = `Installed: v${esc(j.version)} (${esc(j.commit || "?")}, branch ${esc(j.branch)})`;
    if (!j.update_available) { updBody.innerHTML = `<b>You have the latest version.</b><span class="muted">${cur}</span>`; return; }
    updBody.innerHTML = `<b>${j.behind} update(s) available` + (j.remote_version ? ` — v${esc(j.remote_version)}` : "") +
      `</b><span class="muted">${cur}</span><ul>` +
      j.changes.map((c) => `<li>${esc(c.date)} · ${esc(c.title)}</li>`).join("") + "</ul>" +
      `<span class="muted">The console restarts after the update (about 10 seconds). Ignition must be SAFE.</span>`;
    updApply.hidden = false;
  });
  updApply.addEventListener("click", async () => {
    updApply.hidden = true;
    updBody.textContent = "Downloading and installing the update…";
    const j = await api("/api/update/apply");
    if (!j.ok) { updBody.textContent = j.error || "Update failed"; return; }
    updBody.textContent = `Updated ${j.from} → ${j.to}. Restarting…`;
    const t0 = Date.now();
    await new Promise((r) => setTimeout(r, 2500));
    const wait = async () => {
      try { const r = await fetch("/health", { cache: "no-store" }); if (r.ok) { location.reload(); return; } } catch (e) {}
      if (Date.now() - t0 > 60000) { updBody.textContent = "Restart is taking long. Close and open SFT STELLAR again."; return; }
      setTimeout(wait, 1000);
    };
    wait();
  });
  $("btn-power").addEventListener("click", async () => {
    if (!window.confirm("Close SFT STELLAR? Cameras and telemetry will stop.")) return;
    const j = await api("/api/system/shutdown");
    if (j.ok) document.body.innerHTML = '<div class="closed">SFT STELLAR is closed. You can close this window.</div>';
  });

  poll(); chartLoop(); requestAnimationFrame(frame);
})();
