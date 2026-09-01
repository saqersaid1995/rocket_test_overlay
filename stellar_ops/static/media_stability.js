(() => {
  if (typeof refresh !== 'function' || typeof render !== 'function') return;

  const originalRender = render;
  let lastStructure = '';

  // Scene-bank buttons are controls, not confidence monitors. Keeping an MJPEG
  // <img> in every scene button can exhaust the browser's per-host connection
  // pool. Preview and Program remain the only continuous broadcast monitors.
  if (typeof sourceDeck === 'function') {
    sourceDeck = function stableSourceDeck() {
      const liveScenes = (D.broadcast_scenes || []).filter(s => s.scene_type === 'LIVE');
      return `<section class="source-deck"><header><b>CAMERAS & LIVE SCENES</b><small>SELECT DIRECTLY TO PREVIEW</small></header><div>${liveScenes.map(s => {
        const source = (s.sources || []).find(x => x.kind === 'camera');
        const camera = (D.camera_profiles || []).find(c => c.device_id === source?.source);
        const state = camera?.runtime_live ? 'LIVE' : esc(camera?.runtime_status || 'OFFLINE');
        return `<button class="source-shot ${s.id === D.broadcast.preview_scene_id ? 'preview' : ''}" data-preview="${s.id}"><span class="source-offline">${state}</span><strong>${esc(s.name)}</strong><small>${esc(camera?.device_id || 'NO CAMERA')}</small></button>`;
      }).join('')}</div></section>`;
    };
  }

  const stableStructure = () => {
    try {
      return JSON.stringify({
        view,
        display: window.__DISPLAY_SLUG__ || null,
        broadcast: [
          D?.broadcast?.program_scene_id,
          D?.broadcast?.preview_scene_id,
          D?.broadcast?.recording,
          D?.broadcast?.state
        ],
        scenes: (D?.broadcast_scenes || []).map(s => [
          s.id, s.name, s.scene_type, s.overlay_package_id, s.transition, s.public_safe,
          (s.sources || []).map(x => [x.kind, x.source, x.slot])
        ]),
        cameras: (D?.camera_profiles || []).map(c => [
          c.device_id, c.name, c.mode, c.configured, c.stream_url, c.popout_url
        ]),
        overlays: (D?.overlay_packages || []).map(p => [p.id, p.name, p.version, p.state, p.public_safe]),
        graphs: (D?.graph_definitions || []).map(g => [g.id, g.name, g.time_window, g.channels]),
        walls: (D?.video_walls || []).map(w => [w.id, w.name, w.grid, w.tiles]),
        displays: (D?.display_pages || []).map(p => [p.slug, p.name, p.purpose, p.resolution, p.layout]),
        destinations: (D?.stream_destinations || []).map(d => [
          d.id, d.name, d.provider, d.enabled, d.secret_configured, d.ingest_url
        ])
      });
    } catch (_) {
      return 'invalid';
    }
  };

  function patchLiveOnly() {
    syncHeader();

    const stateTag = document.querySelector('.page-title .tag');
    if (stateTag) {
      const value = String(D?.broadcast?.state || 'OFF_AIR');
      stateTag.textContent = value;
      stateTag.className = `tag ${value.toLowerCase().replaceAll('_','-')}`;
    }

    const readiness = document.querySelector('.production-readiness');
    if (readiness) {
      const program = typeof scene === 'function' ? scene(D.broadcast.program_scene_id) : null;
      const cameraReady = (D.camera_profiles || []).some(c => c.runtime_live) || program?.scene_type !== 'LIVE';
      const overlayReady = program?.public_safe !== false;
      const destinationReady = (D.stream_destinations || []).some(d => d.enabled && d.secret_configured);
      const ready = cameraReady && overlayReady && destinationReady;
      readiness.classList.toggle('ready', ready);
      readiness.classList.toggle('blocked', !ready);
      const state = readiness.querySelector('header span');
      if (state) state.textContent = ready ? 'READY' : 'BLOCKED';
    }

    if (typeof draw === 'function') draw();
  }

  refresh = async function stableMediaRefresh() {
    if (refreshPending || document.hidden) return;
    refreshPending = true;
    try {
      const r = await fetch('/api/media/snapshot', {cache: 'no-store'});
      if (!r.ok) throw new Error(`Media snapshot ${r.status}`);
      D = await r.json();
      samples.push(D.telemetry?.channels || {});
      if (samples.length > 240) samples.shift();

      const signature = stableStructure();
      const changed = signature !== lastStructure;

      // Normal telemetry/camera-health refreshes must never rebuild the DOM or
      // recreate Preview/Program MJPEG requests.
      if (!changed || operatorIsEditing()) {
        patchLiveOnly();
        return;
      }

      originalRender();
      lastStructure = stableStructure();
    } catch (e) {
      toast(e.message, true);
    } finally {
      refreshPending = false;
    }
  };

  render = function stableMediaRender(...args) {
    const result = originalRender.apply(this, args);
    lastStructure = stableStructure();
    return result;
  };

  // Do not intercept normal navigation. Once the scene-bank duplicate streams
  // are removed there are only two persistent broadcast requests, so Chrome has
  // free connections for System Configuration / Mission Control navigation.
  lastStructure = stableStructure();
})();
