(() => {
  const button = document.querySelector('#jump-ignition');
  if (!button) return;

  // workspace.js still carries a legacy /api/ignition onclick handler.
  // Clear it so this button has exactly one owner: the bench-only Ethernet LED test.
  button.onclick = null;

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
    button.textContent = 'LED TEST…';

    try {
      const response = await fetch('/api/bench/ignition/pulse', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Ethernet bench LED test failed');

      setVisual('LED ACTIVE', true);
      if (typeof toast === 'function') {
        toast('Bench LED pulse sent to PT-01 over Ethernet');
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
  button.title = 'Bench LED test over the PT-01 Ethernet edge connection';
  button.addEventListener('click', event => {
    event.preventDefault();
    event.stopImmediatePropagation();
    pulseBenchIgnition();
  });
})();
