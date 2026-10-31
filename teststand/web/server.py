"""Web section: the operator console (one page) and its small JSON API.

The web layer only forwards operator commands to the other sections and shows
their status. It has no ignition logic of its own.

Console watchdog: the page polls /api/state ~10 times per second. If the system
is ARMED or counting down and no console has polled for 3 seconds (browser
closed, laptop asleep, network cable pulled), ignition is aborted to SAFE.
"""
from __future__ import annotations

import os
import threading
import time

from flask import Flask, Response, abort, jsonify, render_template, request

from .. import events, updater
from ..ignition.controller import ARMED, COUNTDOWN, IgnitionError

CONSOLE_TIMEOUT_S = 3.0


def create_app(system) -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    last_poll = {"t": time.time()}

    def watchdog():
        while True:
            time.sleep(0.25)
            if system.ignition.state in (ARMED, COUNTDOWN) and time.time() - last_poll["t"] > CONSOLE_TIMEOUT_S:
                system.ignition.abort("operator console lost contact")

    threading.Thread(target=watchdog, name="console-watchdog", daemon=True).start()

    def ok(**extra):
        return jsonify(ok=True, **extra)

    def fail(message: str, code: int = 409):
        return jsonify(ok=False, error=message), code

    @app.get("/")
    def console():
        return render_template("console.html", cameras=system.cameras.snapshot(), cfg=system.cfg,
                               version=updater.version())

    def _thrust_now():
        th = system.thrust
        if not th.enabled:
            return {"enabled": False}
        t = system.hub.snapshot()
        return {"enabled": True, "n": th.force(t.get("pressure_bar")), "peak_n": th.force(t.get("peak_bar") or 0),
                "max_n": th.force(system.cfg["pressure"]["max_expected_bar"]), "A": th.A, "B": th.B,
                "ambient_bar": th.pa_bar}

    @app.get("/api/state")
    def state():
        last_poll["t"] = time.time()
        after = int(request.args.get("events_after", 0))
        return jsonify(
            server_time=time.time(),
            telemetry=system.hub.snapshot(),
            pressure_cfg=system.cfg["pressure"],
            thrust=_thrust_now(),
            ignition=system.ignition.snapshot(),
            recorder=system.recorder.snapshot(),
            cameras=system.cameras.snapshot(),
            hardware=system.hardware.summary(),
            events=events.recent(after),
        )

    @app.get("/api/history")
    def history():
        seconds = min(float(request.args.get("s", 30)), 60)
        return jsonify(points=system.hub.history(seconds), server_time=time.time())

    # ----- ignition -----
    @app.post("/api/ignition/<action>")
    def ignition(action: str):
        ign = system.ignition
        try:
            if action == "arm":
                ign.arm()
            elif action == "disarm":
                ign.disarm()
            elif action == "start":
                body = request.get_json(silent=True) or {}
                if float(body.get("held_s", 0)) < ign.cfg["hold_to_start_s"] - 0.05:
                    return fail("Hold the START button longer")
                ign.start_countdown()
            elif action == "abort":
                ign.abort("operator ABORT")
            else:
                abort(404)
        except IgnitionError as exc:
            events.log("IGNITION", f"Refused: {exc}", "WARN")
            return fail(str(exc))
        return ok(ignition=ign.snapshot())

    # ----- pressure -----
    @app.post("/api/pressure/<action>")
    def pressure(action: str):
        hub = system.hub
        if action == "zero":
            if system.ignition.state != "SAFE":
                return fail("Zero only when SAFE")
            if hub.tare() is None:
                return fail("No pressure data yet")
        elif action == "clear-zero":
            hub.clear_tare()
        elif action == "reset-peak":
            hub.reset_peak()
        else:
            abort(404)
        return ok()

    # ----- recording -----
    @app.post("/api/record/<action>")
    def record(action: str):
        if action == "start":
            system.recorder.start("manual")
        elif action == "stop":
            system.recorder.stop("manual")
        else:
            abort(404)
        return ok(recorder=system.recorder.snapshot())

    @app.post("/api/session/open")
    def open_session_folder():
        """Open the last test's folder in Windows Explorer."""
        last = system.recorder.last_session or {}
        folder = last.get("folder")
        if not folder or not os.path.isdir(folder):
            return jsonify(ok=False, error="No saved test folder yet"), 404
        if not hasattr(os, "startfile"):
            return jsonify(ok=False, error=f"Open this folder by hand: {folder}"), 409
        os.startfile(folder)  # noqa: S606 - local folder written by this app
        return ok(folder=folder)

    @app.post("/api/note")
    def note():
        text = ((request.get_json(silent=True) or {}).get("text") or "").strip()[:300]
        if text:
            events.log("OPERATOR", text)
        return ok()

    # ----- cameras -----
    @app.get("/camera/<cam_id>.mjpg")
    def camera_stream(cam_id: str):
        cam = system.cameras.cameras.get(cam_id)
        if cam is None:
            abort(404)

        def gen():
            last = 0.0
            idle_since = time.time()
            while True:
                frame, t = cam.wait_frame(last, timeout=2.0)
                if frame is None or t == last:
                    if time.time() - idle_since > 20:
                        return  # camera down: free this thread; the page reconnects by itself
                    continue
                idle_since = time.time()
                last = t
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")

        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-store"})

    # ----- system: version, updates, shutdown -----
    def _busy() -> str | None:
        if system.ignition.state != "SAFE":
            return "Ignition must be SAFE"
        if system.recorder.active:
            return "Stop the recording first"
        return None

    @app.get("/api/system")
    def system_info():
        return jsonify(ok=True, **updater.current())

    @app.post("/api/update/check")
    def update_check():
        try:
            info = updater.check()
        except Exception as exc:  # noqa: BLE001
            events.log("UPDATE", f"Update check failed: {exc}", "WARN")
            return fail(f"Update check failed: {exc}", 502)
        events.log("UPDATE", f"{info['behind']} new update(s) available" if info["update_available"]
                   else "Already up to date")
        return ok(**info)

    @app.post("/api/update/apply")
    def update_apply():
        reason = _busy()
        if reason:
            return fail(f"Cannot update now: {reason}")
        try:
            result = updater.apply()
        except Exception as exc:  # noqa: BLE001
            events.log("UPDATE", f"Update failed: {exc}", "WARN")
            return fail(f"Update failed: {exc}", 502)
        events.log("UPDATE", "Restarting with the new version…")
        updater.restart_later(on_exit=system.stop)
        return ok(restarting=True, **result)

    @app.post("/api/system/shutdown")
    def shutdown():
        reason = _busy()
        if reason:
            return fail(f"Cannot shut down now: {reason}")
        events.log("SYSTEM", "Shut down from the console")

        def _bye():
            time.sleep(0.5)
            try:
                system.stop()
            finally:
                os._exit(0)

        threading.Thread(target=_bye, daemon=True).start()
        return ok()

    from .settings_routes import register as register_settings
    register_settings(app, system)
    from .firmware_routes import register as register_firmware
    register_firmware(app, system)
    from .hardware_routes import register as register_hardware
    register_hardware(app, system)

    @app.get("/health")
    def health():
        return jsonify(ok=True, telemetry=system.hub.snapshot()["link"], ignition=system.ignition.state)

    return app
