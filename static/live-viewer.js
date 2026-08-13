(() => {
  "use strict";
  // A dropped MJPEG connection (network blip, server restart) leaves a dead
  // <img> with no automatic retry, so reconnect on error with backoff.
  const frame = document.querySelector("#viewerFrame");
  if (!frame) return;
  const baseSrc = frame.getAttribute("src");
  let backoffMs = 1000;

  frame.addEventListener("error", () => {
    setTimeout(() => {
      frame.src = `${baseSrc}?retry=${Date.now()}`;
      backoffMs = Math.min(backoffMs * 1.5, 8000);
    }, backoffMs);
  });

  frame.addEventListener("load", () => { backoffMs = 1000; });
})();
