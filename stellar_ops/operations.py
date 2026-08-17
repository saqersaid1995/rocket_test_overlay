from __future__ import annotations

import json
import hashlib
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
CREATE TABLE IF NOT EXISTS test_articles(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 article_class TEXT NOT NULL, serial_number TEXT NOT NULL, name TEXT NOT NULL,
 family TEXT NOT NULL, configuration_revision TEXT NOT NULL,
 build_status TEXT NOT NULL, state TEXT NOT NULL, notes TEXT,
 identified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS article_components(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
 component_type TEXT NOT NULL, position TEXT NOT NULL DEFAULT 'PRIMARY',
 serial_or_lot TEXT NOT NULL, part_number TEXT, revision TEXT,
 status TEXT NOT NULL, notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(operation_id,component_type,position),
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS configuration_baselines(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 baseline_code TEXT NOT NULL, revision TEXT NOT NULL, state TEXT NOT NULL,
 article_id INTEGER NOT NULL, notes TEXT, canonical_sha256 TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, released_at TEXT, released_by TEXT,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id),
 FOREIGN KEY(article_id) REFERENCES test_articles(id));
CREATE TABLE IF NOT EXISTS baseline_items(
 id INTEGER PRIMARY KEY AUTOINCREMENT, baseline_id INTEGER NOT NULL,
 item_type TEXT NOT NULL, reference TEXT NOT NULL, revision TEXT NOT NULL,
 required INTEGER NOT NULL, verification_status TEXT NOT NULL,
 source TEXT NOT NULL, notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(baseline_id,item_type),
 FOREIGN KEY(baseline_id) REFERENCES configuration_baselines(id));
CREATE TABLE IF NOT EXISTS staffing_plans(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 state TEXT NOT NULL, approved_at TEXT, approved_by TEXT, notes TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS operation_role_assignments(
 id INTEGER PRIMARY KEY AUTOINCREMENT, staffing_plan_id INTEGER NOT NULL,
 role_code TEXT NOT NULL, person_name TEXT NOT NULL, call_sign TEXT NOT NULL,
 organization TEXT NOT NULL, contact_method TEXT NOT NULL,
 qualification_status TEXT NOT NULL, availability_status TEXT NOT NULL,
 decision_authority INTEGER NOT NULL, conflict_group TEXT,
 notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(staffing_plan_id,role_code),
 FOREIGN KEY(staffing_plan_id) REFERENCES staffing_plans(id));
CREATE TABLE IF NOT EXISTS operation_procedures(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 document_code TEXT NOT NULL, revision TEXT NOT NULL, title TEXT NOT NULL,
 state TEXT NOT NULL, entry_conditions TEXT NOT NULL, exit_conditions TEXT NOT NULL,
 abort_policy TEXT NOT NULL, canonical_sha256 TEXT, approved_at TEXT, approved_by TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS operation_procedure_steps(
 id INTEGER PRIMARY KEY AUTOINCREMENT, procedure_id INTEGER NOT NULL,
 sequence INTEGER NOT NULL, step_code TEXT NOT NULL, phase TEXT NOT NULL,
 step_type TEXT NOT NULL, instruction TEXT NOT NULL, responsible_role TEXT NOT NULL,
 verification_mode TEXT NOT NULL, verifier_role TEXT, expected_evidence TEXT NOT NULL,
 safety_critical INTEGER NOT NULL, hold_condition TEXT, abort_action TEXT,
 updated_at TEXT NOT NULL,
 UNIQUE(procedure_id,sequence), UNIQUE(procedure_id,step_code),
 FOREIGN KEY(procedure_id) REFERENCES operation_procedures(id));
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
    article = db.execute("SELECT * FROM test_articles WHERE operation_id=?", (operation_id,)).fetchone()
    item["article"] = dict(article) if article else None
    item["components"] = [dict(x) for x in db.execute(
        "SELECT * FROM article_components WHERE operation_id=? ORDER BY component_type,position", (operation_id,))]
    baseline = db.execute("SELECT * FROM configuration_baselines WHERE operation_id=?", (operation_id,)).fetchone()
    item["baseline"] = dict(baseline) if baseline else None
    if item["baseline"]:
        item["baseline"]["items"] = [dict(x) for x in db.execute(
            "SELECT * FROM baseline_items WHERE baseline_id=? ORDER BY item_type", (baseline["id"],))]
    staffing = db.execute("SELECT * FROM staffing_plans WHERE operation_id=?", (operation_id,)).fetchone()
    item["staffing"] = dict(staffing) if staffing else None
    if item["staffing"]:
        item["staffing"]["assignments"] = [dict(x) for x in db.execute(
            "SELECT * FROM operation_role_assignments WHERE staffing_plan_id=? ORDER BY role_code", (staffing["id"],))]
    procedure = db.execute("SELECT * FROM operation_procedures WHERE operation_id=?", (operation_id,)).fetchone()
    item["procedure"] = dict(procedure) if procedure else None
    if item["procedure"]:
        item["procedure"]["steps"] = [dict(x) for x in db.execute(
            "SELECT * FROM operation_procedure_steps WHERE procedure_id=? ORDER BY sequence", (procedure["id"],))]
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


@operations.get("/ops/<int:operation_id>/article")
def article_builder(operation_id: int):
    with connect() as db:
        item = operation_view(db, operation_id)
    if not item:
        return "Operation not found", 404
    return render_template("ops_article.html", operation=item)


@operations.get("/ops/<int:operation_id>/baseline")
def baseline_builder(operation_id: int):
    with connect() as db:
        item = operation_view(db, operation_id)
    if not item:
        return "Operation not found", 404
    return render_template("ops_baseline.html", operation=item,
                           required_types=baseline_requirements(item["operation_type"]))


@operations.get("/ops/<int:operation_id>/team")
def team_builder(operation_id: int):
    with connect() as db:
        item = operation_view(db, operation_id)
    if not item: return "Operation not found", 404
    return render_template("ops_team.html", operation=item,
                           roles=role_catalog(item["operation_type"]),
                           required_roles=sorted(required_roles(item["operation_type"])))


@operations.get("/ops/<int:operation_id>/procedure")
def procedure_builder(operation_id: int):
    with connect() as db:
        item = operation_view(db, operation_id)
    if not item: return "Operation not found", 404
    roles = item.get("staffing", {}).get("assignments", []) if item.get("staffing") else []
    return render_template("ops_procedure.html", operation=item, assigned_roles=roles)


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
    routes = {"ARTICLE": f"/ops/{operation_id}/article", "BASELINE": f"/ops/{operation_id}/baseline",
              "TEAM": f"/ops/{operation_id}/team",
              "PROCEDURE": f"/ops/{operation_id}/procedure",
              "EXECUTION": "/workspace", "REVIEW": "/workspace?mode=review"}
    return jsonify(ok=True, stage=item["current_stage"], url=routes.get(item["current_stage"], f"/ops/{operation_id}"))


def article_requirements(operation_type: str) -> set[str]:
    if operation_type == "ROCKET_LAUNCH": return {"PROPULSION", "AVIONICS", "RECOVERY", "LAUNCHER"}
    if operation_type == "STATIC_FIRE": return {"CASE", "NOZZLE", "PROPELLANT_BATCH", "IGNITER"}
    if operation_type == "PRESSURE_TEST": return {"CASE", "PRESSURE_CLOSURE"}
    if operation_type == "RECOVERY_TEST": return {"RECOVERY"}
    if operation_type == "AVIONICS_TEST": return {"AVIONICS", "POWER"}
    return {"PRIMARY_ASSEMBLY"}


def baseline_requirements(operation_type: str) -> set[str]:
    required = {"ARTICLE", "PROCEDURE", "CHANNEL_MAP", "LIMIT_PROFILE",
                "DEVICE_MANIFEST", "CAMERA_MANIFEST", "SOFTWARE"}
    if operation_type == "ROCKET_LAUNCH":
        required |= {"VEHICLE_CONFIGURATION", "RECOVERY_CONFIGURATION"}
    return required


def role_catalog(operation_type: str) -> list[dict]:
    roles = [
        {"code": "TD", "name": "Test Director", "authority": True, "group": "EXECUTION_COMMAND"},
        {"code": "RSO", "name": "Range Safety Officer", "authority": True, "group": "INDEPENDENT_SAFETY"},
        {"code": "LCO", "name": "Launch Control Officer", "authority": True, "group": "FIRE_CONTROL"},
        {"code": "PROP", "name": "Propulsion Lead", "authority": False, "group": "ENGINEERING"},
        {"code": "INST", "name": "Instrumentation Lead", "authority": False, "group": "ENGINEERING"},
        {"code": "GND", "name": "Ground Operations Lead", "authority": False, "group": "FIELD_OPERATIONS"},
        {"code": "DATA", "name": "Data & Video Lead", "authority": False, "group": "DATA_CONTROL"},
    ]
    if operation_type == "ROCKET_LAUNCH":
        roles += [{"code": "LD", "name": "Launch Director", "authority": True, "group": "EXECUTION_COMMAND"},
                  {"code": "REC", "name": "Recovery Lead", "authority": False, "group": "FIELD_OPERATIONS"},
                  {"code": "AVN", "name": "Avionics Lead", "authority": False, "group": "ENGINEERING"}]
    return roles


def required_roles(operation_type: str) -> set[str]:
    roles = {"TD", "RSO", "LCO", "PROP", "INST", "GND", "DATA"}
    if operation_type == "ROCKET_LAUNCH": roles |= {"LD", "REC", "AVN"}
    return roles


PROCEDURE_PHASES = {"SITE", "PREPARATION", "COUNTDOWN", "EXECUTION", "SAFING", "CONTINGENCY"}
PROCEDURE_STEP_TYPES = {"ACTION", "VERIFY", "HOLD_POINT", "POLL", "COMMAND", "CONTINGENCY"}


def validate_procedure_steps(steps: list, assigned_roles: set[str]) -> tuple[list, list[str]]:
    normalized, errors, sequences, codes = [], [], set(), set()
    for index, entry in enumerate(steps, 1):
        try: sequence = int(entry.get("sequence", index))
        except (TypeError, ValueError): sequence = 0
        code = str(entry.get("step_code", "")).strip().upper()
        phase = str(entry.get("phase", "")).strip().upper()
        step_type = str(entry.get("step_type", "ACTION")).strip().upper()
        instruction = str(entry.get("instruction", "")).strip()
        responsible = str(entry.get("responsible_role", "")).strip().upper()
        mode = str(entry.get("verification_mode", "SELF")).strip().upper()
        verifier = str(entry.get("verifier_role", "")).strip().upper() or None
        evidence = str(entry.get("expected_evidence", "")).strip()
        critical = bool(entry.get("safety_critical", False))
        hold = str(entry.get("hold_condition", "")).strip()
        abort = str(entry.get("abort_action", "")).strip()
        if sequence < 1 or sequence in sequences: errors.append(f"step {index} has an invalid or duplicate sequence")
        if not valid_code(code) or code in codes: errors.append(f"step {index} requires a unique controlled step code")
        if phase not in PROCEDURE_PHASES: errors.append(f"{code or index} has an invalid phase")
        if step_type not in PROCEDURE_STEP_TYPES: errors.append(f"{code or index} has an invalid step type")
        if not instruction or not evidence: errors.append(f"{code or index} requires instruction and expected evidence")
        if responsible not in assigned_roles: errors.append(f"{code or index} responsible role is not assigned")
        if mode not in {"SELF", "TWO_PERSON", "AUTOMATED"}: errors.append(f"{code or index} has an invalid verification mode")
        if mode == "TWO_PERSON" and (not verifier or verifier not in assigned_roles or verifier == responsible):
            errors.append(f"{code or index} requires a different assigned verifier")
        if critical and (not abort or mode == "SELF"): errors.append(f"{code or index} safety-critical step requires abort action and independent/automated verification")
        if step_type == "HOLD_POINT" and not hold: errors.append(f"{code or index} hold point requires a release condition")
        sequences.add(sequence); codes.add(code)
        normalized.append((sequence, code, phase, step_type, instruction, responsible, mode, verifier,
                           evidence, int(critical), hold or None, abort or None))
    return normalized, errors


@operations.post("/api/ops/<int:operation_id>/procedure")
def save_procedure(operation_id: int):
    p = request.get_json(silent=True) or {}; steps = p.get("steps", [])
    code = str(p.get("document_code", "")).strip().upper(); revision = str(p.get("revision", "")).strip().upper()
    title = str(p.get("title", "")).strip(); entry = str(p.get("entry_conditions", "")).strip()
    exit_conditions = str(p.get("exit_conditions", "")).strip(); abort_policy = str(p.get("abort_policy", "")).strip()
    if not valid_code(code) or not revision or not title or not entry or not exit_conditions or not abort_policy:
        return jsonify(error="procedure code, revision, title, entry conditions, exit conditions and abort policy are required"), 400
    if not isinstance(steps, list): return jsonify(error="procedure steps must be a list"), 400
    stamp = utc_now()
    with connect() as db:
        operation = db.execute("SELECT * FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
        if not operation: return jsonify(error="operation not found"), 404
        if operation["current_stage"] != "PROCEDURE": return jsonify(error="procedure can only be edited during the PROCEDURE stage"), 409
        staffing = db.execute("SELECT * FROM staffing_plans WHERE operation_id=?", (operation_id,)).fetchone()
        if not staffing or staffing["state"] != "APPROVED": return jsonify(error="an approved staffing plan is required"), 409
        assigned = {x["role_code"] for x in db.execute("SELECT role_code FROM operation_role_assignments WHERE staffing_plan_id=?", (staffing["id"],))}
        normalized, errors = validate_procedure_steps(steps, assigned)
        if errors: return jsonify(error="; ".join(errors)), 400
        existing = db.execute("SELECT * FROM operation_procedures WHERE operation_id=?", (operation_id,)).fetchone()
        if existing and existing["state"] == "APPROVED": return jsonify(error="approved procedures are immutable; create a controlled revision"), 409
        db.execute("""INSERT INTO operation_procedures(operation_id,document_code,revision,title,state,entry_conditions,exit_conditions,
            abort_policy,created_at,updated_at) VALUES(?,?,?,?, 'DRAFT',?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET
            document_code=excluded.document_code,revision=excluded.revision,title=excluded.title,entry_conditions=excluded.entry_conditions,
            exit_conditions=excluded.exit_conditions,abort_policy=excluded.abort_policy,updated_at=excluded.updated_at""",
            (operation_id, code, revision, title, entry, exit_conditions, abort_policy, stamp, stamp))
        procedure_id = db.execute("SELECT id FROM operation_procedures WHERE operation_id=?", (operation_id,)).fetchone()["id"]
        db.execute("DELETE FROM operation_procedure_steps WHERE procedure_id=?", (procedure_id,))
        for row in normalized:
            db.execute("""INSERT INTO operation_procedure_steps(procedure_id,sequence,step_code,phase,step_type,instruction,responsible_role,
                verification_mode,verifier_role,expected_evidence,safety_critical,hold_condition,abort_action,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (procedure_id, *row, stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "PROCEDURE_UPDATED", "TEST DIRECTOR", f"Procedure {code}/{revision} saved with {len(normalized)} controlled steps"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/procedure/approve")
def approve_procedure(operation_id: int):
    actor = str((request.get_json(silent=True) or {}).get("approved_by", "TEST DIRECTOR")).strip() or "TEST DIRECTOR"
    stamp = utc_now()
    with connect() as db:
        operation = db.execute("SELECT * FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
        if not operation: return jsonify(error="operation not found"), 404
        if operation["current_stage"] != "PROCEDURE": return jsonify(error="PROCEDURE is not the active workflow stage"), 409
        procedure = db.execute("SELECT * FROM operation_procedures WHERE operation_id=?", (operation_id,)).fetchone()
        if not procedure: return jsonify(error="save the procedure draft first"), 409
        baseline = db.execute("SELECT id FROM configuration_baselines WHERE operation_id=? AND state='RELEASED'", (operation_id,)).fetchone()
        baseline_ref = db.execute("SELECT reference,revision FROM baseline_items WHERE baseline_id=? AND item_type='PROCEDURE'", (baseline["id"],)).fetchone() if baseline else None
        if not baseline_ref or baseline_ref["reference"].upper() != procedure["document_code"] or baseline_ref["revision"].upper() != procedure["revision"]:
            return jsonify(error="procedure identity does not match the released configuration baseline"), 409
        steps = [dict(x) for x in db.execute("SELECT * FROM operation_procedure_steps WHERE procedure_id=? ORDER BY sequence", (procedure["id"],))]
        phases = {x["phase"] for x in steps}
        missing_phases = sorted({"SITE", "PREPARATION", "COUNTDOWN", "EXECUTION", "SAFING"} - phases)
        if len(steps) < 5 or missing_phases: return jsonify(error="procedure is incomplete; missing operational phases: " + ", ".join(missing_phases)), 409
        if not any(x["step_type"] == "HOLD_POINT" for x in steps): return jsonify(error="procedure requires at least one controlled hold point"), 409
        if not any(x["step_type"] == "CONTINGENCY" or x["phase"] == "CONTINGENCY" for x in steps): return jsonify(error="procedure requires an explicit contingency or abort step"), 409
        canonical = {"schema":"SMTCS-PROCEDURE/1","operation":operation["code"],"document":{"code":procedure["document_code"],"revision":procedure["revision"]},
                     "entry":procedure["entry_conditions"],"exit":procedure["exit_conditions"],"abort":procedure["abort_policy"],
                     "steps":[{k:x[k] for k in ("sequence","step_code","phase","step_type","instruction","responsible_role","verification_mode","verifier_role","expected_evidence","safety_critical","hold_condition","abort_action")} for x in steps]}
        digest = hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        db.execute("UPDATE operation_procedures SET state='APPROVED',canonical_sha256=?,approved_at=?,approved_by=?,updated_at=? WHERE id=?", (digest, stamp, actor, stamp, procedure["id"]))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner=?,updated_at=? WHERE operation_id=? AND section_key='PROCEDURE'", (actor, stamp, operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',updated_at=? WHERE operation_id=? AND section_key='INSTRUMENTATION'", (stamp, operation_id))
        db.execute("UPDATE operation_registry SET current_stage='INSTRUMENTATION',updated_at=? WHERE id=?", (stamp, operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "PROCEDURE_APPROVED", actor, f"Procedure approved with SHA-256 {digest}; Instrumentation unlocked"))
    return jsonify(ok=True,sha256=digest,url=url_for("operations.operation_detail",operation_id=operation_id))


@operations.post("/api/ops/<int:operation_id>/team")
def save_team(operation_id: int):
    p = request.get_json(silent=True) or {}; assignments = p.get("assignments", [])
    if not isinstance(assignments, list): return jsonify(error="assignments must be a list"), 400
    allowed_roles = {x["code"]: x for x in role_catalog("")}
    normalized = []
    for entry in assignments:
        role = str(entry.get("role_code", "")).strip().upper()
        person = str(entry.get("person_name", "")).strip()
        call_sign = str(entry.get("call_sign", "")).strip().upper()
        organization = str(entry.get("organization", "")).strip()
        contact = str(entry.get("contact_method", "")).strip()
        qualification = str(entry.get("qualification_status", "UNVERIFIED")).strip().upper()
        availability = str(entry.get("availability_status", "TENTATIVE")).strip().upper()
        if role and (not person or not call_sign or not organization or not contact):
            return jsonify(error=f"{role} requires person, call sign, organization and contact method"), 400
        if qualification not in {"UNVERIFIED", "CURRENT", "WAIVER_REQUIRED"} or availability not in {"TENTATIVE", "CONFIRMED", "UNAVAILABLE"}:
            return jsonify(error=f"invalid qualification or availability status for {role}"), 400
        if role: normalized.append((role, person, call_sign, organization, contact, qualification, availability,
                                    str(entry.get("notes", "")).strip()))
    stamp = utc_now()
    with connect() as db:
        operation = db.execute("SELECT * FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
        if not operation: return jsonify(error="operation not found"), 404
        if operation["current_stage"] != "TEAM": return jsonify(error="team can only be edited during the TEAM stage"), 409
        allowed_roles = {x["code"]: x for x in role_catalog(operation["operation_type"])}
        unknown = sorted({x[0] for x in normalized} - set(allowed_roles))
        if unknown: return jsonify(error="unsupported roles: " + ", ".join(unknown)), 400
        existing = db.execute("SELECT * FROM staffing_plans WHERE operation_id=?", (operation_id,)).fetchone()
        if existing and existing["state"] == "APPROVED": return jsonify(error="approved staffing plans are locked"), 409
        db.execute("""INSERT INTO staffing_plans(operation_id,state,notes,created_at,updated_at) VALUES(?,'DRAFT',?,?,?)
            ON CONFLICT(operation_id) DO UPDATE SET notes=excluded.notes,updated_at=excluded.updated_at""",
            (operation_id, str(p.get("notes", "")).strip(), stamp, stamp))
        plan_id = db.execute("SELECT id FROM staffing_plans WHERE operation_id=?", (operation_id,)).fetchone()["id"]
        db.execute("DELETE FROM operation_role_assignments WHERE staffing_plan_id=?", (plan_id,))
        for role, person, call_sign, organization, contact, qualification, availability, notes in normalized:
            meta = allowed_roles[role]
            db.execute("""INSERT INTO operation_role_assignments(staffing_plan_id,role_code,person_name,call_sign,organization,
                contact_method,qualification_status,availability_status,decision_authority,conflict_group,notes,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(staffing_plan_id,role_code) DO UPDATE SET person_name=excluded.person_name,
                call_sign=excluded.call_sign,organization=excluded.organization,contact_method=excluded.contact_method,
                qualification_status=excluded.qualification_status,availability_status=excluded.availability_status,
                decision_authority=excluded.decision_authority,conflict_group=excluded.conflict_group,notes=excluded.notes,updated_at=excluded.updated_at""",
                (plan_id, role, person, call_sign, organization, contact, qualification, availability,
                 int(meta["authority"]), meta["group"], notes, stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "TEAM_UPDATED", "TEST DIRECTOR", f"Staffing plan updated with {len(normalized)} assignments"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/team/approve")
def approve_team(operation_id: int):
    actor = str((request.get_json(silent=True) or {}).get("approved_by", "TEST DIRECTOR")).strip()
    stamp = utc_now()
    with connect() as db:
        operation = db.execute("SELECT * FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
        if not operation: return jsonify(error="operation not found"), 404
        if operation["current_stage"] != "TEAM": return jsonify(error="TEAM is not the active workflow stage"), 409
        plan = db.execute("SELECT * FROM staffing_plans WHERE operation_id=?", (operation_id,)).fetchone()
        if not plan: return jsonify(error="save the staffing plan first"), 409
        rows = [dict(x) for x in db.execute("SELECT * FROM operation_role_assignments WHERE staffing_plan_id=?", (plan["id"],))]
        by_role = {x["role_code"]: x for x in rows}
        missing = sorted(required_roles(operation["operation_type"]) - set(by_role))
        if missing: return jsonify(error="mandatory roles are unassigned: " + ", ".join(missing)), 409
        not_ready = sorted(x["role_code"] for x in rows if x["role_code"] in required_roles(operation["operation_type"]) and
                           (x["qualification_status"] != "CURRENT" or x["availability_status"] != "CONFIRMED"))
        if not_ready: return jsonify(error="roles are not qualified and confirmed: " + ", ".join(not_ready)), 409
        people = {}
        for row in rows: people.setdefault(row["person_name"].casefold(), []).append(row)
        conflicts = []
        for assignments in people.values():
            codes = {x["role_code"] for x in assignments}
            if "RSO" in codes and len(codes) > 1: conflicts.append(f"RSO must be independent ({assignments[0]['person_name']})")
            if "TD" in codes and "LCO" in codes: conflicts.append(f"TD and LCO must be separate ({assignments[0]['person_name']})")
            if "LD" in codes and "RSO" in codes: conflicts.append(f"LD and RSO must be separate ({assignments[0]['person_name']})")
        calls = [x["call_sign"] for x in rows]
        if len(calls) != len(set(calls)): conflicts.append("call signs must be unique")
        if conflicts: return jsonify(error="authority conflict: " + "; ".join(conflicts)), 409
        db.execute("UPDATE staffing_plans SET state='APPROVED',approved_at=?,approved_by=?,updated_at=? WHERE id=?", (stamp, actor, stamp, plan["id"]))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner=?,updated_at=? WHERE operation_id=? AND section_key='TEAM'", (actor, stamp, operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',updated_at=? WHERE operation_id=? AND section_key='PROCEDURE'", (stamp, operation_id))
        db.execute("UPDATE operation_registry SET current_stage='PROCEDURE',updated_at=? WHERE id=?", (stamp, operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "TEAM_APPROVED", actor, "Staffing and authority plan approved; Procedure unlocked"))
    return jsonify(ok=True, url=url_for("operations.operation_detail", operation_id=operation_id))


@operations.post("/api/ops/<int:operation_id>/baseline")
def save_baseline(operation_id: int):
    p = request.get_json(silent=True) or {}
    code = str(p.get("baseline_code", "")).strip().upper()
    revision = str(p.get("revision", "")).strip().upper()
    items = p.get("items", [])
    if not valid_code(code) or not revision:
        return jsonify(error="baseline code and revision are required"), 400
    if not isinstance(items, list):
        return jsonify(error="baseline items must be a list"), 400
    normalized = []
    allowed_states = {"DRAFT", "VERIFIED", "APPROVED", "NOT_APPLICABLE"}
    for entry in items:
        item_type = str(entry.get("item_type", "")).strip().upper()
        reference = str(entry.get("reference", "")).strip()
        item_revision = str(entry.get("revision", "")).strip().upper()
        status = str(entry.get("verification_status", "DRAFT")).strip().upper()
        source = str(entry.get("source", "CONTROLLED_RECORD")).strip().upper()
        if not item_type or not reference or not item_revision:
            return jsonify(error="each baseline item requires type, reference and revision"), 400
        if status not in allowed_states:
            return jsonify(error=f"invalid verification status for {item_type}"), 400
        normalized.append((item_type, reference, item_revision, status, source,
                           str(entry.get("notes", "")).strip()))
    stamp = utc_now()
    with connect() as db:
        operation = db.execute("SELECT * FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
        if not operation: return jsonify(error="operation not found"), 404
        if operation["current_stage"] != "BASELINE":
            return jsonify(error="configuration baseline can only be edited during the BASELINE stage"), 409
        article = db.execute("SELECT * FROM test_articles WHERE operation_id=?", (operation_id,)).fetchone()
        if not article or article["state"] != "IDENTIFIED":
            return jsonify(error="an identified test article or vehicle is required"), 409
        existing = db.execute("SELECT * FROM configuration_baselines WHERE operation_id=?", (operation_id,)).fetchone()
        if existing and existing["state"] == "RELEASED":
            return jsonify(error="released baselines are immutable; create a controlled revision instead"), 409
        db.execute("""INSERT INTO configuration_baselines(operation_id,baseline_code,revision,state,article_id,notes,created_at,updated_at)
            VALUES(?,?,?,'DRAFT',?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET baseline_code=excluded.baseline_code,
            revision=excluded.revision,article_id=excluded.article_id,notes=excluded.notes,updated_at=excluded.updated_at""",
            (operation_id, code, revision, article["id"], str(p.get("notes", "")).strip(), stamp, stamp))
        baseline_id = db.execute("SELECT id FROM configuration_baselines WHERE operation_id=?", (operation_id,)).fetchone()["id"]
        normalized = [x for x in normalized if x[0] != "ARTICLE"]
        normalized.append(("ARTICLE", article["serial_number"], article["configuration_revision"],
                           "VERIFIED", "ARTICLE_REGISTRY", article["name"]))
        required_types = baseline_requirements(operation["operation_type"])
        for item_type, reference, item_revision, status, source, notes in normalized:
            db.execute("""INSERT INTO baseline_items(baseline_id,item_type,reference,revision,required,verification_status,source,notes,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(baseline_id,item_type) DO UPDATE SET reference=excluded.reference,
                revision=excluded.revision,required=excluded.required,verification_status=excluded.verification_status,
                source=excluded.source,notes=excluded.notes,updated_at=excluded.updated_at""",
                (baseline_id, item_type, reference, item_revision, int(item_type in required_types), status, source, notes, stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "BASELINE_UPDATED", "CONFIGURATION MANAGER", f"Baseline {code}/{revision} draft saved"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/baseline/release")
def release_baseline(operation_id: int):
    stamp = utc_now()
    with connect() as db:
        operation = db.execute("SELECT * FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
        if not operation: return jsonify(error="operation not found"), 404
        if operation["current_stage"] != "BASELINE": return jsonify(error="BASELINE is not the active workflow stage"), 409
        baseline = db.execute("SELECT * FROM configuration_baselines WHERE operation_id=?", (operation_id,)).fetchone()
        if not baseline: return jsonify(error="save the configuration baseline draft first"), 409
        if baseline["state"] != "DRAFT": return jsonify(error="baseline is already released"), 409
        article = db.execute("SELECT * FROM test_articles WHERE id=?", (baseline["article_id"],)).fetchone()
        items = [dict(x) for x in db.execute("SELECT * FROM baseline_items WHERE baseline_id=? ORDER BY item_type", (baseline["id"],))]
        approved = {x["item_type"] for x in items if x["verification_status"] in {"VERIFIED", "APPROVED"}}
        missing = sorted(baseline_requirements(operation["operation_type"]) - approved)
        if missing: return jsonify(error="required baseline items are missing or unverified: " + ", ".join(missing)), 409
        canonical = {"schema": "SMTCS-BASELINE/1", "operation": {"code": operation["code"], "type": operation["operation_type"]},
                     "article": {"serial": article["serial_number"], "revision": article["configuration_revision"]},
                     "baseline": {"code": baseline["baseline_code"], "revision": baseline["revision"]},
                     "items": [{k: x[k] for k in ("item_type", "reference", "revision", "source")} for x in items]}
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        actor = str((request.get_json(silent=True) or {}).get("released_by", "CONFIGURATION MANAGER")).strip() or "CONFIGURATION MANAGER"
        db.execute("UPDATE configuration_baselines SET state='RELEASED',canonical_sha256=?,released_at=?,released_by=?,updated_at=? WHERE id=?",
                   (digest, stamp, actor, stamp, baseline["id"]))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner=?,updated_at=? WHERE operation_id=? AND section_key='BASELINE'", (actor, stamp, operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',updated_at=? WHERE operation_id=? AND section_key='TEAM'", (stamp, operation_id))
        db.execute("UPDATE operation_registry SET current_stage='TEAM',updated_at=? WHERE id=?", (stamp, operation_id))
        if operation["runtime_operation_id"]:
            active_run = db.execute("SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1", (operation["runtime_operation_id"],)).fetchone()
            if active_run: db.execute("UPDATE test_runs SET configuration_revision=? WHERE id=?", (f"{baseline['baseline_code']}/{baseline['revision']}", active_run["id"]))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "BASELINE_RELEASED", actor, f"Baseline released with SHA-256 {digest}"))
    return jsonify(ok=True, sha256=digest, url=url_for("operations.operation_detail", operation_id=operation_id))


@operations.post("/api/ops/<int:operation_id>/article")
def save_article(operation_id: int):
    p = request.get_json(silent=True) or {}; components = p.get("components", [])
    serial = str(p.get("serial_number", "")).strip().upper(); name = str(p.get("name", "")).strip()
    family = str(p.get("family", "")).strip(); revision = str(p.get("configuration_revision", "")).strip().upper()
    article_class = str(p.get("article_class", "")).strip().upper(); build_status = str(p.get("build_status", "ASSEMBLY")).strip().upper()
    if not serial or not name or not family or not revision or article_class not in {"MOTOR_ASSEMBLY", "FLIGHT_VEHICLE", "TEST_ASSEMBLY"}:
        return jsonify(error="article class, serial, name, family and configuration revision are required"), 400
    if not isinstance(components, list): return jsonify(error="components must be a list"), 400
    normalized_components = []
    for component in components:
        kind = str(component.get("component_type", "")).strip().upper(); identity = str(component.get("serial_or_lot", "")).strip().upper()
        position = str(component.get("position", "PRIMARY")).strip().upper() or "PRIMARY"
        status = str(component.get("status", "ASSIGNED")).strip().upper()
        if not kind or not identity: return jsonify(error="each component requires type and serial/lot identity"), 400
        if status not in {"ASSIGNED", "VERIFIED", "INSTALLED", "AVAILABLE"}: return jsonify(error=f"invalid component status for {kind}"), 400
        normalized_components.append((kind, position, identity, str(component.get("part_number", "")).strip(),
                                      str(component.get("revision", "")).strip().upper(), status, str(component.get("notes", "")).strip()))
    stamp = utc_now()
    with connect() as db:
        operation = db.execute("SELECT operation_type,current_stage FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
        if not operation: return jsonify(error="operation not found"), 404
        if operation["current_stage"] != "ARTICLE": return jsonify(error="test article can only be edited during the ARTICLE stage"), 409
        db.execute("""INSERT INTO test_articles(operation_id,article_class,serial_number,name,family,configuration_revision,build_status,state,notes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'DRAFT',?,?,?) ON CONFLICT(operation_id) DO UPDATE SET article_class=excluded.article_class,
            serial_number=excluded.serial_number,name=excluded.name,family=excluded.family,configuration_revision=excluded.configuration_revision,
            build_status=excluded.build_status,notes=excluded.notes,updated_at=excluded.updated_at""", (operation_id, article_class, serial, name,
            family, revision, build_status, str(p.get("notes", "")).strip(), stamp, stamp))
        for kind, position, identity, part_number, component_revision, status, notes in normalized_components:
            db.execute("""INSERT INTO article_components(operation_id,component_type,position,serial_or_lot,part_number,revision,status,notes,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(operation_id,component_type,position) DO UPDATE SET serial_or_lot=excluded.serial_or_lot,
                part_number=excluded.part_number,revision=excluded.revision,status=excluded.status,notes=excluded.notes,updated_at=excluded.updated_at""",
                (operation_id, kind, position, identity, part_number, component_revision, status, notes, stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "ARTICLE_UPDATED", "CONFIGURATION ENGINEER", f"Article {serial} and {len(normalized_components)} component records saved"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/article/complete")
def complete_article(operation_id: int):
    stamp = utc_now()
    with connect() as db:
        operation = db.execute("SELECT * FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
        if not operation: return jsonify(error="operation not found"), 404
        if operation["current_stage"] != "ARTICLE": return jsonify(error="ARTICLE is not the active workflow stage"), 409
        article = db.execute("SELECT * FROM test_articles WHERE operation_id=?", (operation_id,)).fetchone()
        if not article: return jsonify(error="save the test article or vehicle identity first"), 409
        components = [dict(x) for x in db.execute("SELECT * FROM article_components WHERE operation_id=?", (operation_id,))]
        available = {x["component_type"] for x in components if x["status"] in {"ASSIGNED", "VERIFIED", "INSTALLED", "AVAILABLE"}}
        missing = sorted(article_requirements(operation["operation_type"]) - available)
        if missing: return jsonify(error="required article components are missing: " + ", ".join(missing)), 409
        db.execute("UPDATE test_articles SET state='IDENTIFIED',identified_at=?,updated_at=? WHERE operation_id=?", (stamp, stamp, operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner='CONFIGURATION ENGINEER',updated_at=? WHERE operation_id=? AND section_key='ARTICLE'", (stamp, operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',updated_at=? WHERE operation_id=? AND section_key='BASELINE'", (stamp, operation_id))
        db.execute("UPDATE operation_registry SET current_stage='BASELINE',status='PREPARATION',updated_at=? WHERE id=?", (stamp, operation_id))
        if operation["runtime_operation_id"]:
            active_run = db.execute("SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1", (operation["runtime_operation_id"],)).fetchone()
            if active_run:
                db.execute("UPDATE test_runs SET test_article=?,configuration_revision=? WHERE id=?", (f"{article['name']} / {article['serial_number']}", article["configuration_revision"], active_run["id"]))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "ARTICLE_IDENTIFIED", "CONFIGURATION ENGINEER", f"Article {article['serial_number']} identified; Configuration Baseline unlocked"))
    return jsonify(ok=True, url=url_for("operations.operation_detail", operation_id=operation_id))
