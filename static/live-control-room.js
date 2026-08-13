(() => {
  "use strict";
  const SESSION_ID = window.LIVE_SESSION_ID;
  const API = `/api/live/${SESSION_ID}`;
  const STATE_POLL_MS = 750;

  const programFrame = document.querySelector("#programFrame");
  const missionClock = document.querySelector("#missionClock");
  const statusChip = document.querySelector("#statusChip");
  const statusLabel = document.querySelector("#statusLabel");
  const missionNameLabel = document.querySelector("#missionNameLabel");
  const watchLink = document.querySelector("#watchLink");
  const holdButton = document.querySelector("#holdButton");
  const resumeButton = document.querySelector("#resumeButton");
  const abortButton = document.querySelector("#abortButton");
  const cameraStrip = document.querySelector("#cameraStrip");
  const telemetryRows = document.querySelector("#telemetryRows");
  const telemetryConnected = document.querySelector("#telemetryConnected");
  const environmentRows = document.querySelector("#environmentRows");
  const checklistRows = document.querySelector("#checklistRows");
  const checklistReady = document.querySelector("#checklistReady");
  const templateRows = document.querySelector("#templateRows");
  const armButton = document.querySelector("#armButton");
  const broadcastStartButton = document.querySelector("#broadcastStartButton");
  const broadcastStopButton = document.querySelector("#broadcastStopButton");
  const broadcastStatus = document.querySelector("#broadcastStatus");
  const eventLog = document.querySelector("#eventLog");

  watchLink.href = `/live/${SESSION_ID}/watch`;

  const STATUS_COLORS = {
    pre_ignition: "#94A3B8", countdown: "#94A3B8", hot_fire: "#4ADE80",
    liftoff: "#4ADE80", ascent: "#38BDE8", test_complete: "#FACC15",
    mission_complete: "#FACC15", hold: "#F2B84B", abort: "#DB7479",
  };

  async function postJson(path, body) {
    const response = await fetch(`${API}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed: ${path}`);
    return payload;
  }

  function fmtTime(ts) {
    const date = new Date(ts * 1000);
    return date.toLocaleTimeString([], { hour12: false });
  }

  // -- cameras --------------------------------------------------------------

  let cameraAttachIndex = 1;

  function renderCameras(state) {
    cameraStrip.replaceChildren();
    const indices = Object.keys(state.cameras).map(Number).sort((a, b) => a - b);
    indices.forEach(index => {
      const camera = state.cameras[String(index)];
      const tile = document.createElement("div");
      tile.className = "camera-tile" + (camera.connected ? " connected" : "") + (camera.program ? " active" : "");
      const img = document.createElement("img");
      img.src = `${API}/camera/${index}/stream`;
      img.alt = `Camera ${index}`;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.innerHTML = `<span>CAM ${index}</span><span class="dot"></span>`;
      tile.append(img, meta);
      tile.addEventListener("click", () => {
        postJson("/program/camera", { camera_index: index }).catch(showTransientError);
      });
      cameraStrip.append(tile);
      cameraAttachIndex = Math.max(cameraAttachIndex, index + 1);
    });

    const attach = document.createElement("div");
    attach.className = "camera-attach";
    const indexInput = document.createElement("input");
    indexInput.type = "number";
    indexInput.min = "1";
    indexInput.value = String(cameraAttachIndex);
    indexInput.placeholder = "Index";
    const uriInput = document.createElement("input");
    uriInput.type = "text";
    uriInput.placeholder = "rtsp://... or local path";
    const button = document.createElement("button");
    button.className = "btn small";
    button.textContent = "Attach camera";
    button.addEventListener("click", () => {
      const index = Number(indexInput.value) || cameraAttachIndex;
      const uri = uriInput.value.trim();
      if (!uri) return;
      button.disabled = true;
      postJson(`/camera/${index}`, { uri })
        .catch(showTransientError)
        .finally(() => { button.disabled = false; });
    });
    attach.append(indexInput, uriInput, button);
    cameraStrip.append(attach);
  }

  // -- telemetry --------------------------------------------------------------

  function renderTelemetry(state) {
    const telemetry = state.telemetry;
    telemetryConnected.textContent = telemetry.connected ? "live" : "no data";
    telemetryConnected.style.color = telemetry.connected ? "var(--green)" : "var(--muted)";
    telemetryRows.replaceChildren();
    const names = Object.keys(telemetry.latest).sort();
    if (!names.length) {
      const empty = document.createElement("div");
      empty.className = "kv-row";
      empty.innerHTML = `<span class="k">Waiting for telemetry…</span>`;
      telemetryRows.append(empty);
      return;
    }
    names.forEach(name => {
      const row = document.createElement("div");
      row.className = "kv-row";
      row.innerHTML = `<span class="k">${name}</span><span class="v${telemetry.connected ? "" : " stale"}">${telemetry.latest[name].toFixed(2)}</span>`;
      telemetryRows.append(row);
    });
  }

  // -- environment ------------------------------------------------------------

  function renderEnvironment(state) {
    const limits = state.environment_limits;
    const latest = state.telemetry.latest;
    environmentRows.replaceChildren();
    const wind = latest.wind_speed;
    const temp = latest.temp_c;

    function addRow(label, value, unit, min, max) {
      const row = document.createElement("div");
      const has = typeof value === "number";
      const over = has && (value < min || value > max);
      row.className = "limit-row" + (over ? " over" : "");
      const pct = has ? Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100)) : 0;
      row.innerHTML = `
        <span class="k">${label}</span>
        <span class="bar"><i style="width:${pct}%"></i></span>
        <span class="v">${has ? value.toFixed(1) + unit : "—"} <small style="color:var(--muted)">(${min}–${max}${unit})</small></span>`;
      environmentRows.append(row);
    }
    addRow("Wind speed", wind, " m/s", 0, limits.max_wind_speed_mps);
    addRow("Temperature", temp, "°C", limits.temp_min_c, limits.temp_max_c);
  }

  // -- checklist / countdown ---------------------------------------------------

  function renderChecklist(state) {
    checklistReady.textContent = state.checklist_ready ? "all clear" : "not clear";
    checklistReady.style.color = state.checklist_ready ? "var(--green)" : "var(--amber)";
    checklistRows.replaceChildren();
    state.checklist.forEach(item => {
      const row = document.createElement("div");
      row.className = "checklist-item";
      const label = document.createElement("div");
      label.className = "label";
      const title = document.createElement("b");
      title.textContent = item.label;
      if (item.hold_point) {
        const badge = document.createElement("span");
        badge.className = "hold-badge";
        badge.textContent = "HOLD POINT";
        title.append(badge);
      }
      label.append(title);
      if (item.note) {
        const note = document.createElement("small");
        note.textContent = item.note;
        label.append(note);
      }
      const controls = document.createElement("div");
      controls.className = "state-btn";
      ["go", "no_go"].forEach(state_ => {
        const btn = document.createElement("button");
        btn.textContent = state_ === "go" ? "GO" : "NO";
        btn.className = state_ + (item.state === state_ ? " active" : "");
        btn.disabled = item.source === "auto";
        btn.addEventListener("click", () => {
          postJson(`/checklist/${item.id}`, { state: state_ }).catch(showTransientError);
        });
        controls.append(btn);
      });
      row.append(label, controls);
      checklistRows.append(row);
    });
  }

  // -- template ---------------------------------------------------------------

  let loadedTemplates = [];
  let activeSelection = null; // set from server state, not client click history

  function renderTemplateList() {
    templateRows.replaceChildren();
    if (!loadedTemplates.length) {
      const empty = document.createElement("div");
      empty.className = "kv-row";
      empty.innerHTML = `<span class="k">No templates installed. Install one from the Editor's Design step.</span>`;
      templateRows.append(empty);
      return;
    }
    loadedTemplates.forEach(template => {
      const card = document.createElement("div");
      const key = `${template.id}@${template.version}`;
      const isActive = activeSelection === key;
      card.className = "template-card" + (isActive ? " active" : "");
      const copy = document.createElement("div");
      copy.className = "copy";
      copy.innerHTML = `<strong>${template.name && template.name.en ? template.name.en : template.id}</strong><small>v${template.version}</small>`;
      const button = document.createElement("button");
      button.className = "btn small" + (isActive ? " primary" : "");
      button.textContent = isActive ? "Active" : "Use";
      button.disabled = !template.validation.valid;
      button.addEventListener("click", () => {
        button.disabled = true;
        postJson("/template", {
          template_id: template.id,
          template_version: template.version,
          sha256: template.sha256,
        }).then(() => {
          activeSelection = key;
          renderTemplateList();
        }).catch(showTransientError).finally(() => { button.disabled = false; });
      });
      card.append(copy, button);
      templateRows.append(card);
    });
  }

  async function loadTemplates() {
    templateRows.replaceChildren();
    const loading = document.createElement("div");
    loading.className = "kv-row";
    loading.innerHTML = `<span class="k">Loading templates…</span>`;
    templateRows.append(loading);
    try {
      const response = await fetch("/api/templates", { headers: { Accept: "application/json" } });
      const payload = await response.json();
      loadedTemplates = Array.isArray(payload.templates) ? payload.templates : [];
      renderTemplateList();
    } catch (error) {
      templateRows.replaceChildren();
      const failed = document.createElement("div");
      failed.className = "kv-row";
      failed.innerHTML = `<span class="k">${error.message}</span>`;
      templateRows.append(failed);
    }
  }

  // -- event log ----------------------------------------------------------------

  function renderEventLog(state) {
    eventLog.replaceChildren();
    state.event_log.forEach(entry => {
      const row = document.createElement("div");
      row.className = "row";
      const time = document.createElement("time");
      time.textContent = fmtTime(entry.ts);
      const message = document.createElement("span");
      message.textContent = entry.message;
      row.append(time, message);
      eventLog.append(row);
    });
  }

  // -- top-level state application -----------------------------------------------

  let armed = false;
  let programStreamStarted = false;

  function applyState(state) {
    missionNameLabel.textContent = `${state.mission_name} · ${state.mission_type === "launch" ? "Launch" : "Static fire"}`;
    missionClock.textContent = state.mission_clock;
    const color = STATUS_COLORS[state.aborted ? "abort" : (state.currently_holding && state.mission_time_s < 0 ? "hold" : "")] || null;
    let statusText = state.aborted ? "ABORT" : (state.currently_holding && state.mission_time_s < 0 ? "HOLD" : (state.mission_time_s < 0 ? "COUNTING DOWN" : "LIVE"));
    statusLabel.textContent = statusText;
    const chipColor = color || (state.mission_time_s < 0 ? "#94A3B8" : "#4ADE80");
    statusChip.style.color = chipColor;
    statusChip.style.background = chipColor + "22";

    holdButton.disabled = state.manual_hold || state.aborted;
    resumeButton.disabled = !state.manual_hold || state.aborted;
    abortButton.disabled = state.aborted;

    renderCameras(state);
    renderTelemetry(state);
    renderEnvironment(state);
    renderChecklist(state);
    renderEventLog(state);

    const serverSelectionKey = state.active_template
      ? `${state.active_template.template_id}@${state.active_template.template_version}`
      : null;
    if (serverSelectionKey !== activeSelection) {
      activeSelection = serverSelectionKey;
      if (loadedTemplates.length) renderTemplateList();
    }

    armed = state.armed;
    armButton.disabled = armed;
    armButton.textContent = armed ? "Armed" : "Arm session";
    broadcastStartButton.disabled = !armed || state.broadcast_live;
    broadcastStopButton.disabled = !armed || !state.broadcast_live;
    broadcastStatus.className = "broadcast-status" + (state.broadcast_live ? " live" : "");
    broadcastStatus.querySelector("span:last-child").textContent = state.broadcast_live
      ? "Live"
      : (armed ? "Armed, standing by" : "Not armed");

    if (armed && !programStreamStarted) {
      programStreamStarted = true;
      programFrame.src = `${API}/program/stream`;
    }
  }

  function showTransientError(error) {
    // Errors surface inline in the event log context already covered by the
    // server-side session log; keep this path lightweight for a first draft.
    console.error(error);
  }

  async function pollState() {
    try {
      const response = await fetch(`${API}/state`);
      if (response.ok) applyState(await response.json());
    } catch (error) {
      console.error(error);
    } finally {
      setTimeout(pollState, STATE_POLL_MS);
    }
  }

  armButton.addEventListener("click", () => {
    armButton.disabled = true;
    postJson("/arm").catch(showTransientError);
  });
  broadcastStartButton.addEventListener("click", () => postJson("/broadcast/start").catch(showTransientError));
  broadcastStopButton.addEventListener("click", () => postJson("/broadcast/stop").catch(showTransientError));
  holdButton.addEventListener("click", () => postJson("/hold").catch(showTransientError));
  resumeButton.addEventListener("click", () => postJson("/resume").catch(showTransientError));
  abortButton.addEventListener("click", () => {
    if (confirm("Abort the mission? This cannot be undone.")) {
      postJson("/abort").catch(showTransientError);
    }
  });

  loadTemplates();
  pollState();
})();
