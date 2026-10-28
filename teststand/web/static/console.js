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

  const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);

  function pill(el, label, value, cls) {
    el.className = "pill " + (cls || "");
    el.innerHTML = `<span>${label}</span><b>${value}</b>`;
  }

  function render(s) {
    const t = s.telemetry, ign = s.ignition, rec = s.recorder;

    // top bar
    pill($("pill-link"), "Telemetry", t.link, t.link === "LIVE" ? "ok" : (t.link === "STALE" || t.link === "NO DATA") ? "warn" : "bad");
    pill($("pill-ign"), ign.physical ? "Ignition" : "Ignition (sim)", ign.state,
      ign.state === "SAFE" ? "ok" : ign.state === "ARMED" ? "warn" : "bad");
    pill($("pill-rec"), "Recording", rec.active ? `${rec.elapsed_s}s` : "Off", rec.active ? "rec" : "");

    // pressure
    const p = t.pressure_bar;
    $("p-now").textContent = fmt(p, 2);
    $("p-psi").textContent = p === null ? "—" : fmt(p * 14.5038, 1);
    $("p-peak").textContent = fmt(t.peak_bar, 2);
    $("p-tare").textContent = fmt(t.tare_bar, 3);
    const ro = document.querySelector(".readout");
    ro.classList.toggle("alarm", p !== null && p >= CFG.alarm);
    ro.classList.toggle("stale", t.link !== "LIVE");
    $("p-fill").style.width = p === null ? "0" : Math.min(100, (p / CFG.max) * 100) + "%";
    $("p-alarm").style.left = Math.min(100, (CFG.alarm / CFG.max) * 100) + "%";
    $("p-meta").textContent = t.firmware ? `${t.firmware} · ${t.remote || ""}` : "waiting for ESP32…";
    $("link-detail").textContent = t.port_error ? t.port_error
      : t.link === "OFFLINE" ? `No connection from the ESP32 (listening on port ${t.port})`
      : `${t.rate_hz} Hz · ${t.samples} samples · gaps ${t.gaps} · bad frames ${t.bad_frames}` +
        (t.voltage_v !== null ? ` · ${fmt(t.voltage_v, 4)} V` : "");

    // ignition
    document.body.dataset.state = ign.state;
    $("ign-state").textContent = ign.state;
    $("ign-driver").textContent = ign.physical ? "ESP32 relay output" : "Simulation, no output";
    const hw = ign.hardware;
    $("ign-hw").textContent = ign.physical
      ? `Controller: ${hw.state}` + (hw.age_s !== null ? ` (${hw.age_s}s ago)` : " (no status yet)") +
        (hw.fire_count !== undefined && hw.fire_count !== null ? ` · fires ${hw.fire_count}` : "")
      : `Countdown ${ign.countdown_s}s · pulse ${ign.pulse_ms} ms`;
    if (ign.state === "COUNTDOWN" && ign.t_minus !== null) countdownEnd = performance.now() + ign.t_minus * 1000;
    else if (ign.state !== "FIRING") countdownEnd = null;

    $("btn-arm").textContent = ign.state === "ARMED" ? "DISARM" : "ARM";
    $("btn-arm").disabled = !(ign.state === "SAFE" || ign.state === "ARMED");
    $("btn-start").disabled = ign.state !== "ARMED";
    $("btn-relay-arm").disabled = $("btn-relay-fire").disabled = ign.state !== "SAFE";
    let note = "";
    if (ign.state === "ARMED" && ign.arm_remaining_s !== null) note = `Auto-disarm in ${ign.arm_remaining_s}s if no action.`;
    else if (ign.state === "SAFE" && ign.last_abort_reason) note = `Last abort: ${ign.last_abort_reason}`;
    else if (ign.state === "SAFE" && t.link !== "LIVE") note = "ARM is blocked until pressure data is LIVE (see SYSTEMS above).";
    $("ign-note").textContent = note;

    // cameras
    for (const c of s.cameras) {
      const fig = $("cam-" + c.id);
      if (!fig) continue;
      const st = fig.querySelector(".cam-status");
      if (c.recording) { st.textContent = `● REC ${c.record_s}s`; st.className = "cam-status rec"; }
      else { st.textContent = c.record_failed ? "REC FAILED" : c.status; st.className = "cam-status " + (c.status === "LIVE" ? "ok" : "bad"); }
    }

    // recorder
    $("btn-rec").textContent = rec.active ? "Stop recording" : "Start recording";
    $("rec-session").textContent = rec.active ? rec.session : "";
    $("rec-detail").textContent = rec.active
      ? `${rec.samples} samples` + (rec.auto_stop_pending ? " · auto-stop scheduled" : "")
      : "Starts by itself at countdown (auto-record).";
    if (rec.last) {
      const L = rec.last;
      $("rec-last").textContent = `Last test ${L.session}: peak ${L.peak_pressure_bar} bar` +
        (L.burn_time_s_estimate ? ` · burn ≈ ${L.burn_time_s_estimate}s` : "") +
        (L.fired ? "" : " · not fired") + ` · ${L.samples} samples`;
    }

    renderSystems(s);

    // events
    if (s.events.length) {
      const ol = $("events");
      for (const e of s.events) {
        lastEventId = Math.max(lastEventId, e.id);
        const li = document.createElement("li");
        li.className = e.level;
        const time = new Date(e.t * 1000).toLocaleTimeString([], { hour12: false });
        li.innerHTML = `<span class="t">${time}</span><span class="s">${e.source}</span>`;
        li.appendChild(document.createTextNode(e.message));
        ol.prepend(li);
      }
      while (ol.children.length > 300) ol.lastChild.remove();
    }
  }

  // ---------------- systems bar ----------------
  function chip(el, text, cls, title) { el.textContent = text; el.className = "chip " + cls; el.title = title || ""; }
  function renderSystems(s) {
    const t = s.telemetry, hw = s.ignition.hardware;
    if (t.port_error) chip($("chip-net"), "ESP32 link", "bad", t.port_error);
    else if (t.link === "LIVE") chip($("chip-net"), "ESP32 link", "ok", `Connected from ${t.remote}`);
    else if (t.link === "NO DATA" || t.link === "STALE") chip($("chip-net"), "ESP32 link", "warn", t.link);
    else chip($("chip-net"), "ESP32 link", "bad", "ESP32 not connected — run the system check");
    if (t.link !== "LIVE") chip($("chip-sensor"), "Sensor", t.link === "NO DATA" ? "bad" : "", "No pressure data");
    else if (t.voltage_v !== null && (t.voltage_v < 0.01 || t.voltage_v > 4.9)) chip($("chip-sensor"), "Sensor", "warn", `${t.voltage_v} V`);
    else chip($("chip-sensor"), "Sensor", "ok", `${t.rate_hz} Hz`);
    if (!s.ignition.physical) chip($("chip-ign"), "Ignition (simulation)", "warn", "Simulation: no relay switches");
    else if (hw.module) chip($("chip-ign"), "Ignition", "ok", `ESP32 ignition module: ${hw.state}`);
    else chip($("chip-ign"), "Ignition", "bad", "No ignition module answering on the ESP32");
    const box = $("chip-cams");
    if (box.childElementCount !== s.cameras.length) {
      box.innerHTML = "";
      for (const c of s.cameras) { const e = document.createElement("span"); e.id = "chip-cam-" + c.id; box.appendChild(e); }
    }
    for (const c of s.cameras) {
      const e = $("chip-cam-" + c.id);
      chip(e, c.name, c.status === "LIVE" ? "ok" : c.status === "DISABLED" ? "" : "bad", c.error || c.status);
    }
    const h = s.hardware, he = $("chip-hw");
    he.hidden = !h || !h.report;
    if (h && h.report) {
      if (h.no_code) chip(he, `Hardware: ${h.no_code} with no code`, "warn", "Something is connected that the code does not use");
      else if (h.missing) chip(he, `Hardware: ${h.missing} missing`, "bad", "A device the code uses does not answer");
      else chip(he, "Hardware", "ok", "Everything connected is used by the code");
    }
  }
  document.querySelector(".sysbar").addEventListener("click", (e) => { if (e.target.classList.contains("chip") && e.target.id !== "chip-hw") openCheck(); });

  // ---------------- system check ----------------
  const checkModal = $("check-modal");
  const escH = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  async function openCheck() {
    checkModal.hidden = false;
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
  $("check-close").addEventListener("click", () => (checkModal.hidden = true));
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
    if (j.ok) $("relay-note").textContent = `${which.toUpperCase()} relay clicked ${j.pulse_ms} ms`;
  }
  $("btn-relay-arm").addEventListener("click", () => relayTest("arm"));
  $("btn-relay-fire").addEventListener("click", () => relayTest("fire"));

  // ---------------- countdown + clock (smooth, 60 fps) ----------------
  function frame() {
    $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false });
    if (countdownEnd !== null) {
      const left = (countdownEnd - performance.now()) / 1000;
      const txt = left > 0 ? "T-" + left.toFixed(1) : "T+0";
      $("countdown").textContent = txt;
      $("tminus").textContent = txt;
    } else {
      $("tminus").textContent = state && state.ignition.state === "FIRING" ? "FIRE" : "";
    }
    requestAnimationFrame(frame);
  }

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
    ymax = niceMax(Math.max(ymax * 1.1, state && state.ignition.state !== "SAFE" ? CFG.alarm * 1.1 : 0));
    const x = (t) => L + pw * (1 - (now - t) / chartWin);
    const y = (v) => T + ph * (1 - v / ymax);
    ctx.font = "500 12px Barlow, system-ui, sans-serif";
    ctx.strokeStyle = "#1d2732"; ctx.fillStyle = "#7a8899"; ctx.lineWidth = 1;
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
      ctx.strokeStyle = "rgba(240,68,68,.65)"; ctx.setLineDash([6, 4]);
      ctx.beginPath(); ctx.moveTo(L, y(CFG.alarm)); ctx.lineTo(W - R, y(CFG.alarm)); ctx.stroke();
      ctx.setLineDash([]);
    }
    if (!pts.length) return;
    ctx.beginPath();
    pts.forEach((p, i) => { const xx = x(p[0]), yy = y(p[1]); i ? ctx.lineTo(xx, yy) : ctx.moveTo(xx, yy); });
    ctx.strokeStyle = "#36c6e8"; ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.stroke();
    const area = ctx.createLinearGradient(0, T, 0, T + ph);
    area.addColorStop(0, "rgba(54,198,232,.18)"); area.addColorStop(1, "rgba(54,198,232,0)");
    ctx.lineTo(x(pts[pts.length - 1][0]), y(0)); ctx.lineTo(x(pts[0][0]), y(0)); ctx.closePath();
    ctx.fillStyle = area; ctx.fill();
  }
  function niceMax(v) {
    const m = Math.pow(10, Math.floor(Math.log10(v)));
    for (const k of [1, 2, 2.5, 5, 10]) if (k * m >= v) return k * m;
    return 10 * m;
  }
  document.querySelectorAll(".seg").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".seg").forEach((x) => x.classList.remove("on"));
    b.classList.add("on"); chartWin = +b.dataset.win;
  }));

  // ---------------- ignition controls ----------------
  const armModal = $("arm-modal"), checks = armModal.querySelectorAll(".chk");
  $("btn-arm").addEventListener("click", () => {
    if (state && state.ignition.state === "ARMED") { api("/api/ignition/disarm"); return; }
    checks.forEach((c) => (c.checked = false));
    $("arm-confirm").disabled = true;
    armModal.hidden = false;
  });
  checks.forEach((c) => c.addEventListener("change", () => {
    $("arm-confirm").disabled = ![...checks].every((x) => x.checked);
  }));
  $("arm-cancel").addEventListener("click", () => (armModal.hidden = true));
  $("arm-confirm").addEventListener("click", async () => { armModal.hidden = true; await api("/api/ignition/arm"); });

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

  const doAbort = () => { holdCancel(); api("/api/ignition/abort"); };
  $("btn-abort").addEventListener("click", doAbort);
  document.addEventListener("keydown", (e) => {
    const typing = document.activeElement && document.activeElement.tagName === "INPUT";
    if (e.key === "Escape" || (e.code === "Space" && !typing)) { e.preventDefault(); doAbort(); }
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
  $("upd-close").addEventListener("click", () => (updModal.hidden = true));
  $("btn-update").addEventListener("click", async () => {
    updModal.hidden = false; updApply.hidden = true;
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
