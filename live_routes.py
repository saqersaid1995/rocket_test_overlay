"""Live Studio routes: session setup, control room, viewer, and the API
(camera/telemetry ingestion, MJPEG streaming, readiness/countdown control).

Kept as a Blueprint, separate from app.py's existing editor/export routes,
since Live Studio is a parallel subsystem sharing only the ROTPL template
registry (via app_state.py).
"""

from __future__ import annotations

import re
import time
from typing import Any

import cv2
from flask import Blueprint, Response, jsonify, render_template, request

from live_session import (
    LaunchDetails,
    MissionConfig,
    StaticFireDetails,
    _placeholder_frame,
    live_sessions,
)

live_bp = Blueprint("live", __name__)

_CHANNEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_STREAM_FRAME_INTERVAL_S = 1.0 / 12.0


def _error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _get_session_or_404(session_id: str):
    session = live_sessions.get(session_id)
    if session is None:
        return None
    return session


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@live_bp.get("/live/new")
def live_setup_page():
    return render_template("live_setup.html")


@live_bp.get("/live/<session_id>/control-room")
def control_room_page(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    return render_template("control_room.html", session_id=session_id)


@live_bp.get("/live/<session_id>/watch")
def viewer_page(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    return render_template("viewer.html", session_id=session_id)


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------


@live_bp.post("/api/live/sessions")
def create_session():
    payload = _json_body()
    mission_name = str(payload.get("mission_name") or "").strip()
    if not mission_name:
        return _error("Mission name is required.")
    mission_type = str(payload.get("mission_type") or "static_fire").strip()
    if mission_type not in ("static_fire", "launch"):
        return _error("mission_type must be 'static_fire' or 'launch'.")
    try:
        t0_epoch = float(payload.get("t0_epoch"))
    except (TypeError, ValueError):
        return _error("t0_epoch (target ignition/launch time, epoch seconds) is required.")

    hold_points = payload.get("hold_points_s")
    if not isinstance(hold_points, list) or not hold_points:
        hold_points = [-120.0]
    try:
        hold_points = sorted(float(value) for value in hold_points)
    except (TypeError, ValueError):
        return _error("hold_points_s must be a list of numbers.")

    try:
        mission = MissionConfig(
            mission_name=mission_name,
            mission_type=mission_type,
            run_number=str(payload.get("run_number") or ""),
            site=str(payload.get("site") or ""),
            team=str(payload.get("team") or ""),
            t0_epoch=t0_epoch,
            organization_name=str(payload.get("organization_name") or "STELLAR KINETICS"),
            accent=str(payload.get("accent") or "#F59E0B"),
            camera_label=str(payload.get("camera_label") or "CAM 1"),
            pressure_unit=str(payload.get("pressure_unit") or "bar"),
            thrust_unit=str(payload.get("thrust_unit") or "N"),
            max_wind_speed_mps=float(payload.get("max_wind_speed_mps", 12.0)),
            temp_min_c=float(payload.get("temp_min_c", -10.0)),
            temp_max_c=float(payload.get("temp_max_c", 45.0)),
            hold_points_s=hold_points,
        )
    except (TypeError, ValueError) as exc:
        return _error(f"Invalid mission data: {exc}")

    try:
        if mission_type == "launch":
            launch_payload = payload.get("launch") or {}
            details = LaunchDetails(
                vehicle_type=str(launch_payload.get("vehicle_type") or ""),
                stage_count=int(launch_payload.get("stage_count", 1)),
                target_altitude_km=float(launch_payload.get("target_altitude_km", 0.0)),
                target_orbit=str(launch_payload.get("target_orbit") or ""),
                evacuation_radius_m=float(launch_payload.get("evacuation_radius_m", 0.0)),
                launch_window_s=float(launch_payload.get("launch_window_s", 1800.0)),
                ascent_duration_s=float(launch_payload.get("ascent_duration_s", 480.0)),
                max_velocity_mps=float(launch_payload.get("max_velocity_mps", 8000.0)),
            )
        else:
            static_payload = payload.get("static_fire") or {}
            pressure_limit = static_payload.get("pressure_limit")
            details = StaticFireDetails(
                engine_type=str(static_payload.get("engine_type") or ""),
                oxidizer=str(static_payload.get("oxidizer") or ""),
                fuel=str(static_payload.get("fuel") or ""),
                ablative_material=str(static_payload.get("ablative_material") or ""),
                expected_burn_duration_s=float(
                    static_payload.get("expected_burn_duration_s", 10.0)
                ),
                pressure_limit=float(pressure_limit) if pressure_limit not in (None, "") else None,
            )
    except (TypeError, ValueError) as exc:
        return _error(f"Invalid mission-type details: {exc}")

    session = live_sessions.create(mission, details)
    return jsonify({"session": session.state_snapshot()}), 201


@live_bp.get("/api/live/<session_id>")
def get_session(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    return jsonify({"session": session.state_snapshot()})


@live_bp.get("/api/live/<session_id>/state")
def get_session_state(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    return jsonify(session.state_snapshot())


@live_bp.post("/api/live/<session_id>/arm")
def arm_session(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    session.arm()
    return jsonify({"session": session.state_snapshot()})


@live_bp.post("/api/live/<session_id>/end")
def end_session(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    live_sessions.end(session_id)
    return jsonify({"ended": True})


# --------------------------------------------------------------------------
# Cameras
# --------------------------------------------------------------------------


@live_bp.post("/api/live/<session_id>/camera/<int:camera_index>")
def attach_camera(session_id: str, camera_index: int):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    payload = _json_body()
    uri = str(payload.get("uri") or "").strip()
    if not uri:
        return _error("uri is required (an RTSP URL or a local video path).")
    session.attach_camera(camera_index, uri)
    return jsonify({"session": session.state_snapshot()})


@live_bp.post("/api/live/<session_id>/program/camera")
def set_program_camera(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    payload = _json_body()
    try:
        camera_index = int(payload.get("camera_index"))
    except (TypeError, ValueError):
        return _error("camera_index is required.")
    try:
        session.set_program_camera(camera_index)
    except KeyError as exc:
        return _error(str(exc), 404)
    return jsonify({"session": session.state_snapshot()})


@live_bp.get("/api/live/<session_id>/camera/<int:camera_index>/stream")
def camera_stream(session_id: str, camera_index: int):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    camera = session.cameras.get(camera_index)
    if camera is None:
        return _error(f"Camera {camera_index} is not attached.", 404)

    def generate():
        boundary = b"--frame\r\n"
        while True:
            frame, _ts, _connected = camera.latest()
            if frame is None:
                frame = _placeholder_frame("NO SIGNAL")
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
            if ok:
                yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + encoded.tobytes() + b"\r\n"
            time.sleep(_STREAM_FRAME_INTERVAL_S)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@live_bp.get("/api/live/<session_id>/program/stream")
def program_stream(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    compositor = session.compositor
    if compositor is None:
        return _error("Session is not armed yet; POST /arm first.", 409)

    def generate():
        boundary = b"--frame\r\n"
        last_generation = -1
        while not compositor.stopped:
            with compositor.condition:
                compositor.condition.wait_for(
                    lambda: compositor.generation != last_generation or compositor.stopped,
                    timeout=2.0,
                )
                jpeg = compositor.latest_jpeg
                last_generation = compositor.generation
            if compositor.stopped:
                break
            if jpeg is None:
                continue
            yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


# --------------------------------------------------------------------------
# Telemetry ingestion
# --------------------------------------------------------------------------


@live_bp.post("/api/live/<session_id>/telemetry")
def ingest_telemetry(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    payload = _json_body()
    channels = payload.get("channels")
    if not isinstance(channels, dict) or not channels:
        return _error("channels must be a non-empty object of name -> number.")
    parsed: dict[str, float] = {}
    for name, value in channels.items():
        if not isinstance(name, str) or not _CHANNEL_NAME_RE.match(name):
            return _error(f"Invalid channel name: {name!r}")
        try:
            parsed[name] = float(value)
        except (TypeError, ValueError):
            return _error(f"Channel {name!r} must be numeric, got {value!r}")
    ts = payload.get("ts")
    try:
        ts = float(ts) if ts is not None else None
    except (TypeError, ValueError):
        return _error("ts must be a number (epoch seconds) if provided.")
    session.ingest_telemetry(parsed, ts)
    return jsonify({"ok": True, "received": parsed})


# --------------------------------------------------------------------------
# Readiness / countdown control
# --------------------------------------------------------------------------


@live_bp.post("/api/live/<session_id>/checklist/<item_id>")
def set_checklist_item(session_id: str, item_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    payload = _json_body()
    state = str(payload.get("state") or "").strip()
    try:
        item = session.set_checklist_state(item_id, state)
    except KeyError as exc:
        return _error(str(exc), 404)
    except (PermissionError, ValueError) as exc:
        return _error(str(exc))
    return jsonify({"item": item, "session": session.state_snapshot()})


@live_bp.post("/api/live/<session_id>/hold")
def hold_session(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    session.hold()
    return jsonify({"session": session.state_snapshot()})


@live_bp.post("/api/live/<session_id>/resume")
def resume_session(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    session.resume()
    return jsonify({"session": session.state_snapshot()})


@live_bp.post("/api/live/<session_id>/abort")
def abort_session(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    session.abort()
    return jsonify({"session": session.state_snapshot()})


# --------------------------------------------------------------------------
# Active template
# --------------------------------------------------------------------------


@live_bp.post("/api/live/<session_id>/template")
def set_active_template(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    payload = _json_body()
    template_id = str(payload.get("template_id") or "").strip()
    version = str(payload.get("template_version") or "").strip()
    sha256 = str(payload.get("sha256") or "").strip().lower()
    if not template_id or not version or not sha256:
        return _error("template_id, template_version, and sha256 are all required.")
    session.select_template(template_id, version, sha256)
    renderer = session.resolve_active_template()
    if renderer is None:
        return _error("Could not resolve the selected template; check it is activatable.", 409)
    return jsonify({"session": session.state_snapshot()})


# --------------------------------------------------------------------------
# Broadcast
# --------------------------------------------------------------------------


@live_bp.post("/api/live/<session_id>/broadcast/start")
def broadcast_start(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    session.set_broadcast_live(True)
    return jsonify({"session": session.state_snapshot()})


@live_bp.post("/api/live/<session_id>/broadcast/stop")
def broadcast_stop(session_id: str):
    session = _get_session_or_404(session_id)
    if session is None:
        return _error("Live session not found or has expired.", 404)
    session.set_broadcast_live(False)
    return jsonify({"session": session.state_snapshot()})
