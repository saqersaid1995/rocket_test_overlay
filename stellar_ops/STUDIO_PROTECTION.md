# Rocket Overlay Studio protection contract

The following paths are protected from Stellar Ops feature work:

```text
app.py
templates/index.html
static/app.js
static/app.css
rocket_overlay.py
rocket_overlay_broadcast.py
rocket_overlay_broadcast (1).py
rocket_overlay_broadcast (2).py
rocket_overlay_broadcast-3).py
rotpl_registry.py
rotpl_renderer.py
UPLOAD_TEMPLATE_HERE/
rocket-test-video-overlay/
```

Operations changes must be additive under `stellar_ops/`, `docs/stellar_ops/`, and dedicated operations test paths. Any future integration with Studio requires a separate, explicitly approved change request.

