(() => {
  const state = window.__AIRSPACE_V10__;
  if (!state) return;

  if (window.__AIRSPACE_LIVE_HOTFIX__?.destroy) window.__AIRSPACE_LIVE_HOTFIX__.destroy();

  const ctl = new AbortController();
  const ctx = window.__AIRSPACE_LIVE_HOTFIX__ = {
    timer: null,
    busy: false,
    destroy() {
      ctl.abort();
      if (this.timer) clearInterval(this.timer);
      delete window.__AIRSPACE_LIVE_HOTFIX__;
    }
  };

  const rad = v => Number(v) * Math.PI / 180;
  function haversine(lat1, lon1, lat2, lon2) {
    const R = 6371.0088;
    const p1 = rad(lat1), p2 = rad(lat2), dp = rad(lat2-lat1), dl = rad(lon2-lon1);
    const a = Math.sin(dp/2)**2 + Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
  }
  function bearing(lat1, lon1, lat2, lon2) {
    const p1 = rad(lat1), p2 = rad(lat2), dl = rad(lon2-lon1);
    const y = Math.sin(dl) * Math.cos(p2);
    const x = Math.cos(p1)*Math.sin(p2) - Math.sin(p1)*Math.cos(p2)*Math.cos(dl);
    return (Math.atan2(y,x) * 180 / Math.PI + 360) % 360;
  }
  function normalize(raw) {
    if (!state.cfg) return null;
    const lat = Number(raw.lat), lon = Number(raw.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    let altitude = raw.alt_baro === 'ground' ? 0 : Number(raw.alt_baro ?? raw.alt_geom);
    if (!Number.isFinite(altitude)) altitude = null;
    const gs = Number(raw.gs), track = Number(raw.track);
    const distance = haversine(state.cfg.latitude, state.cfg.longitude, lat, lon);
    return {
      hex: String(raw.hex || `${lat}:${lon}`),
      callsign: String(raw.flight || '').trim() || '—',
      registration: String(raw.r || '').trim() || '—',
      aircraft_type: String(raw.t || '').trim() || '—',
      lat, lon,
      altitude_ft: altitude,
      ground_speed_kmh: Number.isFinite(gs) ? gs * 1.852 : null,
      track_deg: Number.isFinite(track) ? track : null,
      distance_km: distance,
      bearing_deg: bearing(state.cfg.latitude, state.cfg.longitude, lat, lon)
    };
  }

  async function withTimeout(url, ms) {
    const c = new AbortController();
    const timer = setTimeout(() => c.abort(), ms);
    const abort = () => c.abort();
    ctl.signal.addEventListener('abort', abort, {once:true});
    try {
      return await fetch(url, {cache:'no-store', signal:c.signal, headers:{Accept:'application/json'}});
    } finally {
      clearTimeout(timer);
      ctl.signal.removeEventListener('abort', abort);
    }
  }

  async function updateTraffic() {
    if (!state.cfg || ctx.busy) return;
    ctx.busy = true;
    state.loading = true;
    state.status = state.lastUpdated ? 'UPDATING' : 'CONNECTING';
    state.message = state.lastUpdated ? 'Refreshing live ADS-B / MLAT observations…' : 'Connecting to live ADS-B / MLAT source…';
    renderWorkspace();

    const radiusNm = Math.max(1, Math.ceil(Number(state.radius || 50) / 1.852));
    let errorText = '';
    try {
      let response = null;
      let payload = null;
      let provider = '';

      try {
        response = await withTimeout(`https://api.adsb.lol/v2/point/${state.cfg.latitude}/${state.cfg.longitude}/${radiusNm}?_=${Date.now()}`, 3200);
        if (!response.ok) throw new Error(`ADSB.lol HTTP ${response.status}`);
        payload = await response.json();
        provider = 'ADSB.lol';
      } catch (e) {
        errorText = e?.message || String(e);
      }

      if (payload) {
        const raw = payload.ac || payload.aircraft || [];
        state.traffic = raw.map(normalize).filter(Boolean).filter(a => a.distance_km <= Number(state.radius || 50)).sort((a,b) => a.distance_km-b.distance_km);
        state.provider = provider;
      } else {
        const q = new URLSearchParams({
          radius_km: String(state.radius || 50),
          lat: String(state.cfg.latitude),
          lon: String(state.cfg.longitude),
          site_name: state.cfg.site_name || 'Operation Site',
          _: String(Date.now())
        });
        response = await withTimeout(`/api/airspace/traffic?${q}`, 4500);
        const j = await response.json();
        if (!response.ok || !j.ok) throw new Error(j.message || `Server ADS-B HTTP ${response.status}`);
        state.traffic = (j.traffic?.aircraft || []).map(a => ({...a, distance_km:Number(a.distance_km), bearing_deg:Number(a.bearing_deg)})).sort((a,b) => a.distance_km-b.distance_km);
        state.provider = j.traffic?.provider || 'SERVER ADS-B';
      }

      state.lastUpdated = Date.now();
      state.status = 'OBSERVATIONAL';
      state.message = state.traffic.length ? `${state.traffic.length} live targets observed.` : 'No ADS-B / MLAT targets observed in this radius.';
    } catch (e) {
      state.status = 'UNAVAILABLE';
      state.message = `Traffic update failed: ${e?.message || errorText || 'source unavailable'}`;
    } finally {
      state.loading = false;
      ctx.busy = false;
      renderWorkspace();
    }
  }

  // Replace the slow 10 s timer created by v10 with a 3 s live refresh loop.
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  state.status = 'CONNECTING';
  state.message = 'Connecting to live ADS-B / MLAT source…';
  renderWorkspace();
  setTimeout(updateTraffic, 50);
  ctx.timer = setInterval(updateTraffic, 3000);
})();