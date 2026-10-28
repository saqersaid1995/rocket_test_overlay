/* SFT STELLAR firmware page */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let saved = "";
  let lastJob = null;

  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }
  async function post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      const j = await r.json();
      if (!j.ok) toast(j.error);
      return j;
    } catch (e) { toast("Server not reachable"); return { ok: false }; }
  }

  async function load() {
    const j = await (await fetch("/api/firmware/source", { cache: "no-store" })).json();
    $("code").value = j.code; saved = j.code;
  }
  async function save() {
    if ($("code").value === saved) return true;
    const j = await post("/api/firmware/source", { code: $("code").value });
    if (j.ok) { saved = $("code").value; toast("Saved"); }
    return j.ok;
  }

  $("fw-save").addEventListener("click", save);
  $("fw-compile").addEventListener("click", async () => { if (await save()) post("/api/firmware/compile", {}); });
  $("fw-both").addEventListener("click", async () => {
    if (!confirm("Compile and upload this code to the ESP32 over Ethernet?\nPressure data stops for about a minute while the ESP32 updates and restarts.")) return;
    if (await save()) post("/api/firmware/compile", { upload: true });
  });
  $("fw-upload").addEventListener("click", async () => {
    if (!confirm("Upload the last compiled firmware to the ESP32?")) return;
    post("/api/firmware/upload", {});
  });
  $("fw-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    if (!confirm(`Upload ${f.name} (${f.size} bytes) to the ESP32?`)) { e.target.value = ""; return; }
    const fd = new FormData(); fd.append("file", f);
    try {
      const j = await (await fetch("/api/firmware/upload", { method: "POST", body: fd })).json();
      if (!j.ok) toast(j.error);
    } catch (err) { toast("Server not reachable"); }
    e.target.value = "";
  });

  // Tab key inserts two spaces; Ctrl+S saves
  $("code").addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const t = e.target, s = t.selectionStart;
      t.setRangeText("  ", s, t.selectionEnd, "end");
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault(); save();
    }
  });
  window.addEventListener("beforeunload", (e) => { if ($("code").value !== saved) { e.preventDefault(); e.returnValue = ""; } });

  async function poll() {
    try {
      const j = await (await fetch("/api/firmware/status", { cache: "no-store" })).json();
      const dev = $("fw-device");
      dev.textContent = j.device_firmware ? `ESP32: ${j.device_firmware}` : "ESP32 not connected";
      dev.className = "chip " + (j.link === "LIVE" ? (j.ota_capable ? "ok" : "warn") : "bad");
      $("fw-info").textContent = `Board ${j.fqbn}` + (j.bin ? `, last build ${(j.bin.size / 1024).toFixed(0)} KB at ${new Date(j.bin.time * 1000).toLocaleTimeString()}` : "") +
        (j.cli ? "" : ". arduino-cli not found: install Arduino IDE 2");
      $("fw-name").textContent = `${j.sketch}.ino`;
      const job = j.job;
      const running = job.state === "RUNNING";
      ["fw-compile", "fw-upload", "fw-both", "fw-file"].forEach((id) => ($(id).disabled = running));
      $("fw-upload").disabled = running || !j.bin;
      const names = { compile: "Compile", "compile+upload": "Compile and upload", upload: "Upload" };
      const states = { RUNNING: "running", DONE: "done", FAILED: "failed", IDLE: "" };
      const label = job.kind ? `${names[job.kind] || job.kind} ${states[job.state] ?? job.state}` : "Ready";
      $("fw-job").textContent = label + (running && job.kind === "upload" ? ` ${job.progress}%` : "");
      $("fw-job").className = job.state === "DONE" ? "ok" : job.state === "FAILED" ? "bad" : running ? "run" : "";
      $("fw-progress").style.width = (job.kind === "upload" ? job.progress : running ? 100 : 0) + "%";
      if (job.log !== lastJob) {
        const log = $("fw-log");
        const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
        log.textContent = job.log; lastJob = job.log;
        if (atEnd) log.scrollTop = log.scrollHeight;
      }
    } catch (e) { /* server restarting */ }
    setTimeout(poll, 700);
  }
  load(); poll();
})();
