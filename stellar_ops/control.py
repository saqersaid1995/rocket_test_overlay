from __future__ import annotations

import math
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context
from .adapters import inspect_csv, test_adapter
from .camera_runtime import (camera_status, has_password, mjpeg_frames,
                             save_password, test_camera)
from .database import add_column, apply_once, connect_database
from .evidence import close_package, open_package
from .telemetry_runtime import (ensure_schema as ensure_runtime_schema, evaluate_alarms,
                                recording_status, runtime_snapshot)

ROOT = Path(__file__).resolve().parent
CONTROL_DB = Path(os.environ.get("STELLAR_OPS_DATA", ROOT / "data")) / "control.db"
control = Blueprint("control", __name__)
OPERATION_ID = "OP-QUAL-STATIC-001"
DEVICE_TYPES = {"DAQ", "PRESSURE", "LOAD-CELL", "THERMOCOUPLE", "IP-CAMERA", "CONTROLLER", "TIME", "LOGGER"}
ADAPTER_TYPES = {"SMTCS_EDGE_TCP", "SIMULATOR", "MODBUS_TCP", "OPC_UA", "TCP", "SERIAL", "MODBUS_RTU", "CAN", "ONVIF", "RTSP", "CSV_REPLAY", "NTP"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect() -> sqlite3.Connection:
    return connect_database(CONTROL_DB)


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
CREATE TABLE IF NOT EXISTS channel_lifecycle(
 operation_id TEXT NOT NULL, channel_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 retired_at TEXT, PRIMARY KEY(operation_id,channel_id));
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
CREATE TABLE IF NOT EXISTS test_runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL, test_article TEXT NOT NULL, configuration_revision TEXT,
 propellant_batch TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, activated_at TEXT,
 closed_at TEXT, notes TEXT, active INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS workspace_layouts(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, name TEXT NOT NULL,
 console_role TEXT NOT NULL, layout_json TEXT NOT NULL, is_default INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL, UNIQUE(operation_id,name));
CREATE TABLE IF NOT EXISTS alarm_actions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, alarm_id INTEGER NOT NULL,
 action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, occurred_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS limit_profiles(
 operation_id TEXT NOT NULL, name TEXT NOT NULL, phase TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 settings_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(operation_id,name));
CREATE TABLE IF NOT EXISTS evidence_packages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, run_id INTEGER NOT NULL,
 recording_session_id INTEGER, created_at TEXT NOT NULL, closed_at TEXT, state TEXT NOT NULL,
 directory TEXT NOT NULL, manifest_path TEXT, manifest_sha256 TEXT, telemetry_batches INTEGER NOT NULL DEFAULT 0,
 telemetry_samples INTEGER NOT NULL DEFAULT 0, sequence_gaps INTEGER NOT NULL DEFAULT 0);
