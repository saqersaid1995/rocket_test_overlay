/* Shared status for Hardware, Firmware and Settings: ignition state, recording,
   T-minus, a state bar with ABORT when not SAFE, and Esc = ABORT. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let state = null, countdownEnd = null, lastIgn = null;

  window.STELLAR = window.STELLAR || {};
  const status = window.STELLAR.status = window.STELLAR.status || {};

  status.pill = (el, label, value, cls) => {
    if (!el) return;
    el.className = "pill " + (cls || "");
    el.innerHTML = `<span>${label}</span><b>${value}</b>`;
  };
  status.ignClass = (st) => ({ SAFE: "ok", ARMED: "warn", COUNTDOWN: "fire", FIRING: "fire", TEST: "info" }[st] || "bad");
  status.mode = (ign) => ign.physical ? "Live relay" : "Simulation";
  status.announce = (st, ign) => {
    if (st === lastIgn) return;
    const words = { SAFE: "Ignition safe", ARMED: "Ignition armed", COUNTDOWN: "Countdown started", FIRING: "Fire", TEST: "Relay test" };
    if (lastIgn !== null && $("sr-live")) $("sr-live").textContent = words[st] || st;
    lastIgn = st;
  };
  status.flash = (word, cls) => {
    const fx = $("fx");
    if (!fx) return;
    $("fx-word").textContent = word;
    fx.className = "fx on " + cls;
  };
  status.clearFx = () => { const fx = $("fx"); if (fx) fx.className = "fx"; };

  if (document.body.classList.contains("page-console")) return;   // console.js drives the console

  async function abort() {
    try { await fetch("/api/ignition/abort", { method: "POST" }); } catch (e) { /* retried by next press */ }
  }

  async function poll() {
    try {
      const r = await fetch("/api/state?events_after=999999999", { cache: "no-store" });
      state = await r.json();
      render(state);
    } catch (e) { /* server restarting */ }
    setTimeout(poll, 400);
  }

  function render(s) {
    const ign = s.ignition, rec = s.recorder, st = ign.state;
    status.pill($("pill-ign"), ign.physical ? "Ignition" : "Ignition (sim)", st, status.ignClass(st));
    status.pill($("pill-rec"), "Recording", rec.active ? `${rec.elapsed_s}s` : "Off", rec.active ? "rec" : "");
    document.body.dataset.state = st;
    status.announce(st, ign);
    if (st === "COUNTDOWN" && ign.t_minus !== null) countdownEnd = performance.now() + ign.t_minus * 1000;
    else if (st !== "FIRING") countdownEnd = null;
    const bar = $("statebar");
    bar.hidden = st === "SAFE";
    bar.className = "statebar " + status.ignClass(st);
    $("sb-state").textContent = st;
    $("sb-detail").textContent = st === "ARMED" && ign.arm_remaining_s !== null
      ? `${status.mode(ign)}. Auto-disarm in ${ign.arm_remaining_s} s.` : status.mode(ign);
    if (st === "FIRING") status.flash("FIRE", "fire"); else status.clearFx();
  }

  function frame() {
    $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false });
    const txt = countdownEnd !== null
      ? ((countdownEnd - performance.now()) / 1000 > 0 ? "T-" + ((countdownEnd - performance.now()) / 1000).toFixed(1) : "T+0")
      : state && state.ignition.state === "FIRING" ? "FIRE" : "";
    $("tminus").textContent = txt;
    if (countdownEnd !== null) $("sb-detail").textContent = txt;
    requestAnimationFrame(frame);
  }

  $("sb-abort").addEventListener("click", abort);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state && state.ignition.state !== "SAFE") { e.preventDefault(); abort(); }
  });
  poll();
  requestAnimationFrame(frame);
})();
