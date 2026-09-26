(() => {
  const button = document.querySelector('#mission-video-record');
  if (!button) return;

  let state = 'STOPPED';
  let startedAt = null;
  let busy = false;

  function elapsedText() {
    if (!startedAt) return '00:00';
    const total = Math.max(0, (Date.now() - startedAt) / 1000);
    const minutes = Math.floor(total / 60);
    const seconds = Math.floor(total % 60);
    return `${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}`;
  }

  function paint(recording) {
    state = recording?.state || 'STOPPED';
    const active = state === 'RECORDING';
    button.classList.toggle('video-recording-active', active);
    button.classList.toggle('video-recording-failed', state === 'FAILED');
    button.disabled = busy;

    if (active) {
      if (!startedAt) {
        const duration = Number(recording?.duration_seconds || 0);
        startedAt = Date.now() - duration * 1000;
      }
      button.textContent = `STOP REC · ${elapsedText()}`;
      button.title = recording?.file
        ? `Recording RAW main camera: ${recording.file}`
        : 'RAW main-camera recording is active';
      return;
    }

    startedAt = null;
    button.textContent = state === 'FAILED' ? 'VIDEO REC FAILED' : 'VIDEO REC';
    button.title = recording?.error || 'Record RAW main camera at native quality';
  }

  async function status() {
    try {
      const response = await fetch('/api/media/mission-recording/status', {cache: 'no-store'});
      const payload = await response.json();
      if (response.ok) paint(payload.recording || {});
    } catch (_) {
      // Mission Control must remain usable during a transient media status error.
    }
  }

  async function toggleRecording() {
    if (busy) return;
    busy = true;
    button.disabled = true;
    const action = state === 'RECORDING' ? 'STOP' : 'START';
    try {
      const response = await fetch('/api/media/mission-recording', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action}),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Video recording command failed');

      paint(payload.recording || {
        state: action === 'START' ? 'RECORDING' : 'STOPPED',
      });

      if (typeof toast === 'function') {
        if (action === 'START') {
          toast('Raw high-quality camera recording started');
        } else if (payload.recording?.state === 'RECORDED') {
          toast('Video saved and verified');
        } else {
          toast('Raw camera recording stopped');
        }
      }
    } catch (error) {
      if (typeof toast === 'function') toast(error.message, true);
      await status();
    } finally {
      busy = false;
      button.disabled = false;
    }
  }

  button.addEventListener('click', toggleRecording);
  status();
  window.setInterval(() => {
    if (state === 'RECORDING') {
      button.textContent = `STOP REC · ${elapsedText()}`;
    }
  }, 1000);
  window.setInterval(status, 5000);
})();
