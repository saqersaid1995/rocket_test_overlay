"""Firmware page: edit the ESP32 code, compile it and upload it over Ethernet."""
from __future__ import annotations

from flask import jsonify, render_template, request

from .. import updater


def register(app, system) -> None:
    fw = system.firmware

    def ok(**extra):
        return jsonify(ok=True, **extra)

    def fail(message: str, code: int = 409):
        return jsonify(ok=False, error=message), code

    def busy() -> str | None:
        if system.ignition.state != "SAFE":
            return "Ignition must be SAFE"
        if system.recorder.active:
            return "Stop the recording first"
        return None

    @app.get("/firmware")
    def firmware_page():
        return render_template("firmware.html", version=updater.version())

    @app.get("/api/firmware/source")
    def fw_source():
        return ok(code=fw.read(), path=str(fw.sketch))

    @app.post("/api/firmware/source")
    def fw_save():
        code = (request.get_json(silent=True) or {}).get("code")
        if not isinstance(code, str) or not code.strip():
            return fail("Empty code", 400)
        fw.write(code)
        return ok()

    @app.get("/api/firmware/status")
    def fw_status():
        return ok(**fw.status())

    @app.post("/api/firmware/compile")
    def fw_compile():
        then_upload = bool((request.get_json(silent=True) or {}).get("upload"))
        if then_upload and busy():
            return fail(f"Cannot upload now: {busy()}")
        try:
            fw.start_compile(then_upload)
        except RuntimeError as exc:
            return fail(str(exc))
        return ok()

    @app.post("/api/firmware/upload")
    def fw_upload():
        reason = busy()
        if reason:
            return fail(f"Cannot upload now: {reason}")
        data = None
        if "file" in request.files:
            data = request.files["file"].read()
        try:
            fw.start_upload(data)
        except RuntimeError as exc:
            return fail(str(exc))
        return ok()
