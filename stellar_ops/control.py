from __future__ import annotations

import math
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request
from .adapters import inspect_csv, test_adapter

ROOT = Path(__file__).resolve().parent
CONTROL_DB = Path(os.environ.get("STELLAR_OPS_DATA", ROOT / "data")) / "control.db"
control = Blueprint("control", __name__)
OPERATION_ID = "OP-QUAL-STATIC-001"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect() -> sqlite3.Connection:
    CONTROL_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(CONTROL_DB)
    connection.row_factory = sqlite3.Row
    return connection


SCHEMA = """
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 operation_type TEXT NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL,
 prior_state TEXT, active_hold TEXT, countdown_seconds INTEGER NOT NULL DEFAULT 10,
 firing_started_monotonic REAL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stations(
 operation_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
 authority TEXT NOT NULL, required INTEGER NOT NULL, decision TEXT NOT NULL,
 operator_name TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(operation_id,code));
CREATE TABLE IF NOT EXISTS devices(
 operation_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
 device_type TEXT NOT NULL, protocol TEXT NOT NULL, endpoint TEXT NOT NULL,
 health TEXT NOT NULL, recording TEXT NOT NULL, required INTEGER NOT NULL,
 PRIMARY KEY(operation_id,id));
CREATE TABLE IF NOT EXISTS channels(
 operation_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
 unit TEXT NOT NULL, source_id TEXT NOT NULL, quality TEXT NOT NULL,
 warning REAL, critical REAL, sample_rate INTEGER NOT NULL,
 PRIMARY KEY(operation_id,id));
CREATE TABLE IF NOT EXISTS procedure_steps(
 operation_id TEXT NOT NULL, sequence INTEGER NOT NULL, phase TEXT NOT NULL,
 title TEXT NOT NULL, role TEXT NOT NULL, verification TEXT NOT NULL,
 status TEXT NOT NULL, completed_by TEXT, completed_at TEXT,
 PRIMARY KEY(operation_id,sequence));
CREATE TABLE IF NOT EXISTS events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL, event_type TEXT NOT NULL, source TEXT NOT NULL,
 severity TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alarms(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
 opened_at TEXT NOT NULL, priority TEXT NOT NULL, source TEXT NOT NULL,
 message TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device_integrations(
 operation_id TEXT NOT NULL, device_id TEXT NOT NULL,
 adapter_type TEXT NOT NULL, config_json TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, last_test_at TEXT,
 last_test_status TEXT NOT NULL DEFAULT 'NOT_TESTED', last_test_message TEXT,
 PRIMARY KEY(operation_id,device_id));
CREATE TABLE IF NOT EXISTS channel_integrations(
 operation_id TEXT NOT NULL, channel_id TEXT NOT NULL,
 raw_field TEXT NOT NULL, calibration_slope REAL NOT NULL DEFAULT 1,
 calibration_intercept REAL NOT NULL DEFAULT 0, stale_timeout_ms INTEGER NOT NULL,
 required_for_commit INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(operation_id,channel_id));
CREATE TABLE IF NOT EXISTS replay_datasets(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
 filename TEXT NOT NULL, uploaded_at TEXT NOT NULL, row_count INTEGER NOT NULL,
 columns_json TEXT NOT NULL, preview_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS edge_sessions(
 device_id TEXT NOT NULL, boot_id TEXT NOT NULL, remote_addr TEXT NOT NULL,
 firmware TEXT, connected_at TEXT NOT NULL, disconnected_at TEXT,
 last_seen TEXT NOT NULL, last_sequence INTEGER, total_samples INTEGER NOT NULL DEFAULT 0,
 sequence_gaps INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
 PRIMARY KEY(device_id,boot_id));
CREATE TABLE IF NOT EXISTS edge_batches(
 id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, boot_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, received_at TEXT NOT NULL, first_sample_us INTEGER NOT NULL,
 sample_period_us INTEGER NOT NULL, sample_count INTEGER NOT NULL,
 channels_json TEXT NOT NULL, UNIQUE(device_id,boot_id,sequence));
"""

