(() => {
  "use strict";
  const SESSION_ID = window.LIVE_SESSION_ID;
  const API = `/api/live/${SESSION_ID}`;
  const STATE_POLL_MS = 750;

  const multiviewCanvas = document.querySelector("#multiviewCanvas");
  const cameraAttachBar = document.querySelector("#cameraAttachBar");
  const cameraDetailsRows = document.querySelector("#cameraDetailsRows");
  const missionClock = document.querySelector("#missionClock");
  const statusChip = document.querySelector("#statusChip");
  const statusLabel = document.querySelector("#statusLabel");
  const missionNameLabel = document.querySelector("#missionNameLabel");
  const watchLink = document.querySelector("#watchLink");
  const holdButton = document.querySelector("#holdButton");
  const resumeButton = document.querySelector("#resumeButton");
  const abortButton = document.querySelector("#abortButton");
  const telemetryRows = document.querySelector("#telemetryRows");
  const telemetryConnected = document.querySelector("#telemetryConnected");
  const environmentRows = document.querySelector("#environmentRows");
  const checklistRows = document.querySelector("#checklistRows");
  const checklistReady = document.querySelector("#checklistReady");
  const checklistNewLabel = document.querySelector("#checklistNewLabel");
  const checklistNewRequired = document.querySelector("#checklistNewRequired");
  const checklistNewHold = document.querySelector("#checklistNewHold");
  const checklistAddButton = document.querySelector("#checklistAddButton");
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

  async function requestJson(method, path, body) {
    const response = await fetch(`${API}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: method === "DELETE" ? undefined : JSON.stringify(body || {}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed: ${path}`);
    return payload;
  }
  const postJson = (path, body) => requestJson("POST", path, body);
  const patchJson = (path, body) => requestJson("PATCH", path, body);
  const deleteJson = path => requestJson("DELETE", path);

  function fmtTime(ts) {
    const date = new Date(ts * 1000);
    return date.toLocaleTimeString([], { hour12: false });
  }

  function showTransientError(error) {
    // Errors surface inline in the event log context already covered by the
    // server-side session log; keep this path lightweight for a first draft.
    console.error(error);
  }

  // -- multiview canvas: free-form drag/resize camera + program tiles ---------

  const tileNodes = new Map(); // tileId -> {node, img, badge}
  let localLayout = {};
  let mvDragState = null;

  function mvClamp(value, min, max) { return Math.min(max, Math.max(min, value)); }
  function mvSnap(value) {
    const points = [0, .05, .1, .25, .5, .75, .9, .95, 1];
    const near = points.find(point => Math.abs(point - value) < .008);
    return near ?? Math.round(value * 1000) / 1000;
  }

  function beginDrag(event, tileId) {
    if (event.target.closest(".resize-handle, .mv-tile-btn")) return;
    const rect = localLayout[tileId];
    const entry = tileNodes.get(tileId);
    if (!rect || !entry) return;
    mvDragState = { mode: "move", tileId, startX: event.clientX, startY: event.clientY, x: rect.x, y: rect.y };
    entry.node.classList.add("dragging");
    entry.node.setPointerCapture(event.pointerId);
  }

  function beginResize(event, tileId) {
    event.stopPropagation();
    const rect = localLayout[tileId];
    const entry = tileNodes.get(tileId);
    if (!rect || !entry) return;
    mvDragState = { mode: "resize", tileId, startX: event.clientX, startY: event.clientY, w: rect.w, h: rect.h };
    entry.node.classList.add("dragging");
    entry.node.setPointerCapture(event.pointerId);
  }

  window.addEventListener("pointermove", event => {
    if (!mvDragState) return;
    const canvasRect = multiviewCanvas.getBoundingClientRect();
    const dx = (event.clientX - mvDragState.startX) / canvasRect.width;
    const dy = (event.clientY - mvDragState.startY) / canvasRect.height;
    const rect = localLayout[mvDragState.tileId];
    if (!rect) return;
    if (mvDragState.mode === "move") {
      rect.x = mvSnap(mvClamp(mvDragState.x + dx, 0, 1 - rect.w));
      rect.y = mvSnap(mvClamp(mvDragState.y + dy, 0, 1 - rect.h));
    } else {
      rect.w = mvSnap(mvClamp(mvDragState.w + dx, 0.05, 1 - rect.x));
      rect.h = mvSnap(mvClamp(mvDragState.h + dy, 0.05, 1 - rect.y));
    }
    const entry = tileNodes.get(mvDragState.tileId);
    if (entry) positionTile(entry.node, rect);
  });

  window.addEventListener("pointerup", () => {
    if (!mvDragState) return;
    const entry = tileNodes.get(mvDragState.tileId);
    if (entry) entry.node.classList.remove("dragging");
    mvDragState = null;
    postJson("/layout", { layout: localLayout }).catch(showTransientError);
  });

  function positionTile(node, rect) {
    node.style.left = `${rect.x * 100}%`;
    node.style.top = `${rect.y * 100}%`;
    node.style.width = `${rect.w * 100}%`;
    node.style.height = `${rect.h * 100}%`;
  }

  function createTile(tileId) {
    const node = document.createElement("div");
    node.className = "mv-tile" + (tileId === "program" ? " program" : "");

    const img = document.createElement("img");
    img.alt = tileId === "program" ? "Program feed" : `Camera ${tileId}`;
    node.append(img);

    const label = document.createElement("div");
    label.className = "mv-tile-label";
    label.textContent = tileId === "program" ? "PROGRAM" : `CAM ${tileId}`;
    node.append(label);

    if (tileId !== "program") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn small mv-tile-btn";
      button.textContent = "Make Program";
      button.addEventListener("pointerdown", event => event.stopPropagation());
      button.addEventListener("click", () => {
        postJson("/program/camera", { camera_index: Number(tileId) }).catch(showTransientError);
      });
      node.append(button);
    }

    const badge = document.createElement("div");
    badge.className = "mv-badge";
    node.append(badge);

    const handle = document.createElement("i");
    handle.className = "resize-handle";
    node.append(handle);

    node.addEventListener("pointerdown", event => beginDrag(event, tileId));
    handle.addEventListener("pointerdown", event => beginResize(event, tileId));

    multiviewCanvas.append(node);
    return { node, img, badge };
  }

  function ensureTiles(state) {
    const cameraIds = Object.keys(state.cameras).sort((a, b) => Number(a) - Number(b));
    const desiredIds = ["program", ...cameraIds];

    for (const [tileId, entry] of Array.from(tileNodes.entries())) {
      if (!desiredIds.includes(tileId)) {
        entry.node.remove();
        tileNodes.delete(tileId);
      }
    }
    desiredIds.forEach(tileId => {
      if (!tileNodes.has(tileId)) tileNodes.set(tileId, createTile(tileId));
    });

    const programEntry = tileNodes.get("program");
    if (programEntry && state.armed && !programEntry.img.src) {
      programEntry.img.src = `${API}/program/stream`;
    }
    cameraIds.forEach(camId => {
      const entry = tileNodes.get(camId);
      if (entry && !entry.img.src) entry.img.src = `${API}/camera/${camId}/stream`;
    });
  }

  function renderMultiview(state) {
    ensureTiles(state);

    Object.entries(state.cameras).forEach(([camId, camera]) => {
      const entry = tileNodes.get(camId);
      if (!entry) return;
      entry.node.classList.toggle("is-program-source", Boolean(camera.program));
      const stale = camera.connected && typeof camera.frame_age_s === "number" && camera.frame_age_s >= 2.0;
      const dotClass = !camera.connected ? "" : (stale ? "stale" : "live");
      const resolution = camera.width && camera.height ? `${camera.width}×${camera.height}` : "—";
      const fps = typeof camera.fps === "number" ? `${camera.fps.toFixed(1)} fps` : "—";
      entry.badge.innerHTML = `<span class="dot ${dotClass}"></span><span>${resolution}</span><span>${fps}</span>`;
    });

    // Never fight an in-progress drag: only re-apply server positions when idle.
    if (mvDragState === null) {
      localLayout = state.camera_layout;
      tileNodes.forEach((entry, tileId) => {
        const rect = localLayout[tileId];
        if (rect) positionTile(entry.node, rect);
      });
    }
  }

  // -- camera attach bar (built once, not rebuilt every poll) -----------------

  let cameraAttachIndex = 1;

  function renderCameraAttachBar(state) {
    const indices = Object.keys(state.cameras).map(Number);
    if (indices.length) cameraAttachIndex = Math.max(...indices) + 1;
    const indexInput = cameraAttachBar.querySelector("input[type=number]");
    if (indexInput && document.activeElement !== indexInput) {
      indexInput.value = String(cameraAttachIndex);
    }
    if (cameraAttachBar.dataset.built) return;
    cameraAttachBar.dataset.built = "1";
    cameraAttachBar.replaceChildren();

    const newIndexInput = document.createElement("input");
    newIndexInput.type = "number";
    newIndexInput.min = "1";
    newIndexInput.value = String(cameraAttachIndex);
    newIndexInput.placeholder = "Index";
    const uriInput = document.createElement("input");
    uriInput.type = "text";
    uriInput.placeholder = "rtsp://... or local path";
    const button = document.createElement("button");
    button.className = "btn small";
    button.textContent = "Attach camera";
    button.addEventListener("click", () => {
      const index = Number(newIndexInput.value) || cameraAttachIndex;
      const uri = uriInput.value.trim();
      if (!uri) return;
      button.disabled = true;
      postJson(`/camera/${index}`, { uri })
        .then(() => { uriInput.value = ""; })
        .catch(showTransientError)
        .finally(() => { button.disabled = false; });
    });
    cameraAttachBar.append(newIndexInput, uriInput, button);
  }

  // -- camera details panel ----------------------------------------------------

  function renderCameraDetails(state) {
    cameraDetailsRows.replaceChildren();
    const indices = Object.keys(state.cameras).map(Number).sort((a, b) => a - b);
    if (!indices.length) {
      const empty = document.createElement("div");
      empty.className = "kv-row";
      empty.innerHTML = `<span class="k">No cameras attached</span>`;
      cameraDetailsRows.append(empty);
      return;
    }
    indices.forEach(index => {
      const camera = state.cameras[String(index)];
      const resolution = camera.width && camera.height ? `${camera.width}×${camera.height}` : "—";
      const fps = typeof camera.fps === "number" ? `${camera.fps.toFixed(1)} fps` : "—";
      const age = typeof camera.frame_age_s === "number" ? `${camera.frame_age_s.toFixed(1)}s` : "—";
      const stale = !camera.connected || (typeof camera.frame_age_s === "number" && camera.frame_age_s >= 2.0);
      const row = document.createElement("div");
      row.className = "kv-row";
      row.innerHTML = `<span class="k">CAM ${index}${camera.program ? " · Program" : ""}</span>` +
        `<span class="v${stale ? " stale" : ""}">${resolution} · ${fps} · age ${age}</span>`;
      cameraDetailsRows.append(row);
    });
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

  // -- checklist / countdown (fully editable live) -----------------------------

  function beginRename(item, titleNode) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = item.label;
    const commit = () => {
      const value = input.value.trim();
      if (input.isConnected) input.replaceWith(titleNode);
      if (value && value !== item.label) {
        patchJson(`/checklist/items/${item.id}`, { label: value }).catch(showTransientError);
      }
    };
    input.addEventListener("blur", commit);
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") input.blur();
      if (event.key === "Escape") { input.value = item.label; input.blur(); }
    });
    titleNode.replaceWith(input);
    input.focus();
    input.select();
  }

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
      if (item.source !== "auto") {
        title.style.cursor = "pointer";
        title.title = "Click to rename";
        title.addEventListener("click", () => beginRename(item, title));
      }
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

      const actions = document.createElement("div");
      actions.className = "row-actions";

      const reqChip = document.createElement("button");
      reqChip.type = "button";
      reqChip.className = "chip" + (item.required ? " active" : "");
      reqChip.textContent = "REQ";
      reqChip.title = "Toggle required";
      reqChip.addEventListener("click", () => {
        patchJson(`/checklist/items/${item.id}`, { required: !item.required }).catch(showTransientError);
      });

      const holdChip = document.createElement("button");
      holdChip.type = "button";
      holdChip.className = "chip" + (item.hold_point ? " active" : "");
      holdChip.textContent = "HOLD";
      holdChip.title = "Toggle hold point";
      holdChip.addEventListener("click", () => {
        patchJson(`/checklist/items/${item.id}`, { hold_point: !item.hold_point }).catch(showTransientError);
      });

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

      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "ci-delete";
      deleteButton.textContent = "×";
      deleteButton.title = "Delete item";
      deleteButton.addEventListener("click", () => {
        if (confirm(`Remove checklist item "${item.label}"?`)) {
          deleteJson(`/checklist/items/${item.id}`).catch(showTransientError);
        }
      });

      actions.append(reqChip, holdChip, controls, deleteButton);
      row.append(label, actions);
      checklistRows.append(row);
    });
  }

  checklistAddButton.addEventListener("click", () => {
    const label = checklistNewLabel.value.trim();
    if (!label) return;
    checklistAddButton.disabled = true;
    postJson("/checklist/items", {
      label,
      required: checklistNewRequired.checked,
      hold_point: checklistNewHold.checked,
    }).then(() => {
      checklistNewLabel.value = "";
      checklistNewHold.checked = false;
    }).catch(showTransientError).finally(() => { checklistAddButton.disabled = false; });
  });

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

    renderMultiview(state);
    renderCameraAttachBar(state);
    renderCameraDetails(state);
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
