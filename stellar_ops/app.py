from __future__ import annotations

import os
import shutil
import time

from flask import Flask, redirect, url_for

from .airspace import airspace
from .audit_integrity import verify_audit_ledger
from .bench_ignition import bench_ignition
from .build_info import system_identity
from .control import CONTROL_DB, OPERATION_ID, connect, control, init_control_db
from .deployment_guard import (
    apply_security_headers,
    deployment_assessment,
    mutation_guard,
)
from .edge_runtime import ensure_edge_gateway
from .observability import begin_request, finish_request, process_metrics
from .media import media
from .media_frame_preview import media_frame_preview
from .media_stream_runtime import install_media_stream_optimizations
from .operations import operations
from .pressure_edge import ensure_pressure_edge_integration
from .runtime_context import get_runtime_context
from .telemetry_runtime import recording_status
from .weather import weather

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("STELLAR_OPS_SECRET", "development-only-change-me")
install_media_stream_optimizations()
app.register_blueprint(control)
app.register_blueprint(media)
app.register_blueprint(media_frame_preview)
app.register_blueprint(bench_ignition)
app.register_blueprint(operations)
app.register_blueprint(weather)
app.register_blueprint(airspace)
app.before_request(begin_request)


@app.before_request
def ensure_physical_pressure_telemetry():
    """Keep PT-01 bound to the inbound Ethernet edge path.

    The ESP32 commissioning UI stays local on Wi-Fi. Operational telemetry is
    pushed to Stellar Ops over SMTCS-EDGE/1 TCP on port 9100; no HTTP polling is
    used in the operational path.
    """
    init_control_db()
    try:
        ensure_pressure_edge_integration(CONTROL_DB, OPERATION_ID)
        ensure_edge_gateway(CONTROL_DB)
    except (RuntimeError, OSError) as exc:
        app.logger.warning("Pressure Ethernet telemetry setup failed: %s", exc)


app.before_request(lambda: mutation_guard(app, CONTROL_DB))
app.after_request(finish_request)
app.after_request(apply_security_headers)


@app.context_processor
def inject_system_identity():
    return {"system_identity": system_identity()}


@app.get("/")
def home():
    return redirect(url_for("operations.dashboard"))


@app.get("/health")
def health():
    init_control_db()
    identity = system_identity()
    with connect() as db:
        db_started = time.monotonic()
        db.execute("SELECT 1").fetchone()
        latency = round((time.monotonic() - db_started) * 1000, 2)
        operation = db.execute(
            "SELECT mode,state,active_hold FROM operations WHERE id=?", (OPERATION_ID,)
        ).fetchone()
        db_hold_reason = operation["active_hold"] if operation else None
        edge = db.execute(
            "SELECT device_id,status,last_seen,total_samples,sequence_gaps "
            "FROM edge_sessions ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
        run = db.execute(
            "SELECT id,code,status FROM test_runs "
            "WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (OPERATION_ID,),
        ).fetchone()
        recording = recording_status(db, OPERATION_ID)
        runtime_context = get_runtime_context(db)
        runtime_boot = db.execute(
            "SELECT boot_id,started_at,reconciled_state FROM runtime_boot WHERE id=1"
        ).fetchone()
        audit_integrity = verify_audit_ledger(db)
        rejected_commands = db.execute(
            """SELECT count(*) FROM command_journal
               WHERE operation_id=? AND outcome='REJECTED'""",
            (OPERATION_ID,),
        ).fetchone()[0]

    CONTROL_DB.parent.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(CONTROL_DB.parent)
    free_percent = round(disk.free / disk.total * 100, 1)
    deployment = deployment_assessment(
        secret_key=app.config["SECRET_KEY"],
        database_path=CONTROL_DB,
    )
    production_blocked = (
        deployment["environment"] == "PRODUCTION"
        and deployment["status"] == "BLOCKED"
    )
    ready = (
        latency < 1000
        and free_percent >= 5
        and run is not None
        and audit_integrity["valid"]
        and not production_blocked
    )
    return {
        "status": "ready" if ready else "degraded",
        "service": "stellar-ops",
        "build": identity,
        "process": {
            "started_at": process_metrics()["started_at"],
            "uptime_seconds": process_metrics()["uptime_seconds"],
        },
        "database": {
            "status": "ready",
            "latency_ms": latency,
            "journal_mode": "WAL",
            "path": str(CONTROL_DB),
        },
        "disk": {
            "free_bytes": disk.free,
            "free_percent": free_percent,
            "status": "ready" if free_percent >= 5 else "critical",
        },
        "operation": dict(operation) if operation else None,
        "active_run": dict(run) if run else None,
        "runtime_context": runtime_context,
        "deployment": deployment,
        "audit_integrity": audit_integrity,
        "execution_safety": {
            "runtime_boot": dict(runtime_boot) if runtime_boot else None,
            "rejected_commands": rejected_commands,
            "fail_safe_hold": bool(
                operation
                and operation["state"] == "HOLD"
                and "restart detected" in (db_hold_reason or "")
            ),
        },
        "recording": recording,
        "edge": dict(edge) if edge else {"status": "NO_DEVICE"},
        "edge_listener": {
            "host": os.environ.get("STELLAR_OPS_EDGE_HOST", "0.0.0.0"),
            "port": int(os.environ.get("STELLAR_OPS_EDGE_PORT", "9100")),
        },
        "security": {
            "development_secret": app.config["SECRET_KEY"] == "development-only-change-me"
        },
    }, (200 if ready else 503)


@app.get("/health/live")
def liveness():
    return {
        "status": "alive",
        "service": "stellar-ops",
        "build": system_identity(),
    }


# =====================================================================
# 🔥 IGNITION API – sends pulse command to ESP32 relay
# =====================================================================

@app.route('/api/ignition', methods=['POST'])
def trigger_ignition():
    import requests

    # ESP32 Ethernet IP (must match your configuration)
    ESP32_IP = "192.168.1.50"
    url = f"http://{ESP32_IP}/relay/pulse"

    try:
        # Send pulse command (2 seconds duration)
        response = requests.post(url, json={"duration_ms": 2000}, timeout=2)

        if response.status_code == 200:
            return {"ok": True, "message": "Ignition command sent to ESP32"}
        else:
            return {"ok": False, "message": f"ESP32 error: {response.status_code}"}, 500

    except requests.exceptions.ConnectionError:
        return {"ok": False, "message": "Cannot reach ESP32. Check IP address."}, 500
    except Exception as e:
        return {"ok": False, "message": f"Error: {str(e)}"}, 500


# =====================================================================

if __name__ == "__main__":
    init_control_db()
    ensure_pressure_edge_integration(CONTROL_DB, OPERATION_ID)
    ensure_edge_gateway(CONTROL_DB)
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5001")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
        threaded=True,
    )