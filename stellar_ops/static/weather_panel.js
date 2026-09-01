(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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

  function ensureShell() {
    let shell = $('#weather-strip');
    if (shell) return shell;
    const alarm = $('#alarm-banner');
    if (!alarm) return null;
    shell = document.createElement('section');
    shell.id = 'weather-strip';
    shell.className = 'weather-strip';
    shell.innerHTML = `
      <div class="weather-head">
        <span><small>WEATHER SERVICE</small><strong>OPEN-METEO</strong></span>
        <span id="weather-site"><small>OPERATION SITE</small><strong>NOT CONFIGURED</strong></span>
        <span id="weather-status"><small>STATUS</small><strong>WAITING</strong></span>
        <div class="weather-actions"><button id="weather-refresh" type="button">REFRESH</button><button id="weather-location" type="button">SET LOCATION</button></div>
      </div>
      <div id="weather-body" class="weather-body"><div class="weather-empty">Weather data loading…</div></div>`;
    alarm.insertAdjacentElement('afterend', shell);
    $('#weather-refresh').onclick = () => loadWeather(true);
    $('#weather-location').onclick = openLocationDialog;
    return shell;
  }

  function render() {
    if (!ensureShell()) return;
    const site = $('#weather-site strong');
    const status = $('#weather-status strong');
    const body = $('#weather-body');
    if (!snapshot) return;
    const cfg = snapshot.settings || {};
    site.textContent = cfg.site_name || 'Operation Site';
    status.textContent = snapshot.status || 'UNKNOWN';

    if (!snapshot.ok || !snapshot.weather) {
      body.innerHTML = `<div class="weather-empty"><b>${esc(snapshot.status || 'UNAVAILABLE')}</b><span>${esc(snapshot.message || 'Set the operation-site coordinates.')}</span></div>`;
      return;
    }

    const w = snapshot.weather;
    const a = w.wind?.['10m'] || {};
    const levels = ['80m','120m','180m'];
    body.innerHTML = `
      <div class="weather-metrics">
        <div><small>WIND 10 m</small><strong>${value(a.speed_kmh,1,' km/h')}</strong><em>${direction(a.direction_deg)} ${compass(a.direction_deg)}</em></div>
        <div><small>GUST</small><strong>${value(a.gust_kmh,1,' km/h')}</strong><em>forecast</em></div>
        <div><small>TEMPERATURE</small><strong>${value(w.temperature_c,1,' °C')}</strong><em>${value(w.relative_humidity_percent,0,'% RH')}</em></div>
        <div><small>PRESSURE</small><strong>${value(w.surface_pressure_hpa,1,' hPa')}</strong><em>surface</em></div>
        <div><small>VISIBILITY</small><strong>${visibility(w.visibility_m)}</strong><em>${value(w.cloud_cover_percent,0,'% cloud')}</em></div>
        <div><small>PRECIPITATION</small><strong>${value(w.precipitation_mm,1,' mm')}</strong><em>${value(w.precipitation_probability_percent,0,'% chance')}</em></div>
      </div>
      <div class="weather-profile">
        <span><b>WIND PROFILE</b><small>FORECAST · NOT ON-SITE TELEMETRY</small></span>
        ${levels.map(level => {
          const row = w.wind?.[level] || {};
          return `<span><code>${level}</code><b>${value(row.speed_kmh,1,' km/h')}</b><em>${direction(row.direction_deg)} ${compass(row.direction_deg)}</em></span>`;
        }).join('')}
      </div>
      <div class="weather-foot">Forecast time ${esc(w.forecast_time_utc || '—')} UTC · Updated ${esc((w.fetched_at_utc || '').slice(11,19) || '—')} UTC · Source Open-Meteo</div>`;
  }

  async function loadWeather(force = false) {
    ensureShell();
    const status = $('#weather-status strong');
    if (status) status.textContent = 'UPDATING';
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

  function start() {
    ensureShell();
    bindDialog();
    loadWeather(false);
    if (timer) clearInterval(timer);
    timer = setInterval(() => loadWeather(false), 300000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
