(() => {
  const STORAGE_KEY = 'stellar_airspace_site_v1';
  const RADIUS_KEY = 'stellar_airspace_radius_v1';
  const RADII = [10, 25, 50, 100, 150];
  const REFRESH_MS = 2000;

  if (window.__AIRSPACE_V12__?.destroy) window.__AIRSPACE_V12__.destroy();

  const controller = new AbortController();
  const signal = controller.signal;
  const rad = v => Number(v) * Math.PI / 180;
  const valid = (lat, lon) => Number.isFinite(lat) && Number.isFinite(lon) && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180;
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (v, d = 0, suffix = '') => v == null || !Number.isFinite(Number(v)) ? '—' : `${Number(v).toFixed(d)}${suffix}`;
  const readStored = () => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const j = JSON.parse(raw);
      const lat = Number(j.latitude), lon = Number(j.longitude);
      return valid(lat, lon) ? {site_name: j.site_name || 'Operation Site', latitude: lat, longitude: lon} : null;
    } catch (_) { return null; }
  };
  const readRadius = () => {
    const r = Number(localStorage.getItem(RADIUS_KEY) || 50);
    return RADII.includes(r) ? r : 50;
  };
  const persist = cfg => { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg)); } catch (_) {} };
  const severity = d => d <= 10 ? 'CRITICAL' : d <= 25 ? 'CAUTION' : 'AWARENESS';
  const bearing = (a,b,c,d) => {
    const p1=rad(a), p2=rad(c), dl=rad(d-b);
    const y=Math.sin(dl)*Math.cos(p2);
    const x=Math.cos(p1)*Math.sin(p2)-Math.sin(p1)*Math.cos(p2)*Math.cos(dl);
    return (Math.atan2(y,x)*180/Math.PI+360)%360;
  };
  const diff = (a,b) => Math.abs(((Number(a)-Number(b)+540)%360)-180);

  const state = window.__AIRSPACE_V12__ = {
    cfg: readStored(),
    radius: readRadius(),
    traffic: [],
    provider: 'SERVER ADS-B',
    status: 'IDLE',
    message: 'Waiting for first traffic update…',
    lastUpdated: null,
    loading: false,
    selected: null,
    timer: null,
    request: null,
    observer: null,
    raf: 0,
    map: null,
    baseLayer: null,
    dynamicLayer: null,
    mapSignature: '',
    history: new Map(),
    destroy() {
      controller.abort();
      if (this.timer) clearInterval(this.timer);
      if (this.request) this.request.abort();
      if (this.observer) this.observer.disconnect();
      if (this.raf) cancelAnimationFrame(this.raf);
      if (this.map?.isConnected) this.map.remove();
      delete window.__AIRSPACE_V12__;
    }
  };

  function trend(a) {
    if (a.track_deg == null || !state.cfg) return 'UNKNOWN';
    const toSite = bearing(a.lat, a.lon, state.cfg.latitude, state.cfg.longitude);
    const d = diff(a.track_deg, toSite);
    return d <= 45 ? 'APPROACHING' : d >= 135 ? 'DEPARTING' : 'CROSSING';
  }

  function installStyles() {
    if (document.getElementById('airspace-v12-style')) return;
    const s = document.createElement('style');
    s.id = 'airspace-v12-style';
    s.textContent = `
      .air12-shell{min-height:430px;background:#061117;color:#d9eef7}.air12-setup{min-height:390px;display:flex;align-items:center;justify-content:center;padding:18px}.air12-card{width:min(560px,92%);border:1px solid #143445;background:#07141b;padding:14px}.air12-card h3{margin:0 0 6px;font-size:12px}.air12-card p{margin:0 0 12px;font-size:8px;color:#6c8794}.air12-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.air12-grid label:first-child{grid-column:1/-1}.air12-grid label{display:flex;flex-direction:column;gap:4px;font-size:7px;letter-spacing:.08em;color:#4d7d93}.air12-grid input{height:29px;padding:0 8px;border:1px solid #245063;background:#041016;color:#d9eef7}.air12-actions{display:flex;align-items:center;gap:8px;margin-top:10px}.air12-actions button,.air12-controls button,.air12-controls select{height:27px;border:1px solid #245063;background:#07141b;color:#cdeaf6;font-size:8px;padding:0 9px}.air12-error{font-size:8px;color:#ff7777}
      .air12-top{display:grid;grid-template-columns:minmax(150px,1.2fr) minmax(120px,.8fr) 130px auto;border-bottom:1px solid #143445;background:#07141b}.air12-cell{padding:7px 9px;border-right:1px solid #143445;min-width:0}.air12-cell small,.air12-summary small{display:block;font-size:7px;letter-spacing:.08em;color:#4d7d93}.air12-cell b{display:block;margin-top:2px;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.air12-controls{display:flex;align-items:center;gap:6px;padding:6px 8px}.air12-controls .primary{border-color:#2f7894;color:#29c8f0}
      .air12-layout{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(330px,.8fr);min-height:370px}.air12-map-host{position:relative;min-height:370px;background:#031017;border-right:1px solid #143445;overflow:hidden}.air12-side{min-width:0;display:flex;flex-direction:column}.air12-summary{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid #143445}.air12-summary>div{padding:8px;border-right:1px solid #143445}.air12-summary strong{display:block;margin-top:3px;font-size:13px}.air12-summary em{display:block;margin-top:2px;font-size:7px;color:#6c8794;font-style:normal}.air12-list{overflow:auto;max-height:270px}.air12-table{width:100%;border-collapse:collapse;font-size:8px}.air12-table th{position:sticky;top:0;background:#07141b;color:#4d7d93;text-align:left;padding:6px;border-bottom:1px solid #143445}.air12-table td{padding:6px;border-bottom:1px solid #102b38}.air12-table tbody tr{cursor:pointer}.air12-table tbody tr:hover,.air12-table tr.selected{background:#0a1b23}.air12-table code{font-size:9px;color:#d9eef7}.air12-table small{display:block;margin-top:2px;color:#6c8794}.air12-critical{color:#ff6767}.air12-caution{color:#e9ae42}.air12-awareness{color:#64cceb}.air12-empty{padding:16px;color:#6c8794;font-size:9px}.air12-foot{margin-top:auto;padding:7px 9px;border-top:1px solid #143445;color:#6c8794;font-size:7px}.air12-foot b{color:#e9ae42}
      #airspace-v12-map{position:fixed;display:none;z-index:20;overflow:hidden;background:#07141b}#airspace-v12-map .air12-base,#airspace-v12-map .air12-dynamic{position:absolute;inset:0}#airspace-v12-map .tile{position:absolute;width:256px;height:256px;user-select:none;pointer-events:none}#airspace-v12-map .ring{position:absolute;border:1px solid rgba(41,200,240,.72);border-radius:50%;transform:translate(-50%,-50%)}#airspace-v12-map .ring.inner{border-color:rgba(233,174,66,.72)}#airspace-v12-map .ring.critical{border-color:rgba(255,103,103,.82)}#airspace-v12-map .site{position:absolute;width:11px;height:11px;border:2px solid #fff;background:#29c8f0;border-radius:50%;transform:translate(-50%,-50%)}#airspace-v12-map .plane{position:absolute;transform:translate(-50%,-50%);border:0;background:transparent;color:#d9eef7;font-size:17px;cursor:pointer;text-shadow:0 0 3px #000;transition:left .35s linear,top .35s linear,rotate .35s linear}#airspace-v12-map .plane.caution{color:#e9ae42}#airspace-v12-map .plane.critical{color:#ff6767}#airspace-v12-map .plane.selected{font-size:21px;filter:drop-shadow(0 0 4px #fff)}#airspace-v12-map .maplabel,#airspace-v12-map .updated{position:absolute;left:8px;padding:4px 6px;background:rgba(3,13,18,.88);border:1px solid #143445;color:#7ea6b7;font-size:7px;z-index:3}#airspace-v12-map .maplabel{top:8px}#airspace-v12-map .updated{bottom:6px}#airspace-v12-map .attrib{position:absolute;right:4px;bottom:3px;padding:2px 4px;background:rgba(255,255,255,.78);color:#222;font-size:7px;z-index:3}#airspace-v12-map .air12-tracks{position:absolute;inset:0;z-index:2;overflow:visible}#airspace-v12-map .trail{fill:none;stroke:#64cceb;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;opacity:.55}#airspace-v12-map .trail.caution{stroke:#e9ae42}#airspace-v12-map .trail.critical{stroke:#ff6767}#airspace-v12-map .heading{stroke:#64cceb;stroke-width:1.25;stroke-dasharray:4 3;opacity:.75}#airspace-v12-map .heading.caution{stroke:#e9ae42}#airspace-v12-map .heading.critical{stroke:#ff6767}
      @media(max-width:1100px){.air12-layout{grid-template-columns:1fr}.air12-map-host{border-right:0;border-bottom:1px solid #143445}.air12-top{grid-template-columns:1fr 1fr}.air12-controls{grid-column:1/-1}.air12-summary{grid-template-columns:1fr 1fr}}
    `;
    document.head.appendChild(s);
  }

  function setupBody() {
    const cfg = state.cfg || {site_name:'Operation Site', latitude:24.2601511, longitude:55.789476};
    return `<div class="air12-shell"><div class="air12-setup"><div class="air12-card"><h3>SET OPERATION SITE</h3><p>Enter the coordinates used as the centre of the traffic monitor.</p><div class="air12-grid"><label>SITE NAME<input id="air12-name" value="${esc(cfg.site_name)}"></label><label>LATITUDE<input id="air12-lat" type="number" step="any" value="${cfg.latitude}"></label><label>LONGITUDE<input id="air12-lon" type="number" step="any" value="${cfg.longitude}"></label></div><div class="air12-actions"><button id="air12-save" type="button">SAVE LOCATION</button><span id="air12-error" class="air12-error"></span></div></div></div></div>`;
  }

  function dataBody() {
    return `<div class="air12-shell"><div class="air12-top"><div class="air12-cell"><small>OPERATION SITE</small><b id="air12-site">—</b></div><div class="air12-cell"><small>TRAFFIC SOURCE</small><b id="air12-provider">—</b></div><div class="air12-cell"><small>LAST UPDATE</small><b id="air12-updated">—</b></div><div class="air12-controls"><select id="air12-radius">${RADII.map(v=>`<option value="${v}">${v} km</option>`).join('')}</select><button id="air12-refresh" class="primary" type="button">REFRESH</button><button id="air12-change" type="button">LOCATION</button></div></div><div class="air12-layout"><div class="air12-map-host" id="air12-map-host"></div><div class="air12-side"><div class="air12-summary"><div><small>OBSERVED</small><strong id="air12-observed">0</strong><em id="air12-radius-label">—</em></div><div><small>NEAREST</small><strong id="air12-nearest">—</strong><em id="air12-nearest-name">no target</em></div><div><small>APPROACHING</small><strong id="air12-approaching">0</strong><em>track assessment</em></div><div><small>ALERTS</small><strong id="air12-alerts" class="air12-awareness">NONE</strong><em id="air12-status">IDLE</em></div></div><div id="air12-list" class="air12-list"></div><div class="air12-foot"><b>OBSERVATIONAL ONLY.</b> Absence of an ADS-B/MLAT target does not mean the airspace is clear. CAA/AIS/ATC and NOTAM verification remain authoritative.</div></div></div></div>`;
  }

  function panelBody() { return state.cfg ? dataBody() : setupBody(); }
  function airspacePanel(item) { return panelShell(item, panelBody(), 'LIVE TRAFFIC · 2 S UPDATE · SITUATIONAL AWARENESS'); }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el && el.textContent !== String(value)) el.textContent = String(value);
  }

  function ensurePanelMode() {
    const section = document.querySelector('[data-panel="airspace"]');
    const body = section?.querySelector('.panel-body');
    if (!body) return false;
    const wantsData = !!state.cfg;
    const hasData = !!document.getElementById('air12-map-host');
    const hasSetup = !!document.getElementById('air12-save');
    if ((wantsData && !hasData) || (!wantsData && !hasSetup)) body.innerHTML = panelBody();
    return true;
  }

  function updatePanel() {
    if (!ensurePanelMode() || !state.cfg) {
      queueMap();
      return;
    }

    const ac = state.traffic;
    const near = ac[0] || null;
    const crit = ac.filter(a => a.distance_km <= 10).length;
    const caut = ac.filter(a => a.distance_km > 10 && a.distance_km <= 25).length;
    const app = ac.filter(a => trend(a) === 'APPROACHING').length;
    const updated = state.lastUpdated ? new Date(state.lastUpdated).toISOString().slice(11,19) + 'Z' : '—';

    setText('air12-site', state.cfg.site_name);
    setText('air12-provider', state.provider);
    setText('air12-updated', updated);
    setText('air12-observed', ac.length);
    setText('air12-radius-label', `${state.radius} km radius`);
    setText('air12-nearest', near ? fmt(near.distance_km,1,' km') : '—');
    setText('air12-nearest-name', near?.callsign || 'no target');
    setText('air12-approaching', app);
    setText('air12-status', state.status);

    const radius = document.getElementById('air12-radius');
    if (radius && Number(radius.value) !== state.radius) radius.value = String(state.radius);
    const refresh = document.getElementById('air12-refresh');
    if (refresh) {
      refresh.textContent = 'REFRESH';
      refresh.classList.toggle('loading', state.loading);
      refresh.disabled = state.loading;
    }

    const alerts = document.getElementById('air12-alerts');
    if (alerts) {
      alerts.textContent = crit ? `${crit} CRIT` : caut ? `${caut} CAUT` : 'NONE';
      alerts.className = crit ? 'air12-critical' : caut ? 'air12-caution' : 'air12-awareness';
    }

    const list = document.getElementById('air12-list');
    if (list) {
      const rows = ac.slice(0,60).map(a => {
        const sev = severity(a.distance_km);
        return `<tr data-air12-hex="${esc(a.hex)}" class="${state.selected===a.hex?'selected':''}"><td><code>${esc(a.callsign||'—')}</code><small>${esc(a.aircraft_type||a.registration||'—')}</small></td><td class="air12-${sev.toLowerCase()}">${fmt(a.distance_km,1,' km')}<small>${fmt(a.bearing_deg,0,'°')}</small></td><td>${fmt(a.altitude_ft,0,' ft')}<small>${fmt(a.ground_speed_kmh,0,' km/h')}</small></td><td>${fmt(a.track_deg,0,'°')}<small>${trend(a)}</small></td></tr>`;
      }).join('');
      const html = rows ? `<table class="air12-table"><thead><tr><th>CALLSIGN</th><th>DIST / BRG</th><th>ALT / SPEED</th><th>TRACK / TREND</th></tr></thead><tbody>${rows}</tbody></table>` : `<div class="air12-empty">${esc(state.message)}</div>`;
      if (list.dataset.signature !== html) {
        list.innerHTML = html;
        list.dataset.signature = html;
      }
    }
    queueMap();
  }

  async function loadTraffic() {
    if (!state.cfg || state.loading) return;
    state.loading = true;
    state.status = state.lastUpdated ? 'UPDATING' : 'CONNECTING';
    state.message = state.lastUpdated ? 'Refreshing live ADS-B / MLAT observations…' : 'Connecting to live ADS-B / MLAT source…';
    updatePanel();

    if (state.request) state.request.abort();
    state.request = new AbortController();
    const timeout = setTimeout(() => state.request?.abort(), 6500);
    const q = new URLSearchParams({radius_km:String(state.radius), lat:String(state.cfg.latitude), lon:String(state.cfg.longitude), site_name:state.cfg.site_name, _:String(Date.now())});

    try {
      const r = await fetch(`/api/airspace/traffic?${q}`, {cache:'no-store', signal:state.request.signal});
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.message || `HTTP ${r.status}`);
      state.traffic = (j.traffic?.aircraft || []).map(a => ({
        ...a,
        lat:Number(a.lat), lon:Number(a.lon), distance_km:Number(a.distance_km), bearing_deg:Number(a.bearing_deg),
        altitude_ft:a.altitude_ft==null?null:Number(a.altitude_ft),
        ground_speed_kmh:a.ground_speed_kmh==null?null:Number(a.ground_speed_kmh),
        track_deg:a.track_deg==null?null:Number(a.track_deg)
      })).filter(a => Number.isFinite(a.lat) && Number.isFinite(a.lon) && Number.isFinite(a.distance_km)).sort((a,b)=>a.distance_km-b.distance_km);
      const seenHex = new Set(state.traffic.map(a => a.hex));
      for (const hex of state.history.keys()) if (!seenHex.has(hex)) state.history.delete(hex);
      state.traffic.forEach(a => {
        if (!a.hex) return;
        const h = state.history.get(a.hex) || [];
        const last = h[h.length - 1];
        if (!last || last.lat !== a.lat || last.lon !== a.lon) {
          h.push({lat:a.lat, lon:a.lon});
          if (h.length > 6) h.shift();
        }
        state.history.set(a.hex, h);
      });
      state.provider = j.traffic?.provider || 'SERVER ADS-B';
      state.status = j.status || 'OBSERVATIONAL';
      const ts = Number(j.traffic?.fetched_at_epoch);
      state.lastUpdated = ts > 0 ? ts * 1000 : Date.now();
      state.message = state.traffic.length ? `${state.traffic.length} live targets observed.` : 'No ADS-B / MLAT targets observed in this radius.';
    } catch (e) {
      if (e.name === 'AbortError') {
        if (!state.lastUpdated) {
          state.status = 'TIMEOUT';
          state.message = 'Traffic source timed out. Retry or wait for the next automatic update.';
        }
      } else {
        state.status = 'UNAVAILABLE';
        state.message = `Traffic update failed: ${e.message || 'source unavailable'}`;
      }
    } finally {
      clearTimeout(timeout);
      state.loading = false;
      state.request = null;
      updatePanel();
    }
  }

  const zoomFor = r => r<=12?11:r<=28?10:r<=60?9:r<=120?8:7;
  function worldPixel(lat,lon,z) {
    const scale=256*2**z, x=(lon+180)/360*scale, s=Math.sin(rad(lat));
    const y=(0.5-Math.log((1+s)/(1-s))/(4*Math.PI))*scale;
    return {x,y};
  }

  function ensureMap() {
    if (state.map?.isConnected) return state.map;
    const map = document.createElement('div');
    map.id = 'airspace-v12-map';
    const base = document.createElement('div');
    base.className = 'air12-base';
    const dynamic = document.createElement('div');
    dynamic.className = 'air12-dynamic';
    map.append(base, dynamic);
    document.body.appendChild(map);
    state.map = map;
    state.baseLayer = base;
    state.dynamicLayer = dynamic;
    state.mapSignature = '';
    return map;
  }

  function rebuildBase(w,h,z,left,top,n,mpp) {
    const base = state.baseLayer;
    if (!base) return;
    const frag = document.createDocumentFragment();
    for (let tx=Math.floor(left/256)-1; tx<=Math.floor((left+w)/256)+1; tx++) {
      for (let ty=Math.floor(top/256)-1; ty<=Math.floor((top+h)/256)+1; ty++) {
        if (ty<0 || ty>=n) continue;
        const x=((tx%n)+n)%n;
        const img=document.createElement('img');
        img.className='tile'; img.alt=''; img.draggable=false;
        img.src=`/api/airspace/tile/${z}/${x}/${ty}.png`;
        img.style.left=`${tx*256-left}px`; img.style.top=`${ty*256-top}px`;
        frag.appendChild(img);
      }
    }
    [[state.radius,''],[25,'inner'],[10,'critical']].forEach(([km,klass]) => {
      if (km > state.radius) return;
      const ring=document.createElement('div');
      const d=Number(km)*2000/mpp;
      ring.className=`ring ${klass}`;
      ring.style.cssText=`width:${d}px;height:${d}px;left:${w/2}px;top:${h/2}px`;
      frag.appendChild(ring);
    });
    const site=document.createElement('div');
    site.className='site'; site.style.left=`${w/2}px`; site.style.top=`${h/2}px`;
    frag.appendChild(site);
    const label=document.createElement('div');
    label.className='maplabel'; label.textContent=`${state.cfg.site_name} · ${state.radius} km monitor`;
    frag.appendChild(label);
    const at=document.createElement('div');
    at.className='attrib'; at.textContent='© OpenStreetMap contributors';
    frag.appendChild(at);
    base.replaceChildren(frag);
  }

  function updateDynamic(w,h,z,left,top,mpp) {
    const layer = state.dynamicLayer;
    if (!layer) return;
    let tracks = layer.querySelector('svg.air12-tracks');
    if (!tracks) {
      tracks = document.createElementNS('http://www.w3.org/2000/svg','svg');
      tracks.setAttribute('class','air12-tracks');
      layer.insertBefore(tracks, layer.firstChild);
    }
    tracks.setAttribute('width', w); tracks.setAttribute('height', h);
    tracks.setAttribute('viewBox', `0 0 ${w} ${h}`);
    const trackFrag = document.createDocumentFragment();

    const existing = new Map([...layer.querySelectorAll('.plane')].map(el => [el.dataset.hex, el]));
    const keep = new Set();

    state.traffic.forEach(a => {
      const p=worldPixel(a.lat,a.lon,z), x=p.x-left, y=p.y-top;
      if (x<-20 || x>w+20 || y<-20 || y>h+20) return;
      keep.add(a.hex);
      let b=existing.get(a.hex);
      if (!b) {
        b=document.createElement('button');
        b.type='button'; b.className='plane'; b.dataset.hex=a.hex; b.textContent='✈';
        layer.appendChild(b);
      }
      const sev=severity(a.distance_km);
      const sevClass=sev==='CRITICAL'?'critical':sev==='CAUTION'?'caution':'';
      b.className=`plane ${sevClass} ${state.selected===a.hex?'selected':''}`;
      b.style.left=`${x}px`; b.style.top=`${y}px`; b.style.rotate=`${Number(a.track_deg||0)}deg`;
      b.title=`${a.callsign||'—'} · ${fmt(a.distance_km,1,' km')} · ${fmt(a.altitude_ft,0,' ft')}`;

      const hist=state.history.get(a.hex);
      if (hist && hist.length>=2) {
        const pts=hist.map(pt=>{const hp=worldPixel(pt.lat,pt.lon,z); return `${(hp.x-left).toFixed(1)},${(hp.y-top).toFixed(1)}`;}).join(' ')+` ${x.toFixed(1)},${y.toFixed(1)}`;
        const poly=document.createElementNS('http://www.w3.org/2000/svg','polyline');
        poly.setAttribute('points',pts); poly.setAttribute('class',`trail ${sevClass}`);
        trackFrag.appendChild(poly);
      }
      if (a.track_deg!=null && a.ground_speed_kmh) {
        const LOOKAHEAD_S=120;
        const distPx=(a.ground_speed_kmh*LOOKAHEAD_S/3600*1000)/mpp;
        const bearing=a.track_deg*Math.PI/180;
        const dx=Math.sin(bearing)*distPx, dy=-Math.cos(bearing)*distPx;
        const line=document.createElementNS('http://www.w3.org/2000/svg','line');
        line.setAttribute('x1',x.toFixed(1)); line.setAttribute('y1',y.toFixed(1));
        line.setAttribute('x2',(x+dx).toFixed(1)); line.setAttribute('y2',(y+dy).toFixed(1));
        line.setAttribute('class',`heading ${sevClass}`);
        trackFrag.appendChild(line);
      }
    });

    tracks.replaceChildren(trackFrag);
    existing.forEach((el, hex) => { if (!keep.has(hex)) el.remove(); });

    let upd = layer.querySelector('.updated');
    if (!upd) {
      upd=document.createElement('div'); upd.className='updated'; layer.appendChild(upd);
    }
    upd.textContent=`LIVE · ${state.lastUpdated?new Date(state.lastUpdated).toISOString().slice(11,19)+'Z':'waiting'}`;
  }

  function drawMap() {
    state.raf = 0;
    const host=document.getElementById('air12-map-host');
    if (!state.cfg || !host) {
      if (state.map) state.map.style.display='none';
      return;
    }

    const map=ensureMap();
    const r=host.getBoundingClientRect();
    if (r.width<20 || r.height<20 || r.bottom<0 || r.top>innerHeight) {
      map.style.display='none';
      return;
    }

    map.style.display='block';
    map.style.left=`${Math.round(r.left)}px`;
    map.style.top=`${Math.round(r.top)}px`;
    map.style.width=`${Math.round(r.width)}px`;
    map.style.height=`${Math.round(r.height)}px`;

    const w=r.width, h=r.height, z=zoomFor(state.radius);
    const center=worldPixel(state.cfg.latitude,state.cfg.longitude,z);
    const left=center.x-w/2, top=center.y-h/2, n=2**z;
    const mpp=156543.03392*Math.cos(rad(state.cfg.latitude))/2**z;
    const signature=[state.cfg.latitude.toFixed(6),state.cfg.longitude.toFixed(6),state.radius,z,Math.round(w),Math.round(h),Math.round(left),Math.round(top)].join('|');

    if (signature !== state.mapSignature) {
      rebuildBase(w,h,z,left,top,n,mpp);
      state.mapSignature = signature;
    }
    updateDynamic(w,h,z,left,top,mpp);
  }

  function queueMap() {
    if (!state.raf) state.raf=requestAnimationFrame(drawMap);
  }

  document.addEventListener('click', e => {
    const t=e.target instanceof Element ? e.target.closest('button,tr') : null;
    if (!t) return;
    if (t.id==='air12-save') {
      e.preventDefault();
      const lat=Number(document.getElementById('air12-lat')?.value), lon=Number(document.getElementById('air12-lon')?.value), name=(document.getElementById('air12-name')?.value||'Operation Site').trim();
      if (!valid(lat,lon)) {
        const er=document.getElementById('air12-error'); if(er) er.textContent='Enter valid coordinates.';
        return;
      }
      state.cfg={site_name:name||'Operation Site',latitude:lat,longitude:lon};
      persist(state.cfg);
      state.traffic=[]; state.lastUpdated=null; state.status='IDLE'; state.message='Waiting for first traffic update…';
      state.mapSignature=''; ensurePanelMode(); updatePanel(); setTimeout(loadTraffic,0); return;
    }
    if (t.id==='air12-refresh') { e.preventDefault(); loadTraffic(); return; }
    if (t.id==='air12-change') {
      e.preventDefault();
      if(state.request) state.request.abort();
      state.traffic=[]; state.lastUpdated=null; state.selected=null; state.status='IDLE'; state.message='Waiting for first traffic update…'; state.cfg=null; state.mapSignature='';
      try{localStorage.removeItem(STORAGE_KEY);}catch(_){}
      if(state.map) state.map.style.display='none';
      ensurePanelMode(); return;
    }
    if (t.dataset?.air12Hex) { state.selected=t.dataset.air12Hex; updatePanel(); return; }
    if (t.classList?.contains('plane') && t.dataset.hex) { state.selected=t.dataset.hex; updatePanel(); }
  }, {capture:true, signal});

  document.addEventListener('change', e => {
    if (e.target?.id!=='air12-radius') return;
    const r=Number(e.target.value)||50;
    state.radius=RADII.includes(r)?r:50;
    try{localStorage.setItem(RADIUS_KEY,String(state.radius));}catch(_){}
    state.traffic=[]; state.lastUpdated=null; state.status='IDLE'; state.message='Waiting for first traffic update…'; state.mapSignature='';
    updatePanel(); setTimeout(loadTraffic,0);
  }, {signal});

  installStyles();
  PANEL_NAMES.airspace='AIRSPACE & LIVE TRAFFIC';
  renderers.airspace=airspacePanel;

  const requested=new URLSearchParams(location.search).get('panel');
  if(requested==='airspace') {
    document.body.classList.add('popout');
    layout=[{panel:'airspace',span:3,order:0}];
  } else if(!layout.some(x=>x.panel==='airspace')) {
    const i=layout.findIndex(x=>x.panel==='command'), at=i>=0?i+1:layout.length;
    layout.splice(at,0,{panel:'airspace',span:2,order:at});
    layout.forEach((x,n)=>x.order=n);
  }

  renderWorkspace();
  updatePanel();

  const workspace=document.getElementById('workspace');
  if(workspace) {
    state.observer=new MutationObserver(mutations => {
      if (mutations.some(m => m.type==='childList' && m.target===workspace)) {
        ensurePanelMode();
        updatePanel();
      } else {
        queueMap();
      }
    });
    state.observer.observe(workspace,{childList:true,subtree:false});
  }

  addEventListener('resize',()=>{state.mapSignature='';queueMap();},{passive:true,signal});
  addEventListener('scroll',()=>queueMap(),{passive:true,capture:true,signal});

  if(state.cfg) setTimeout(loadTraffic,100);
  state.timer=setInterval(()=>{if(state.cfg&&!state.loading)loadTraffic();},REFRESH_MS);
})();