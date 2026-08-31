(() => {
  if (typeof sceneMonitor !== 'function') return;

  const frameUrl = bus => `/api/media/bus/${bus}/frame.jpg`;

  sceneMonitor = function finiteSceneMonitor(s, bus = '') {
    const source = (s.sources || []).find(x => x.kind === 'camera');
    const camera = (D.camera_profiles || []).find(x => x.device_id === source?.source);
    const packageInfo = (D.overlay_packages || []).find(x => x.id === s.overlay_package_id);
    const picture = camera?.runtime_live
      ? `<img class="camera-layer" data-live-bus="${esc(bus || 'preview')}" src="${frameUrl(bus || 'preview')}?v=${Date.now()}" alt="${esc(camera.name)} live preview">`
      : `<div class="monitor-offline"><b>${esc(s.name)}</b><small>${camera ? esc(camera.runtime_status) : 'CONTROLLED SLATE'}</small></div>`;
    return `<div class="monitor">${picture}<div class="confidence-label"><b>${esc(s.name)}</b><span>${esc(camera?.device_id || s.scene_type)} · ${packageInfo ? esc(packageInfo.name) : 'NO OVERLAY'}</span></div></div>`;
  };

  if (typeof sourceDeck === 'function') {
    sourceDeck = function finiteSourceDeck() {
      const liveScenes = (D.broadcast_scenes || []).filter(s => s.scene_type === 'LIVE');
      return `<section class="source-deck"><header><b>CAMERAS & LIVE SCENES</b><small>SELECT DIRECTLY TO PREVIEW</small></header><div>${liveScenes.map(s => {
        const source = (s.sources || []).find(x => x.kind === 'camera');
        const camera = (D.camera_profiles || []).find(c => c.device_id === source?.source);
        const state = camera?.runtime_live ? 'LIVE' : esc(camera?.runtime_status || 'OFFLINE');
        return `<button class="source-shot ${s.id === D.broadcast.preview_scene_id ? 'preview' : ''}" data-preview="${s.id}"><span class="source-offline">${state}</span><strong>${esc(s.name)}</strong><small>${esc(camera?.device_id || 'NO CAMERA')}</small></button>`;
      }).join('')}</div></section>`;
    };
  }

  let refreshing = false;
  const refreshFrames = async () => {
    if (refreshing || document.hidden) return;
    const images = [...document.querySelectorAll('img[data-live-bus]')];
    if (!images.length) return;
    refreshing = true;
    try {
      await Promise.all(images.map(img => new Promise(resolve => {
        const bus = img.dataset.liveBus;
        const probe = new Image();
        probe.onload = () => {
          if (img.isConnected) img.src = probe.src;
          resolve();
        };
        probe.onerror = resolve;
        probe.src = `${frameUrl(bus)}?v=${Date.now()}-${Math.random()}`;
      })));
    } finally {
      refreshing = false;
    }
  };

  setInterval(refreshFrames, 200);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshFrames();
  });

  // Replace any MJPEG elements created by media.js immediately. This keeps the
  // browser connection pool free for normal page navigation.
  if (typeof render === 'function') render();
})();