STATIONS = [
    ("TD", "Test Director", "Conduct test sequence"),
    ("RSO", "Range Safety", "Range clear and safety hold"),
    ("LCO", "Launch Control", "SAFE/ARM station"),
    ("PROP", "Propulsion", "Test article and safing"),
    ("INST", "Instrumentation", "DAQ and channel validity"),
    ("GND", "Ground Operations", "Stand and site readiness"),
    ("DATA", "Data & Video", "Evidence recording integrity"),
]
DEVICES = [
    ("DAQ-01", "Primary DAQ", "DAQ", "MODBUS-TCP", "SIM://daq-01", "SIMULATED", "RECORDING", 1),
    ("PT-01", "Chamber Pressure", "PRESSURE", "ANALOG-DAQ", "DAQ-01/AI-01", "SIMULATED", "N/A", 1),
    ("LC-01", "Thrust Load Cell", "LOAD-CELL", "ANALOG-DAQ", "DAQ-01/AI-02", "SIMULATED", "N/A", 1),
    ("TC-01", "Motor Case Temperature", "THERMOCOUPLE", "ANALOG-DAQ", "DAQ-01/AI-03", "SIMULATED", "N/A", 1),
    ("CAM-01", "Motor Wide", "IP-CAMERA", "ONVIF-T/RTSP", "UNASSIGNED", "NOT_CONNECTED", "STOPPED", 1),
    ("CAM-02", "Nozzle Close", "IP-CAMERA", "ONVIF-T/RTSP", "UNASSIGNED", "NOT_CONNECTED", "STOPPED", 1),
    ("FC-01", "Field Controller", "CONTROLLER", "SIMULATOR", "SIM://field-01", "SIMULATED", "N/A", 1),
    ("TIME-01", "Site Time Authority", "TIME", "NTP", "LOCAL", "GOOD", "N/A", 1),
]
CHANNELS = [
    ("motor.chamber_pressure", "Chamber pressure", "bar", "PT-01", "SIMULATED", 55.0, 70.0, 1000),
    ("motor.thrust", "Motor thrust", "N", "LC-01", "SIMULATED", 450.0, 550.0, 1000),
    ("motor.case_temperature", "Case temperature", "°C", "TC-01", "SIMULATED", 75.0, 95.0, 10),
    ("ignition.continuity", "Ignition continuity", "state", "FC-01", "SIMULATED", None, None, 2),
]
STEPS = [
    (10, "SITE", "Verify exclusion zone established", "RSO", "TWO_PERSON"),
    (20, "SITE", "Confirm test stand mechanical inspection", "GND", "TWO_PERSON"),
    (30, "CONFIG", "Verify motor serial and configuration revision", "PROP", "TWO_PERSON"),
    (40, "DATA", "Confirm DAQ channels and calibration references", "INST", "TWO_PERSON"),
    (50, "DATA", "Start telemetry recording and verify sample flow", "INST", "AUTOMATIC"),
    (60, "VIDEO", "Confirm mandatory camera views recording", "DATA", "AUTOMATIC"),
    (70, "SAFETY", "Confirm ignition circuit remains SAFE", "LCO", "TWO_PERSON"),
    (80, "SAFETY", "Declare range clear", "RSO", "TWO_PERSON"),
    (90, "POLL", "Conduct station Go/No-Go poll", "TD", "AUTOMATIC"),
    (100, "ARM", "Request field controller ARM transition", "LCO", "TWO_PERSON"),
    (110, "COUNT", "Authorise terminal countdown", "TD", "TWO_PERSON"),
    (120, "POST", "Confirm post-fire pressure decay", "PROP", "AUTOMATIC"),
    (130, "POST", "Declare test article safe to approach", "RSO", "TWO_PERSON"),
    (140, "DATA", "Seal operation evidence package", "DATA", "AUTOMATIC"),
]


def event(db: sqlite3.Connection, kind: str, source: str, severity: str, message: str) -> None:
    db.execute("INSERT INTO events(operation_id,occurred_at,event_type,source,severity,message) VALUES(?,?,?,?,?,?)",
               (OPERATION_ID, utc_now(), kind, source, severity, message))


