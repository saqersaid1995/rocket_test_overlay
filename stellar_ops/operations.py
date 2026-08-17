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
