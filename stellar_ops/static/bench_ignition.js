(() => {
  const button = document.querySelector('#jump-ignition');
  if (!button) return;

  // Clear any legacy owner so this button is bench-LED-only.
  button.onclick = null;

  let busy = false;
  let active = false;

  const setVisual = () => {
    button.disabled = busy;
    button.classList.toggle('active', active);
    button.textContent = active ? 'LED OFF' : 'IGNITION TEST';
    button.title = active
      ? 'Bench LED is ON — click to turn it OFF'
      : 'Bench LED test over the PT-01 Ethernet edge connection';
  };

  async function readStatus() {
    try {
      const response = await fetch('/api/bench/ignition', {cache: 'no-store'});
      const payload = await response.json();
      if (response.ok) active = Boolean(payload.active);
    } catch (_) {
      // Keep the current UI state if status cannot be read.
    }
    setVisual();
  }

  async function setBenchLed(nextState) {
    if (busy) return;
    busy = true;
    setVisual();

    try {
      const response = await fetch(`/api/bench/ignition/${nextState ? 'on' : 'off'}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Ethernet bench LED command failed');

      active = Boolean(payload.active);
      if (typeof toast === 'function') {
        toast(active ? 'Bench LED ON over Ethernet' : 'Bench LED OFF');
      }
    } catch (error) {
      if (typeof toast === 'function') toast(error.message, true);
    } finally {
      busy = false;
      setVisual();
    }
  }

  button.addEventListener('click', event => {
    event.preventDefault();
    event.stopImmediatePropagation();
    setBenchLed(!active);
  });

  readStatus();
})();