def init_control_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        stamp = utc_now()
        db.execute("INSERT OR IGNORE INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (OPERATION_ID, "QST-001", "RNX-71V Static Qualification", "STATIC_MOTOR_TEST",
                    "SIMULATION", "CHECKOUT", None, None, 10, None, stamp))
        for code, name, authority in STATIONS:
            db.execute("INSERT OR IGNORE INTO stations VALUES(?,?,?,?,?,?,?,?)",
                       (OPERATION_ID, code, name, authority, 1, "PENDING", "UNASSIGNED", stamp))
        for row in DEVICES:
            db.execute("INSERT OR IGNORE INTO devices VALUES(?,?,?,?,?,?,?,?,?)", (OPERATION_ID, *row))
            default_adapter = "SIMULATOR" if row[5] == "SIMULATED" else ("ONVIF" if row[2] == "IP-CAMERA" else row[3].replace("-", "_"))
            db.execute("INSERT OR IGNORE INTO device_integrations VALUES(?,?,?,?,?,?,?,?)",
                       (OPERATION_ID, row[0], default_adapter, json.dumps({"endpoint": row[4]}), 1, None, "NOT_TESTED", None))
        for row in CHANNELS:
            db.execute("INSERT OR IGNORE INTO channels VALUES(?,?,?,?,?,?,?,?,?)", (OPERATION_ID, *row))
            db.execute("INSERT OR IGNORE INTO channel_integrations VALUES(?,?,?,?,?,?,?)",
                       (OPERATION_ID, row[0], row[0].split(".")[-1], 1.0, 0.0, max(100, int(3000 / row[7])), 1))
        for row in STEPS:
            db.execute("INSERT OR IGNORE INTO procedure_steps VALUES(?,?,?,?,?,?,?,?,?)",
                       (OPERATION_ID, *row, "PENDING", None, None))
        if db.execute("SELECT count(*) FROM events WHERE operation_id=?", (OPERATION_ID,)).fetchone()[0] == 0:
            event(db, "OPERATION", "SYSTEM", "INFO", "Simulation operation baseline created")


@control.before_request
def ensure_control_database() -> None:
    init_control_db()


def telemetry(operation: sqlite3.Row) -> dict:
    if operation["state"] != "FIRING" or not operation["firing_started_monotonic"]:
        return {"elapsed": 0.0, "pressure": 0.0, "thrust": 0.0, "temperature": 28.0, "continuity": "SAFE"}
    elapsed = max(0.0, time.monotonic() - operation["firing_started_monotonic"])
    if elapsed < 0.35:
        ramp = elapsed / 0.35
    elif elapsed < 6.8:
        ramp = 1.0 - 0.025 * math.sin(elapsed * 4.1)
    elif elapsed < 8.0:
        ramp = max(0.0, (8.0 - elapsed) / 1.2)
    else:
        ramp = 0.0
    return {"elapsed": round(elapsed, 3), "pressure": round(61.5 * ramp, 2),
            "thrust": round(435.0 * ramp, 1), "temperature": round(28 + min(elapsed, 8) * 5.2, 1),
            "continuity": "FIRED"}


def snapshot() -> dict:
    init_control_db()
    with connect() as db:
        op = db.execute("SELECT * FROM operations WHERE id=?", (OPERATION_ID,)).fetchone()
        data = {"operation": dict(op), "stations": [dict(x) for x in db.execute("SELECT * FROM stations WHERE operation_id=? ORDER BY rowid", (OPERATION_ID,))],
                "devices": [dict(x) for x in db.execute("SELECT * FROM devices WHERE operation_id=? ORDER BY rowid", (OPERATION_ID,))],
                "channels": [dict(x) for x in db.execute("SELECT * FROM channels WHERE operation_id=? ORDER BY rowid", (OPERATION_ID,))],
                "steps": [dict(x) for x in db.execute("SELECT * FROM procedure_steps WHERE operation_id=? ORDER BY sequence", (OPERATION_ID,))],
                "events": [dict(x) for x in db.execute("SELECT * FROM events WHERE operation_id=? ORDER BY sequence DESC LIMIT 40", (OPERATION_ID,))],
                "alarms": [dict(x) for x in db.execute("SELECT * FROM alarms WHERE operation_id=? AND state!='CLOSED' ORDER BY id DESC", (OPERATION_ID,))]}
        data["integrations"] = [dict(x) for x in db.execute("""SELECT i.*,d.name,d.device_type,d.endpoint
            FROM device_integrations i JOIN devices d ON d.operation_id=i.operation_id AND d.id=i.device_id
            WHERE i.operation_id=? ORDER BY d.rowid""", (OPERATION_ID,))]
        data["channel_integrations"] = [dict(x) for x in db.execute("SELECT * FROM channel_integrations WHERE operation_id=? ORDER BY rowid", (OPERATION_ID,))]
        data["replays"] = [dict(x) for x in db.execute("SELECT id,filename,uploaded_at,row_count,columns_json,active FROM replay_datasets WHERE operation_id=? ORDER BY id DESC", (OPERATION_ID,))]
        data["edge_sessions"] = [dict(x) for x in db.execute("SELECT * FROM edge_sessions ORDER BY last_seen DESC LIMIT 20")]
        data["telemetry"] = telemetry(op)
        return data


