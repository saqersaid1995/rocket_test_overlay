(() => {
  if (typeof render !== 'function') return;

  const originalRender = render;
  let lastStructure = '';
  let interactionUntil = 0;
  let pendingFullRender = false;

  const now = () => performance.now();
  const markInteraction = (ms = 900) => {
    interactionUntil = Math.max(interactionUntil, now() + ms);
  };

  const isInteractive = () => {
    const active = document.activeElement;
    const dialogOpen = Boolean(document.querySelector('dialog[open]'));
    const editing = Boolean(active && active !== document.body && active.matches?.('button,input,select,textarea,[contenteditable="true"]'));
    return dialogOpen || editing || now() < interactionUntil;
  };

  const setText = (selector, value) => {
    const node = document.querySelector(selector);
    if (node && node.textContent !== String(value)) node.textContent = String(value);
  };

  const setBadge = (node, value) => {
    if (!node) return;
    const text = String(value || 'UNKNOWN');
    if (node.textContent !== text) node.textContent = text;
    node.className = `badge ${text.toLowerCase().replaceAll('_','-')}`;
  };

  const liveFieldsOnly = () => {
    try {
      const op = data?.operation || {};
      const t = data?.telemetry || {};
      const meta = t.meta || {};
      const record = data?.recording || { state: 'STOPPED' };

      setText('#state', op.state || 'UNKNOWN');
      const lamp = document.querySelector('#state-lamp');
      if (lamp) lamp.className = 'lamp ' + String(op.state || '').toLowerCase();
      setText('#source-mode', op.mode || 'UNKNOWN');
      setText('#source-status', meta.status || t.source_mode || 'UNKNOWN');
      setText('#recording-state', record.state || 'STOPPED');
      setText('#pressure', Number(t.pressure || 0).toFixed(2));
      setText('#thrust', Number(t.thrust || 0).toFixed(1));
      setText('#temperature', Number(t.temperature || 0).toFixed(1));
      setText('#continuity', t.continuity || 'UNKNOWN');

      const sourceMeta = document.querySelector('#source-meta');
      if (sourceMeta && t.source_mode === 'LIVE') {
        sourceMeta.textContent = `DEVICE ${meta.device_id || '—'} · SAMPLES ${meta.total_samples || 0} · GAPS ${meta.sequence_gaps || 0} · AGE ${meta.age_ms ?? '—'} ms`;
      }

      const deviceById = Object.fromEntries((data?.devices || []).map(d => [d.id, d]));
      document.querySelectorAll('#integrations-table tr').forEach(row => {
        const id = row.querySelector('code')?.textContent?.trim();
        const device = deviceById[id];
        if (!device) return;
        const badges = row.querySelectorAll('.badge');
        if (badges[1]) setBadge(badges[1], device.health || 'UNKNOWN');
      });

      const channelById = Object.fromEntries((data?.channels || []).map(c => [c.id, c]));
      document.querySelectorAll('#channel-integrations-table tr').forEach(row => {
        const id = row.querySelector('code')?.textContent?.trim();
        const channel = channelById[id];
        if (!channel) return;
        const badges = row.querySelectorAll('.badge');
        if (badges[0]) setBadge(badges[0], channel.quality || 'UNKNOWN');
      });
    } catch (_) {
      // A structural render will recover any markup that changed.
    }
  };

  const structureSignature = () => {
    try {
      return JSON.stringify({
        mode: data?.operation?.mode,
        state: data?.operation?.state,
        stations: (data?.stations || []).map(x => [x.code, x.decision, x.operator_name]),
        devices: (data?.devices || []).map(x => [x.id, x.enabled, x.name, x.endpoint, x.protocol, x.required]),
        integrations: (data?.integrations || []).map(x => [x.device_id, x.enabled, x.adapter_type, x.endpoint]),
        channels: (data?.channels || []).map(x => [x.id, x.enabled, x.source_id, x.warning, x.critical, x.sample_rate]),
        channel_integrations: (data?.channel_integrations || []).map(x => [x.channel_id, x.raw_field, x.calibration_slope, x.calibration_intercept, x.required_for_commit]),
        steps: (data?.steps || []).map(x => [x.sequence, x.status]),
        alarms: (data?.alarms || []).map(x => [x.id, x.state, x.priority, x.message]),
        events: (data?.events || []).slice(0, 8).map(x => x.sequence),
        replays: (data?.replays || []).map(x => [x.id, x.active, x.row_count]),
        diagnostic: [data?.latest_diagnostic?.id, data?.latest_diagnostic?.overall_status],
        backups: (data?.backups || []).map(x => [x.name || x.filename, x.created_at])
      });
    } catch (_) {
      return 'invalid';
    }
  };

  render = function stableRender(...args) {
    liveFieldsOnly();
    const signature = structureSignature();
    const structureChanged = signature !== lastStructure;

    if (!structureChanged) return;

    if (isInteractive()) {
      pendingFullRender = true;
      return;
    }

    const result = originalRender.apply(this, args);
    lastStructure = structureSignature();
    pendingFullRender = false;
    return result;
  };

  document.addEventListener('pointerdown', () => markInteraction(1100), true);
  document.addEventListener('pointerup', () => markInteraction(350), true);
  document.addEventListener('keydown', () => markInteraction(900), true);
  document.addEventListener('focusin', event => {
    if (event.target?.matches?.('button,input,select,textarea,[contenteditable="true"]')) markInteraction(900);
  }, true);
  document.addEventListener('submit', () => markInteraction(1400), true);

  lastStructure = structureSignature();

  setInterval(() => {
    if (pendingFullRender && !isInteractive()) render();
  }, 150);
})();
