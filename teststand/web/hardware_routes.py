"""Hardware page: pins and I2C devices the ESP32 sees right now."""
from __future__ import annotations

from flask import jsonify, render_template

from .. import updater


def register(app, system) -> None:
    hw = system.hardware

    @app.get("/hardware")
    def hardware_page():
        return render_template("hardware.html", version=updater.version())

    @app.get("/api/hardware")
    def hardware_state():
        return jsonify(ok=True, **hw.snapshot())

    @app.post("/api/hardware/scan")
    def hardware_scan():
        # a scan pauses pressure sampling for ~15 ms, so not while a test is running
        if system.ignition.state != "SAFE" or system.recorder.active:
            return jsonify(ok=False, error="Scan only when SAFE and not recording"), 409
        if not hw.request():
            return jsonify(ok=False, error="ESP32 not connected"), 409
        return jsonify(ok=True)
