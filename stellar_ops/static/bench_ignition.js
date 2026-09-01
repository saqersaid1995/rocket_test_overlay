(() => {
  const button = document.querySelector('#jump-ignition');
  if (!button) return;

  let busy = false;
  let resetTimer = null;

  const setVisual = (label, active = false) => {
    button.textContent = label;
    button.classList.toggle('active', active);
    button.disabled = busy;
  };

  async function pulseBenchIgnition() {
    if (busy) return;
    busy = true;
    button.disabled = true;
    button.textContent = 'BENCH TEST…';

    try {
      const response = await fetch('/api/bench/ignition/pulse', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Bench ignition test failed');

      setVisual('BENCH ACTIVE', true);
      if (typeof toast === 'function') {
        toast('Bench ignition simulation pulse accepted — no physical output driven');
      }

      clearTimeout(resetTimer);
      resetTimer = setTimeout(() => {
        busy = false;
        button.disabled = false;
        setVisual('IGNITION TEST', false);
      }, Math.max(600, Number(payload.pulse_ms || 500) + 150));
    } catch (error) {
      busy = false;
      button.disabled = false;
      setVisual('IGNITION TEST', false);
      if (typeof toast === 'function') toast(error.message, true);
    }
  }

  button.textContent = 'IGNITION TEST';
  button.title = 'Bench simulation only — no physical relay output';
  button.addEventListener('click', event => {
    event.preventDefault();
    pulseBenchIgnition();
  });
})();
