(() => {
  if (typeof renderWorkspace !== 'function' || typeof syncHeader !== 'function') return;

  const originalRenderWorkspace = renderWorkspace;
  const originalCameraPanelSignature = typeof cameraPanelSignature === 'function' ? cameraPanelSignature : null;
  let lastStructure = '';
  let interactionUntil = 0;
  let pendingFullRender = false;

  const now = () => performance.now();
  const markInteraction = (ms = 700) => {
    interactionUntil = Math.max(interactionUntil, now() + ms);
  };
  const isInteractive = () => {
    const active = document.activeElement;
    return Boolean(
      document.querySelector('dialog[open]') ||
      now() < interactionUntil ||
      (active && active !== document.body && active.matches?.('button,input,select,textarea,[contenteditable="true"]'))
    );
  };

  // Camera stream identity must describe the stream topology, not changing live
  // metrics such as FPS/latency/time offset. Otherwise every telemetry update
  // destroys the <img> element and forces a new MJPEG connection.
  if (originalCameraPanelSignature) {
    cameraPanelSignature = function stableCameraPanelSignature() {
      return `${data.operation.mode}:${cameraColumns}:` + (data.devices || [])
        .filter(d => d.device_type === 'IP-CAMERA' && d.enabled)
        .map(c => {
          const integration = data.integrations?.find(i => i.device_id === c.id);
          return [
            c.id,
            c.name,
            c.endpoint,
            c.protocol,
            integration?.adapter_type || '',
            Boolean(integration?.secret_configured),
            Boolean(integration?.enabled)
          ].join(':');
        }).join('|');
    };
  }

  const structureSignature = () => JSON.stringify({
    layout: (layout || []).map(x => [x.panel, x.order, x.span]),
    locked,
    phaseAware,
    state: data?.operation?.state,
    mode: data?.operation?.mode,
    recording: data?.recording?.state,
    run: (data?.runs || []).map(r => [r.id, r.active, r.status, r.configuration_revision]),
    steps: (data?.steps || []).map(s => [s.sequence, s.status]),
    stations: (data?.stations || []).map(s => [s.code, s.decision, s.operator_name]),
    alarms: (data?.alarms || []).map(a => [a.id, a.state, a.priority, a.message]),
    incidents: (data?.incidents || []).map(i => [i.id, i.status, i.severity, i.title]),
    events: (data?.events || []).slice(0, 10).map(e => e.sequence),
    devices: (data?.devices || []).map(d => [d.id, d.enabled, d.name, d.endpoint, d.protocol]),
    integrations: (data?.integrations || []).map(i => [i.device_id, i.enabled, i.adapter_type, i.endpoint, i.secret_configured]),
    channels: (data?.channels || []).map(c => [c.id, c.enabled, c.source_id, c.warning, c.critical])
  });

  const setText = (node, value) => {
    if (node && node.textContent !== String(value)) node.textContent = String(value);
  };

  function patchMissionPanel() {
    const panel = document.querySelector('[data-panel="mission"]');
    if (!panel) return;
    const values = panel.querySelectorAll('.mission-summary .metric strong');
    const t = data.telemetry || {};
    if (values[1]) setText(values[1], Number(t.pressure || 0).toFixed(2));
    if (values[2]) setText(values[2], Number(t.thrust || 0).toFixed(1));
    if (values[3]) setText(values[3], Number(t.temperature || 0).toFixed(1));
  }

  function patchCommandPanel() {
    const panel = document.querySelector('[data-panel="command"]');
    if (!panel) return;
    const values = panel.querySelectorAll('.command-status b');
    if (values[0]) setText(values[0], data.runtime_context?.context_state || 'NOT RELEASED');
    if (values[1]) setText(values[1], data.operation?.state || 'UNKNOWN');
    if (values[2]) setText(values[2], data.operation?.mode || 'UNKNOWN');
    if (values[3]) setText(values[3], data.recording?.state || 'STOPPED');
  }

  function patchNetworkPanel() {
    const panel = document.querySelector('[data-panel="network"]');
    if (!panel) return;
    const values = panel.querySelectorAll('.network-grid .metric strong');
    const meta = data.telemetry?.meta || {};
    if (values[0]) setText(values[0], meta.device_id || '—');
    if (values[1]) setText(values[1], meta.total_samples || 0);
    if (values[2]) setText(values[2], meta.sequence_gaps || 0);
    const em = panel.querySelector('.network-grid .metric em');
    if (em) setText(em, meta.status || 'NO DEVICE');
  }

  function patchStoragePanel() {
    const panel = document.querySelector('[data-panel="storage"]');
    if (!panel) return;
    const values = panel.querySelectorAll('.storage-grid .metric strong');
    const sampleCount = (data.edge_sessions || []).reduce((n, s) => n + Number(s.total_samples || 0), 0);
    if (values[0]) setText(values[0], data.recording?.state || 'STOPPED');
    if (values[1]) setText(values[1], sampleCount);
  }

  function patchCameraPanel() {
    const panel = document.querySelector('[data-panel="cameras"]');
    if (!panel) return;
    for (const camera of (data.devices || []).filter(d => d.device_type === 'IP-CAMERA' && d.enabled)) {
      const tile = panel.querySelector(`[data-camera-id="${CSS.escape(camera.id)}"]`);
      if (!tile) continue;
      const status = tile.querySelector('[data-camera-status]');
      if (status) setText(status, camera.health || 'UNKNOWN');
      const metrics = tile.querySelector('[data-camera-metrics]');
      if (metrics) {
        setText(metrics, `${camera.width || '—'}×${camera.height || '—'} · ${camera.fps || '—'} FPS · ${camera.latency_ms || '—'} ms`);
      }
    }
  }

  function patchLiveOnly() {
    try {
      syncHeader();
      patchMissionPanel();
      patchCommandPanel();
      patchNetworkPanel();
      patchStoragePanel();
      patchCameraPanel();
      if (typeof drawPlots === 'function') drawPlots();
    } catch (_) {
      // A later structural render will recover any panel whose markup differs.
    }
  }

  renderWorkspace = function stableWorkspaceRender(...args) {
    const signature = structureSignature();
    const changed = signature !== lastStructure;

    patchLiveOnly();

    if (!changed) return;
    if (isInteractive()) {
      pendingFullRender = true;
      return;
    }

    const result = originalRenderWorkspace.apply(this, args);
    lastStructure = structureSignature();
    pendingFullRender = false;
    return result;
  };

  document.addEventListener('pointerdown', () => markInteraction(900), true);
  document.addEventListener('pointerup', () => markInteraction(250), true);
  document.addEventListener('keydown', () => markInteraction(700), true);
  document.addEventListener('focusin', event => {
    if (event.target?.matches?.('button,input,select,textarea,[contenteditable="true"]')) markInteraction(700);
  }, true);
  document.addEventListener('submit', () => markInteraction(1200), true);

  // Establish the signature for the already-rendered initial workspace.
  lastStructure = structureSignature();

  setInterval(() => {
    if (pendingFullRender && !isInteractive()) renderWorkspace();
  }, 120);
})();