"""

WORKSPACE_PRESETS = {
    "TEST DIRECTOR": ["mission","procedure","poll","alarms","events","cameras"],
    "INSTRUMENTATION": ["telemetry","channels","network","alarms","events","storage"],
    "PROPULSION": ["telemetry","derived","mission","procedure","alarms","events"],
    "DATA & VIDEO": ["cameras","storage","network","events","telemetry","alarms"],
    "OBSERVER": ["mission","telemetry","cameras","events"],
}

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
    run=db.execute("SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",(OPERATION_ID,)).fetchone()
    db.execute("INSERT INTO events(operation_id,occurred_at,event_type,source,severity,message,run_id) VALUES(?,?,?,?,?,?,?)",
               (OPERATION_ID, utc_now(), kind, source, severity, message,run["id"] if run else None))


def init_control_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        stamp = utc_now()
        def run_linkage_migration(connection):
            add_column(connection,"events","run_id INTEGER")
            add_column(connection,"alarms","run_id INTEGER")
            add_column(connection,"edge_batches","run_id INTEGER")
        apply_once(db,1,"link operational records to test runs",stamp,run_linkage_migration)
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
            db.execute("INSERT OR IGNORE INTO channel_lifecycle VALUES(?,?,1,NULL)", (OPERATION_ID, row[0]))
        for row in STEPS:
            db.execute("INSERT OR IGNORE INTO procedure_steps VALUES(?,?,?,?,?,?,?,?,?)",
                       (OPERATION_ID, *row, "PENDING", None, None))
        if not db.execute("SELECT 1 FROM test_runs WHERE operation_id=?",(OPERATION_ID,)).fetchone():
            db.execute("""INSERT INTO test_runs(operation_id,code,title,test_article,configuration_revision,status,created_at,active)
                VALUES(?,?,?,?,?,'PLANNING',?,1)""",(OPERATION_ID,"RUN-SRM-2026-001","RNX-71V Static Qualification","RNX-71V / SERIAL UNASSIGNED","WORKING REV",stamp))
        active_run=db.execute("SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",(OPERATION_ID,)).fetchone()
        if active_run:
            db.execute("UPDATE events SET run_id=? WHERE operation_id=? AND run_id IS NULL",(active_run["id"],OPERATION_ID))
            db.execute("UPDATE alarms SET run_id=? WHERE operation_id=? AND run_id IS NULL",(active_run["id"],OPERATION_ID))
            db.execute("UPDATE edge_batches SET run_id=? WHERE run_id IS NULL",(active_run["id"],))
        for role, panels in WORKSPACE_PRESETS.items():
            layout=[{"panel":panel,"order":index,"span":2 if panel in {"telemetry","cameras"} else 1} for index,panel in enumerate(panels)]
            db.execute("INSERT OR IGNORE INTO workspace_layouts(operation_id,name,console_role,layout_json,is_default,updated_at) VALUES(?,?,?,?,1,?)",
                       (OPERATION_ID,role.title()+" Console",role,json.dumps(layout),stamp))
        for name,phase in (("CHECKOUT","CHECKOUT"),("STATIC FIRE","FIRING"),("POST FIRE","POST_FIRE")):
            db.execute("INSERT OR IGNORE INTO limit_profiles VALUES(?,?,?,1,?,?)",(OPERATION_ID,name,phase,json.dumps({"persistence_samples":5,"hysteresis_percent":2}),stamp))
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


def configuration_error(db: sqlite3.Connection) -> str | None:
    op = db.execute("SELECT state FROM operations WHERE id=?", (OPERATION_ID,)).fetchone()
    if not op or op["state"] not in {"CHECKOUT", "HOLD"}:
        return "configuration changes are only allowed during CHECKOUT or HOLD"
    ensure_runtime_schema(db)
    if recording_status(db, OPERATION_ID).get("state") == "RECORDING":
        return "stop the active recording before changing device or channel configuration"
    return None


def snapshot() -> dict:
    init_control_db()
    with connect() as db:
        op = db.execute("SELECT * FROM operations WHERE id=?", (OPERATION_ID,)).fetchone()
        ensure_runtime_schema(db)
        op_dict = dict(op)
        active_run_row=db.execute("SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",(OPERATION_ID,)).fetchone()
        active_run_id=active_run_row["id"] if active_run_row else None
        runtime = runtime_snapshot(db, op_dict, telemetry(op))
        evaluate_alarms(db, OPERATION_ID, runtime)
        data = {"operation": op_dict, "stations": [dict(x) for x in db.execute("SELECT * FROM stations WHERE operation_id=? ORDER BY rowid", (OPERATION_ID,))],
                "devices": [dict(x) for x in db.execute("""SELECT d.*,COALESCE(i.enabled,0) AS enabled
                    FROM devices d LEFT JOIN device_integrations i ON i.operation_id=d.operation_id AND i.device_id=d.id
                    WHERE d.operation_id=? ORDER BY d.rowid""", (OPERATION_ID,))],
                "channels": [dict(x) for x in db.execute("""SELECT c.*,COALESCE(l.enabled,1) AS enabled
                    FROM channels c LEFT JOIN channel_lifecycle l ON l.operation_id=c.operation_id AND l.channel_id=c.id
                    WHERE c.operation_id=? ORDER BY c.rowid""", (OPERATION_ID,))],
                "steps": [dict(x) for x in db.execute("SELECT * FROM procedure_steps WHERE operation_id=? ORDER BY sequence", (OPERATION_ID,))],
                "events": [dict(x) for x in db.execute("SELECT * FROM events WHERE operation_id=? AND run_id IS ? ORDER BY sequence DESC LIMIT 40", (OPERATION_ID,active_run_id))],
                "alarms": [dict(x) for x in db.execute("SELECT * FROM alarms WHERE operation_id=? AND run_id IS ? AND state!='CLOSED' ORDER BY id DESC", (OPERATION_ID,active_run_id))]}
        data["integrations"] = [dict(x) for x in db.execute("""SELECT i.*,d.name,d.device_type,d.endpoint
            FROM device_integrations i JOIN devices d ON d.operation_id=i.operation_id AND d.id=i.device_id
            WHERE i.operation_id=? ORDER BY d.rowid""", (OPERATION_ID,))]
        for integration in data["integrations"]:
            integration["secret_configured"] = has_password(integration["device_id"]) if integration["device_type"] == "IP-CAMERA" else False
        data["channel_integrations"] = [dict(x) for x in db.execute("SELECT * FROM channel_integrations WHERE operation_id=? ORDER BY rowid", (OPERATION_ID,))]
        data["replays"] = [dict(x) for x in db.execute("SELECT id,filename,uploaded_at,row_count,columns_json,active FROM replay_datasets WHERE operation_id=? ORDER BY id DESC", (OPERATION_ID,))]
        data["edge_sessions"] = [dict(x) for x in db.execute("SELECT * FROM edge_sessions ORDER BY last_seen DESC LIMIT 20")]
        data["runs"] = [dict(x) for x in db.execute("SELECT * FROM test_runs WHERE operation_id=? ORDER BY id DESC",(OPERATION_ID,))]
        data["workspaces"] = [dict(x) for x in db.execute("SELECT * FROM workspace_layouts WHERE operation_id=? ORDER BY console_role,name",(OPERATION_ID,))]
        data["limit_profiles"] = [dict(x) for x in db.execute("SELECT * FROM limit_profiles WHERE operation_id=? ORDER BY name",(OPERATION_ID,))]
        data["evidence_packages"] = [dict(x) for x in db.execute("SELECT * FROM evidence_packages WHERE operation_id=? AND run_id IS ? ORDER BY id DESC",(OPERATION_ID,active_run_id))]
        data["telemetry"] = runtime
        data["recording"] = recording_status(db, OPERATION_ID)
        for channel in data["channels"]:
            channel["quality"] = "DISABLED" if not channel["enabled"] else runtime.get("channels", {}).get(channel["id"], {}).get("quality", "NO_DATA")
        channel_by_source = {}
        for channel in data["channels"]:
            if not channel["enabled"]: continue
            quality = runtime.get("channels", {}).get(channel["id"], {}).get("quality")
            if quality:
                channel_by_source.setdefault(channel["source_id"], []).append(quality)
        for device in data["devices"]:
            if not device["enabled"]:
                device["health"] = "DISABLED"
                device["recording"] = "STOPPED"
            elif device["device_type"] == "IP-CAMERA":
                device["health"] = camera_status(device["id"])["status"]
                device["recording"] = "STOPPED"
            elif op_dict["mode"] == "LIVE":
                qualities = channel_by_source.get(device["id"], [])
                if device["id"] == runtime.get("meta", {}).get("device_id"):
                    device["health"] = runtime.get("meta", {}).get("status", "DISCONNECTED")
                elif qualities:
                    device["health"] = "GOOD" if all(q == "GOOD" for q in qualities) else qualities[0]
                elif device["id"] != "TIME-01":
                    device["health"] = "NOT_CONNECTED"
                device["recording"] = data["recording"]["state"] if device["id"] == "DAQ-01" else "N/A"
            elif op_dict["mode"] == "REPLAY":
                device["health"] = "REPLAY" if device["id"] in channel_by_source or device["id"] == "DAQ-01" else device["health"]
                device["recording"] = data["recording"]["state"] if device["id"] == "DAQ-01" else device["recording"]
        return data


@control.get("/control")
def console():
    return render_template("control.html", initial=snapshot())


@control.get("/workspace")
def workspace_console():
    return render_template("workspace.html", initial=snapshot())


@control.get("/api/control/snapshot")
def api_snapshot():
    return jsonify(snapshot())


@control.get("/api/control/stream")
def api_stream():
    @stream_with_context
    def generate():
        while True:
            yield f"data:{json.dumps(snapshot(),separators=(',',':'))}\n\n"
            time.sleep(0.5)
    return Response(generate(),mimetype="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@control.post("/api/control/workspace")
def save_workspace():
    payload=request.get_json(silent=True) or {}; name=str(payload.get("name","")).strip(); role=str(payload.get("console_role","")).strip().upper(); layout=payload.get("layout")
    allowed={"mission","telemetry","derived","procedure","poll","alarms","events","cameras","channels","network","storage"}
    if not name or len(name)>80 or role not in WORKSPACE_PRESETS: return jsonify(error="valid workspace name and console role are required"),400
    if not isinstance(layout,list) or not layout or len(layout)>16: return jsonify(error="workspace must contain between 1 and 16 panels"),400
    if any(not isinstance(item,dict) or item.get("panel") not in allowed or int(item.get("span",1)) not in {1,2,3} for item in layout): return jsonify(error="workspace contains an invalid panel definition"),400
    normalized=[{"panel":item["panel"],"order":index,"span":int(item.get("span",1))} for index,item in enumerate(layout)]
    with connect() as db:
        db.execute("""INSERT INTO workspace_layouts(operation_id,name,console_role,layout_json,is_default,updated_at)
            VALUES(?,?,?,?,0,?) ON CONFLICT(operation_id,name) DO UPDATE SET console_role=excluded.console_role,
            layout_json=excluded.layout_json,updated_at=excluded.updated_at""",(OPERATION_ID,name,role,json.dumps(normalized,separators=(",",":")),utc_now()))
        event(db,"WORKSPACE_CONFIG","SYSTEMS","INFO",f"Workspace saved: {name}")
    return jsonify(ok=True,name=name)


@control.post("/api/control/run")
def create_run():
    payload=request.get_json(silent=True) or {}; code=str(payload.get("code","")).strip().upper(); title=str(payload.get("title","")).strip(); article=str(payload.get("test_article","")).strip()
    if not code or not title or not article: return jsonify(error="run code, title and test article are required"),400
    if not all(char.isalnum() or char in "-_" for char in code): return jsonify(error="run code may contain letters, numbers, hyphen and underscore only"),400
    with connect() as db:
        try:
            cursor=db.execute("""INSERT INTO test_runs(operation_id,code,title,test_article,configuration_revision,propellant_batch,status,created_at,notes)
                VALUES(?,?,?,?,?,?,'PLANNING',?,?)""",(OPERATION_ID,code,title,article,str(payload.get("configuration_revision","")).strip(),str(payload.get("propellant_batch","")).strip(),utc_now(),str(payload.get("notes","")).strip()))
        except sqlite3.IntegrityError: return jsonify(error="run code already exists"),409
        event(db,"RUN_CREATED","TEST_DIRECTOR","INFO",f"Run {code} created")
    return jsonify(ok=True,id=cursor.lastrowid,code=code)


@control.post("/api/control/run/<int:run_id>/activate")
def activate_run(run_id: int):
    with connect() as db:
        if recording_status(db,OPERATION_ID).get("state")=="RECORDING": return jsonify(error="stop recording before changing the active run"),409
        operation=db.execute("SELECT state FROM operations WHERE id=?",(OPERATION_ID,)).fetchone()
        if operation["state"] not in {"CHECKOUT","HOLD"}: return jsonify(error="a run may only be activated during CHECKOUT or HOLD"),409
        run=db.execute("SELECT code FROM test_runs WHERE operation_id=? AND id=?",(OPERATION_ID,run_id)).fetchone()
        if not run: return jsonify(error="run not found"),404
        previous=db.execute("SELECT code FROM test_runs WHERE operation_id=? AND active=1",(OPERATION_ID,)).fetchone()
        if previous: event(db,"RUN_SUSPENDED","TEST_DIRECTOR","WARNING",f"Run {previous['code']} suspended for run change")
        db.execute("UPDATE test_runs SET active=0,status=CASE WHEN status='ACTIVE' THEN 'SUSPENDED' ELSE status END WHERE operation_id=?",(OPERATION_ID,)); db.execute("UPDATE test_runs SET active=1,status='ACTIVE',activated_at=? WHERE id=?",(utc_now(),run_id))
        db.execute("UPDATE operations SET state='CHECKOUT',prior_state=NULL,active_hold=NULL,firing_started_monotonic=NULL,updated_at=? WHERE id=?",(utc_now(),OPERATION_ID))
        db.execute("UPDATE stations SET decision='PENDING',updated_at=? WHERE operation_id=?",(utc_now(),OPERATION_ID)); db.execute("UPDATE procedure_steps SET status='PENDING',completed_by=NULL,completed_at=NULL WHERE operation_id=?",(OPERATION_ID,))
        event(db,"RUN_ACTIVATED","TEST_DIRECTOR","WARNING",f"Run {run['code']} activated")
    return jsonify(ok=True,id=run_id)


@control.post("/api/control/alarm/<int:alarm_id>/action")
def alarm_action(alarm_id: int):
    payload=request.get_json(silent=True) or {}; action=str(payload.get("action","")).upper(); reason=str(payload.get("reason","")).strip()
    if action not in {"ACKNOWLEDGE","SHELVE","CLOSE"}: return jsonify(error="invalid alarm action"),400
    if action in {"SHELVE","CLOSE"} and not reason: return jsonify(error="a reason is required for this alarm action"),400
    states={"ACKNOWLEDGE":"ACTIVE_ACKNOWLEDGED","SHELVE":"SHELVED","CLOSE":"CLOSED"}
    with connect() as db:
        alarm=db.execute("SELECT * FROM alarms WHERE operation_id=? AND id=?",(OPERATION_ID,alarm_id)).fetchone()
        if not alarm: return jsonify(error="alarm not found"),404
        if action=="CLOSE" and db.execute("SELECT 1 FROM alarm_keys WHERE operation_id=? AND alarm_id=?",(OPERATION_ID,alarm_id)).fetchone():
            return jsonify(error="the alarm condition is still active; acknowledge or shelve it until the source recovers"),409
        db.execute("UPDATE alarms SET state=? WHERE id=?",(states[action],alarm_id)); db.execute("INSERT INTO alarm_actions(operation_id,alarm_id,action,actor,reason,occurred_at) VALUES(?,?,?,?,?,?)",(OPERATION_ID,alarm_id,action,"CONSOLE OPERATOR",reason,utc_now()))
        event(db,"ALARM_ACTION","CONSOLE OPERATOR","WARNING",f"Alarm {alarm_id} {action.lower()}: {reason or 'acknowledged'}")
    return jsonify(ok=True,id=alarm_id,state=states[action])


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
    device_type = str(payload["device_type"]).strip().upper()
    if device_type not in DEVICE_TYPES: return jsonify(error="unsupported device type"), 400
    if adapter not in ADAPTER_TYPES: return jsonify(error="unsupported adapter type"), 400
    if device_type == "IP-CAMERA" and adapter not in {"ONVIF", "RTSP"}: return jsonify(error="IP cameras require ONVIF or RTSP adapter"), 400
    if adapter in {"ONVIF", "RTSP"} and device_type != "IP-CAMERA": return jsonify(error="ONVIF and RTSP adapters are only valid for IP cameras"), 400
    endpoint = str(payload["endpoint"]).strip()
    config = {"endpoint": endpoint, "username": str(payload.get("username", "")).strip(),
              "profile": str(payload.get("profile", "")).strip(), "notes": str(payload.get("notes", "")).strip()}
    with connect() as db:
        blocked = configuration_error(db)
        if blocked: return jsonify(error=blocked), 409
        password = str(payload.get("password", ""))
        if device_type == "IP-CAMERA" and password:
            try:
                save_password(device_id, password)
            except RuntimeError as exc:
                return jsonify(error=str(exc)), 503
        db.execute("""INSERT INTO devices(operation_id,id,name,device_type,protocol,endpoint,health,recording,required)
          VALUES(?,?,?,?,?,?,?,'STOPPED',?)
          ON CONFLICT(operation_id,id) DO UPDATE SET name=excluded.name,device_type=excluded.device_type,
          protocol=excluded.protocol,endpoint=excluded.endpoint,required=excluded.required""",
          (OPERATION_ID, device_id, str(payload["name"]).strip(), device_type, adapter,
           endpoint, "NOT_TESTED", 1 if payload.get("required", True) else 0))
        db.execute("""INSERT INTO device_integrations(operation_id,device_id,adapter_type,config_json,enabled,last_test_status)
          VALUES(?,?,?,?,1,'NOT_TESTED') ON CONFLICT(operation_id,device_id) DO UPDATE SET
          adapter_type=excluded.adapter_type,config_json=excluded.config_json,enabled=1,last_test_status='NOT_TESTED',last_test_message=NULL""",
          (OPERATION_ID, device_id, adapter, json.dumps(config)))
        event(db, "DEVICE_CONFIG", "INSTRUMENTATION", "INFO", f"Device {device_id} saved with {adapter} adapter")
    return jsonify(ok=True, device_id=device_id)


@control.post("/api/control/device/<device_id>/state")
def set_device_state(device_id: str):
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    device_id = device_id.upper()
    with connect() as db:
        blocked = configuration_error(db)
        if blocked: return jsonify(error=blocked), 409
        row = db.execute("SELECT name FROM devices WHERE operation_id=? AND id=?", (OPERATION_ID,device_id)).fetchone()
        if not row: return jsonify(error="device not found"), 404
        if not enabled:
            linked = [x["id"] for x in db.execute("""SELECT c.id FROM channels c LEFT JOIN channel_lifecycle l
                ON l.operation_id=c.operation_id AND l.channel_id=c.id WHERE c.operation_id=? AND c.source_id=?
                AND COALESCE(l.enabled,1)=1 ORDER BY c.id""", (OPERATION_ID,device_id))]
            if linked: return jsonify(error="archive or reassign linked channels first: " + ", ".join(linked)), 409
        db.execute("UPDATE device_integrations SET enabled=?,last_test_status='NOT_TESTED',last_test_message=NULL WHERE operation_id=? AND device_id=?",
                   (1 if enabled else 0,OPERATION_ID,device_id))
        event(db,"DEVICE_RESTORED" if enabled else "DEVICE_ARCHIVED","INSTRUMENTATION","INFO" if enabled else "WARNING",f"Device {device_id} {'restored' if enabled else 'archived'}")
    return jsonify(ok=True,device_id=device_id,enabled=enabled)


@control.post("/api/control/device/<device_id>/test")
def test_device(device_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM device_integrations WHERE operation_id=? AND device_id=?", (OPERATION_ID, device_id)).fetchone()
        if not row: return jsonify(error="device integration not found"), 404
        if not row["enabled"]: return jsonify(error="restore the device before testing its connection"), 409
        config = json.loads(row["config_json"])
        device = db.execute("SELECT device_type FROM devices WHERE operation_id=? AND id=?", (OPERATION_ID, device_id)).fetchone()
        if device and device["device_type"] == "IP-CAMERA":
            result = test_camera(device_id, row["adapter_type"], config.get("endpoint", ""), config.get("username", ""))
        else:
            result = test_adapter(row["adapter_type"], config.get("endpoint", ""))
        db.execute("UPDATE device_integrations SET last_test_at=?,last_test_status=?,last_test_message=? WHERE operation_id=? AND device_id=?",
                   (utc_now(), result.status, result.message, OPERATION_ID, device_id))
        db.execute("UPDATE devices SET health=? WHERE operation_id=? AND id=?", (result.status, OPERATION_ID, device_id))
        event(db, "CONNECTION_TEST", "INSTRUMENTATION", "INFO" if result.ok else "WARNING", f"{device_id}: {result.message}")
    return jsonify(result.to_dict()), 200 if result.ok else 422


@control.get("/api/control/camera/<device_id>/stream.mjpg")
def camera_stream(device_id: str):
    with connect() as db:
        row = db.execute("""SELECT i.adapter_type,i.config_json,i.enabled,d.device_type
            FROM device_integrations i JOIN devices d ON d.operation_id=i.operation_id AND d.id=i.device_id
            WHERE i.operation_id=? AND i.device_id=?""", (OPERATION_ID, device_id.upper())).fetchone()
    if not row or row["device_type"] != "IP-CAMERA":
        return jsonify(error="camera device not found"), 404
    if not row["enabled"]:
        return jsonify(error="camera is archived"), 409
    config = json.loads(row["config_json"])
    profile = "main" if request.args.get("profile") == "main" else "preview"
    frames = mjpeg_frames(device_id.upper(), row["adapter_type"], config.get("endpoint", ""), config.get("username", ""), profile)
    return Response(frames, mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate", "X-Content-Type-Options": "nosniff"})


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
    if not math.isfinite(slope) or not math.isfinite(intercept) or slope == 0: return jsonify(error="calibration slope must be finite and non-zero"),400
    if rate<1 or rate>10000 or stale<10: return jsonify(error="sample rate or stale timeout is outside supported limits"),400
    if warning is not None and critical is not None and warning >= critical: return jsonify(error="warning limit must be below critical limit"),400
    with connect() as db:
        blocked = configuration_error(db)
        if blocked: return jsonify(error=blocked), 409
        if not db.execute("""SELECT 1 FROM devices d JOIN device_integrations i ON i.operation_id=d.operation_id AND i.device_id=d.id
            WHERE d.operation_id=? AND d.id=? AND i.enabled=1""",(OPERATION_ID,payload["source_id"])).fetchone(): return jsonify(error="source device does not exist or is disabled"),409
        db.execute("""INSERT INTO channels VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(operation_id,id) DO UPDATE SET name=excluded.name,unit=excluded.unit,source_id=excluded.source_id,warning=excluded.warning,critical=excluded.critical,sample_rate=excluded.sample_rate""",
                   (OPERATION_ID,channel_id,str(payload["name"]).strip(),str(payload["unit"]).strip(),payload["source_id"],"NOT_TESTED",warning,critical,rate))
        db.execute("""INSERT INTO channel_integrations VALUES(?,?,?,?,?,?,?) ON CONFLICT(operation_id,channel_id) DO UPDATE SET raw_field=excluded.raw_field,calibration_slope=excluded.calibration_slope,calibration_intercept=excluded.calibration_intercept,stale_timeout_ms=excluded.stale_timeout_ms,required_for_commit=excluded.required_for_commit""",
                   (OPERATION_ID,channel_id,str(payload["raw_field"]).strip(),slope,intercept,stale,1 if payload.get("required",True) else 0))
        db.execute("""INSERT INTO channel_lifecycle VALUES(?,?,1,NULL) ON CONFLICT(operation_id,channel_id)
            DO UPDATE SET enabled=1,retired_at=NULL""", (OPERATION_ID,channel_id))
        event(db,"CHANNEL_CONFIG","INSTRUMENTATION","INFO",f"Channel {channel_id} configuration saved")
    return jsonify(ok=True,channel_id=channel_id)


@control.post("/api/control/channel/<path:channel_id>/state")
def set_channel_state(channel_id: str):
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    with connect() as db:
        blocked = configuration_error(db)
        if blocked: return jsonify(error=blocked), 409
        if not db.execute("SELECT 1 FROM channels WHERE operation_id=? AND id=?",(OPERATION_ID,channel_id)).fetchone():
            return jsonify(error="channel not found"),404
        if enabled:
            source = db.execute("SELECT source_id FROM channels WHERE operation_id=? AND id=?",(OPERATION_ID,channel_id)).fetchone()[0]
            active = db.execute("SELECT enabled FROM device_integrations WHERE operation_id=? AND device_id=?",(OPERATION_ID,source)).fetchone()
            if not active or not active["enabled"]: return jsonify(error="restore the source device before restoring this channel"),409
        db.execute("""INSERT INTO channel_lifecycle VALUES(?,?,?,?) ON CONFLICT(operation_id,channel_id)
            DO UPDATE SET enabled=excluded.enabled,retired_at=excluded.retired_at""",
            (OPERATION_ID,channel_id,1 if enabled else 0,None if enabled else utc_now()))
        event(db,"CHANNEL_RESTORED" if enabled else "CHANNEL_ARCHIVED","INSTRUMENTATION","INFO" if enabled else "WARNING",f"Channel {channel_id} {'restored' if enabled else 'archived'}")
    return jsonify(ok=True,channel_id=channel_id,enabled=enabled)


@control.post("/api/control/replay")
def upload_replay():
    file = request.files.get("file")
    if not file or not file.filename: return jsonify(error="CSV file is required"),400
    try: report=inspect_csv(file.read())
    except (UnicodeDecodeError,ValueError) as exc: return jsonify(error=str(exc)),400
    with connect() as db:
        ensure_runtime_schema(db)
        db.execute("UPDATE replay_datasets SET active=0 WHERE operation_id=?",(OPERATION_ID,))
        cursor=db.execute("INSERT INTO replay_datasets(operation_id,filename,uploaded_at,row_count,columns_json,preview_json,active) VALUES(?,?,?,?,?,?,1)",
                          (OPERATION_ID,file.filename,utc_now(),report["row_count"],json.dumps(report["columns"]),json.dumps(report["preview"])))
        db.execute("INSERT OR REPLACE INTO replay_payloads(dataset_id,rows_json) VALUES(?,?)",(cursor.lastrowid,json.dumps(report["rows"],separators=(",",":"))))
        db.execute("INSERT INTO replay_runtime(operation_id,dataset_id,state,speed,cursor,started_cursor) VALUES(?,?,'PAUSED',1,0,0) ON CONFLICT(operation_id) DO UPDATE SET dataset_id=excluded.dataset_id,state='PAUSED',speed=1,cursor=0,started_wall_time=NULL,started_cursor=0",(OPERATION_ID,cursor.lastrowid))
        event(db,"REPLAY_DATASET","INSTRUMENTATION","INFO",f"CSV replay dataset loaded: {file.filename} ({report['row_count']} rows)")
    return jsonify(ok=True,id=cursor.lastrowid,columns=report["columns"],row_count=report["row_count"],preview=report["preview"])


@control.post("/api/control/mode")
def set_mode():
    mode=(request.get_json(silent=True) or {}).get("mode","").upper()
    if mode not in {"SIMULATION","LIVE","REPLAY"}: return jsonify(error="invalid source mode"),400
    with connect() as db:
        op=db.execute("SELECT * FROM operations WHERE id=?",(OPERATION_ID,)).fetchone()
        if op["state"] not in {"CHECKOUT","HOLD"}: return jsonify(error="source mode may only change during CHECKOUT or HOLD"),409
        ensure_runtime_schema(db)
        if recording_status(db,OPERATION_ID).get("state")=="RECORDING": return jsonify(error="stop the active recording before changing source mode"),409
        if mode=="REPLAY" and not db.execute("SELECT 1 FROM replay_runtime WHERE operation_id=? AND dataset_id IS NOT NULL",(OPERATION_ID,)).fetchone(): return jsonify(error="load a replay dataset first"),409
        db.execute("UPDATE operations SET mode=?,updated_at=? WHERE id=?",(mode,utc_now(),OPERATION_ID)); event(db,"SOURCE_MODE","INSTRUMENTATION","WARNING" if mode!="LIVE" else "INFO",f"Telemetry source changed to {mode}")
    return jsonify(ok=True,mode=mode)


@control.post("/api/control/recording")
def set_recording():
    action=(request.get_json(silent=True) or {}).get("action","").upper()
    evidence_result=None
    with connect() as db:
        ensure_runtime_schema(db); op=db.execute("SELECT * FROM operations WHERE id=?",(OPERATION_ID,)).fetchone(); current=recording_status(db,OPERATION_ID)
        if action=="START":
            if current.get("state")=="RECORDING": return jsonify(error="recording is already active"),409
            run=db.execute("SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",(OPERATION_ID,)).fetchone()
            if not run: return jsonify(error="activate a test run before recording"),409
            total=db.execute("SELECT COALESCE(sum(total_samples),0) FROM edge_sessions").fetchone()[0]
            cursor=db.execute("INSERT INTO recording_sessions(operation_id,run_id,source_mode,started_at,state,started_by,sample_count_start) VALUES(?,?,?,?, 'RECORDING','INSTRUMENTATION',?)",(OPERATION_ID,run["id"],op["mode"],utc_now(),total))
            package_id=open_package(db,OPERATION_ID,cursor.lastrowid,CONTROL_DB.parent); event(db,"RECORDING","INSTRUMENTATION","INFO",f"Recording session {cursor.lastrowid} started in {op['mode']} mode; evidence package {package_id} opened")
        elif action=="STOP" and current.get("state")=="RECORDING":
            total=db.execute("SELECT COALESCE(sum(total_samples),0) FROM edge_sessions").fetchone()[0]
            db.execute("UPDATE recording_sessions SET stopped_at=?,state='STOPPED',sample_count_stop=? WHERE id=?",(utc_now(),total,current["id"])); evidence_result=close_package(db,OPERATION_ID,current["id"]); event(db,"RECORDING","INSTRUMENTATION","WARNING",f"Recording session {current['id']} stopped; evidence package {evidence_result['package_id']} sealed")
        else: return jsonify(error="recording command is invalid"),409
    return jsonify(ok=True,evidence=evidence_result)


@control.post("/api/control/replay/control")
def replay_control():
    payload=request.get_json(silent=True) or {}; action=str(payload.get("action","")).upper()
    with connect() as db:
        ensure_runtime_schema(db); row=db.execute("SELECT * FROM replay_runtime WHERE operation_id=?",(OPERATION_ID,)).fetchone()
        if not row or not row["dataset_id"]: return jsonify(error="no replay dataset loaded"),409
        if action=="PLAY": db.execute("UPDATE replay_runtime SET state='PLAYING',started_wall_time=?,started_cursor=cursor WHERE operation_id=?",(time.time(),OPERATION_ID))
        elif action=="PAUSE":
            current=replay_control_cursor(db,row); db.execute("UPDATE replay_runtime SET state='PAUSED',cursor=?,started_wall_time=NULL,started_cursor=? WHERE operation_id=?",(current,current,OPERATION_ID))
        elif action=="SEEK":
            cursor=max(0,int(payload.get("cursor",0))); db.execute("UPDATE replay_runtime SET state='PAUSED',cursor=?,started_wall_time=NULL,started_cursor=? WHERE operation_id=?",(cursor,cursor,OPERATION_ID))
        elif action=="SPEED":
            speed=float(payload.get("speed",1));
            if speed not in {0.5,1,2,10}: return jsonify(error="unsupported replay speed"),400
            current=replay_control_cursor(db,row); db.execute("UPDATE replay_runtime SET speed=?,cursor=?,started_cursor=?,started_wall_time=? WHERE operation_id=?",(speed,current,current,time.time() if row["state"]=="PLAYING" else None,OPERATION_ID))
        else: return jsonify(error="invalid replay command"),400
        event(db,"REPLAY_CONTROL","INSTRUMENTATION","INFO",f"Replay command {action}")
    return jsonify(ok=True)


def replay_control_cursor(db,row):
    payload=db.execute("SELECT rows_json FROM replay_payloads WHERE dataset_id=?",(row["dataset_id"],)).fetchone(); count=len(json.loads(payload["rows_json"])) if payload else 0
    if row["state"]!="PLAYING" or row["started_wall_time"] is None:return min(max(0,row["cursor"]),max(0,count-1))
    return min(max(0,count-1),row["started_cursor"]+int((time.time()-row["started_wall_time"])*row["speed"]*20))


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
            if op["mode"] == "LIVE":
                ensure_runtime_schema(db)
                live = runtime_snapshot(db, dict(op), telemetry(op))
                recording = recording_status(db, OPERATION_ID)
                if recording.get("state") != "RECORDING" or recording.get("source_mode") != "LIVE":
                    return jsonify(error="LIVE telemetry recording must be active before countdown"), 409
                required = {row["channel_id"] for row in db.execute(
                    """SELECT i.channel_id FROM channel_integrations i LEFT JOIN channel_lifecycle l
                    ON l.operation_id=i.operation_id AND l.channel_id=i.channel_id
                    WHERE i.operation_id=? AND i.required_for_commit=1 AND COALESCE(l.enabled,1)=1""",
                    (OPERATION_ID,))}
                bad = sorted(channel_id for channel_id in required
                             if live.get("channels", {}).get(channel_id, {}).get("quality") != "GOOD")
                if bad:
                    return jsonify(error="required LIVE channels are not GOOD: " + ", ".join(bad)), 409
                if live.get("meta", {}).get("sequence_gaps", 0):
                    return jsonify(error="Ethernet stream contains sequence gaps; disposition the data loss before countdown"), 409
            db.execute("UPDATE operations SET state='COUNTDOWN',updated_at=? WHERE id=?", (utc_now(), OPERATION_ID)); event(db, "STATE", "TEST_DIRECTOR", "INFO", "Terminal countdown authorised")
        elif action == "FIRE" and op["state"] == "COUNTDOWN":
            db.execute("UPDATE operations SET state='FIRING',firing_started_monotonic=?,updated_at=? WHERE id=?", (time.monotonic(), utc_now(), OPERATION_ID)); event(db, "FIELD_ACK", "FIELD_CONTROLLER_SIM", "CRITICAL", "SIMULATED firing event acknowledged")
        elif action == "POST_FIRE" and op["state"] == "FIRING" and runtime_snapshot(db, dict(op), telemetry(op))["elapsed"] >= 8:
            db.execute("UPDATE operations SET state='POST_FIRE',updated_at=? WHERE id=?", (utc_now(), OPERATION_ID)); event(db, "STATE", "TEST_DIRECTOR", "INFO", "Post-fire phase entered")
        elif action == "RESET_SIM":
            db.execute("UPDATE operations SET state='CHECKOUT',prior_state=NULL,active_hold=NULL,firing_started_monotonic=NULL,updated_at=? WHERE id=?", (utc_now(), OPERATION_ID)); db.execute("UPDATE stations SET decision='PENDING',updated_at=? WHERE operation_id=?", (utc_now(), OPERATION_ID)); db.execute("UPDATE procedure_steps SET status='PENDING',completed_by=NULL,completed_at=NULL WHERE operation_id=?", (OPERATION_ID,)); event(db, "RESET", "SYSTEM", "INFO", "Simulation attempt reset")
        else: return jsonify(error=f"command {action} is not valid from {op['state']}"), 409
    return jsonify(ok=True)
