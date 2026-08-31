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

  // The main workspace script calls refreshWorkspaceSelectors() for every live
  // snapshot. Its original implementation rewrites select.innerHTML each time,
  // which destroys/recreates <option> nodes and makes an open native dropdown
  // close/reopen/flicker. Replace it with a topology-aware updater that only
  // touches a select when its actual option set changed.
  if (typeof refreshWorkspaceSelectors === 'function') {
    let profileOptionsKey = '';
    let savedOptionsKey = '';

    refreshWorkspaceSelectors = function stableRefreshWorkspaceSelectors() {
      if (popoutPanel) return;
      const profile = document.querySelector('#console-profile');
      const saved = document.querySelector('#saved-workspace');
      if (!profile || !saved) return;

      const workspaces = data?.workspaces || [];
      const roles = [...new Set(workspaces.map(w => w.console_role))];
      const currentRole = profile.value;
      const nextProfileKey = JSON.stringify(roles);

      if (nextProfileKey !== profileOptionsKey) {
        const fragment = document.createDocumentFragment();
        roles.forEach(role => {
          const option = document.createElement('option');
          option.value = role;
          option.textContent = role;
          fragment.appendChild(option);
        });
        profile.replaceChildren(fragment);
        profileOptionsKey = nextProfileKey;
        if (roles.includes(currentRole)) profile.value = currentRole;
      }

      const effectiveRole = roles.includes(profile.value) ? profile.value : (roles[0] || '');
      if (effectiveRole && profile.value !== effectiveRole) profile.value = effectiveRole;

      const currentWorkspaceId = saved.value;
      const candidates = workspaces.filter(w => w.console_role === effectiveRole);
      const nextSavedKey = JSON.stringify(candidates.map(w => [w.id, w.name]));

      if (nextSavedKey !== savedOptionsKey) {
        const fragment = document.createDocumentFragment();
        candidates.forEach(workspace => {
          const option = document.createElement('option');
          option.value = String(workspace.id);
          option.textContent = workspace.name;
          fragment.appendChild(option);
        });
        saved.replaceChildren(fragment);
        savedOptionsKey = nextSavedKey;
        if (candidates.some(w => String(w.id) === currentWorkspaceId)) {
          saved.value = currentWorkspaceId;
        }
      }
    };
  }

  // Stream identity must not depend on changing metrics such as FPS or latency.
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

  // IMPORTANT: this signature contains configuration/topology only.
  // Telemetry, health, alarms, events, timestamps and live quality MUST NOT
  // trigger replacement of #workspace. Those values change continuously.
  const structureSignature = () => JSON.stringify({
    layout: (layout || []).map(x => [x.panel, x.order, x.span]),
    locked,
    phaseAware,
    phase: data?.operation?.state,
    mode: data?.operation?.mode,
    release: [data?.runtime_context?.context_state, data?.runtime_context?.release_code],
    runs: (data?.runs || []).map(r => [r.id, r.active, r.configuration_revision]),
    steps: (data?.steps || []).map(s => [s.sequence, s.status]),
    stations: (data?.stations || []).map(s => [s.code, s.decision, s.operator_name]),
    devices: (data?.devices || []).map(d => [d.id, d.enabled, d.name, d.endpoint, d.protocol, d.device_type]),
    integrations: (data?.integrations || []).map(i => [i.device_id, i.enabled, i.adapter_type, i.endpoint, i.secret_configured]),
    channels: (data?.channels || []).map(c => [c.id, c.enabled, c.source_id, c.warning, c.critical, c.sample_rate])
  });

  const setText = (node, value) => {
    if (node && node.textContent !== String(value)) node.textContent = String(value);
  };
  const setBadge = (node, value) => {
    if (!node) return;
    const text = String(value || 'UNKNOWN');
    if (node.textContent !== text) node.textContent = text;
    node.className = `badge ${text.toLowerCase().replaceAll('_','-')}`;
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

  function patchChannelsPanel() {
    const panel = document.querySelector('[data-panel="channels"]');
    if (!panel) return;
    const runtime = data.telemetry?.channels || {};
    panel.querySelectorAll('tbody tr').forEach(row => {
      const id = row.querySelector('code')?.textContent?.trim();
      const item = runtime[id];
      if (!id || !item) return;
      const cells = row.querySelectorAll('td');
      if (cells[1]) setText(cells[1], `${item.value ?? '—'} ${item.unit || ''}`.trim());
      if (cells[2]) {
        const badgeNode = cells[2].querySelector('.badge');
        if (badgeNode) setBadge(badgeNode, item.quality || 'NO_DATA');
      }
      if (cells[3]) setText(cells[3], `${item.age_ms ?? '—'} ms`);
    });
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
      if (metrics) setText(metrics, `${camera.width || '—'}×${camera.height || '—'} · ${camera.fps || '—'} FPS · ${camera.latency_ms || '—'} ms`);
    }
  }

  function patchAlarmBanner() {
    const banner = document.querySelector('#alarm-banner');
    if (!banner) return;
    const active = (data.alarms || []).filter(a => a.state !== 'CLOSED');
    const critical = active.filter(a => a.priority === 'P1');
    banner.className = 'alarm-banner ' + (critical.length ? 'critical' : active.length ? 'warning' : '');
    const label = banner.querySelector('span');
    if (label) setText(label, critical.length ? `${critical.length} CRITICAL ALARM${critical.length > 1 ? 'S' : ''} — OPERATOR ACTION REQUIRED` : active.length ? `${active.length} ACTIVE ALARM${active.length > 1 ? 'S' : ''}` : 'NO CRITICAL ALARMS');
  }

  function patchLiveOnly() {
    try {
      syncHeader();
      patchMissionPanel();
      patchCommandPanel();
      patchNetworkPanel();
      patchStoragePanel();
      patchChannelsPanel();
      patchCameraPanel();
      patchAlarmBanner();
      if (typeof drawPlots === 'function') drawPlots();
    } catch (_) {
      // Keep the live stream running even if an optional panel is absent.
    }
  }

  renderWorkspace = function stableWorkspaceRender(...args) {
    patchLiveOnly();
    const signature = structureSignature();
    const changed = signature !== lastStructure;
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

  lastStructure = structureSignature();

  setInterval(() => {
    if (pendingFullRender && !isInteractive()) renderWorkspace();
  }, 120);
})();
