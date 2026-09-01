(() => {
  // Legacy compatibility loader. The complete Airspace implementation now lives
  // in one controller only: airspace_panel_v12.js.
  if (window.__AIRSPACE_V12_LOADER__) return;
  window.__AIRSPACE_V12_LOADER__ = true;

  if (window.__AIRSPACE_V11__?.destroy) window.__AIRSPACE_V11__.destroy();
  if (window.__AIRSPACE_LIVE_HOTFIX__?.destroy) window.__AIRSPACE_LIVE_HOTFIX__.destroy();

  const script = document.createElement('script');
  script.src = `/static/airspace_panel_v12.js?v=v12-single-controller-${Date.now()}`;
  script.async = false;
  script.dataset.airspaceV12 = 'true';
  script.onerror = () => {
    console.error('Failed to load consolidated Airspace controller v12');
  };
  document.head.appendChild(script);
})();