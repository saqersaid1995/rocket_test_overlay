(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot',"'":'&#39;'}[c]));
  const value = (v, digits = 0, suffix = '') => (v === null || v === undefined || Number.isNaN(Number(v))) ? '—' : `${Number(v).toFixed(digits)}${suffix}`;
  const direction = (deg) => (deg === null || deg === undefined) ? '—' : `${Math.round(Number(deg))}°`;
  const visibility = (m) => (m === null || m === undefined) ? '—' : Number(m) >= 10000 ? '10+ km' : `${(Number(m) / 1000).toFixed(1)} km`;
  const compass = (deg) => {
    if (deg === null || deg === undefined) return '—';
    const dirs = ['N','NE','E','SE','S','SW','W','NW'];
    return dirs[Math.round(Number(deg) / 45) % 8];
  };

  let snapshot = null;
  let timer = null;
  let observer = null;
  let inserting = false;

  function installStyles() {
    if ($('#weather-panel-style')) return;
    const style = document.createElement('style');
    style.id = 'weather-panel-style';
    style.textContent = `
      #weather-strip.weather-panel{margin:0;border:1px solid #143445;background:#061117;overflow:hidden;min-height:0;align-self:start}
      #weather-strip.weather-panel>header{height:34px;padding:0 10px;display:flex;align-items:center;gap:8px;border-bottom:1px solid #143445;background:#07141b}
      #weather-strip.weather-panel>header>b{font-size:10px;letter-spacing:.09em;color:#d9eef7}
      #weather-strip.weather-panel>header>small{font-size:8px;letter-spacing:.08em;color:#4d7d93}
      #weather-strip .weather-panel-actions{margin-left:auto;display:flex;gap:5px}
      #weather-strip .weather-panel-actions button{height:23px;padding:0 8px;border-radius:0;font-size:8px}
      #weather-strip .weather-panel-body{padding:0}
      #weather-strip .weather-summary{display:grid;grid-template-columns:1.35fr .8fr;border-bottom:1px solid #143445}
      #weather-strip .weather-summary>div{padding:8px 10px;border-right:1px solid #143445;min-width:0}
      #weather-strip .weather-summary>div:last-child{border-right:0}
      #weather-strip .weather-summary small,#weather-strip .weather-metric small,#weather-strip .weather-level small{display:block;font-size:8px;letter-spacing:.08em;color:#4d7d93;text-transform:uppercase}
      #weather-strip .weather-summary strong{display:block;margin-top:3px;font-size:12px;color:#d9eef7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      #weather-strip .weather-status-good{color:#54e39b!important}
      #weather-strip .weather-status-stale{color:#e9ae42!important}
      #weather-strip .weather-status-bad{color:#ff6b6b!important}
      #weather-strip .weather-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border-bottom:1px solid #143445}
      #weather-strip .weather-metric{padding:9px 10px;border-right:1px solid #143445;border-bottom:1px solid #143445;min-width:0}
      #weather-strip .weather-metric:nth-child(2n){border-right:0}
      #weather-strip .weather-metric:nth-last-child(-n+2){border-bottom:0}
      #weather-strip .weather-metric strong{display:block;margin-top:4px;font-size:17px;font-weight:600;color:#d9eef7;line-height:1.05}
      #weather-strip .weather-metric em{display:block;margin-top:4px;font-size:9px;color:#6c8794;font-style:normal}
      #weather-strip .weather-section-title{padding:7px 10px;border-bottom:1px solid #143445;font-size:8px;letter-spacing:.09em;color:#4d7d93;text-transform:uppercase;background:#07141b}
      #weather-strip .weather-profile{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid #143445}
      #weather-strip .weather-level{padding:8px 10px;border-right:1px solid #143445}
      #weather-strip .weather-level:last-child{border-right:0}
      #weather-strip .weather-level b{display:block;margin-top:3px;font-size:12px;color:#d9eef7}
      #weather-strip .weather-level em{display:block;margin-top:3px;font-size:9px;color:#6c8794;font-style:normal}
      #weather-strip .weather-foot{padding:6px 10px;font-size:8px;line-height:1.45;color:#557786;letter-spacing:.02em}
      #weather-strip .weather-empty{padding:18px 12px;display:flex;flex-direction:column;gap:5px;min-height:90px;justify-content:center}
      #weather-strip .weather-empty b{font-size:11px;color:#d9eef7}
      #weather-strip .weather-empty span{font-size:9px;color:#6c8794;line-height:1.4}
      @media(max-width:900px){#weather-strip .weather-metrics{grid-template-columns:1fr}#weather-strip .weather-metric{border-right:0!important}#weather-strip .weather-metric:nth-last-child(-n+2){border-bottom:1px solid #143445}#weather-strip .weather-metric:last-child{border-bottom:0}}
    `;
    document.head.appendChild(style);
  }

  function bindPanelButtons() {
    const refresh = $('#weather-refresh');
    const location = $('#weather-location');
    if (refresh) refresh.onclick = () => loadWeather(true);
    if (location) location.onclick = openLocationDialog;
  }

  function ensureShell() {
    const workspace = $('#workspace');
    if (!workspace || inserting) return $('#weather-strip');
    let shell = $('#weather-strip');
    if (shell && shell.parentElement === workspace) return shell;

    inserting = true;
    if (shell) shell.remove();
    shell = document.createElement('section');
    shell.id = 'weather-strip';
    shell.className = 'panel span-1 weather-panel';
    shell.dataset.panel = 'weather';
    shell.innerHTML = `
      <header>
        <b>WEATHER / OPEN-METEO</b>
        <small>FORECAST / EXTERNAL DATA</small>
        <div class="weather-panel-actions">
          <button id="weather-refresh" type="button">REFRESH</button>
          <button id="weather-location" type="button">SET LOCATION</button>
        </div>
      </header>
      <div id="weather-body" class="weather-panel-body"><div class="weather-empty"><b>LOADING WEATHER</b><span>Retrieving Open-Meteo forecast for the operation site.</span></div></div>`;

    const command = workspace.querySelector('.panel[data-panel="command"]');
    const mission = workspace.querySelector('.panel[data-panel="mission"]');
    if (command) command.insertAdjacentElement('afterend', shell);
    else if (mission) mission.insertAdjacentElement('afterend', shell);
    else workspace.prepend(shell);
    bindPanelButtons();
    inserting = false;
    return shell;
  }

  function statusClass(status) {
    if (status === 'FORECAST') return 'weather-status-good';
    if (status === 'STALE_FORECAST') return 'weather-status-stale';
    return 'weather-status-bad';
  }

  function render() {
    const shell = ensureShell();
    if (!shell || !snapshot) return;
    const body = $('#weather-body');
    if (!body) return;
    const cfg = snapshot.settings || {};

    if (!snapshot.ok || !snapshot.weather) {
      body.innerHTML = `<div class="weather-empty"><b class="${statusClass(snapshot.status)}">${esc(snapshot.status || 'UNAVAILABLE')}</b><span>${esc(snapshot.message || 'Set the operation-site coordinates.')}</span></div>`;
      return;
    }

    const w = snapshot.weather;
    const a = w.wind?.['10m'] || {};
    const levels = ['80m','120m','180m'];
    body.innerHTML = `
      <div class="weather-summary">
        <div><small>OPERATION SITE</small><strong>${esc(cfg.site_name || 'Operation Site')}</strong></div>
        <div><small>STATUS</small><strong class="${statusClass(snapshot.status)}">${esc(snapshot.status || 'UNKNOWN')}</strong></div>
      </div>
      <div class="weather-metrics">
        <div class="weather-metric"><small>WIND 10 m</small><strong>${value(a.speed_kmh,1,' km/h')}</strong><em>${direction(a.direction_deg)} ${compass(a.direction_deg)}</em></div>
        <div class="weather-metric"><small>GUST</small><strong>${value(a.gust_kmh,1,' km/h')}</strong><em>forecast maximum gust</em></div>
        <div class="weather-metric"><small>TEMPERATURE</small><strong>${value(w.temperature_c,1,' °C')}</strong><em>${value(w.relative_humidity_percent,0,'% RH')}</em></div>
        <div class="weather-metric"><small>PRESSURE</small><strong>${value(w.surface_pressure_hpa,1,' hPa')}</strong><em>surface pressure</em></div>
        <div class="weather-metric"><small>VISIBILITY</small><strong>${visibility(w.visibility_m)}</strong><em>${value(w.cloud_cover_percent,0,'% cloud')}</em></div>
        <div class="weather-metric"><small>PRECIPITATION</small><strong>${value(w.precipitation_mm,1,' mm')}</strong><em>${value(w.precipitation_probability_percent,0,'% chance')}</em></div>
      </div>
      <div class="weather-section-title">WIND PROFILE · FORECAST / NOT ON-SITE TELEMETRY</div>
      <div class="weather-profile">
        ${levels.map(level => {
          const row = w.wind?.[level] || {};
          return `<div class="weather-level"><small>${level}</small><b>${value(row.speed_kmh,1,' km/h')}</b><em>${direction(row.direction_deg)} ${compass(row.direction_deg)}</em></div>`;
        }).join('')}
      </div>
      <div class="weather-foot">Forecast ${esc(w.forecast_time_utc || '—')} UTC<br>Updated ${esc((w.fetched_at_utc || '').slice(11,19) || '—')} UTC · Source Open-Meteo</div>`;
  }

  async function loadWeather(force = false) {
    ensureShell();
    try {
      const r = await fetch(`/api/weather${force ? '?refresh=1' : ''}`, {cache:'no-store'});
      snapshot = await r.json();
    } catch (e) {
      snapshot = {ok:false,status:'UNAVAILABLE',message:e.message,settings:snapshot?.settings || {}};
    }
    render();
  }

  function openLocationDialog() {
    const dialog = $('#weather-location-dialog');
    if (!dialog) return;
    const cfg = snapshot?.settings || {};
    $('#weather-site-name').value = cfg.site_name || '';
    $('#weather-latitude').value = cfg.latitude ?? '';
    $('#weather-longitude').value = cfg.longitude ?? '';
    dialog.showModal();
    $('#weather-site-name').focus();
  }

  function bindDialog() {
    const dialog = $('#weather-location-dialog');
    const form = $('#weather-location-form');
    if (!dialog || !form) return;
    $('#weather-location-cancel').onclick = () => dialog.close();
    form.onsubmit = async (e) => {
      e.preventDefault();
      const payload = {
        site_name: $('#weather-site-name').value.trim(),
        latitude: $('#weather-latitude').value,
        longitude: $('#weather-longitude').value,
      };
      const submit = $('#weather-location-save');
      submit.disabled = true;
      submit.textContent = 'SAVING…';
      try {
        const r = await fetch('/api/weather/location', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify(payload),
        });
        const result = await r.json();
        if (!r.ok) throw new Error(result.error || 'Unable to save weather location');
        snapshot = result.weather;
        dialog.close();
        render();
      } catch (err) {
        alert(`Weather location error: ${err.message}`);
      } finally {
        submit.disabled = false;
        submit.textContent = 'SAVE LOCATION';
      }
    };
  }

  function watchWorkspace() {
    const workspace = $('#workspace');
    if (!workspace || observer) return;
    observer = new MutationObserver(() => {
      if (inserting) return;
      if (!$('#weather-strip') || $('#weather-strip').parentElement !== workspace) {
        ensureShell();
        render();
      }
    });
    observer.observe(workspace, {childList:true});
  }

  function start() {
    const legacy = document.querySelector('body > #weather-strip');
    if (legacy) legacy.remove();
    installStyles();
    ensureShell();
    bindDialog();
    watchWorkspace();
    loadWeather(false);
    if (timer) clearInterval(timer);
    timer = setInterval(() => loadWeather(false), 300000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
