from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from .control import OPERATION_ID, connect, init_control_db

operations = Blueprint("operations", __name__)

MISSION_TYPES = {"STATIC_TEST", "LAUNCH", "QUALIFICATION", "REHEARSAL"}
OPERATION_TYPES = {
    "STATIC_FIRE": "Static Fire",
    "PRESSURE_TEST": "Pressure Test",
    "AVIONICS_TEST": "Avionics Test",
    "RECOVERY_TEST": "Recovery Test",
    "HIL_TEST": "Hardware-in-the-Loop",
    "FULL_REHEARSAL": "Full Rehearsal",
    "ROCKET_LAUNCH": "Sounding Rocket Launch",
    "RECOVERY_OPERATION": "Recovery Operation",
}
WORKFLOW = [
    ("BRIEF", "Operation Brief"),
    ("ARTICLE", "Test Article / Vehicle"),
    ("BASELINE", "Configuration Baseline"),
    ("TEAM", "Team & Authority"),
    ("PROCEDURE", "Procedure"),
    ("INSTRUMENTATION", "Instrumentation"),
    ("VIDEO", "Video & Recording"),
    ("READINESS", "Readiness Review"),
    ("REHEARSAL", "Rehearsal"),
    ("EXECUTION", "Live Execution"),
    ("REVIEW", "Review & Closure"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 mission_type TEXT NOT NULL, objectives TEXT NOT NULL, target_date TEXT,
 status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operation_registry(
 id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id INTEGER NOT NULL,
 runtime_operation_id TEXT, code TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 operation_type TEXT NOT NULL, site TEXT NOT NULL, planned_start TEXT,
 objective TEXT NOT NULL, success_criteria_json TEXT NOT NULL,
 owner TEXT NOT NULL, risk_class TEXT NOT NULL, status TEXT NOT NULL,
 current_stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(mission_id) REFERENCES missions(id));
CREATE TABLE IF NOT EXISTS operation_workflow_sections(
 operation_id INTEGER NOT NULL, section_key TEXT NOT NULL, name TEXT NOT NULL,
 sequence INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT,
 blocker TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(operation_id,section_key),
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS operation_activity(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
 occurred_at TEXT NOT NULL, activity_type TEXT NOT NULL,
 actor TEXT NOT NULL, message TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def init_operations_db() -> None:
    init_control_db()
    stamp = utc_now()
    with connect() as db:
        db.executescript(SCHEMA)
        mission = db.execute("SELECT id FROM missions WHERE code='QUALSRM-01'").fetchone()
        if not mission:
            cursor = db.execute("""INSERT INTO missions(code,name,mission_type,objectives,target_date,status,created_at,updated_at)
                VALUES('QUALSRM-01','QualSRM Flight Qualification','QUALIFICATION',?,'2026-09-30','ACTIVE',?,?)""",
                ("Qualify the RNX-71V propulsion system and prepare the integrated sounding rocket for flight.", stamp, stamp))
            mission_id = cursor.lastrowid
        else:
            mission_id = mission["id"]
        operation = db.execute("SELECT id FROM operation_registry WHERE code='QST-001'").fetchone()
        if not operation:
            cursor = db.execute("""INSERT INTO operation_registry(
                mission_id,runtime_operation_id,code,title,operation_type,site,planned_start,
                objective,success_criteria_json,owner,risk_class,status,current_stage,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (mission_id, OPERATION_ID, "QST-001",
                "RNX-71V Static Qualification", "STATIC_FIRE", "Al Buraimi Test Site", None,
                "Validate motor performance, structural integrity and the operational test chain.",
                json.dumps(["Stable ignition", "Pressure remains within approved envelope", "Evidence package is complete"]),
                "Test Director", "HAZARDOUS", "PREPARATION", "ARTICLE", stamp, stamp))
            operation_id = cursor.lastrowid
            for sequence, (key, name) in enumerate(WORKFLOW, 1):
                status = "COMPLETE" if key == "BRIEF" else ("ACTIVE" if key == "ARTICLE" else "LOCKED")
                owner = "Test Director" if key in {"BRIEF", "READINESS", "EXECUTION"} else None
                db.execute("INSERT INTO operation_workflow_sections VALUES(?,?,?,?,?,?,?,?)",
                           (operation_id, key, name, sequence, status, owner, None, stamp))
            db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                       (operation_id, stamp, "CREATED", "SYSTEM", "Existing qualification operation registered in the operational workflow"))


@operations.before_request
def ensure_operations() -> None:
    init_operations_db()


def operation_view(db: sqlite3.Connection, operation_id: int) -> dict | None:
    row = db.execute("""SELECT o.*,m.code mission_code,m.name mission_name,m.mission_type,m.status mission_status
        FROM operation_registry o JOIN missions m ON m.id=o.mission_id WHERE o.id=?""", (operation_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["success_criteria"] = json.loads(item.pop("success_criteria_json") or "[]")
    item["sections"] = [dict(x) for x in db.execute(
        "SELECT * FROM operation_workflow_sections WHERE operation_id=? ORDER BY sequence", (operation_id,))]
    complete = sum(1 for x in item["sections"] if x["status"] == "COMPLETE")
    item["progress"] = round(complete / max(1, len(item["sections"])) * 100)
    item["next_section"] = next((x for x in item["sections"] if x["status"] in {"ACTIVE", "AVAILABLE"}), None)
    item["activity"] = [dict(x) for x in db.execute(
        "SELECT * FROM operation_activity WHERE operation_id=? ORDER BY id DESC LIMIT 12", (operation_id,))]
    return item


@operations.get("/ops")
def dashboard():
    with connect() as db:
        rows = db.execute("SELECT id FROM operation_registry ORDER BY updated_at DESC").fetchall()
        items = [operation_view(db, row["id"]) for row in rows]
    active = next((x for x in items if x["status"] not in {"CLOSED", "CANCELLED"}), None)
    return render_template("ops_dashboard.html", operations=items, active=active)


@operations.get("/ops/new")
def new_operation():
    with connect() as db:
        missions = [dict(x) for x in db.execute("SELECT * FROM missions ORDER BY updated_at DESC")]
    return render_template("ops_new.html", missions=missions, operation_types=OPERATION_TYPES)


@operations.get("/ops/<int:operation_id>")
def operation_detail(operation_id: int):
    with connect() as db:
        item = operation_view(db, operation_id)
    if not item:
        return "Operation not found", 404
    return render_template("ops_detail.html", operation=item)


def valid_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,31}", value))


@operations.post("/api/ops")
def create_operation():
    p = request.get_json(silent=True) or request.form
    mission_id = p.get("mission_id"); mission_name = str(p.get("mission_name", "")).strip()
    mission_code = str(p.get("mission_code", "")).strip().upper()
    code = str(p.get("code", "")).strip().upper(); title = str(p.get("title", "")).strip()
    operation_type = str(p.get("operation_type", "")).strip().upper()
    objective = str(p.get("objective", "")).strip(); site = str(p.get("site", "")).strip()
    owner = str(p.get("owner", "")).strip(); risk = str(p.get("risk_class", "HAZARDOUS")).upper()
    criteria = p.get("success_criteria", [])
    if isinstance(criteria, str): criteria = [x.strip() for x in criteria.splitlines() if x.strip()]
    if not valid_code(code) or not title or operation_type not in OPERATION_TYPES or not objective or not site or not owner or not criteria:
        return jsonify(error="operation code, title, type, objective, site, owner and success criteria are required"), 400
    stamp = utc_now()
    with connect() as db:
        if mission_id:
            mission = db.execute("SELECT id FROM missions WHERE id=?", (mission_id,)).fetchone()
            if not mission: return jsonify(error="selected mission was not found"), 404
            resolved_mission_id = mission["id"]
        else:
            if not valid_code(mission_code) or not mission_name:
                return jsonify(error="new mission requires a valid code and name"), 400
            try:
                cursor = db.execute("""INSERT INTO missions(code,name,mission_type,objectives,status,created_at,updated_at)
                    VALUES(?,?,?,?,'PLANNING',?,?)""", (mission_code, mission_name, "LAUNCH" if operation_type == "ROCKET_LAUNCH" else "QUALIFICATION", objective, stamp, stamp))
            except sqlite3.IntegrityError:
                return jsonify(error="mission code already exists"), 409
            resolved_mission_id = cursor.lastrowid
        try:
            cursor = db.execute("""INSERT INTO operation_registry(
                mission_id,code,title,operation_type,site,planned_start,objective,success_criteria_json,
                owner,risk_class,status,current_stage,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'PLANNING','ARTICLE',?,?)""", (resolved_mission_id, code, title,
                operation_type, site, str(p.get("planned_start", "")).strip() or None, objective,
                json.dumps(criteria), owner, risk, stamp, stamp))
        except sqlite3.IntegrityError:
            return jsonify(error="operation code already exists"), 409
        operation_id = cursor.lastrowid
        for sequence, (key, name) in enumerate(WORKFLOW, 1):
            status = "COMPLETE" if key == "BRIEF" else ("ACTIVE" if key == "ARTICLE" else "LOCKED")
            db.execute("INSERT INTO operation_workflow_sections VALUES(?,?,?,?,?,?,?,?)",
                       (operation_id, key, name, sequence, status, owner if key == "BRIEF" else None, None, stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "CREATED", owner, f"Operation {code} created and brief completed"))
    return jsonify(ok=True, id=operation_id, url=url_for("operations.operation_detail", operation_id=operation_id))


@operations.post("/api/ops/<int:operation_id>/continue")
def continue_operation(operation_id: int):
    with connect() as db:
        item = operation_view(db, operation_id)
    if not item: return jsonify(error="operation not found"), 404
    routes = {"EXECUTION": "/workspace", "REVIEW": "/workspace?mode=review"}
    return jsonify(ok=True, stage=item["current_stage"], url=routes.get(item["current_stage"], f"/ops/{operation_id}"))
