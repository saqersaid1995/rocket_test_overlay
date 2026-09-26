(() => {
  const button = document.querySelector('#pressure-log');
  const dialog = document.querySelector('#pressure-capture-dialog');
  const body = document.querySelector('#pressure-capture-body');
  const close = document.querySelector('#pressure-capture-close');
  if (!button || !dialog || !body) return;

  let active = null;
  let timer = null;

  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[ch]);

  const formatTime = seconds => {
    const total = Math.max(0, Number(seconds || 0));
    const minutes = Math.floor(total / 60);
    return `${String(minutes).padStart(2,'0')}:${(total % 60).toFixed(1).padStart(4,'0')}`;
  };

  function paintButton() {
    const isRecording = Boolean(active?.active);
    button.classList.toggle('pressure-recording', isRecording);
    if (isRecording) {
      button.textContent =
        `PRESSURE LOG ${formatTime(active.elapsed_s)} · ${Number(active.current_pressure_bar || 0).toFixed(2)} bar · PK ${Number(active.peak_bar || 0).toFixed(2)}`;
      button.title = 'Full-rate 200 Hz pressure capture is active';
    } else {
      button.textContent = 'PRESSURE LOG';
      button.title = 'Open high-rate pressure capture log';
    }
  }

  async function readStatus() {
    try {
      const response = await fetch('/api/pressure-capture/status', {cache:'no-store'});
      const payload = await response.json();
      if (response.ok) active = payload.active || null;
    } catch (_) {
      // Keep the last known state through a transient UI/network error.
    }
    paintButton();
  }

  async function loadCaptures() {
    body.innerHTML = '<div class="pressure-capture-loading">LOADING PRESSURE CAPTURES…</div>';
    try {
      const response = await fetch('/api/pressure-capture', {cache:'no-store'});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Unable to read pressure captures');
      const captures = payload.captures || [];
      if (!captures.length) {
        body.innerHTML = '<div class="pressure-capture-empty">NO PRESSURE CAPTURES YET</div>';
        return;
      }
      body.innerHTML = `
        <table class="pressure-capture-table">
          <thead><tr>
            <th>ID</th><th>IGNITION UTC</th><th>STATE</th><th>TIME</th>
            <th>CURRENT</th><th>PEAK</th><th>SAMPLES</th><th>MISSING</th><th>ACTIONS</th>
          </tr></thead>
          <tbody>
            ${captures.map(capture => `
              <tr class="${capture.active ? 'capture-active' : ''}">
                <td><code>#${capture.id}</code></td>
                <td>${esc(capture.trigger_at)}</td>
                <td><b>${esc(capture.state)}</b></td>
                <td>${formatTime(capture.elapsed_s)}</td>
                <td>${Number(capture.current_pressure_bar || 0).toFixed(3)} bar</td>
                <td>${Number(capture.peak_bar || 0).toFixed(3)} bar</td>
                <td>${Number(capture.sample_count || 0).toLocaleString()}</td>
                <td class="${Number(capture.missing_samples || 0) ? 'capture-gap' : ''}">${Number(capture.missing_samples || 0).toLocaleString()}</td>
                <td class="pressure-capture-actions">
                  ${capture.active ? `<button data-stop-capture="${capture.id}" class="danger">STOP</button>` : ''}
                  <a class="button-link" href="/api/pressure-capture/${capture.id}/excel">EXCEL</a>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>`;
      body.querySelectorAll('[data-stop-capture]').forEach(stopButton => {
        stopButton.addEventListener('click', async () => {
          stopButton.disabled = true;
          try {
            const response = await fetch(
              `/api/pressure-capture/${stopButton.dataset.stopCapture}/stop`,
              {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({reason:'MANUAL_STOP'})
              }
            );
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || 'Unable to stop pressure capture');
            if (typeof toast === 'function') toast('Pressure capture stopped');
            await readStatus();
            await loadCaptures();
          } catch (error) {
            if (typeof toast === 'function') toast(error.message, true);
            stopButton.disabled = false;
          }
        });
      });
    } catch (error) {
      body.innerHTML = `<div class="pressure-capture-empty">${esc(error.message)}</div>`;
    }
  }

  button.addEventListener('click', async event => {
    event.preventDefault();
    await loadCaptures();
    dialog.showModal();
  });

  close?.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });

  readStatus();
  timer = window.setInterval(async () => {
    await readStatus();
    if (dialog.open) await loadCaptures();
  }, 500);

  window.addEventListener('beforeunload', () => {
    if (timer) window.clearInterval(timer);
  });
})();