@control.get("/control")
def console():
    return render_template("control.html", initial=snapshot())


@control.get("/api/control/snapshot")
def api_snapshot():
    return jsonify(snapshot())


@control.post("/api/control/device")
def save_device():
    payload = request.get_json(silent=True) or {}
    required = ("id", "name", "device_type", "adapter_type", "endpoint")
    if any(not str(payload.get(key, "")).strip() for key in required):
        return jsonify(error="id, name, device type, adapter and endpoint are required"), 400
    device_id = str(payload["id"]).strip().upper()
    if not all(char.isalnum() or char in "-_" for char in device_id):
        return jsonify(error="device id may contain letters, numbers, hyphen and underscore only"), 400
    adapter = str(payload["adapter_type"]).strip().upper()
    endpoint = str(payload["endpoint"]).strip()
    config = {"endpoint": endpoint, "username": str(payload.get("username", "")).strip(),
              "profile": str(payload.get("profile", "")).strip(), "notes": str(payload.get("notes", "")).strip()}
    with connect() as db:
        db.execute("""INSERT INTO devices(operation_id,id,name,device_type,protocol,endpoint,health,recording,required)
          VALUES(?,?,?,?,?,?,?,'STOPPED',?)
          ON CONFLICT(operation_id,id) DO UPDATE SET name=excluded.name,device_type=excluded.device_type,
          protocol=excluded.protocol,endpoint=excluded.endpoint,required=excluded.required""",
          (OPERATION_ID, device_id, str(payload["name"]).strip(), str(payload["device_type"]).strip().upper(), adapter,
           endpoint, "NOT_TESTED", 1 if payload.get("required", True) else 0))
        db.execute("""INSERT INTO device_integrations(operation_id,device_id,adapter_type,config_json,enabled,last_test_status)
          VALUES(?,?,?,?,1,'NOT_TESTED') ON CONFLICT(operation_id,device_id) DO UPDATE SET
          adapter_type=excluded.adapter_type,config_json=excluded.config_json,enabled=1,last_test_status='NOT_TESTED',last_test_message=NULL""",
          (OPERATION_ID, device_id, adapter, json.dumps(config)))
        event(db, "DEVICE_CONFIG", "INSTRUMENTATION", "INFO", f"Device {device_id} saved with {adapter} adapter")
    return jsonify(ok=True, device_id=device_id)


