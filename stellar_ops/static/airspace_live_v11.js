(() => {
  const state = window.__AIRSPACE_V10__;
  if (!state) return;

  if (window.__AIRSPACE_V11__?.destroy) window.__AIRSPACE_V11__.destroy();
  const ctl = new AbortController();
  const signal = ctl.signal;
  const ctx = window.__AIRSPACE_V11__ = {
    timer:null,
    busy:false,
    subtitleObserver:null,
    destroy(){
      ctl.abort();
      if(this.timer) clearInterval(this.timer);
      if(this.subtitleObserver) this.subtitleObserver.disconnect();
      delete window.__AIRSPACE_V11__;
    }
  };

  function fixSubtitle(){
    const p = document.querySelector('[data-panel="airspace"] header small');
    const text = 'LIVE TRAFFIC · 2 S UPDATE · SITUATIONAL AWARENESS';
    if (p && p.textContent !== text) p.textContent = text;
  }

  function renderNow(){
    try {
      renderWorkspace();
      fixSubtitle();
    } catch (_) {}
  }

  async function updateTraffic(){
    if (!state.cfg || ctx.busy) return;
    ctx.busy = true;
    state.loading = true;
    state.status = state.lastUpdated ? 'UPDATING' : 'CONNECTING';
    state.message = state.lastUpdated ? 'Refreshing live ADS-B / MLAT observations…' : 'Connecting to live ADS-B / MLAT source…';
    renderNow();

    const q = new URLSearchParams({
      radius_km:String(state.radius || 50),
      lat:String(state.cfg.latitude),
      lon:String(state.cfg.longitude),
      site_name:state.cfg.site_name || 'Operation Site',
      _:String(Date.now())
    });

    const c = new AbortController();
    const t = setTimeout(() => c.abort(), 6000);
    const abort = () => c.abort();
    signal.addEventListener('abort', abort, {once:true});
    try {
      const r = await fetch(`/api/airspace/traffic?${q}`, {cache:'no-store', signal:c.signal});
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.message || `HTTP ${r.status}`);

      state.traffic = (j.traffic?.aircraft || []).map(a => ({
        ...a,
        lat:Number(a.lat),
        lon:Number(a.lon),
        distance_km:Number(a.distance_km),
        bearing_deg:Number(a.bearing_deg),
        altitude_ft:a.altitude_ft == null ? null : Number(a.altitude_ft),
        ground_speed_kmh:a.ground_speed_kmh == null ? null : Number(a.ground_speed_kmh),
        track_deg:a.track_deg == null ? null : Number(a.track_deg)
      })).filter(a => Number.isFinite(a.lat) && Number.isFinite(a.lon)).sort((a,b)=>a.distance_km-b.distance_km);

      state.provider = j.traffic?.provider || 'SERVER ADS-B';
      state.status = j.status || 'OBSERVATIONAL';
      state.lastUpdated = Number(j.traffic?.fetched_at_epoch) > 0 ? Number(j.traffic.fetched_at_epoch) * 1000 : Date.now();
      state.message = state.traffic.length ? `${state.traffic.length} live targets observed.` : 'No ADS-B / MLAT targets observed in this radius.';
    } catch (e) {
      if (e.name !== 'AbortError') {
        state.status = 'UNAVAILABLE';
        state.message = `Traffic update failed: ${e.message || 'source unavailable'}`;
      }
    } finally {
      clearTimeout(t);
      signal.removeEventListener('abort', abort);
      state.loading = false;
      ctx.busy = false;
      renderNow();
    }
  }

  // Disable every older polling loop so only one live controller owns updates.
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  if (window.__AIRSPACE_LIVE_HOTFIX__?.destroy) window.__AIRSPACE_LIVE_HOTFIX__.destroy();

  state.status = 'CONNECTING';
  state.message = 'Connecting to live ADS-B / MLAT source…';
  renderNow();

  setTimeout(updateTraffic, 100);
  ctx.timer = setInterval(updateTraffic, 2000);

  // Observe only to repair the subtitle after workspace re-renders. The guard in
  // fixSubtitle prevents the observer from creating its own mutation loop.
  const workspace = document.getElementById('workspace');
  if (workspace) {
    ctx.subtitleObserver = new MutationObserver(fixSubtitle);
    ctx.subtitleObserver.observe(workspace, {childList:true,subtree:true});
  }
  fixSubtitle();
})();