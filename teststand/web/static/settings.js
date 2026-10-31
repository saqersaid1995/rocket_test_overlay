/* SFT STELLAR settings page */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  let original = null;   // settings as loaded
  let dirty = false;

  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }
  async function post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      return await r.json();
    } catch (e) { return { ok: false, error: "Server not reachable" }; }
  }

  // ---------------- tabs ----------------
  function showTab(name) {
    document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + name));
    document.querySelectorAll(".side-nav a").forEach((a) => a.classList.toggle("on", a.dataset.tab === name));
    if (name === "check" && !$("checks").childElementCount) runCheck();
  }
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1) || "check"));

  // ---------------- simple fields ----------------
  const fields = () => document.querySelectorAll(".tab [name]");
  function fill(s) {
    fields().forEach((el) => {
      const [sec, key] = el.name.split(".");
      const v = s[sec] ? s[sec][key] : undefined;
      if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? "";
    });
    const pc = s.pc_ip;
    $("esp-help").innerHTML =
      `On the ESP32 setup page (Wi-Fi <b>PressureMonitor</b> → <code>http://192.168.4.1</code>) set:<br>` +
      `• Stellar Ops receiver IP = <b class="ip">${esc(pc || "this PC's Ethernet IP")}</b><br>` +
      `• Receiver TCP port = <b class="ip">${esc(s.telemetry.listen_port)}</b><br>` +
      `then press <b>SAVE &amp; RESTART ETHERNET</b>.` +
      (pc ? "" : `<br><br>This PC has no address on the ESP32 network. Give its Ethernet card a fixed IP such as <code>192.168.1.10</code>.`);
  }
  function collect() {
    const out = {};
    fields().forEach((el) => {
      const [sec, key] = el.name.split(".");
      (out[sec] = out[sec] || {})[key] = el.type === "checkbox" ? el.checked : el.value;
    });
    out.cameras = [...document.querySelectorAll(".cam-card")].map(readCam);
    return out;
  }
  function markDirty() { dirty = true; $("savebar").hidden = false; }
  // camera-device controls are sent with "Apply to camera", not with Save
  const counts = (t) => t.closest(".tab:not(#tab-check)") && !t.closest(".device");
  document.addEventListener("input", (e) => { if (counts(e.target)) markDirty(); });
  document.addEventListener("change", (e) => { if (counts(e.target)) markDirty(); });

  // ---------------- cameras ----------------
  let camSeq = 0;
  function addCam(c) {
    const node = $("cam-tpl").content.firstElementChild.cloneNode(true);
    c = c || { name: "Camera " + (document.querySelectorAll(".cam-card").length + 1), enabled: true, brand: "generic" };
    c = { preview_width: 640, preview_fps: 10, ...c };
    node.dataset.id = c.id || "cam" + Date.now().toString(36) + (camSeq++);
    node.querySelectorAll("[data-f]").forEach((el) => {
      const v = c[el.dataset.f];
      if (el.type === "checkbox") el.checked = v !== false; else if (v !== undefined && v !== null) el.value = v;
    });
    const advanced = !c.ip && (c.url || c.preview_url || c.record_url);
    if (advanced) {
      if (!c.preview_url && c.url) node.querySelector('[data-f="preview_url"]').value = c.url;
      setMode(node, true);
    }
    $("cam-list").appendChild(node);
    return node;
  }
  function setMode(node, advanced) {
    node.querySelector(".simple").hidden = advanced;
    node.querySelector(".advanced").hidden = !advanced;
    node.querySelector('[data-act="mode"]').textContent = advanced ? "Use IP form" : "Use full URL";
    node.dataset.mode = advanced ? "url" : "ip";
  }
  function readCam(node) {
    const c = { id: node.dataset.id };
    const adv = node.dataset.mode === "url";
    node.querySelectorAll("[data-f]").forEach((el) => {
      const f = el.dataset.f;
      const inSimple = !!el.closest(".simple"), inAdv = !!el.closest(".advanced");
      if ((adv && inSimple) || (!adv && inAdv)) return;
      c[f] = el.type === "checkbox" ? el.checked : el.value.trim();
    });
    return c;
  }
  $("cam-list").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const node = btn.closest(".cam-card");
    const act = btn.dataset.act;
    if (act === "remove") { node.remove(); markDirty(); return; }
    if (act === "mode") { setMode(node, node.dataset.mode !== "url"); markDirty(); return; }
    if (act === "device") { loadDevice(node, btn); return; }
    if (act === "device-apply") { applyDevice(node, btn); return; }
    if (act === "onvif") {
      const ipEl = node.querySelector('[data-f="ip"]');
      const user = node.querySelector('[data-f="user"]').value.trim();
      let pass = node.querySelector('[data-f="password"]').value;
      const msg = node.querySelector(".test-msg");
      if (!ipEl.value.trim()) { msg.className = "small test-msg bad"; msg.textContent = "Enter the camera IP first"; return; }
      msg.className = "muted small test-msg"; msg.textContent = "Asking the camera for its streams…"; btn.disabled = true;
      const j = await post("/api/cameras/onvif", { ip: ipEl.value.trim(), user, password: pass === "********" ? "" : pass });
      btn.disabled = false;
      if (!j.ok || !j.streams || !j.streams.length) {
        msg.className = "small test-msg bad";
        msg.textContent = (j.error || "No streams returned") + (j.rtsp_ports && j.rtsp_ports.length ? ` · RTSP port open: ${j.rtsp_ports.join(", ")}` : "");
        return;
      }
      const withCred = (u) => user && !/\/\/[^/]*@/.test(u) ? u.replace("//", `//${encodeURIComponent(user)}:${encodeURIComponent(pass)}@`) : u;
      const px = (st) => (+st.width || 0) * (+st.height || 0);
      const sorted = [...j.streams].sort((a, b) => px(b) - px(a));
      setMode(node, true);
      node.querySelector('[data-f="record_url"]').value = withCred(sorted[0].uri);
      node.querySelector('[data-f="preview_url"]').value = withCred(sorted[sorted.length - 1].uri);
      msg.className = "small test-msg ok";
      msg.textContent = `${j.model || "Camera"}: ${j.streams.length} stream(s) found — press Test preview.`;
      markDirty();
      return;
    }
    if (act === "test" || act === "test-rec") {
      const c = readCam(node);
      const msg = node.querySelector(".test-msg"), img = node.querySelector(".test-img");
      const body = c.ip ? { ...c, which: act === "test" ? "preview" : "record" }
                        : { url: act === "test" ? (c.preview_url || c.record_url) : (c.record_url || c.preview_url) };
      msg.className = "muted small test-msg"; msg.textContent = "Connecting to the camera…"; btn.disabled = true;
      const j = await post("/api/cameras/test", body);
      btn.disabled = false;
      if (j.ok) {
        img.src = j.image; img.hidden = false;
        msg.className = "small test-msg ok"; msg.textContent = `Picture received in ${j.seconds}s · ${j.url}`;
      } else {
        img.hidden = true;
        msg.className = "small test-msg bad"; msg.textContent = j.error || "Failed";
      }
    }
  });
  // ---------------- camera device settings (Hikvision ISAPI) ----------------
  function camLogin(node) {
    const g = (f) => { const el = node.querySelector(`[data-f="${f}"]`); return el ? el.value.trim() : ""; };
    let ip = g("ip"), user = g("user"), password = node.querySelector('[data-f="password"]').value;
    if (!ip) {   // full-URL mode: take IP / user from the address
      const m = /^rtsp:\/\/(?:([^:@/]*)(?::([^@/]*))?@)?([^:/]+)/.exec(g("record_url") || g("preview_url"));
      if (m) { user = decodeURIComponent(m[1] || ""); password = decodeURIComponent(m[2] || ""); ip = m[3]; }
    }
    return { id: node.dataset.id, ip, user, password };
  }
  function opt(v, label, cur) { return `<option value="${esc(v)}"${String(v) === String(cur) ? " selected" : ""}>${esc(label)}</option>`; }
  async function loadDevice(node, btn) {
    const box = node.querySelector(".device");
    box.hidden = false; box.innerHTML = '<span class="muted small">Reading settings from the camera…</span>';
    btn.disabled = true;
    const j = await post("/api/cameras/device/read", camLogin(node));
    btn.disabled = false;
    if (!j.ok) { box.innerHTML = `<span class="small test-msg bad">${esc(j.error)}</span>`; return; }
    let html = `<div class="dev-h"><b>${esc(j.model || "Camera")}</b> <span class="muted small">firmware ${esc(j.firmware)}</span></div><div class="form">`;
    for (const [name, st] of Object.entries(j.streams)) {
      const label = name === "main" ? "Main stream (recording)" : "Sub stream";
      html += `<label>${label} — resolution<select data-dev="${name}.resolution">` +
        st.resolutions.map((r) => opt(r, r, st.resolution)).join("") + `</select></label>` +
        `<label>${label} — frame rate<select data-dev="${name}.fps">` +
        st.fps_options.map((f) => opt(f, f + " fps", st.fps)).join("") + `</select></label>`;
    }
    html += "</div>";
    if (j.osd) {
      html += `<div class="form"><label class="check"><input type="checkbox" data-dev="osd.date_time"${j.osd.date_time ? " checked" : ""}> Show date and time on the picture</label>` +
        `<label class="check"><input type="checkbox" data-dev="osd.camera_name"${j.osd.camera_name ? " checked" : ""}> Show camera name on the picture</label>`;
      for (const t of j.osd.texts) {
        html += `<label class="check"><input type="checkbox" data-dev-text="${esc(t.id)}"${t.enabled ? " checked" : ""}> Text ${esc(t.id)}: ` +
          `<input data-dev-textval="${esc(t.id)}" value="${esc(t.text)}" style="flex:1"></label>`;
      }
      html += "</div>";
    }
    if (j.errors.length) html += `<div class="small test-msg bad">${esc(j.errors.join(" · "))}</div>`;
    html += `<div class="row"><button class="small primary" data-act="device-apply">Apply to camera</button><span class="small muted dev-msg"></span></div>`;
    box.innerHTML = html;
  }
  async function applyDevice(node, btn) {
    const box = node.querySelector(".device");
    const changes = { streams: {}, osd: {} };
    box.querySelectorAll("select[data-dev]").forEach((el) => {
      const [name, key] = el.dataset.dev.split(".");
      (changes.streams[name] = changes.streams[name] || {})[key] = el.value;
    });
    box.querySelectorAll("input[type=checkbox][data-dev]").forEach((el) => (changes.osd[el.dataset.dev.split(".")[1]] = el.checked));
    const texts = [...box.querySelectorAll("[data-dev-text]")].map((el) => ({
      id: el.dataset.devText, enabled: el.checked,
      text: box.querySelector(`[data-dev-textval="${el.dataset.devText}"]`).value }));
    if (texts.length) changes.osd.texts = texts;
    if (!Object.keys(changes.osd).length) delete changes.osd;
    const msg = box.querySelector(".dev-msg");
    msg.textContent = "Sending to the camera…"; btn.disabled = true;
    const j = await post("/api/cameras/device/apply", { ...camLogin(node), changes });
    btn.disabled = false;
    msg.className = "small dev-msg " + (j.ok ? "test-msg ok" : "test-msg bad");
    msg.textContent = j.ok ? "Saved in the camera. The picture restarts in a few seconds." : j.error;
  }

  $("cam-add").addEventListener("click", () => { addCam().querySelector('[data-f="ip"]').focus(); markDirty(); });
  $("cam-scan").addEventListener("click", async () => {
    const box = $("scan-result");
    box.hidden = false; box.innerHTML = '<span class="muted">Searching the network for cameras (about 5 seconds)…</span>';
    $("cam-scan").disabled = true;
    const j = await post("/api/cameras/scan");
    $("cam-scan").disabled = false;
    if (!j.ok) { box.innerHTML = `<span class="muted">${esc(j.error)}</span>`; return; }
    if (!j.found.length) {
      box.innerHTML = '<span class="muted">No cameras found. Check that the camera and this PC are on the same network, or add it by IP.</span>';
      return;
    }
    box.innerHTML = '<b class="small muted">Found on the network:</b>' + j.found.map((f) =>
      `<div class="hit"><span>${esc(f.ip)}</span><span class="muted">${esc(f.info)}</span><span class="grow"></span>` +
      `<button class="small" data-ip="${esc(f.ip)}">Add</button></div>`).join("");
  });
  $("scan-result").addEventListener("click", (e) => {
    const ip = e.target.dataset && e.target.dataset.ip;
    if (!ip) return;
    const node = addCam({ name: "Camera " + ip.split(".").pop(), enabled: true, brand: "generic", ip });
    node.querySelector('[data-f="user"]').focus();
    markDirty();
  });

  // ---------------- load / save ----------------
  async function load() {
    const j = await (await fetch("/api/settings", { cache: "no-store" })).json();
    original = j.settings;
    fill(original);
    thrustUi();
    $("cam-list").innerHTML = "";
    (original.cameras || []).forEach(addCam);
    dirty = false; $("savebar").hidden = true;
  }
  $("discard").addEventListener("click", load);
  $("save").addEventListener("click", async () => {
    $("save").disabled = true; $("save-msg").textContent = "Saving…";
    const j = await post("/api/settings", collect());
    if (!j.ok) { $("save").disabled = false; $("save-msg").textContent = j.error; toast(j.error); return; }
    $("save-msg").textContent = "Saved. Restarting SFT STELLAR…";
    const t0 = Date.now();
    await new Promise((r) => setTimeout(r, 2500));
    const wait = async () => {
      try { const r = await fetch("/health", { cache: "no-store" }); if (r.ok) { location.reload(); return; } } catch (e) {}
      if (Date.now() - t0 > 60000) { $("save-msg").textContent = "Restart is taking long — open SFT STELLAR again."; return; }
      setTimeout(wait, 1000);
    };
    wait();
  });
  // leaving with unsaved changes: ask inside the page (Save / Don't save / Stay)
  document.querySelectorAll(".nav a").forEach((a) => a.addEventListener("click", (e) => {
    if (!dirty) return;
    e.preventDefault();
    $("leave-modal").hidden = false;
    $("leave-modal").dataset.href = a.getAttribute("href");
  }));
  $("leave-stay").addEventListener("click", () => ($("leave-modal").hidden = true));
  $("leave-discard").addEventListener("click", () => { dirty = false; location.href = $("leave-modal").dataset.href; });
  $("leave-save").addEventListener("click", () => { $("leave-modal").hidden = true; $("save").click(); });

  // ---------------- system check ----------------
  async function runCheck() {
    $("checks").innerHTML = '<li class="INFO"><span class="st">…</span><div><b>Checking</b><div class="d">Network, ESP32, firewall, cameras… a few seconds.</div></div></li>';
    $("check-run").disabled = true;
    let j;
    try { j = await (await fetch("/api/check", { cache: "no-store" })).json(); }
    catch (e) { j = { checks: [] }; }
    $("check-run").disabled = false;
    $("checks").innerHTML = j.checks.map((c) => {
      let btn = "";
      if (c.adapter) {
        const label = c.action === "dhcp" ? "Undo" : `Set Ethernet IP to ${c.ip}`;
        btn = `<div class="row" style="margin-top:6px"><button class="small primary" data-adapter="${esc(c.adapter)}" ` +
              `data-ip="${esc(c.ip || "")}" data-action="${esc(c.action)}">${esc(label)}</button></div>`;
      }
      return `<li class="${c.status}"><span class="st">${c.status}</span><div><b>${esc(c.name)}</b>` +
        `<div class="d">${esc(c.detail)}</div>${c.fix ? `<div class="fix">${esc(c.fix)}</div>` : ""}${btn}</div></li>`;
    }).join("");
    $("fw-fix").hidden = !j.checks.some((c) => c.id === "firewall" && (c.status === "FAIL" || c.status === "WARN"));
  }
  $("checks").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-adapter]");
    if (!b) return;
    const { adapter, ip, action } = b.dataset;
    const q = action === "dhcp" ? `Put '${adapter}' back to automatic address?`
                                : `Give '${adapter}' the fixed address ${ip} (mask 255.255.255.0)?`;
    if (!confirm(q + "\nWindows will ask for administrator permission.")) return;
    b.disabled = true; toast("Accept the Windows prompt (administrator)…");
    const j = await post("/api/check/set-ethernet", { adapter, ip, action });
    toast(j.message || j.error || "Done");
    setTimeout(runCheck, 2000);
  });
  $("check-run").addEventListener("click", runCheck);
  $("fw-fix").addEventListener("click", async () => {
    $("fw-fix").disabled = true; toast("Accept the Windows prompt (administrator)…");
    const j = await post("/api/check/fix-firewall");
    $("fw-fix").disabled = false;
    toast(j.message || j.error || "Done");
    runCheck();
  });

  // ---------------- thrust estimate ----------------
  const tq = (n) => document.querySelector(`[name="thrust.${n}"]`);
  const GAMMA = { KNSB: 1.04, KNDX: 1.04, KNSU: 1.044, APCP: 1.2 };
  let thrustTimer = null;
  function thrustUi() {
    const on = tq("enabled").checked;
    $("thrust-fields").querySelectorAll("input,select").forEach((el) => (el.disabled = !on));
    $("thrust-preview").hidden = !on;
    const prop = tq("propellant").value;
    if (GAMMA[prop]) { tq("gamma").value = GAMMA[prop]; tq("gamma").disabled = true; }
    if (!on) return;
    clearTimeout(thrustTimer);
    thrustTimer = setTimeout(thrustPreview, 250);
  }
  async function thrustPreview() {
    const body = {};
    ["propellant", "gamma", "throat_mm", "exit_mm", "ambient_bar", "efficiency", "propellant_g"].forEach((k) => (body[k] = tq(k).value));
    let j;
    try { j = await (await fetch("/api/thrust/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json(); }
    catch (e) { return; }
    const box = $("thrust-preview");
    if (!j.ok) { box.textContent = j.error; return; }
    if (!j.info) { box.textContent = j.message || "Enter the throat diameter."; return; }
    const i = j.info;
    box.innerHTML = `Throat area <b>${i.throat_area_mm2} mm²</b> · expansion ratio <b>${i.expansion_ratio}</b> · exit Mach <b>${i.exit_mach}</b><br>` +
      `At ${i.ref_bar} bar (your alarm pressure): Cf <b>${i.cf_at_ref}</b>, thrust ≈ <b>${Math.round(i.force_at_ref_n)} N</b> (${(i.force_at_ref_n / 9.80665).toFixed(1)} kgf)`;
  }
  document.getElementById("tab-thrust").addEventListener("input", thrustUi);
  document.getElementById("tab-thrust").addEventListener("change", thrustUi);

  load().then(() => showTab(location.hash.slice(1) || "check"));
})();
