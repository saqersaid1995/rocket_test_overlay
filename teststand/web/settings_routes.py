"""Settings page, System check and relay test API.

Settings are written to config.yaml and the console restarts (about 5 s) so every
section starts cleanly with the new values. Saving is refused unless ignition is
SAFE and nothing is recording.
"""
from __future__ import annotations

import base64
import copy
import threading
import time

from flask import jsonify, render_template, request

from .. import diagnostics, events, updater
from ..cameras import discovery
from ..config import load_user_config, save_user_config, validate
from ..ignition.controller import IgnitionError

EDITABLE = {
    "telemetry": {"esp_ip": str, "listen_port": int, "device_id": str},
    "pressure": {"max_expected_bar": float, "alarm_bar": float},
    "ignition": {"driver": str, "countdown_s": int, "pulse_ms": int, "hold_to_start_s": float,
                 "arm_timeout_s": int, "relay_test_ms": int},
    "recorder": {"directory": str, "pre_trigger_s": int, "auto_record": bool, "auto_stop_after_fire_s": int},
}
CAMERA_FIELDS = {"id": str, "name": str, "enabled": bool, "ip": str, "user": str, "password": str,
                 "port": int, "brand": str, "record_path": str, "preview_path": str,
                 "url": str, "record_url": str, "preview_url": str}


def _coerce(kind, value):
    if kind is bool:
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if value in (None, ""):
        return None
    return kind(value)


def register(app, system) -> None:
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

    # ---------------- pages ----------------
    @app.get("/settings")
    def settings_page():
        return render_template("settings.html", version=updater.version(),
                               presets=discovery.PRESET_LABELS)

    # ---------------- settings ----------------
    @app.get("/api/settings")
    def get_settings():
        cfg = system.cfg
        data = {sec: {k: cfg[sec].get(k) for k in fields} for sec, fields in EDITABLE.items()}
        user = load_user_config(system.cfg_path)
        cams = []
        for cam in user.get("cameras") or []:
            c = {k: cam.get(k) for k in CAMERA_FIELDS if cam.get(k) is not None}
            c.setdefault("enabled", True)
            if c.get("password"):
                c["password"] = "********"   # never sent back to the page
            cams.append(c)
        data["cameras"] = cams
        from ..diagnostics import local_ip_towards
        data["pc_ip"] = local_ip_towards(cfg["telemetry"].get("esp_ip") or "192.168.1.50")
        return ok(settings=data)

    @app.post("/api/settings")
    def save_settings():
        reason = busy()
        if reason:
            return fail(f"Cannot save now: {reason}")
        body = request.get_json(silent=True) or {}
        user = copy.deepcopy(load_user_config(system.cfg_path))
        old_cams = {c.get("id"): c for c in user.get("cameras") or []}
        try:
            for sec, fields in EDITABLE.items():
                if sec in body:
                    target = user.setdefault(sec, {}) or {}
                    user[sec] = target
                    for key, kind in fields.items():
                        if key in body[sec]:
                            val = _coerce(kind, body[sec][key])
                            if val is not None:
                                target[key] = val
            if "cameras" in body:
                cams = []
                for raw in body["cameras"]:
                    cam = {}
                    for key, kind in CAMERA_FIELDS.items():
                        if key in raw:
                            val = _coerce(kind, raw[key])
                            if val not in (None, ""):
                                cam[key] = val
                    if cam.get("password") == "********":   # unchanged password
                        prev = old_cams.get(cam.get("id")) or {}
                        if prev.get("password"):
                            cam["password"] = prev["password"]
                        else:
                            cam.pop("password")
                    if cam.get("ip"):   # simple form wins over old full URLs
                        for k in ("url", "record_url", "preview_url"):
                            cam.pop(k, None)
                    cams.append(cam)
                user["cameras"] = cams
            validate(user)
        except (ValueError, TypeError) as exc:
            return fail(f"Invalid setting: {exc}", 400)
        save_user_config(system.cfg_path, user)
        events.log("SETTINGS", "Settings saved — restarting")
        updater.restart_later(on_exit=system.stop)
        return ok(restarting=True)

    # ---------------- cameras ----------------
    @app.post("/api/cameras/test")
    def camera_test():
        body = request.get_json(silent=True) or {}
        try:
            if body.get("ip"):
                if body.get("password") == "********":
                    prev = next((c for c in load_user_config(system.cfg_path).get("cameras") or []
                                 if c.get("id") == body.get("id")), {})
                    body["password"] = prev.get("password", "")
                cfg = validate({"cameras": [{k: body.get(k) for k in CAMERA_FIELDS if body.get(k) not in (None, "")}]})
                cam = cfg["cameras"][0]
                url = cam["preview_url"] if body.get("which") != "record" else cam["record_url"]
            else:
                url = body.get("url") or ""
            if not url:
                return fail("Enter an IP address or a URL", 400)
        except (ValueError, TypeError) as exc:
            return fail(str(exc), 400)
        t0 = time.time()
        frame, err = discovery.grab_frame(url)
        if frame is None:
            return fail(err, 502)
        return ok(image="data:image/jpeg;base64," + base64.b64encode(frame).decode(),
                  seconds=round(time.time() - t0, 1), url=discovery.mask_url(url))

    @app.post("/api/cameras/scan")
    def camera_scan():
        return ok(found=discovery.scan())

    # ---------------- system check ----------------
    @app.get("/api/check")
    def system_check():
        checks = diagnostics.run_all(system)
        return ok(checks=checks, summary=diagnostics.summary(checks))

    @app.post("/api/check/fix-firewall")
    def fix_firewall():
        result = {}

        def work():
            result["msg"] = diagnostics.fix_firewall(int(system.cfg["telemetry"]["listen_port"]))

        t = threading.Thread(target=work)
        t.start()
        t.join(130)
        msg = result.get("msg", "Timed out waiting for the Windows prompt")
        events.log("SYSTEM", msg)
        return ok(message=msg)

    @app.post("/api/check/set-ethernet")
    def set_ethernet():
        body = request.get_json(silent=True) or {}
        adapter, ip = str(body.get("adapter", "")), str(body.get("ip", ""))
        if not adapter or not ip:
            return fail("Missing adapter or IP", 400)
        result = {}
        t = threading.Thread(target=lambda: result.update(msg=diagnostics.set_ethernet_ip(adapter, ip)))
        t.start()
        t.join(130)
        msg = result.get("msg", "Timed out waiting for the Windows prompt")
        events.log("SYSTEM", msg)
        return ok(message=msg)

    # ---------------- relay test ----------------
    @app.post("/api/relay-test/<relay>")
    def relay_test(relay: str):
        try:
            ms = system.ignition.test_relay(relay)
        except IgnitionError as exc:
            return fail(str(exc))
        return ok(relay=relay, pulse_ms=ms)