@control.post("/api/control/device/<device_id>/test")
def test_device(device_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM device_integrations WHERE operation_id=? AND device_id=?", (OPERATION_ID, device_id)).fetchone()
        if not row: return jsonify(error="device integration not found"), 404
        config = json.loads(row["config_json"])
        result = test_adapter(row["adapter_type"], config.get("endpoint", ""))
        db.execute("UPDATE device_integrations SET last_test_at=?,last_test_status=?,last_test_message=? WHERE operation_id=? AND device_id=?",
                   (utc_now(), result.status, result.message, OPERATION_ID, device_id))
        db.execute("UPDATE devices SET health=? WHERE operation_id=? AND id=?", (result.status, OPERATION_ID, device_id))
        event(db, "CONNECTION_TEST", "INSTRUMENTATION", "INFO" if result.ok else "WARNING", f"{device_id}: {result.message}")
    return jsonify(result.to_dict()), 200 if result.ok else 422


@control.post("/api/control/channel")
def save_channel():
    payload = request.get_json(silent=True) or {}
    required = ("id", "name", "unit", "source_id", "raw_field")
    if any(not str(payload.get(key, "")).strip() for key in required): return jsonify(error="all channel identity fields are required"), 400
    channel_id = str(payload["id"]).strip()
    try:
        slope=float(payload.get("slope",1)); intercept=float(payload.get("intercept",0)); rate=int(payload.get("sample_rate",10)); stale=int(payload.get("stale_timeout_ms",1000))
        warning=float(payload["warning"]) if str(payload.get("warning","")).strip() else None
        critical=float(payload["critical"]) if str(payload.get("critical","")).strip() else None
    except (TypeError,ValueError): return jsonify(error="calibration, rates and limits must be numeric"), 400
    if rate<1 or rate>10000 or stale<10: return jsonify(error="sample rate or stale timeout is outside Phase 1 limits"),400
    with connect() as db:
        if not db.execute("SELECT 1 FROM devices WHERE operation_id=? AND id=?",(OPERATION_ID,payload["source_id"])).fetchone(): return jsonify(error="source device does not exist"),409
        db.execute("""INSERT INTO channels VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(operation_id,id) DO UPDATE SET name=excluded.name,unit=excluded.unit,source_id=excluded.source_id,warning=excluded.warning,critical=excluded.critical,sample_rate=excluded.sample_rate""",
                   (OPERATION_ID,channel_id,str(payload["name"]).strip(),str(payload["unit"]).strip(),payload["source_id"],"NOT_TESTED",warning,critical,rate))
        db.execute("""INSERT INTO channel_integrations VALUES(?,?,?,?,?,?,?) ON CONFLICT(operation_id,channel_id) DO UPDATE SET raw_field=excluded.raw_field,calibration_slope=excluded.calibration_slope,calibration_intercept=excluded.calibration_intercept,stale_timeout_ms=excluded.stale_timeout_ms,required_for_commit=excluded.required_for_commit""",
                   (OPERATION_ID,channel_id,str(payload["raw_field"]).strip(),slope,intercept,stale,1 if payload.get("required",True) else 0))
        event(db,"CHANNEL_CONFIG","INSTRUMENTATION","INFO",f"Channel {channel_id} configuration saved")
    return jsonify(ok=True,channel_id=channel_id)


@control.post("/api/control/replay")
def upload_replay():
    file = request.files.get("file")
    if not file or not file.filename: return jsonify(error="CSV file is required"),400
    try: report=inspect_csv(file.read())
    except (UnicodeDecodeError,ValueError) as exc: return jsonify(error=str(exc)),400
    with connect() as db:
        db.execute("UPDATE replay_datasets SET active=0 WHERE operation_id=?",(OPERATION_ID,))
        cursor=db.execute("INSERT INTO replay_datasets(operation_id,filename,uploaded_at,row_count,columns_json,preview_json,active) VALUES(?,?,?,?,?,?,1)",
                          (OPERATION_ID,file.filename,utc_now(),report["row_count"],json.dumps(report["columns"]),json.dumps(report["preview"])))
        event(db,"REPLAY_DATASET","INSTRUMENTATION","INFO",f"CSV replay dataset loaded: {file.filename} ({report['row_count']} rows)")
    return jsonify(ok=True,id=cursor.lastrowid,**report)


@control.post("/api/control/station/<code>")
def set_station(code: str):
    decision = request.json.get("decision", "") if request.is_json else ""
    if decision not in {"GO", "NO_GO", "PENDING"}:
        return jsonify(error="invalid decision"), 400
    with connect() as db:
        changed = db.execute("UPDATE stations SET decision=?,updated_at=? WHERE operation_id=? AND code=?",
                             (decision, utc_now(), OPERATION_ID, code)).rowcount
        if not changed: return jsonify(error="station not found"), 404
        event(db, "POLL", code, "WARNING" if decision == "NO_GO" else "INFO", f"Station decision set to {decision}")
    return jsonify(ok=True)


@control.post("/api/control/step/<int:sequence>/complete")
def complete_step(sequence: int):
    with connect() as db:
        step = db.execute("SELECT * FROM procedure_steps WHERE operation_id=? AND sequence=?", (OPERATION_ID, sequence)).fetchone()
        if not step: return jsonify(error="step not found"), 404
        previous = db.execute("SELECT count(*) FROM procedure_steps WHERE operation_id=? AND sequence<? AND status!='COMPLETE'", (OPERATION_ID, sequence)).fetchone()[0]
        if previous: return jsonify(error="previous required steps are incomplete"), 409
        db.execute("UPDATE procedure_steps SET status='COMPLETE',completed_by=?,completed_at=? WHERE operation_id=? AND sequence=?",
                   (step["role"] + " / SIM", utc_now(), OPERATION_ID, sequence))
        event(db, "PROCEDURE", step["role"], "INFO", f"Step {sequence} completed: {step['title']}")
    return jsonify(ok=True)


@control.post("/api/control/command")
def command():
    action = request.json.get("action", "") if request.is_json else ""
    with connect() as db:
        op = db.execute("SELECT * FROM operations WHERE id=?", (OPERATION_ID,)).fetchone()
        if action == "HOLD" and op["state"] not in {"ABORTED", "POST_FIRE", "CLOSED"}:
            reason = request.json.get("reason", "Operator hold")
            db.execute("UPDATE operations SET prior_state=state,state='HOLD',active_hold=?,updated_at=? WHERE id=?", (reason, utc_now(), OPERATION_ID)); event(db, "HOLD", "TEST_DIRECTOR", "WARNING", reason)
        elif action == "RESUME" and op["state"] == "HOLD":
            db.execute("UPDATE operations SET state=COALESCE(prior_state,'CHECKOUT'),prior_state=NULL,active_hold=NULL,updated_at=? WHERE id=?", (utc_now(), OPERATION_ID)); event(db, "HOLD_RELEASE", "TEST_DIRECTOR", "INFO", "Hold released")
        elif action == "ABORT" and op["state"] not in {"ABORTED", "CLOSED"}:
            db.execute("UPDATE operations SET prior_state=state,state='ABORTED',active_hold='Abort declared',updated_at=? WHERE id=?", (utc_now(), OPERATION_ID)); event(db, "ABORT", "TEST_DIRECTOR", "CRITICAL", "Abort declared; execute safing branch")
        elif action == "COUNTDOWN" and op["state"] == "CHECKOUT":
            no_go = db.execute("SELECT count(*) FROM stations WHERE operation_id=? AND decision!='GO'", (OPERATION_ID,)).fetchone()[0]
            if no_go: return jsonify(error="all required stations must be GO"), 409
            incomplete = db.execute("SELECT count(*) FROM procedure_steps WHERE operation_id=? AND sequence<=90 AND status!='COMPLETE'", (OPERATION_ID,)).fetchone()[0]
            if incomplete: return jsonify(error="pre-countdown procedure is incomplete"), 409
            db.execute("UPDATE operations SET state='COUNTDOWN',updated_at=? WHERE id=?", (utc_now(), OPERATION_ID)); event(db, "STATE", "TEST_DIRECTOR", "INFO", "Terminal countdown authorised")
        elif action == "FIRE" and op["state"] == "COUNTDOWN":
            db.execute("UPDATE operations SET state='FIRING',firing_started_monotonic=?,updated_at=? WHERE id=?", (time.monotonic(), utc_now(), OPERATION_ID)); event(db, "FIELD_ACK", "FIELD_CONTROLLER_SIM", "CRITICAL", "SIMULATED firing event acknowledged")
        elif action == "POST_FIRE" and op["state"] == "FIRING" and telemetry(op)["elapsed"] >= 8:
            db.execute("UPDATE operations SET state='POST_FIRE',updated_at=? WHERE id=?", (utc_now(), OPERATION_ID)); event(db, "STATE", "TEST_DIRECTOR", "INFO", "Post-fire phase entered")
        elif action == "RESET_SIM":
            db.execute("UPDATE operations SET state='CHECKOUT',prior_state=NULL,active_hold=NULL,firing_started_monotonic=NULL,updated_at=? WHERE id=?", (utc_now(), OPERATION_ID)); db.execute("UPDATE stations SET decision='PENDING',updated_at=? WHERE operation_id=?", (utc_now(), OPERATION_ID)); db.execute("UPDATE procedure_steps SET status='PENDING',completed_by=NULL,completed_at=NULL WHERE operation_id=?", (OPERATION_ID,)); event(db, "RESET", "SYSTEM", "INFO", "Simulation attempt reset")
        else: return jsonify(error=f"command {action} is not valid from {op['state']}"), 409
    return jsonify(ok=True)
