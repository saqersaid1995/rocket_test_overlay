from __future__ import annotations

from flask import Blueprint, jsonify, request, send_file

from .control import CONTROL_DB, OPERATION_ID
from .pressure_capture import (
    PressureCaptureError,
    active_capture_status,
    build_excel,
    list_captures,
    stop_capture,
)

pressure_capture_api = Blueprint("pressure_capture_api", __name__)


@pressure_capture_api.get("/api/pressure-capture/status")
def pressure_capture_status():
    active = active_capture_status(CONTROL_DB, OPERATION_ID)
    return jsonify(active=active)


@pressure_capture_api.get("/api/pressure-capture")
def pressure_capture_list():
    return jsonify(captures=list_captures(CONTROL_DB, OPERATION_ID))


@pressure_capture_api.post("/api/pressure-capture/<int:capture_id>/stop")
def pressure_capture_stop(capture_id: int):
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or "MANUAL_STOP")
    try:
        return jsonify(ok=True, capture=stop_capture(CONTROL_DB, capture_id, reason))
    except PressureCaptureError as exc:
        return jsonify(error=str(exc)), 404


@pressure_capture_api.get("/api/pressure-capture/<int:capture_id>/excel")
def pressure_capture_excel(capture_id: int):
    try:
        output, filename = build_excel(CONTROL_DB, capture_id)
    except PressureCaptureError as exc:
        return jsonify(error=str(exc)), 404
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
