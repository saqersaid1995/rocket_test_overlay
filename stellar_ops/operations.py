from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, send_file, url_for

from .control import OPERATION_ID, connect, init_control_db
from .documents import ALLOWED_SCOPES, EXPORT_ROOT, create_package_files, safe_token, scoped_tasks, validate_export

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
CREATE TABLE IF NOT EXISTS configuration_baseline_history(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
 baseline_code TEXT NOT NULL, revision TEXT NOT NULL, canonical_sha256 TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, superseded_reason TEXT NOT NULL,
 superseded_by TEXT NOT NULL, superseded_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
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
CREATE TABLE IF NOT EXISTS instrumentation_plans(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 plan_code TEXT NOT NULL, revision TEXT NOT NULL, state TEXT NOT NULL,
 time_source TEXT NOT NULL, acquisition_mode TEXT NOT NULL, notes TEXT,
 canonical_sha256 TEXT, approved_at TEXT, approved_by TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS measurement_requirements(
 id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL,
 measurement_code TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
 criticality TEXT NOT NULL, device_id TEXT NOT NULL, channel_id TEXT NOT NULL,
 unit TEXT NOT NULL, engineering_min REAL NOT NULL, engineering_max REAL NOT NULL,
 sample_rate_hz INTEGER NOT NULL, required_accuracy TEXT NOT NULL,
 calibration_reference TEXT NOT NULL, calibration_due TEXT NOT NULL,
 warning_limit REAL, critical_limit REAL, abort_limit REAL,
 redundancy TEXT NOT NULL, e2e_status TEXT NOT NULL, required INTEGER NOT NULL,
 notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(plan_id,measurement_code), UNIQUE(plan_id,channel_id),
 FOREIGN KEY(plan_id) REFERENCES instrumentation_plans(id));
CREATE TABLE IF NOT EXISTS video_recording_plans(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 manifest_code TEXT NOT NULL, revision TEXT NOT NULL, state TEXT NOT NULL,
 master_time_source TEXT NOT NULL, recording_window_seconds INTEGER NOT NULL,
 evidence_owner TEXT NOT NULL, notes TEXT, canonical_sha256 TEXT,
 approved_at TEXT, approved_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS camera_view_requirements(
 id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL,
 view_code TEXT NOT NULL, name TEXT NOT NULL, purpose TEXT NOT NULL,
 camera_device_id TEXT NOT NULL, mandatory INTEGER NOT NULL,
 record_mode TEXT NOT NULL, resolution TEXT NOT NULL, fps INTEGER NOT NULL,
 codec TEXT NOT NULL, bitrate_mbps REAL NOT NULL, pre_roll_seconds INTEGER NOT NULL,
 post_roll_seconds INTEGER NOT NULL, time_sync_method TEXT NOT NULL,
 time_sync_status TEXT NOT NULL, signal_test_status TEXT NOT NULL,
 recording_test_status TEXT NOT NULL, primary_storage TEXT NOT NULL,
 backup_storage TEXT NOT NULL, retention_days INTEGER NOT NULL,
 estimated_storage_gb REAL NOT NULL, loss_action TEXT NOT NULL,
 public_safe INTEGER NOT NULL, notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(plan_id,view_code),
 FOREIGN KEY(plan_id) REFERENCES video_recording_plans(id));
CREATE TABLE IF NOT EXISTS readiness_reviews(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 review_code TEXT NOT NULL, review_type TEXT NOT NULL, state TEXT NOT NULL,
 review_chair TEXT NOT NULL, planned_date TEXT NOT NULL, final_decision TEXT NOT NULL,
 decision_rationale TEXT, canonical_sha256 TEXT, approved_at TEXT, approved_by TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS readiness_gates(
 id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL,
 gate_code TEXT NOT NULL, name TEXT NOT NULL, owner_role TEXT NOT NULL,
 required INTEGER NOT NULL, status TEXT NOT NULL, evidence_reference TEXT NOT NULL,
 reviewer TEXT NOT NULL, reviewed_at TEXT, waiver_reason TEXT,
 waiver_authority TEXT, notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(review_id,gate_code),
 FOREIGN KEY(review_id) REFERENCES readiness_reviews(id));
CREATE TABLE IF NOT EXISTS readiness_findings(
 id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL,
 finding_code TEXT NOT NULL, title TEXT NOT NULL, severity TEXT NOT NULL,
 owner TEXT NOT NULL, status TEXT NOT NULL, due_date TEXT NOT NULL,
 disposition TEXT, acceptance_authority TEXT, notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(review_id,finding_code),
 FOREIGN KEY(review_id) REFERENCES readiness_reviews(id));
CREATE TABLE IF NOT EXISTS rehearsal_campaigns(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 rehearsal_code TEXT NOT NULL, rehearsal_type TEXT NOT NULL, source_mode TEXT NOT NULL,
 state TEXT NOT NULL, conductor TEXT NOT NULL, scheduled_at TEXT NOT NULL,
 baseline_sha256 TEXT NOT NULL, procedure_sha256 TEXT NOT NULL,
 result TEXT NOT NULL, summary TEXT, canonical_sha256 TEXT,
 completed_at TEXT, completed_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS rehearsal_checkpoints(
 id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL,
 checkpoint_code TEXT NOT NULL, name TEXT NOT NULL, phase TEXT NOT NULL,
 responsible_role TEXT NOT NULL, objective TEXT NOT NULL, expected_result TEXT NOT NULL,
 critical INTEGER NOT NULL, result TEXT NOT NULL, observed_result TEXT,
 response_time_seconds REAL, evidence_reference TEXT, notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(campaign_id,checkpoint_code),
 FOREIGN KEY(campaign_id) REFERENCES rehearsal_campaigns(id));
CREATE TABLE IF NOT EXISTS rehearsal_anomalies(
 id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL,
 anomaly_code TEXT NOT NULL, title TEXT NOT NULL, severity TEXT NOT NULL,
 owner TEXT NOT NULL, status TEXT NOT NULL, requires_retest INTEGER NOT NULL,
 disposition TEXT, evidence_reference TEXT, notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(campaign_id,anomaly_code),
 FOREIGN KEY(campaign_id) REFERENCES rehearsal_campaigns(id));
CREATE TABLE IF NOT EXISTS execution_releases(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 release_code TEXT NOT NULL, source_mode TEXT NOT NULL, state TEXT NOT NULL,
 planned_start TEXT NOT NULL, valid_until TEXT NOT NULL,
 baseline_sha256 TEXT NOT NULL, procedure_sha256 TEXT NOT NULL,
 readiness_sha256 TEXT NOT NULL, rehearsal_sha256 TEXT NOT NULL,
 release_sha256 TEXT, released_at TEXT, closed_at TEXT,
 outcome TEXT NOT NULL, outcome_summary TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS execution_release_gates(
 id INTEGER PRIMARY KEY AUTOINCREMENT, release_id INTEGER NOT NULL,
 gate_code TEXT NOT NULL, name TEXT NOT NULL, owner_role TEXT NOT NULL,
 status TEXT NOT NULL, evidence_reference TEXT NOT NULL,
 verified_by TEXT NOT NULL, verified_at TEXT, notes TEXT, updated_at TEXT NOT NULL,
 UNIQUE(release_id,gate_code),
 FOREIGN KEY(release_id) REFERENCES execution_releases(id));
CREATE TABLE IF NOT EXISTS execution_authorizations(
 id INTEGER PRIMARY KEY AUTOINCREMENT, release_id INTEGER NOT NULL,
 role_code TEXT NOT NULL, person_name TEXT NOT NULL, decision TEXT NOT NULL,
 attestation TEXT NOT NULL, authorised_at TEXT NOT NULL,
 UNIQUE(release_id,role_code),
 FOREIGN KEY(release_id) REFERENCES execution_releases(id));
CREATE TABLE IF NOT EXISTS post_operation_reviews(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL UNIQUE,
 review_code TEXT NOT NULL, state TEXT NOT NULL, review_chair TEXT NOT NULL,
 review_date TEXT NOT NULL, overall_conclusion TEXT NOT NULL, lessons_learned TEXT NOT NULL,
 evidence_package_reference TEXT NOT NULL, evidence_package_sha256 TEXT NOT NULL,
 closure_sha256 TEXT, closed_at TEXT, closed_by TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS objective_assessments(
 id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL,
 objective_code TEXT NOT NULL, objective_text TEXT NOT NULL, assessment TEXT NOT NULL,
 evidence_reference TEXT NOT NULL, rationale TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(review_id,objective_code), FOREIGN KEY(review_id) REFERENCES post_operation_reviews(id));
CREATE TABLE IF NOT EXISTS closeout_evidence_items(
 id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL,
 item_code TEXT NOT NULL, name TEXT NOT NULL, required INTEGER NOT NULL,
 status TEXT NOT NULL, reference TEXT NOT NULL, sha256 TEXT NOT NULL,
 disposition TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(review_id,item_code), FOREIGN KEY(review_id) REFERENCES post_operation_reviews(id));
CREATE TABLE IF NOT EXISTS corrective_actions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL,
 action_code TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
 severity TEXT NOT NULL, owner TEXT NOT NULL, due_date TEXT NOT NULL,
 status TEXT NOT NULL, closure_evidence TEXT NOT NULL, transfer_reference TEXT NOT NULL,
 notes TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(review_id,action_code), FOREIGN KEY(review_id) REFERENCES post_operation_reviews(id));
CREATE TABLE IF NOT EXISTS departments(
 code TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL,
 lead_role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS personnel(
 id INTEGER PRIMARY KEY AUTOINCREMENT, staff_code TEXT NOT NULL UNIQUE,
 person_name TEXT NOT NULL, department_code TEXT NOT NULL, job_title TEXT NOT NULL,
 email TEXT, phone TEXT, status TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(department_code) REFERENCES departments(code));
CREATE TABLE IF NOT EXISTS task_templates(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_type TEXT NOT NULL,
 task_code TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
 department_code TEXT NOT NULL, responsible_role TEXT NOT NULL,
 accountable_role TEXT NOT NULL, verifier_role TEXT NOT NULL,
 task_type TEXT NOT NULL, phase TEXT NOT NULL,
 start_offset_hours INTEGER NOT NULL, due_offset_hours INTEGER NOT NULL,
 duration_hours REAL NOT NULL, priority TEXT NOT NULL,
 safety_critical INTEGER NOT NULL, required_inputs TEXT NOT NULL,
 acceptance_criteria TEXT NOT NULL, required_evidence TEXT NOT NULL,
 UNIQUE(operation_type,task_code), FOREIGN KEY(department_code) REFERENCES departments(code));
CREATE TABLE IF NOT EXISTS operation_tasks(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
 task_code TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
 department_code TEXT NOT NULL, responsible_role TEXT NOT NULL,
 assigned_person TEXT NOT NULL, accountable_role TEXT NOT NULL,
 verifier_role TEXT NOT NULL, task_type TEXT NOT NULL, phase TEXT NOT NULL,
 planned_start TEXT NOT NULL, due_at TEXT NOT NULL, duration_hours REAL NOT NULL,
 priority TEXT NOT NULL, safety_critical INTEGER NOT NULL,
 required_inputs TEXT NOT NULL, acceptance_criteria TEXT NOT NULL,
 required_evidence TEXT NOT NULL, status TEXT NOT NULL,
 blocker TEXT NOT NULL, source_template TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(operation_id,task_code),
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id),
 FOREIGN KEY(department_code) REFERENCES departments(code));
CREATE TABLE IF NOT EXISTS task_dependencies(
 operation_id INTEGER NOT NULL, predecessor_code TEXT NOT NULL,
 successor_code TEXT NOT NULL, dependency_type TEXT NOT NULL DEFAULT 'FINISH_TO_START',
 PRIMARY KEY(operation_id,predecessor_code,successor_code),
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS operation_milestones(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
 milestone_code TEXT NOT NULL, name TEXT NOT NULL, scheduled_at TEXT NOT NULL,
 owner_role TEXT NOT NULL, status TEXT NOT NULL, notes TEXT NOT NULL,
 UNIQUE(operation_id,milestone_code),
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS task_evidence(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
 task_code TEXT NOT NULL, evidence_code TEXT NOT NULL, evidence_type TEXT NOT NULL,
 title TEXT NOT NULL, reference TEXT NOT NULL, sha256 TEXT NOT NULL,
 supplied_by TEXT NOT NULL, supplied_at TEXT NOT NULL, notes TEXT NOT NULL,
 status TEXT NOT NULL, UNIQUE(operation_id,task_code,evidence_code),
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS task_reviews(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
 task_code TEXT NOT NULL, review_sequence INTEGER NOT NULL,
 reviewer_role TEXT NOT NULL, reviewer_name TEXT NOT NULL,
 decision TEXT NOT NULL, finding TEXT NOT NULL, reviewed_at TEXT NOT NULL,
 UNIQUE(operation_id,task_code,review_sequence),
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS task_status_history(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
 task_code TEXT NOT NULL, previous_status TEXT NOT NULL, new_status TEXT NOT NULL,
 actor TEXT NOT NULL, reason TEXT NOT NULL, changed_at TEXT NOT NULL,
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS task_stakeholders(
 operation_id INTEGER NOT NULL, task_code TEXT NOT NULL,
 role_code TEXT NOT NULL, stakeholder_type TEXT NOT NULL,
 PRIMARY KEY(operation_id,task_code,role_code,stakeholder_type),
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS document_packages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
 package_code TEXT NOT NULL, revision INTEGER NOT NULL, scope_kind TEXT NOT NULL,
 scope_key TEXT NOT NULL, state TEXT NOT NULL, generated_by TEXT NOT NULL,
 generated_at TEXT NOT NULL, released_at TEXT, superseded_at TEXT,
 manifest_sha256 TEXT NOT NULL, validation_json TEXT NOT NULL, notes TEXT NOT NULL,
 UNIQUE(operation_id,package_code,revision),
 FOREIGN KEY(operation_id) REFERENCES operation_registry(id));
CREATE TABLE IF NOT EXISTS generated_documents(
 id INTEGER PRIMARY KEY AUTOINCREMENT, package_id INTEGER NOT NULL,
 document_type TEXT NOT NULL, filename TEXT NOT NULL, storage_path TEXT NOT NULL,
 mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL, sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(package_id,document_type),
 FOREIGN KEY(package_id) REFERENCES document_packages(id));
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


DEPARTMENT_CATALOG = (
    ("OPS", "Operations", "Integrated planning, command and coordination", "TD"),
    ("SAFE", "Safety & Range", "Hazard controls, exclusion zone and stop authority", "RSO"),
    ("PROP", "Propulsion", "Motor configuration, preparation and safing", "PROP"),
    ("INST", "Instrumentation", "Sensors, calibration, acquisition and data quality", "INST"),
    ("GND", "Ground Systems", "Test stand, power, firing circuit and site services", "GND"),
    ("DATA", "Data & Video", "Cameras, recording, time sync and evidence custody", "DATA"),
    ("AVN", "Avionics", "Flight computers, power and telemetry", "AVN"),
    ("REC", "Recovery", "Recovery hardware, team and field response", "REC"),
    ("CFG", "Configuration Management", "Released baselines and controlled changes", "CM"),
)

STATIC_FIRE_TASKS = (
    ("CFG-010", "Freeze test configuration", "Verify article genealogy and release the configuration references used by every downstream team.", "CFG", "CM", "TD", "PROP", "DELIVERABLE", "CONFIGURATION", -240, -168, 4, "CRITICAL", 1, "Article identity; component genealogy", "Released baseline contains every mandatory reference and revision", "Released baseline SHA-256"),
    ("PROP-020", "Complete motor build inspection", "Inspect case, nozzle, propellant grains, seals and igniter interface against the released build record.", "PROP", "PROP", "TD", "RSO", "PREPARATION", "ARTICLE", -168, -120, 3, "CRITICAL", 1, "Released article baseline; build traveller", "All components match serial/lot identity and inspection is accepted", "Signed build inspection and article photographs"),
    ("INST-030", "Install and map instrumentation", "Install pressure, thrust, temperature and ignition-continuity channels and confirm physical-to-canonical mapping.", "INST", "INST", "TD", "PROP", "PREPARATION", "INSTRUMENTATION", -144, -96, 5, "HIGH", 1, "Channel map; device manifest; sensor data sheets", "Every mandatory measurement is mapped to the correct device and channel", "Installation checklist and channel map"),
    ("INST-040", "Validate calibration and end-to-end chain", "Confirm calibration validity, engineering conversion, sample rate, time source and recorded end-to-end response.", "INST", "INST", "TD", "RSO", "VERIFICATION", "INSTRUMENTATION", -96, -72, 4, "CRITICAL", 1, "Installed channels; calibration certificates; limit profile", "All mandatory channels pass E2E test and calibration remains valid through T0", "E2E test record; calibration references; sample trace"),
    ("DATA-050", "Commission cameras and evidence recording", "Frame mandatory views, verify signal, record test, time synchronisation and primary/backup storage.", "DATA", "DATA", "TD", "INST", "PREPARATION", "VIDEO", -96, -60, 4, "HIGH", 0, "Camera manifest; view plan; network allocation", "Mandatory views pass signal, record and sync tests with redundant storage", "Camera test clips; sync check; storage estimate"),
    ("GND-060", "Verify stand and firing circuit", "Inspect test stand restraint, grounding, firing circuit isolation, emergency power removal and local controls.", "GND", "GND", "TD", "RSO", "VERIFICATION", "SITE", -72, -48, 4, "CRITICAL", 1, "Stand drawing; firing schematic; isolation procedure", "Stand is serviceable and firing circuit proves SAFE with hardware power inhibited", "Stand inspection; continuity/isolation test"),
    ("SAFE-070", "Establish site safety controls", "Verify exclusion zone, access control, emergency response, firefighting equipment and stop-work communication.", "SAFE", "RSO", "TD", "GND", "VERIFICATION", "SITE", -72, -36, 3, "CRITICAL", 1, "Site plan; hazard controls; emergency contacts", "Exclusion zone and emergency controls are established and independently verified", "Signed site safety checklist and marked exclusion map"),
    ("OPS-080", "Conduct Test Readiness Review", "Review configuration, staffing, procedure, instrumentation, video, site and safety evidence and disposition findings.", "OPS", "TD", "TD", "RSO", "GATE", "READINESS", -36, -24, 2, "CRITICAL", 1, "Accepted preparation evidence from all departments", "Every mandatory gate is GO and critical findings are closed", "Signed TRR decision and gate matrix"),
    ("OPS-090", "Run integrated dry rehearsal", "Execute the approved sequence in simulation including communications, hold, abort, recording and safing paths.", "OPS", "TD", "TD", "RSO", "REHEARSAL", "REHEARSAL", -24, -12, 3, "CRITICAL", 1, "Approved procedure; approved readiness review", "All mandatory checkpoints pass and no retest anomaly remains open", "Rehearsal record; event log; anomaly disposition"),
    ("OPS-100", "Issue day-of-test handover", "Confirm unchanged configuration, crew presence, evidence recording readiness and current site status before live release.", "OPS", "TD", "TD", "RSO", "MILESTONE", "EXECUTION", -2, -1, 1, "CRITICAL", 1, "Rehearsal record; current configuration fingerprints; crew poll", "TD, RSO and LCO independently authorize handover", "Execution release package"),
)

STATIC_FIRE_DEPENDENCIES = (
    ("CFG-010", "PROP-020"), ("CFG-010", "INST-030"), ("PROP-020", "GND-060"),
    ("INST-030", "INST-040"), ("INST-030", "DATA-050"), ("GND-060", "SAFE-070"),
    ("INST-040", "OPS-080"), ("DATA-050", "OPS-080"), ("SAFE-070", "OPS-080"),
    ("OPS-080", "OPS-090"), ("OPS-090", "OPS-100"),
)

STATIC_FIRE_STAKEHOLDERS = {
    "CFG-010": {"C": ("PROP", "INST", "DATA"), "I": ("RSO", "GND")},
    "PROP-020": {"C": ("CFG", "GND"), "I": ("INST", "DATA")},
    "INST-030": {"C": ("PROP", "DATA"), "I": ("RSO", "GND")},
    "INST-040": {"C": ("PROP", "DATA"), "I": ("RSO", "LCO")},
    "DATA-050": {"C": ("INST", "GND"), "I": ("PROP", "RSO")},
    "GND-060": {"C": ("PROP", "LCO"), "I": ("INST", "DATA")},
    "SAFE-070": {"C": ("GND", "PROP"), "I": ("INST", "DATA", "LCO")},
    "OPS-080": {"C": ("PROP", "INST", "GND", "DATA"), "I": ("LCO",)},
    "OPS-090": {"C": ("RSO", "LCO", "PROP", "INST", "GND", "DATA"), "I": ()},
    "OPS-100": {"C": ("RSO", "LCO"), "I": ("PROP", "INST", "GND", "DATA")},
}


def seed_planning_catalog(db: sqlite3.Connection) -> None:
    for code, name, purpose, lead in DEPARTMENT_CATALOG:
        db.execute("INSERT OR IGNORE INTO departments(code,name,purpose,lead_role,active) VALUES(?,?,?,?,1)", (code, name, purpose, lead))
    for row in STATIC_FIRE_TASKS:
        db.execute("""INSERT OR IGNORE INTO task_templates(operation_type,task_code,title,description,
            department_code,responsible_role,accountable_role,verifier_role,task_type,phase,
            start_offset_hours,due_offset_hours,duration_hours,priority,safety_critical,
            required_inputs,acceptance_criteria,required_evidence) VALUES('STATIC_FIRE',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row)


def parse_t0(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def generate_operation_plan(db: sqlite3.Connection, operation_id: int, replace: bool = False) -> tuple[int, str | None]:
    operation = db.execute("SELECT * FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
    if not operation:
        return 0, "operation not found"
    t0 = parse_t0(operation["planned_start"])
    if not t0:
        return 0, "set PLANNED START in the Operation Brief before generating the preparation plan"
    templates = db.execute("SELECT * FROM task_templates WHERE operation_type=? ORDER BY due_offset_hours", (operation["operation_type"],)).fetchall()
    if not templates:
        return 0, f"no controlled task template exists for {operation['operation_type']}"
    staffing = db.execute("SELECT id FROM staffing_plans WHERE operation_id=?", (operation_id,)).fetchone()
    people = {}
    if staffing:
        people = {x["role_code"]: x["person_name"] for x in db.execute(
            "SELECT role_code,person_name FROM operation_role_assignments WHERE staffing_plan_id=?", (staffing["id"],))}
    if replace:
        db.execute("DELETE FROM task_evidence WHERE operation_id=?", (operation_id,))
        db.execute("DELETE FROM task_reviews WHERE operation_id=?", (operation_id,))
        db.execute("DELETE FROM task_status_history WHERE operation_id=?", (operation_id,))
        db.execute("DELETE FROM task_dependencies WHERE operation_id=?", (operation_id,))
        db.execute("DELETE FROM task_stakeholders WHERE operation_id=?", (operation_id,))
        db.execute("DELETE FROM operation_tasks WHERE operation_id=?", (operation_id,))
        db.execute("DELETE FROM operation_milestones WHERE operation_id=?", (operation_id,))
    stamp = utc_now(); created = 0
    for template in templates:
        start = (t0 + timedelta(hours=template["start_offset_hours"])).isoformat(timespec="minutes")
        due = (t0 + timedelta(hours=template["due_offset_hours"])).isoformat(timespec="minutes")
        assigned = people.get(template["responsible_role"], "UNASSIGNED")
        cursor = db.execute("""INSERT OR IGNORE INTO operation_tasks(operation_id,task_code,title,description,
            department_code,responsible_role,assigned_person,accountable_role,verifier_role,task_type,phase,
            planned_start,due_at,duration_hours,priority,safety_critical,required_inputs,acceptance_criteria,
            required_evidence,status,blocker,source_template,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'NOT_STARTED','','CONTROLLED_TEMPLATE',?)""",
            (operation_id, template["task_code"], template["title"], template["description"], template["department_code"],
             template["responsible_role"], assigned, template["accountable_role"], template["verifier_role"],
             template["task_type"], template["phase"], start, due, template["duration_hours"], template["priority"],
             template["safety_critical"], template["required_inputs"], template["acceptance_criteria"],
             template["required_evidence"], stamp))
        created += cursor.rowcount
    if operation["operation_type"] == "STATIC_FIRE":
        for predecessor, successor in STATIC_FIRE_DEPENDENCIES:
            db.execute("INSERT OR IGNORE INTO task_dependencies(operation_id,predecessor_code,successor_code) VALUES(?,?,?)", (operation_id, predecessor, successor))
        for task_code, groups in STATIC_FIRE_STAKEHOLDERS.items():
            for stakeholder_type, roles in groups.items():
                for role in roles:
                    db.execute("INSERT OR IGNORE INTO task_stakeholders(operation_id,task_code,role_code,stakeholder_type) VALUES(?,?,?,?)",
                               (operation_id, task_code, role, stakeholder_type))
    for code, name, offset, owner in (("TRR", "Test Readiness Review", -24, "TD"), ("DRYRUN", "Integrated Dry Rehearsal", -12, "TD"), ("T0", "Operation Start", 0, "TD")):
        scheduled = (t0 + timedelta(hours=offset)).isoformat(timespec="minutes")
        db.execute("INSERT OR IGNORE INTO operation_milestones(operation_id,milestone_code,name,scheduled_at,owner_role,status,notes) VALUES(?,?,?,?,?,'PLANNED','Generated from T0')", (operation_id, code, name, scheduled, owner))
    return created, None


def seed_demo_planning(db: sqlite3.Connection) -> None:
    demo = db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()
    if not demo:
        return
    generate_operation_plan(db, demo["id"])
    status_map = {"CFG-010":"ACCEPTED", "PROP-020":"ACCEPTED", "INST-030":"IN_PROGRESS", "INST-040":"NOT_STARTED", "DATA-050":"IN_PROGRESS"}
    for code, status in status_map.items():
        db.execute("UPDATE operation_tasks SET status=?,blocker=? WHERE operation_id=? AND task_code=?", (status, "Awaiting final channel installation" if code=="INST-030" else "", demo["id"], code))
    stamp = utc_now()
    demo_evidence = (
        ("CFG-010", "EVD-CFG-01", "CONTROLLED_DOCUMENT", "Released configuration baseline", "DEMO/BASELINE/DEMO-SF-CB-REV-C", "Configuration Manager", "VERIFIED"),
        ("PROP-020", "EVD-PROP-01", "INSPECTION_RECORD", "Motor build inspection", "DEMO/PROP/BUILD-INSPECTION-001", "Maha Al Hinai", "VERIFIED"),
        ("PROP-020", "EVD-PROP-02", "PHOTO_SET", "Article genealogy photographs", "DEMO/PROP/PHOTOSET-001", "Maha Al Hinai", "VERIFIED"),
        ("INST-030", "EVD-INST-01", "CHECKLIST", "Instrumentation installation checklist", "DEMO/INST/INSTALL-DRAFT-001", "Nasser Al Rawahi", "DRAFT"),
    )
    for task_code, evidence_code, evidence_type, title, reference, supplied_by, status in demo_evidence:
        digest = hashlib.sha256(reference.encode()).hexdigest()
        db.execute("""INSERT OR IGNORE INTO task_evidence(operation_id,task_code,evidence_code,evidence_type,title,reference,
            sha256,supplied_by,supplied_at,notes,status) VALUES(?,?,?,?,?,?,?,?,?,'Representative training evidence',?)""",
            (demo["id"], task_code, evidence_code, evidence_type, title, reference, digest, supplied_by, stamp, status))
    for task_code, role, reviewer in (("CFG-010", "PROP", "Maha Al Hinai"), ("PROP-020", "RSO", "Omar Al Balushi")):
        db.execute("""INSERT OR IGNORE INTO task_reviews(operation_id,task_code,review_sequence,reviewer_role,reviewer_name,
            decision,finding,reviewed_at) VALUES(?,?,1,?,?, 'ACCEPTED','Training evidence satisfies the acceptance criteria',?)""",
            (demo["id"], task_code, role, reviewer, stamp))


def seed_training_operation(db: sqlite3.Connection, mission_id: int, stamp: str) -> None:
    """Create an idempotent, clearly labelled end-to-end training record."""
    if db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone():
        return
    fingerprint=lambda label:hashlib.sha256(label.encode()).hexdigest()
    cursor=db.execute("""INSERT INTO operation_registry(mission_id,runtime_operation_id,code,title,operation_type,site,planned_start,objective,success_criteria_json,owner,risk_class,status,current_stage,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(mission_id,OPERATION_ID,"DEMO-SF-001","RNX-71V Training Static Fire","STATIC_FIRE","Al Buraimi Training Stand","2026-09-15T08:00",
        "Demonstrate the complete controlled workflow using representative, non-operational sample data.",json.dumps(["Stable ignition achieved","Chamber pressure remains inside the approved envelope","Telemetry and video evidence package is complete"]),
        "Training Test Director","TRAINING ONLY","TRAINING DEMO","REVIEW",stamp,stamp))
    operation_id=cursor.lastrowid
    for sequence,(key,name) in enumerate(WORKFLOW,1):
        status="ACTIVE" if key=="REVIEW" else "COMPLETE"
        db.execute("INSERT INTO operation_workflow_sections VALUES(?,?,?,?,?,?,?,?)",(operation_id,key,name,sequence,status,"TRAINING TEAM",None,stamp))
    db.execute("INSERT INTO test_articles(operation_id,article_class,serial_number,name,family,configuration_revision,build_status,state,notes,identified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (operation_id,"MOTOR_ASSEMBLY","RNX71V-DEMO-001","RNX-71V Demonstration Motor","RNX-71V","REV-C","INTEGRATED","IDENTIFIED","Training record — not flight or test authority",stamp,stamp,stamp))
    for kind,serial in (("CASE","CASE-DEMO-01"),("NOZZLE","NZL-DEMO-01"),("PROPELLANT_BATCH","RNX-DEMO-BATCH"),("IGNITER","IGN-DEMO-01")):
        db.execute("INSERT INTO article_components(operation_id,component_type,position,serial_or_lot,part_number,revision,status,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (operation_id,kind,"PRIMARY",serial,f"PN-{kind}-01","REV-C","VERIFIED","Representative training genealogy",stamp))
    article_id=db.execute("SELECT id FROM test_articles WHERE operation_id=?",(operation_id,)).fetchone()["id"]
    db.execute("INSERT INTO configuration_baselines(operation_id,baseline_code,revision,state,article_id,notes,canonical_sha256,created_at,updated_at,released_at,released_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (operation_id,"DEMO-SF-CB","REV-C","RELEASED",article_id,"Representative released baseline",fingerprint("demo-baseline"),stamp,stamp,stamp,"Training Configuration Manager"))
    baseline_id=db.execute("SELECT id FROM configuration_baselines WHERE operation_id=?",(operation_id,)).fetchone()["id"]
    for kind in ("ARTICLE","PROCEDURE","CHANNEL_MAP","LIMIT_PROFILE","DEVICE_MANIFEST","CAMERA_MANIFEST","SOFTWARE"):
        db.execute("INSERT INTO baseline_items(baseline_id,item_type,reference,revision,required,verification_status,source,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (baseline_id,kind,f"DEMO/CONFIG/{kind}","REV-C",1,"VERIFIED","TRAINING RECORD","Sample controlled reference",stamp))
    db.execute("INSERT INTO staffing_plans(operation_id,state,approved_at,approved_by,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
               (operation_id,"APPROVED",stamp,"Training Test Director","Representative qualified team",stamp,stamp))
    staffing_id=db.execute("SELECT id FROM staffing_plans WHERE operation_id=?",(operation_id,)).fetchone()["id"]
    roles=(("TD","Aisha Al Harthy","EXECUTION_COMMAND",1),("RSO","Omar Al Balushi","INDEPENDENT_SAFETY",1),("LCO","Salim Al Maawali","FIRE_CONTROL",1),("PROP","Maha Al Hinai","ENGINEERING",0),("INST","Nasser Al Rawahi","ENGINEERING",0),("GND","Khalid Al Riyami","FIELD_OPERATIONS",0),("DATA","Fatma Al Shanfari","DATA_CONTROL",0))
    for role,person,group,authority in roles:
        db.execute("INSERT INTO operation_role_assignments(staffing_plan_id,role_code,person_name,call_sign,organization,contact_method,qualification_status,availability_status,decision_authority,conflict_group,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (staffing_id,role,person,role,"Stellar Kinetics","Operations radio","CURRENT","CONFIRMED",authority,group,"Training identity",stamp))
    db.execute("INSERT INTO operation_procedures(operation_id,document_code,revision,title,state,entry_conditions,exit_conditions,abort_policy,canonical_sha256,approved_at,approved_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (operation_id,"DEMO-SF-PROC","REV-C","RNX-71V Static Fire Procedure","APPROVED","Stand cleared; article installed; permits current","Motor safe; pressure zero; evidence secured","RSO or TD may call ABORT; LCO removes firing power",fingerprint("demo-procedure"),stamp,"Training Test Director",stamp,stamp))
    procedure_id=db.execute("SELECT id FROM operation_procedures WHERE operation_id=?",(operation_id,)).fetchone()["id"]
    steps=((10,"SITE-10","SITE","VERIFY","Verify exclusion zone established","RSO"),(20,"PREP-20","PREPARATION","VERIFY","Confirm article serial and baseline revision","PROP"),(30,"COUNT-30","COUNTDOWN","HOLD_POINT","Conduct final station GO / NO-GO poll","TD"),(40,"FIRE-40","EXECUTION","COMMAND","Issue controlled ignition command","LCO"),(50,"SAFE-50","SAFING","VERIFY","Verify chamber pressure zero and ignition safe","RSO"),(60,"ABORT-60","CONTINGENCY","CONTINGENCY","Execute abort and safe firing circuit","LCO"))
    for seq,code,phase,kind,instruction,role in steps:
        db.execute("INSERT INTO operation_procedure_steps(procedure_id,sequence,step_code,phase,step_type,instruction,responsible_role,verification_mode,verifier_role,expected_evidence,safety_critical,hold_condition,abort_action,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (procedure_id,seq,code,phase,kind,instruction,role,"TWO_PERSON","TD",f"EVENT/{code}",1,"Hold on failed verification","Call ABORT; remove firing power",stamp))
    db.execute("INSERT INTO instrumentation_plans(operation_id,plan_code,revision,state,time_source,acquisition_mode,notes,canonical_sha256,approved_at,approved_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (operation_id,"DEMO-SF-INST","REV-C","APPROVED","TIME-01 / UTC","LIVE_ETHERNET","Representative 1000 Hz measurement chain",fingerprint("demo-instrumentation"),stamp,"Training Instrumentation Lead",stamp,stamp))
    plan_id=db.execute("SELECT id FROM instrumentation_plans WHERE operation_id=?",(operation_id,)).fetchone()["id"]
    measurements=(("CHAMBER_PRESSURE","Chamber Pressure","PRESSURE","PT-01","motor.chamber_pressure","bar",0,80,1000,55,65,70),("THRUST","Motor Thrust","FORCE","LC-01","motor.thrust","N",0,5000,1000,3500,4200,4600),("CASE_TEMPERATURE","Motor Case Temperature","TEMPERATURE","TC-01","motor.case_temperature","degC",-20,250,100,120,160,200),("IGNITION_CONTINUITY","Ignition Continuity","ELECTRICAL","FC-01","ignition.continuity","state",0,1,10,None,None,None))
    for code,name,category,device,channel,unit,minimum,maximum,rate,warning,critical,abort in measurements:
        db.execute("INSERT INTO measurement_requirements(plan_id,measurement_code,name,category,criticality,device_id,channel_id,unit,engineering_min,engineering_max,sample_rate_hz,required_accuracy,calibration_reference,calibration_due,warning_limit,critical_limit,abort_limit,redundancy,e2e_status,required,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (plan_id,code,name,category,"CRITICAL",device,channel,unit,minimum,maximum,rate,"±1% FS",f"CAL-DEMO-{device}","2027-09-01",warning,critical,abort,"RECORDED","PASS",1,"Representative mapped channel",stamp))
    db.execute("INSERT INTO video_recording_plans(operation_id,manifest_code,revision,state,master_time_source,recording_window_seconds,evidence_owner,notes,canonical_sha256,approved_at,approved_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (operation_id,"DEMO-SF-VIDEO","REV-C","APPROVED","TIME-01 / UTC",600,"Training Data Lead","Dual-view evidence plan",fingerprint("demo-video"),stamp,"Training Data Lead",stamp,stamp))
    video_id=db.execute("SELECT id FROM video_recording_plans WHERE operation_id=?",(operation_id,)).fetchone()["id"]
    for code,name,purpose,camera in (("MOTOR_WIDE","Motor Wide","Full stand and article context","CAM-01"),("NOZZLE_CLOSE","Nozzle Close","Ignition and plume evidence","CAM-02")):
        db.execute("INSERT INTO camera_view_requirements(plan_id,view_code,name,purpose,camera_device_id,mandatory,record_mode,resolution,fps,codec,bitrate_mbps,pre_roll_seconds,post_roll_seconds,time_sync_method,time_sync_status,signal_test_status,recording_test_status,primary_storage,backup_storage,retention_days,estimated_storage_gb,loss_action,public_safe,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (video_id,code,name,purpose,camera,1,"ISO","1920x1080",30,"H264",8,30,120,"NTP / UTC","VERIFIED","PASS","PASS","RECORDER-A","NAS-EVIDENCE",365,4.5,"Call HOLD and assess evidence impact",0,"Training camera configuration",stamp))
    db.execute("INSERT INTO readiness_reviews(operation_id,review_code,review_type,state,review_chair,planned_date,final_decision,decision_rationale,canonical_sha256,approved_at,approved_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (operation_id,"DEMO-SF-TRR","TRR","APPROVED","Training Test Director","2026-09-14","GO","All mandatory gates verified with representative evidence",fingerprint("demo-readiness"),stamp,"Training Test Director",stamp,stamp))
    readiness_id=db.execute("SELECT id FROM readiness_reviews WHERE operation_id=?",(operation_id,)).fetchone()["id"]
    for meta in readiness_gate_catalog("STATIC_FIRE"):
        db.execute("INSERT INTO readiness_gates(review_id,gate_code,name,owner_role,required,status,evidence_reference,reviewer,reviewed_at,waiver_reason,waiver_authority,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (readiness_id,meta["code"],meta["name"],meta["owner_role"],1,"GO",f"DEMO/EVIDENCE/{meta['code']}","Training Reviewer",stamp,"","","Representative GO record",stamp))
    db.execute("INSERT INTO readiness_findings(review_id,finding_code,title,severity,owner,status,due_date,disposition,acceptance_authority,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (readiness_id,"RF-DEMO-01","Improve camera slate visibility","LOW","Data Lead","CLOSED","2026-09-14","Slate replaced and recording test repeated","Test Director","Example closed finding",stamp))
    db.execute("INSERT INTO rehearsal_campaigns(operation_id,rehearsal_code,rehearsal_type,source_mode,state,conductor,scheduled_at,baseline_sha256,procedure_sha256,result,summary,canonical_sha256,completed_at,completed_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (operation_id,"DEMO-SF-DRYRUN","DRY_RUN","SIMULATION","COMPLETED","Training Test Director","2026-09-14T15:00",fingerprint("demo-baseline"),fingerprint("demo-procedure"),"PASS","Sequence, hold, abort, telemetry and recording paths passed",fingerprint("demo-rehearsal"),stamp,"Training Test Director",stamp,stamp))
    rehearsal_id=db.execute("SELECT id FROM rehearsal_campaigns WHERE operation_id=?",(operation_id,)).fetchone()["id"]
    for code in sorted(rehearsal_requirements("STATIC_FIRE")):
        db.execute("INSERT INTO rehearsal_checkpoints(campaign_id,checkpoint_code,name,phase,responsible_role,objective,expected_result,critical,result,observed_result,response_time_seconds,evidence_reference,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rehearsal_id,code,code.replace("_"," ").title(),"SEQUENCE","TD",f"Exercise {code.lower()}","Controlled response within procedure",1,"PASS","Expected response observed",2.4,f"DEMO/REHEARSAL/{code}","Training checkpoint",stamp))
    db.execute("INSERT INTO execution_releases(operation_id,release_code,source_mode,state,planned_start,valid_until,baseline_sha256,procedure_sha256,readiness_sha256,rehearsal_sha256,release_sha256,released_at,closed_at,outcome,outcome_summary,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (operation_id,"DEMO-SF-LIVE","LIVE","CLOSED","2026-09-15T08:00","2026-09-15T10:00",fingerprint("demo-baseline"),fingerprint("demo-procedure"),fingerprint("demo-readiness"),fingerprint("demo-rehearsal"),fingerprint("demo-release"),stamp,stamp,"SUCCESS","Stable burn completed; article safed; evidence transferred to review",stamp,stamp))
    release_id=db.execute("SELECT id FROM execution_releases WHERE operation_id=?",(operation_id,)).fetchone()["id"]
    for meta in execution_gate_catalog("STATIC_FIRE"):
        db.execute("INSERT INTO execution_release_gates(release_id,gate_code,name,owner_role,status,evidence_reference,verified_by,verified_at,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (release_id,meta["code"],meta["name"],meta["owner_role"],"GO",f"DEMO/LIVE/{meta['code']}","Training Verifier",stamp,"Representative release gate",stamp))
    for role,person in (("TD","Aisha Al Harthy"),("RSO","Omar Al Balushi"),("LCO","Salim Al Maawali")):
        db.execute("INSERT INTO execution_authorizations(release_id,role_code,person_name,decision,attestation,authorised_at) VALUES(?,?,?,?,?,?)",
                   (release_id,role,person,"GO",f"Training {role} authorisation attestation",stamp))
    db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
               (operation_id,stamp,"TRAINING_RECORD_CREATED","SYSTEM","End-to-end sample operation created for interface and workflow review"))


def init_operations_db() -> None:
    init_control_db()
    stamp = utc_now()
    with connect() as db:
        db.executescript(SCHEMA)
        seed_planning_catalog(db)
        mission = db.execute("SELECT id FROM missions WHERE code='QUALSRM-01'").fetchone()
        if not mission:
            cursor = db.execute("""INSERT INTO missions(code,name,mission_type,objectives,target_date,status,created_at,updated_at)
                VALUES('QUALSRM-01','QualSRM Flight Qualification','QUALIFICATION',?,'2026-09-30','ACTIVE',?,?)""",
                ("Qualify the RNX-71V propulsion system and prepare the integrated sounding rocket for flight.", stamp, stamp))
            mission_id = cursor.lastrowid
        else:
            mission_id = mission["id"]
        seed_training_operation(db, mission_id, stamp)
        seed_demo_planning(db)
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
    item["is_training"] = item["code"].startswith("DEMO-")
    item["success_criteria"] = json.loads(item.pop("success_criteria_json") or "[]")
    item["sections"] = [dict(x) for x in db.execute(
        "SELECT * FROM operation_workflow_sections WHERE operation_id=? ORDER BY sequence", (operation_id,))]
    route_names={"ARTICLE":"article","BASELINE":"baseline","TEAM":"team","PROCEDURE":"procedure","INSTRUMENTATION":"instrumentation","VIDEO":"video","READINESS":"readiness","REHEARSAL":"rehearsal","EXECUTION":"execution","REVIEW":"review"}
    for section in item["sections"]:
        section["url"] = f"/ops/{operation_id}/{route_names[section['section_key']]}" if section["section_key"] in route_names else f"/ops/{operation_id}"
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
    item["baseline_history"] = [dict(x) for x in db.execute(
        "SELECT * FROM configuration_baseline_history WHERE operation_id=? ORDER BY id DESC", (operation_id,))]
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
    instrumentation = db.execute("SELECT * FROM instrumentation_plans WHERE operation_id=?", (operation_id,)).fetchone()
    item["instrumentation"] = dict(instrumentation) if instrumentation else None
    if item["instrumentation"]:
        item["instrumentation"]["measurements"] = [dict(x) for x in db.execute(
            "SELECT * FROM measurement_requirements WHERE plan_id=? ORDER BY criticality DESC,measurement_code", (instrumentation["id"],))]
    video = db.execute("SELECT * FROM video_recording_plans WHERE operation_id=?", (operation_id,)).fetchone()
    item["video_plan"] = dict(video) if video else None
    if item["video_plan"]:
        item["video_plan"]["views"] = [dict(x) for x in db.execute(
            "SELECT * FROM camera_view_requirements WHERE plan_id=? ORDER BY mandatory DESC,view_code", (video["id"],))]
    review = db.execute("SELECT * FROM readiness_reviews WHERE operation_id=?", (operation_id,)).fetchone()
    item["readiness"] = dict(review) if review else None
    if item["readiness"]:
        item["readiness"]["gates"] = [dict(x) for x in db.execute("SELECT * FROM readiness_gates WHERE review_id=? ORDER BY gate_code", (review["id"],))]
        item["readiness"]["findings"] = [dict(x) for x in db.execute("SELECT * FROM readiness_findings WHERE review_id=? ORDER BY severity,finding_code", (review["id"],))]
    rehearsal = db.execute("SELECT * FROM rehearsal_campaigns WHERE operation_id=?", (operation_id,)).fetchone()
    item["rehearsal"] = dict(rehearsal) if rehearsal else None
    if item["rehearsal"]:
        item["rehearsal"]["checkpoints"] = [dict(x) for x in db.execute("SELECT * FROM rehearsal_checkpoints WHERE campaign_id=? ORDER BY checkpoint_code", (rehearsal["id"],))]
        item["rehearsal"]["anomalies"] = [dict(x) for x in db.execute("SELECT * FROM rehearsal_anomalies WHERE campaign_id=? ORDER BY severity,anomaly_code", (rehearsal["id"],))]
    release = db.execute("SELECT * FROM execution_releases WHERE operation_id=?", (operation_id,)).fetchone()
    item["execution_release"] = dict(release) if release else None
    if item["execution_release"]:
        item["execution_release"]["gates"] = [dict(x) for x in db.execute("SELECT * FROM execution_release_gates WHERE release_id=? ORDER BY gate_code", (release["id"],))]
        item["execution_release"]["authorizations"] = [dict(x) for x in db.execute("SELECT * FROM execution_authorizations WHERE release_id=? ORDER BY role_code", (release["id"],))]
    post_review = db.execute("SELECT * FROM post_operation_reviews WHERE operation_id=?", (operation_id,)).fetchone()
    item["post_review"] = dict(post_review) if post_review else None
    if item["post_review"]:
        review_id = post_review["id"]
        item["post_review"]["objectives"] = [dict(x) for x in db.execute("SELECT * FROM objective_assessments WHERE review_id=? ORDER BY objective_code", (review_id,))]
        item["post_review"]["evidence_items"] = [dict(x) for x in db.execute("SELECT * FROM closeout_evidence_items WHERE review_id=? ORDER BY item_code", (review_id,))]
        item["post_review"]["corrective_actions"] = [dict(x) for x in db.execute("SELECT * FROM corrective_actions WHERE review_id=? ORDER BY severity DESC,action_code", (review_id,))]
    item["planning_tasks"] = [dict(x) for x in db.execute("""SELECT t.*,d.name department_name
        FROM operation_tasks t JOIN departments d ON d.code=t.department_code
        WHERE t.operation_id=? ORDER BY t.due_at,t.task_code""", (operation_id,))]
    for task in item["planning_tasks"]:
        task["evidence"] = [dict(x) for x in db.execute(
            "SELECT * FROM task_evidence WHERE operation_id=? AND task_code=? ORDER BY supplied_at,evidence_code",
            (operation_id, task["task_code"]))]
        task["reviews"] = [dict(x) for x in db.execute(
            "SELECT * FROM task_reviews WHERE operation_id=? AND task_code=? ORDER BY review_sequence",
            (operation_id, task["task_code"]))]
        task["verified_evidence_count"] = sum(1 for x in task["evidence"] if x["status"] == "VERIFIED")
        stakeholders = [dict(x) for x in db.execute(
            "SELECT role_code,stakeholder_type FROM task_stakeholders WHERE operation_id=? AND task_code=? ORDER BY stakeholder_type,role_code",
            (operation_id, task["task_code"]))]
        task["consulted_roles"] = [x["role_code"] for x in stakeholders if x["stakeholder_type"] == "C"]
        task["informed_roles"] = [x["role_code"] for x in stakeholders if x["stakeholder_type"] == "I"]
    item["planning_milestones"] = [dict(x) for x in db.execute(
        "SELECT * FROM operation_milestones WHERE operation_id=? ORDER BY scheduled_at", (operation_id,))]
    task_total = len(item["planning_tasks"])
    task_accepted = sum(1 for x in item["planning_tasks"] if x["status"] == "ACCEPTED")
    item["planning_progress"] = round(task_accepted / task_total * 100) if task_total else 0
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
        procedure_identity = next((x for x in (item.get("baseline", {}).get("items", []) if item and item.get("baseline") else []) if x["item_type"] == "PROCEDURE"), None)
    if not item: return "Operation not found", 404
    roles = item.get("staffing", {}).get("assignments", []) if item.get("staffing") else []
    return render_template("ops_procedure.html", operation=item, assigned_roles=roles, procedure_identity=procedure_identity)


@operations.get("/ops/<int:operation_id>/instrumentation")
def instrumentation_builder(operation_id: int):
    with connect() as db:
        item = operation_view(db, operation_id)
        devices = [dict(x) for x in db.execute("SELECT * FROM devices WHERE operation_id=? ORDER BY id", (item["runtime_operation_id"] or OPERATION_ID,))] if item else []
        channels = [dict(x) for x in db.execute("SELECT c.*,COALESCE(l.enabled,1) enabled FROM channels c LEFT JOIN channel_lifecycle l ON l.operation_id=c.operation_id AND l.channel_id=c.id WHERE c.operation_id=? ORDER BY c.id", (item["runtime_operation_id"] or OPERATION_ID,))] if item else []
    if not item: return "Operation not found", 404
    return render_template("ops_instrumentation.html", operation=item, devices=devices, channels=channels,
                           required_measurements=sorted(instrumentation_requirements(item["operation_type"])))


@operations.get("/ops/<int:operation_id>/video")
def video_plan_builder(operation_id: int):
    with connect() as db:
        item = operation_view(db, operation_id)
        cameras = [dict(x) for x in db.execute("SELECT * FROM devices WHERE operation_id=? AND device_type='IP-CAMERA' ORDER BY id", (item["runtime_operation_id"] or OPERATION_ID,))] if item else []
    if not item: return "Operation not found", 404
    return render_template("ops_video_plan.html", operation=item, cameras=cameras,
                           required_views=sorted(video_view_requirements(item["operation_type"])))


@operations.get("/ops/<int:operation_id>/readiness")
def readiness_builder(operation_id:int):
    with connect() as db:item=operation_view(db,operation_id)
    if not item:return "Operation not found",404
    return render_template("ops_readiness.html",operation=item,gates=readiness_gate_catalog(item["operation_type"]))


@operations.get("/ops/<int:operation_id>/rehearsal")
def rehearsal_builder(operation_id:int):
    with connect() as db:item=operation_view(db,operation_id)
    if not item:return "Operation not found",404
    return render_template("ops_rehearsal.html",operation=item,required_checkpoints=sorted(rehearsal_requirements(item["operation_type"])))


@operations.get("/ops/<int:operation_id>/execution")
def execution_builder(operation_id:int):
    with connect() as db:
        item=operation_view(db,operation_id)
        runtime=dict(db.execute("SELECT * FROM operations WHERE id=?",(item["runtime_operation_id"] or OPERATION_ID,)).fetchone()) if item else None
    if not item:return "Operation not found",404
    return render_template("ops_execution.html",operation=item,runtime=runtime,gates=execution_gate_catalog(item["operation_type"]))


@operations.get("/ops/<int:operation_id>/review")
def post_operation_review_builder(operation_id:int):
    with connect() as db:item=operation_view(db,operation_id)
    if not item:return "Operation not found",404
    return render_template("ops_review.html",operation=item,evidence_catalog=closeout_evidence_catalog(item["operation_type"]))


@operations.get("/ops/<int:operation_id>/planning")
def planning_workspace(operation_id: int):
    with connect() as db:
        item = operation_view(db, operation_id)
        departments = [dict(x) for x in db.execute("SELECT * FROM departments WHERE active=1 ORDER BY name")]
        dependencies = [dict(x) for x in db.execute(
            "SELECT * FROM task_dependencies WHERE operation_id=? ORDER BY successor_code,predecessor_code", (operation_id,))]
    if not item:
        return "Operation not found", 404
    dep_map: dict[str, list[str]] = {}
    for dependency in dependencies:
        dep_map.setdefault(dependency["successor_code"], []).append(dependency["predecessor_code"])
    now = datetime.now(timezone.utc)
    for task in item["planning_tasks"]:
        due = parse_t0(task["due_at"])
        task["is_overdue"] = bool(due and due < now and task["status"] not in {"ACCEPTED", "CANCELLED"})
        task["dependencies"] = dep_map.get(task["task_code"], [])
        task["t_minus"] = round((due - parse_t0(item["planned_start"])).total_seconds() / 3600) if due and parse_t0(item["planned_start"]) else None
    return render_template("ops_planning.html", operation=item, departments=departments)


def package_context(db: sqlite3.Connection, operation_id: int) -> tuple[dict | None, list[dict], list[dict]]:
    item = operation_view(db, operation_id)
    if not item:
        return None, [], []
    departments = [dict(x) for x in db.execute("SELECT * FROM departments WHERE active=1 ORDER BY name")]
    assignments = item.get("staffing", {}).get("assignments", []) if item.get("staffing") else []
    return item, departments, assignments


@operations.get("/ops/<int:operation_id>/work-packages")
def work_package_center(operation_id: int):
    with connect() as db:
        item, departments, assignments = package_context(db, operation_id)
    if not item:
        return "Operation not found", 404
    department_packages = []
    for department in departments:
        tasks = [x for x in item["planning_tasks"] if x["department_code"] == department["code"]]
        if tasks:
            department_packages.append({**department, "tasks": tasks, "accepted": sum(x["status"] == "ACCEPTED" for x in tasks),
                                        "blocked": sum(x["status"] == "BLOCKED" for x in tasks)})
    people = []
    for assignment in assignments:
        tasks = [x for x in item["planning_tasks"] if x["responsible_role"] == assignment["role_code"]]
        if tasks:
            people.append({**assignment, "tasks": tasks, "accepted": sum(x["status"] == "ACCEPTED" for x in tasks)})
    return render_template("ops_work_packages.html", operation=item, scope_kind="OVERVIEW",
                           department_packages=department_packages, people=people, scoped_tasks=item["planning_tasks"])


@operations.get("/ops/<int:operation_id>/work-packages/department/<department_code>")
def department_work_package(operation_id: int, department_code: str):
    with connect() as db:
        item, departments, assignments = package_context(db, operation_id)
    if not item:
        return "Operation not found", 404
    department = next((x for x in departments if x["code"] == department_code.upper()), None)
    if not department:
        return "Department not found", 404
    tasks = [x for x in item["planning_tasks"] if x["department_code"] == department["code"]]
    members = [x for x in assignments if any(t["responsible_role"] == x["role_code"] for t in tasks)]
    return render_template("ops_work_packages.html", operation=item, scope_kind="DEPARTMENT", scope=department,
                           scoped_tasks=tasks, members=members, department_packages=[], people=[])


@operations.get("/ops/<int:operation_id>/work-packages/person/<role_code>")
def person_work_package(operation_id: int, role_code: str):
    with connect() as db:
        item, departments, assignments = package_context(db, operation_id)
    if not item:
        return "Operation not found", 404
    assignment = next((x for x in assignments if x["role_code"] == role_code.upper()), None)
    if not assignment:
        return "Assigned role not found", 404
    tasks = [x for x in item["planning_tasks"] if x["responsible_role"] == assignment["role_code"]]
    reviews = [x for x in item["planning_tasks"] if x["verifier_role"] == assignment["role_code"] and x["responsible_role"] != assignment["role_code"]]
    return render_template("ops_work_packages.html", operation=item, scope_kind="PERSON", scope=assignment,
                           scoped_tasks=tasks, verification_queue=reviews, department_packages=[], people=[])


@operations.get("/ops/<int:operation_id>/documents")
def document_export_center(operation_id: int):
    with connect() as db:
        item, departments, assignments = package_context(db, operation_id)
        packages = [dict(x) for x in db.execute(
            "SELECT * FROM document_packages WHERE operation_id=? ORDER BY id DESC", (operation_id,))]
        for package in packages:
            package["documents"] = [dict(x) for x in db.execute(
                "SELECT * FROM generated_documents WHERE package_id=? ORDER BY document_type", (package["id"],))]
            package["validation"] = json.loads(package.pop("validation_json") or "{}")
    if not item:
        return "Operation not found", 404
    validation = validate_export(item)
    release_validation = validate_export(item, release=True)
    return render_template("ops_documents.html", operation=item, departments=departments,
                           assignments=assignments, packages=packages, validation=validation,
                           release_validation=release_validation)


@operations.post("/api/ops/<int:operation_id>/documents/generate")
def generate_document_package(operation_id: int):
    payload = request.get_json(silent=True) or {}
    scope_kind = str(payload.get("scope_kind", "MASTER")).strip().upper()
    scope_key = str(payload.get("scope_key", "ALL")).strip().upper() or "ALL"
    state = str(payload.get("state", "DRAFT")).strip().upper()
    actor = str(payload.get("generated_by", "DOCUMENT CONTROL")).strip() or "DOCUMENT CONTROL"
    notes = str(payload.get("notes", "")).strip()
    if scope_kind not in ALLOWED_SCOPES or state not in {"DRAFT", "RELEASED"}:
        return jsonify(error="scope and document state are invalid"), 400
    with connect() as db:
        item = operation_view(db, operation_id)
        if not item:
            return jsonify(error="operation not found"), 404
        if scope_kind == "DEPARTMENT" and not db.execute("SELECT 1 FROM departments WHERE code=? AND active=1", (scope_key,)).fetchone():
            return jsonify(error="department scope was not found"), 404
        assignment = None
        if scope_kind == "PERSON":
            assignment = next((x for x in ((item.get("staffing") or {}).get("assignments", [])) if x["role_code"] == scope_key), None)
            if not assignment:
                return jsonify(error="person scope requires an assigned operation role"), 404
        tasks = scoped_tasks(item, scope_kind, scope_key)
        if not tasks:
            return jsonify(error="selected scope has no controlled tasks"), 409
        validation = validate_export(item, release=state == "RELEASED")
        if state == "RELEASED" and validation["blockers"]:
            return jsonify(error="released export is blocked", blockers=validation["blockers"]), 409
        prefix = {"MASTER":"MASTER", "DEPARTMENT":f"DEPT-{scope_key}", "PERSON":f"PERSON-{scope_key}"}[scope_kind]
        package_code = f"{item['code']}-{prefix}-WP"
        latest = db.execute("SELECT MAX(revision) revision FROM document_packages WHERE operation_id=? AND package_code=?", (operation_id, package_code)).fetchone()
        revision = int(latest["revision"] or 0) + 1
        if state == "RELEASED":
            stamp = utc_now()
            db.execute("UPDATE document_packages SET state='SUPERSEDED',superseded_at=? WHERE operation_id=? AND package_code=? AND state='RELEASED'", (stamp, operation_id, package_code))
        else:
            stamp = utc_now()
        scope_label = "MASTER OPERATION"
        if scope_kind == "DEPARTMENT":
            scope_label = db.execute("SELECT name FROM departments WHERE code=?", (scope_key,)).fetchone()["name"]
        elif assignment:
            scope_label = f"{assignment['person_name']} / {assignment['role_code']}"
        metadata = {"package_code":package_code,"revision":revision,"state":state,"scope_kind":scope_kind,
                    "scope_key":scope_key,"scope_label":scope_label,"generated_at":stamp,"generated_by":actor}
        directory = EXPORT_ROOT / safe_token(item["code"]) / safe_token(package_code) / f"R{revision}"
        try:
            files = create_package_files(directory, item, tasks, metadata)
        except ImportError as exc:
            return jsonify(error=f"document dependency is missing: {exc.name}; install project requirements and retry"), 503
        manifest_sha = hashlib.sha256(json.dumps({"metadata":metadata,"files":[x["sha256"] for x in files]},sort_keys=True).encode()).hexdigest()
        cursor = db.execute("""INSERT INTO document_packages(operation_id,package_code,revision,scope_kind,scope_key,state,generated_by,generated_at,released_at,superseded_at,manifest_sha256,validation_json,notes)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (operation_id,package_code,revision,scope_kind,scope_key,state,actor,stamp,stamp if state=="RELEASED" else None,None,manifest_sha,json.dumps(validation),notes))
        package_id = cursor.lastrowid
        for file in files:
            db.execute("""INSERT INTO generated_documents(package_id,document_type,filename,storage_path,mime_type,byte_size,sha256,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (package_id,file["document_type"],file["filename"],file["storage_path"],file["mime_type"],file["byte_size"],file["sha256"],stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id,stamp,"DOCUMENT_PACKAGE_GENERATED",actor,f"{state} {scope_label} package {package_code} revision {revision} generated"))
    return jsonify(ok=True, package_id=package_id, url=url_for("operations.document_export_center", operation_id=operation_id))


@operations.get("/ops/<int:operation_id>/documents/files/<int:document_id>")
def download_generated_document(operation_id: int, document_id: int):
    with connect() as db:
        document = db.execute("""SELECT d.* FROM generated_documents d JOIN document_packages p ON p.id=d.package_id
            WHERE d.id=? AND p.operation_id=?""", (document_id, operation_id)).fetchone()
    if not document:
        return "Document not found", 404
    path = Path(document["storage_path"]).resolve()
    root = EXPORT_ROOT.resolve()
    if root not in path.parents or not path.is_file():
        return "Controlled document file is unavailable", 404
    return send_file(path, mimetype=document["mime_type"], as_attachment=True, download_name=document["filename"])


@operations.post("/api/ops/<int:operation_id>/planning/generate")
def generate_planning_workspace(operation_id: int):
    p = request.get_json(silent=True) or {}
    with connect() as db:
        created, error = generate_operation_plan(db, operation_id, bool(p.get("replace")))
        if error:
            return jsonify(error=error), 409
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, utc_now(), "PLAN_GENERATED", "PLANNING CONTROL", f"Preparation plan generated; {created} controlled tasks created"))
    return jsonify(ok=True, created=created, url=url_for("operations.planning_workspace", operation_id=operation_id))


@operations.post("/api/ops/<int:operation_id>/planning/tasks/<task_code>")
def update_planning_task(operation_id: int, task_code: str):
    p = request.get_json(silent=True) or {}
    status = str(p.get("status", "")).strip().upper()
    assigned = str(p.get("assigned_person", "")).strip()
    blocker = str(p.get("blocker", "")).strip()
    if status not in {"NOT_STARTED", "IN_PROGRESS", "BLOCKED", "READY_FOR_REVIEW", "ACCEPTED", "CANCELLED"}:
        return jsonify(error="select a valid controlled task status"), 400
    if not assigned:
        return jsonify(error="an assigned person is required"), 400
    if status == "BLOCKED" and not blocker:
        return jsonify(error="a blocked task requires a clear blocker description"), 400
    stamp = utc_now()
    with connect() as db:
        task = db.execute("SELECT * FROM operation_tasks WHERE operation_id=? AND task_code=?", (operation_id, task_code)).fetchone()
        if not task:
            return jsonify(error="planning task not found"), 404
        assigned_identity = db.execute("""SELECT a.person_name FROM operation_role_assignments a
            JOIN staffing_plans s ON s.id=a.staffing_plan_id WHERE s.operation_id=? AND a.role_code=?""",
            (operation_id, task["responsible_role"])).fetchone()
        if assigned_identity and assigned.casefold() != assigned_identity["person_name"].casefold():
            return jsonify(error=f"{task['responsible_role']} is assigned to {assigned_identity['person_name']} in Team & Authority"), 409
        if status == "ACCEPTED":
            return jsonify(error="ACCEPTED is issued only through the independent task review control"), 409
        dependencies = [x["predecessor_code"] for x in db.execute(
            "SELECT predecessor_code FROM task_dependencies WHERE operation_id=? AND successor_code=?", (operation_id, task_code))]
        if status in {"READY_FOR_REVIEW", "ACCEPTED"} and dependencies:
            marks = ",".join("?" for _ in dependencies)
            incomplete = [x["task_code"] for x in db.execute(
                f"SELECT task_code FROM operation_tasks WHERE operation_id=? AND task_code IN ({marks}) AND status!='ACCEPTED'", (operation_id, *dependencies))]
            if incomplete:
                return jsonify(error="predecessor tasks must be accepted first: " + ", ".join(incomplete)), 409
        evidence = [dict(x) for x in db.execute("SELECT * FROM task_evidence WHERE operation_id=? AND task_code=?", (operation_id, task_code))]
        if status == "READY_FOR_REVIEW" and not any(x["status"] == "VERIFIED" for x in evidence):
            return jsonify(error="attach and verify the required task evidence before requesting review"), 409
        db.execute("UPDATE operation_tasks SET assigned_person=?,status=?,blocker=?,updated_at=? WHERE operation_id=? AND task_code=?",
                   (assigned, status, blocker if status == "BLOCKED" else "", stamp, operation_id, task_code))
        db.execute("INSERT INTO task_status_history(operation_id,task_code,previous_status,new_status,actor,reason,changed_at) VALUES(?,?,?,?,?,?,?)",
                   (operation_id, task_code, task["status"], status, assigned, blocker, stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "TASK_UPDATED", assigned, f"{task_code} changed to {status}"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/planning/tasks/<task_code>/evidence")
def save_task_evidence(operation_id: int, task_code: str):
    p = request.get_json(silent=True) or {}
    code = str(p.get("evidence_code", "")).strip().upper()
    kind = str(p.get("evidence_type", "")).strip().upper()
    title = str(p.get("title", "")).strip(); reference = str(p.get("reference", "")).strip()
    digest = str(p.get("sha256", "")).strip().lower(); supplied_by = str(p.get("supplied_by", "")).strip()
    notes = str(p.get("notes", "")).strip(); status = str(p.get("status", "DRAFT")).strip().upper()
    allowed_types = {"CONTROLLED_DOCUMENT", "CHECKLIST", "INSPECTION_RECORD", "PHOTO_SET", "DATA_FILE", "VIDEO", "CERTIFICATE", "EVENT_LOG"}
    if not valid_code(code) or kind not in allowed_types or not title or not reference or not supplied_by or status not in {"DRAFT", "VERIFIED", "REJECTED"}:
        return jsonify(error="evidence code, supported type, title, reference, supplier and status are required"), 400
    if status == "VERIFIED" and not valid_sha256(digest):
        return jsonify(error="verified evidence requires its complete 64-character SHA-256"), 400
    stamp = utc_now()
    with connect() as db:
        task = db.execute("SELECT * FROM operation_tasks WHERE operation_id=? AND task_code=?", (operation_id, task_code)).fetchone()
        if not task:
            return jsonify(error="planning task not found"), 404
        if task["assigned_person"] != "UNASSIGNED" and supplied_by.casefold() != task["assigned_person"].casefold():
            return jsonify(error=f"task evidence must be supplied by the assigned performer: {task['assigned_person']}"), 409
        db.execute("""INSERT INTO task_evidence(operation_id,task_code,evidence_code,evidence_type,title,reference,sha256,
            supplied_by,supplied_at,notes,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(operation_id,task_code,evidence_code) DO UPDATE SET evidence_type=excluded.evidence_type,
            title=excluded.title,reference=excluded.reference,sha256=excluded.sha256,supplied_by=excluded.supplied_by,
            supplied_at=excluded.supplied_at,notes=excluded.notes,status=excluded.status""",
            (operation_id, task_code, code, kind, title, reference, digest, supplied_by, stamp, notes, status))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "TASK_EVIDENCE_UPDATED", supplied_by, f"{code} attached to {task_code} as {status}"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/planning/tasks/<task_code>/review")
def review_task(operation_id: int, task_code: str):
    p = request.get_json(silent=True) or {}
    reviewer_role = str(p.get("reviewer_role", "")).strip().upper()
    reviewer_name = str(p.get("reviewer_name", "")).strip()
    decision = str(p.get("decision", "")).strip().upper(); finding = str(p.get("finding", "")).strip()
    if not reviewer_role or not reviewer_name or decision not in {"ACCEPTED", "REJECTED"}:
        return jsonify(error="reviewer role, reviewer identity and ACCEPTED or REJECTED decision are required"), 400
    if decision == "REJECTED" and not finding:
        return jsonify(error="a rejected task requires a specific review finding"), 400
    stamp = utc_now()
    with connect() as db:
        task = db.execute("SELECT * FROM operation_tasks WHERE operation_id=? AND task_code=?", (operation_id, task_code)).fetchone()
        if not task:
            return jsonify(error="planning task not found"), 404
        if reviewer_role != task["verifier_role"]:
            return jsonify(error=f"{task_code} requires review by role {task['verifier_role']}"), 409
        approved_reviewer = db.execute("""SELECT a.person_name FROM operation_role_assignments a
            JOIN staffing_plans s ON s.id=a.staffing_plan_id WHERE s.operation_id=? AND a.role_code=?""",
            (operation_id, reviewer_role)).fetchone()
        if approved_reviewer and reviewer_name.casefold() != approved_reviewer["person_name"].casefold():
            return jsonify(error=f"{reviewer_role} review authority is assigned to {approved_reviewer['person_name']}"), 409
        if reviewer_name.casefold() == task["assigned_person"].casefold():
            return jsonify(error="the task performer cannot independently accept their own work"), 409
        evidence = [dict(x) for x in db.execute("SELECT * FROM task_evidence WHERE operation_id=? AND task_code=?", (operation_id, task_code))]
        if decision == "ACCEPTED" and not any(x["status"] == "VERIFIED" and valid_sha256(x["sha256"]) for x in evidence):
            return jsonify(error="acceptance requires at least one VERIFIED evidence item with SHA-256 integrity"), 409
        dependencies = [x["predecessor_code"] for x in db.execute(
            "SELECT predecessor_code FROM task_dependencies WHERE operation_id=? AND successor_code=?", (operation_id, task_code))]
        if decision == "ACCEPTED" and dependencies:
            marks = ",".join("?" for _ in dependencies)
            incomplete = [x["task_code"] for x in db.execute(
                f"SELECT task_code FROM operation_tasks WHERE operation_id=? AND task_code IN ({marks}) AND status!='ACCEPTED'", (operation_id, *dependencies))]
            if incomplete:
                return jsonify(error="predecessor tasks must be accepted first: " + ", ".join(incomplete)), 409
        sequence = db.execute("SELECT COALESCE(MAX(review_sequence),0)+1 n FROM task_reviews WHERE operation_id=? AND task_code=?", (operation_id, task_code)).fetchone()["n"]
        db.execute("INSERT INTO task_reviews(operation_id,task_code,review_sequence,reviewer_role,reviewer_name,decision,finding,reviewed_at) VALUES(?,?,?,?,?,?,?,?)",
                   (operation_id, task_code, sequence, reviewer_role, reviewer_name, decision, finding, stamp))
        new_status = "ACCEPTED" if decision == "ACCEPTED" else "BLOCKED"
        db.execute("UPDATE operation_tasks SET status=?,blocker=?,updated_at=? WHERE operation_id=? AND task_code=?",
                   (new_status, finding if decision == "REJECTED" else "", stamp, operation_id, task_code))
        db.execute("INSERT INTO task_status_history(operation_id,task_code,previous_status,new_status,actor,reason,changed_at) VALUES(?,?,?,?,?,?,?)",
                   (operation_id, task_code, task["status"], new_status, reviewer_name, finding or "Acceptance criteria and evidence verified", stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id, stamp, "TASK_REVIEWED", reviewer_name, f"{task_code} review decision: {decision}"))
    return jsonify(ok=True, status=new_status)


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
              "INSTRUMENTATION": f"/ops/{operation_id}/instrumentation",
              "VIDEO": f"/ops/{operation_id}/video",
              "READINESS": f"/ops/{operation_id}/readiness",
              "REHEARSAL": f"/ops/{operation_id}/rehearsal",
              "EXECUTION": f"/ops/{operation_id}/execution", "REVIEW": f"/ops/{operation_id}/review"}
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


def instrumentation_requirements(operation_type: str) -> set[str]:
    if operation_type == "STATIC_FIRE": return {"CHAMBER_PRESSURE", "THRUST", "CASE_TEMPERATURE", "IGNITION_CONTINUITY"}
    if operation_type == "PRESSURE_TEST": return {"VESSEL_PRESSURE", "CASE_TEMPERATURE"}
    if operation_type == "ROCKET_LAUNCH": return {"ALTITUDE", "ACCELERATION", "BATTERY_VOLTAGE", "IGNITION_CONTINUITY"}
    if operation_type == "AVIONICS_TEST": return {"BATTERY_VOLTAGE", "BUS_CURRENT", "TIME_SYNC"}
    return {"PRIMARY_MEASUREMENT", "TIME_SYNC"}


def video_view_requirements(operation_type: str) -> set[str]:
    if operation_type == "STATIC_FIRE": return {"MOTOR_WIDE", "NOZZLE_CLOSE"}
    if operation_type == "ROCKET_LAUNCH": return {"LAUNCHER_WIDE", "VEHICLE_CLOSE", "TRAJECTORY"}
    if operation_type == "RECOVERY_TEST": return {"DEPLOYMENT_WIDE", "CANOPY_CLOSE"}
    return {"TEST_ARTICLE_WIDE"}


def readiness_gate_catalog(operation_type:str)->list[dict]:
    gates=[("CONFIGURATION","Configuration Baseline","CONFIGURATION MANAGER",True),
           ("STAFFING","Team & Authority","TEST DIRECTOR",True),
           ("PROCEDURE","Approved Procedure","TEST DIRECTOR",True),
           ("INSTRUMENTATION","Measurement Assurance","INSTRUMENTATION LEAD",True),
           ("VIDEO","Video Evidence","DATA & VIDEO LEAD",True),
           ("SAFETY","Hazard Controls & Emergency Response","RSO",True),
           ("SITE","Site, Stand & Exclusion Zone","GROUND OPERATIONS",True)]
    if operation_type=="ROCKET_LAUNCH":gates += [("RANGE","Range Readiness","RSO",True),("AIRSPACE","Airspace Coordination","LAUNCH DIRECTOR",True),("RECOVERY","Recovery Readiness","RECOVERY LEAD",True)]
    return [{"code":c,"name":n,"owner_role":o,"required":r} for c,n,o,r in gates]


def rehearsal_requirements(operation_type:str)->set[str]:
    required={"FULL_SEQUENCE","COMM_CHECK","HOLD_RESPONSE","ABORT_RESPONSE","DATA_RECORDING","VIDEO_RECORDING"}
    if operation_type=="ROCKET_LAUNCH":required|={"RANGE_HOLD","RECOVERY_COMMS"}
    return required


def execution_gate_catalog(operation_type:str)->list[dict]:
    gates=[("CONFIG_UNCHANGED","Configuration fingerprints unchanged","CONFIGURATION MANAGER"),
           ("READINESS_CURRENT","Readiness decision remains current","TEST DIRECTOR"),
           ("REHEARSAL_VALID","Approved rehearsal remains applicable","TEST DIRECTOR"),
           ("CREW_PRESENT","Authorised crew at assigned stations","TEST DIRECTOR"),
           ("SITE_CLEAR","Site and exclusion zone clear","RSO"),
           ("TELEMETRY_LIVE","Required live telemetry healthy","INSTRUMENTATION LEAD"),
           ("RECORDING_ACTIVE","Telemetry evidence recording active","INSTRUMENTATION LEAD"),
           ("VIDEO_ACTIVE","Mandatory camera recording active","DATA & VIDEO LEAD"),
           ("IGNITION_SAFE","Ignition circuit verified SAFE","LCO")]
    if operation_type=="ROCKET_LAUNCH":gates += [("RANGE_RELEASE","Range released for launch","RSO"),("AIRSPACE_RELEASE","Airspace release valid","LAUNCH DIRECTOR"),("RECOVERY_READY","Recovery stations ready","RECOVERY LEAD")]
    return [{"code":c,"name":n,"owner_role":o} for c,n,o in gates]


def closeout_evidence_catalog(operation_type:str)->list[dict]:
    items=[("EXECUTION_RELEASE","Execution release and independent authorisations"),
           ("EVENT_LOG","Immutable event and command log"),
           ("TELEMETRY_PACKAGE","Raw and engineering telemetry package"),
           ("VIDEO_EVIDENCE","Mandatory camera evidence set"),
           ("CONFIGURATION_BASELINE","Released configuration baseline"),
           ("APPROVED_PROCEDURE","Approved executed procedure"),
           ("READINESS_DECISION","Readiness decision record"),
           ("REHEARSAL_RECORD","Applicable rehearsal record"),
           ("SAFING_DECLARATION","Post-operation safing declaration")]
    if operation_type=="ROCKET_LAUNCH":items += [("RANGE_LOG","Range coordination and release log"),("FLIGHT_TRACK","Flight tracking data package"),("RECOVERY_REPORT","Recovery and article disposition report")]
    return [{"code":code,"name":name,"required":True} for code,name in items]


@operations.post("/api/ops/<int:operation_id>/execution")
def save_execution_release(operation_id:int):
    p=request.get_json(silent=True) or {};gates=p.get("gates",[])
    code=str(p.get("release_code","")).strip().upper();source=str(p.get("source_mode","")).strip().upper()
    planned=str(p.get("planned_start","")).strip();valid_until=str(p.get("valid_until","")).strip()
    if not valid_code(code) or source!="LIVE" or not planned or not valid_until or valid_until<=planned:
        return jsonify(error="release code, LIVE source mode, planned start and a later validity time are required"),400
    if not isinstance(gates,list):return jsonify(error="release gates must be a list"),400
    gate_rows=[];errors=[]
    for index,x in enumerate(gates,1):
        gate=str(x.get("gate_code","")).strip().upper();status=str(x.get("status","PENDING")).strip().upper()
        evidence=str(x.get("evidence_reference","")).strip();verifier=str(x.get("verified_by","")).strip()
        if status not in {"PENDING","GO","NO_GO"}:errors.append(f"gate {gate or index} has invalid status")
        if status in {"GO","NO_GO"} and (not evidence or not verifier):errors.append(f"gate {gate or index} requires evidence and verifier")
        gate_rows.append((gate,status,evidence,verifier,utc_now() if status!="PENDING" else None,str(x.get("notes","")).strip()))
    if errors:return jsonify(error="; ".join(errors)),400
    stamp=utc_now()
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="EXECUTION":return jsonify(error="execution release can only be edited during the EXECUTION stage"),409
        rehearsal=db.execute("SELECT * FROM rehearsal_campaigns WHERE operation_id=?",(operation_id,)).fetchone()
        baseline=db.execute("SELECT canonical_sha256 FROM configuration_baselines WHERE operation_id=?",(operation_id,)).fetchone()
        procedure=db.execute("SELECT canonical_sha256 FROM operation_procedures WHERE operation_id=?",(operation_id,)).fetchone()
        readiness=db.execute("SELECT canonical_sha256 FROM readiness_reviews WHERE operation_id=?",(operation_id,)).fetchone()
        if not rehearsal or rehearsal["state"]!="COMPLETED":return jsonify(error="a completed rehearsal is required"),409
        expected={x["code"]:x for x in execution_gate_catalog(operation["operation_type"])};unknown=sorted({x[0] for x in gate_rows}-set(expected))
        if unknown:return jsonify(error="unsupported execution gates: "+", ".join(unknown)),400
        existing=db.execute("SELECT * FROM execution_releases WHERE operation_id=?",(operation_id,)).fetchone()
        if existing and existing["state"] in {"RELEASED","CLOSED"}:return jsonify(error="released execution packages are immutable"),409
        db.execute("""INSERT INTO execution_releases(operation_id,release_code,source_mode,state,planned_start,valid_until,baseline_sha256,procedure_sha256,readiness_sha256,rehearsal_sha256,outcome,created_at,updated_at)
            VALUES(?,?,?,'DRAFT',?,?,?,?,?,?,'PENDING',?,?) ON CONFLICT(operation_id) DO UPDATE SET release_code=excluded.release_code,source_mode=excluded.source_mode,
            planned_start=excluded.planned_start,valid_until=excluded.valid_until,baseline_sha256=excluded.baseline_sha256,procedure_sha256=excluded.procedure_sha256,
            readiness_sha256=excluded.readiness_sha256,rehearsal_sha256=excluded.rehearsal_sha256,updated_at=excluded.updated_at""",
            (operation_id,code,source,planned,valid_until,baseline["canonical_sha256"],procedure["canonical_sha256"],readiness["canonical_sha256"],rehearsal["canonical_sha256"],stamp,stamp))
        release_id=db.execute("SELECT id FROM execution_releases WHERE operation_id=?",(operation_id,)).fetchone()["id"]
        db.execute("DELETE FROM execution_release_gates WHERE release_id=?",(release_id,));db.execute("DELETE FROM execution_authorizations WHERE release_id=?",(release_id,))
        for gate,status,evidence,verifier,verified_at,notes in gate_rows:
            meta=expected[gate];db.execute("""INSERT INTO execution_release_gates(release_id,gate_code,name,owner_role,status,evidence_reference,verified_by,verified_at,notes,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",(release_id,gate,meta["name"],meta["owner_role"],status,evidence,verifier,verified_at,notes,stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",(operation_id,stamp,"EXECUTION_RELEASE_UPDATED","TEST DIRECTOR",f"LIVE execution release {code} updated with {len(gate_rows)} gates"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/execution/release")
def issue_execution_release(operation_id:int):
    p=request.get_json(silent=True) or {};authorizations=p.get("authorizations",[]);stamp=utc_now()
    if not isinstance(authorizations,list):return jsonify(error="authorizations must be a list"),400
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="EXECUTION":return jsonify(error="EXECUTION is not the active workflow stage"),409
        release=db.execute("SELECT * FROM execution_releases WHERE operation_id=?",(operation_id,)).fetchone()
        if not release:return jsonify(error="save the execution release package first"),409
        if release["state"]!="DRAFT":return jsonify(error="execution release has already been issued"),409
        if release["valid_until"]<stamp[:16]:return jsonify(error="execution release validity window has expired"),409
        gates=[dict(x) for x in db.execute("SELECT * FROM execution_release_gates WHERE release_id=?",(release["id"],))]
        expected={x["code"] for x in execution_gate_catalog(operation["operation_type"])};present={x["gate_code"] for x in gates}
        missing=sorted(expected-present)
        if missing:return jsonify(error="mandatory execution gates are missing: "+", ".join(missing)),409
        blocked=sorted(x["gate_code"] for x in gates if x["status"]!="GO")
        if blocked:return jsonify(error="execution gates are not GO: "+", ".join(blocked)),409
        runtime_id=operation["runtime_operation_id"] or OPERATION_ID;runtime=db.execute("SELECT * FROM operations WHERE id=?",(runtime_id,)).fetchone()
        if not runtime or runtime["mode"]!="LIVE":return jsonify(error="runtime telemetry source must be LIVE before release"),409
        if runtime["state"] not in {"CHECKOUT","HOLD"}:return jsonify(error="runtime must be in CHECKOUT or HOLD before release"),409
        staffing=db.execute("SELECT id FROM staffing_plans WHERE operation_id=?",(operation_id,)).fetchone()
        assigned={x["role_code"]:x["person_name"] for x in db.execute("SELECT role_code,person_name FROM operation_role_assignments WHERE staffing_plan_id=?",(staffing["id"],))}
        auth={}
        for x in authorizations:
            role=str(x.get("role_code","")).strip().upper();person=str(x.get("person_name","")).strip();decision=str(x.get("decision","")).strip().upper();attestation=str(x.get("attestation","")).strip()
            if role in auth:return jsonify(error=f"duplicate authorization for {role}"),400
            auth[role]=(person,decision,attestation)
        required={"TD","RSO","LCO"};missing_auth=sorted(required-set(auth))
        if missing_auth:return jsonify(error="independent authorizations are missing: "+", ".join(missing_auth)),409
        for role in required:
            person,decision,attestation=auth[role]
            if assigned.get(role)!=person:return jsonify(error=f"{role} authorization does not match approved staffing assignment"),409
            if decision!="GO" or not attestation:return jsonify(error=f"{role} must provide GO and an explicit attestation"),409
        if len({auth[r][0].casefold() for r in required})!=3:return jsonify(error="TD, RSO and LCO authorizations must be from separate people"),409
        canonical={"schema":"SMTCS-EXECUTION-RELEASE/1","operation":operation["code"],"release":{"code":release["release_code"],"source_mode":release["source_mode"],"valid_until":release["valid_until"],"baseline":release["baseline_sha256"],"procedure":release["procedure_sha256"],"readiness":release["readiness_sha256"],"rehearsal":release["rehearsal_sha256"]},
                   "gates":[{k:x[k] for k in ("gate_code","status","evidence_reference","verified_by")} for x in sorted(gates,key=lambda y:y["gate_code"])],
                   "authorizations":[{"role":r,"person":auth[r][0],"decision":auth[r][1],"attestation":auth[r][2]} for r in sorted(required)]}
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        for role in required:db.execute("INSERT INTO execution_authorizations(release_id,role_code,person_name,decision,attestation,authorised_at) VALUES(?,?,?,?,?,?)",(release["id"],role,*auth[role],stamp))
        db.execute("UPDATE execution_releases SET state='RELEASED',release_sha256=?,released_at=?,updated_at=? WHERE id=?",(digest,stamp,stamp,release["id"]))
        db.execute("UPDATE operation_registry SET status='LIVE RELEASED',updated_at=? WHERE id=?",(stamp,operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",(operation_id,stamp,"EXECUTION_RELEASED","TEST DIRECTOR",f"LIVE execution released with SHA-256 {digest}; Mission Control handoff enabled"))
    return jsonify(ok=True,sha256=digest,url="/workspace")


@operations.post("/api/ops/<int:operation_id>/execution/close")
def close_execution(operation_id:int):
    p=request.get_json(silent=True) or {};outcome=str(p.get("outcome","")).strip().upper();summary=str(p.get("summary","")).strip();actor=str(p.get("closed_by","TEST DIRECTOR")).strip();stamp=utc_now()
    if outcome not in {"SUCCESS","PARTIAL","ABORTED","NO_TEST"} or not summary:return jsonify(error="controlled outcome and summary are required"),400
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone();release=db.execute("SELECT * FROM execution_releases WHERE operation_id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="EXECUTION" or not release or release["state"]!="RELEASED":return jsonify(error="a released execution session is required"),409
        runtime=db.execute("SELECT state FROM operations WHERE id=?",(operation["runtime_operation_id"] or OPERATION_ID,)).fetchone()
        if not runtime or runtime["state"] not in {"POST_FIRE","ABORTED","CLOSED"}:return jsonify(error="runtime must reach POST_FIRE, ABORTED or CLOSED before execution closure"),409
        db.execute("UPDATE execution_releases SET state='CLOSED',outcome=?,outcome_summary=?,closed_at=?,updated_at=? WHERE id=?",(outcome,summary,stamp,stamp,release["id"]))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner=?,updated_at=? WHERE operation_id=? AND section_key='EXECUTION'",(actor,stamp,operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',updated_at=? WHERE operation_id=? AND section_key='REVIEW'",(stamp,operation_id))
        db.execute("UPDATE operation_registry SET current_stage='REVIEW',status='POST OPERATION',updated_at=? WHERE id=?",(stamp,operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",(operation_id,stamp,"EXECUTION_CLOSED",actor,f"Execution closed as {outcome}; Review & Closure unlocked"))
    return jsonify(ok=True,url=url_for("operations.operation_detail",operation_id=operation_id))


def valid_sha256(value:str)->bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}",value))


@operations.post("/api/ops/<int:operation_id>/review")
def save_post_operation_review(operation_id:int):
    p=request.get_json(silent=True) or {};objectives=p.get("objectives",[]);evidence=p.get("evidence_items",[]);actions=p.get("corrective_actions",[])
    code=str(p.get("review_code","")).strip().upper();chair=str(p.get("review_chair","")).strip();review_date=str(p.get("review_date","")).strip()
    conclusion=str(p.get("overall_conclusion","")).strip();lessons=str(p.get("lessons_learned","")).strip();package_ref=str(p.get("evidence_package_reference","")).strip();package_sha=str(p.get("evidence_package_sha256","")).strip().lower()
    if not valid_code(code) or not chair or not review_date:return jsonify(error="review code, chair and review date are required"),400
    if not isinstance(objectives,list) or not isinstance(evidence,list) or not isinstance(actions,list):return jsonify(error="objectives, evidence and corrective actions must be lists"),400
    objective_rows=[];evidence_rows=[];action_rows=[];errors=[]
    for index,x in enumerate(objectives,1):
        ocode=str(x.get("objective_code","")).strip().upper();text=str(x.get("objective_text","")).strip();assessment=str(x.get("assessment","PENDING")).strip().upper();reference=str(x.get("evidence_reference","")).strip();rationale=str(x.get("rationale","")).strip()
        if not valid_code(ocode) or not text or assessment not in {"PENDING","MET","PARTIAL","NOT_MET"}:errors.append(f"objective {index} has incomplete controlled fields")
        if assessment!="PENDING" and (not reference or not rationale):errors.append(f"objective {ocode or index} requires evidence and rationale")
        objective_rows.append((ocode,text,assessment,reference,rationale))
    for index,x in enumerate(evidence,1):
        item_code=str(x.get("item_code","")).strip().upper();name=str(x.get("name","")).strip();required=bool(x.get("required",True));status=str(x.get("status","MISSING")).strip().upper();reference=str(x.get("reference","")).strip();digest=str(x.get("sha256","")).strip().lower();disposition=str(x.get("disposition","")).strip()
        if not valid_code(item_code) or not name or status not in {"VERIFIED","MISSING","NOT_APPLICABLE"}:errors.append(f"evidence item {index} has incomplete controlled fields")
        if status=="VERIFIED" and (not reference or not valid_sha256(digest)):errors.append(f"evidence item {item_code or index} requires a reference and SHA-256")
        if status=="MISSING" and not disposition:errors.append(f"missing evidence item {item_code or index} requires a disposition")
        evidence_rows.append((item_code,name,int(required),status,reference,digest,disposition))
    for index,x in enumerate(actions,1):
        acode=str(x.get("action_code","")).strip().upper();title=str(x.get("title","")).strip();source=str(x.get("source","")).strip();severity=str(x.get("severity","MEDIUM")).strip().upper();owner=str(x.get("owner","")).strip();due=str(x.get("due_date","")).strip();status=str(x.get("status","OPEN")).strip().upper();closure=str(x.get("closure_evidence","")).strip();transfer=str(x.get("transfer_reference","")).strip();notes=str(x.get("notes","")).strip()
        if not valid_code(acode) or not title or not source or severity not in {"LOW","MEDIUM","HIGH","CRITICAL"} or not owner or not due or status not in {"OPEN","CLOSED","TRANSFERRED"}:errors.append(f"corrective action {index} has incomplete controlled fields")
        if status=="CLOSED" and not closure:errors.append(f"closed action {acode or index} requires closure evidence")
        if status=="TRANSFERRED" and not transfer:errors.append(f"transferred action {acode or index} requires a controlled transfer reference")
        action_rows.append((acode,title,source,severity,owner,due,status,closure,transfer,notes))
    if errors:return jsonify(error="; ".join(errors)),400
    stamp=utc_now()
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone();release=db.execute("SELECT state FROM execution_releases WHERE operation_id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="REVIEW" or not release or release["state"]!="CLOSED":return jsonify(error="a closed execution is required before post-operation review"),409
        existing=db.execute("SELECT * FROM post_operation_reviews WHERE operation_id=?",(operation_id,)).fetchone()
        if existing and existing["state"]=="CLOSED":return jsonify(error="closed review records are immutable"),409
        db.execute("""INSERT INTO post_operation_reviews(operation_id,review_code,state,review_chair,review_date,overall_conclusion,lessons_learned,evidence_package_reference,evidence_package_sha256,created_at,updated_at)
            VALUES(?,?,'DRAFT',?,?,?,?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET review_code=excluded.review_code,review_chair=excluded.review_chair,review_date=excluded.review_date,overall_conclusion=excluded.overall_conclusion,lessons_learned=excluded.lessons_learned,evidence_package_reference=excluded.evidence_package_reference,evidence_package_sha256=excluded.evidence_package_sha256,updated_at=excluded.updated_at""",
            (operation_id,code,chair,review_date,conclusion,lessons,package_ref,package_sha,stamp,stamp))
        review_id=db.execute("SELECT id FROM post_operation_reviews WHERE operation_id=?",(operation_id,)).fetchone()["id"]
        db.execute("DELETE FROM objective_assessments WHERE review_id=?",(review_id,));db.execute("DELETE FROM closeout_evidence_items WHERE review_id=?",(review_id,));db.execute("DELETE FROM corrective_actions WHERE review_id=?",(review_id,))
        for row in objective_rows:db.execute("INSERT INTO objective_assessments(review_id,objective_code,objective_text,assessment,evidence_reference,rationale,updated_at) VALUES(?,?,?,?,?,?,?)",(review_id,*row,stamp))
        for row in evidence_rows:db.execute("INSERT INTO closeout_evidence_items(review_id,item_code,name,required,status,reference,sha256,disposition,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(review_id,*row,stamp))
        for row in action_rows:db.execute("INSERT INTO corrective_actions(review_id,action_code,title,source,severity,owner,due_date,status,closure_evidence,transfer_reference,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(review_id,*row,stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",(operation_id,stamp,"POST_REVIEW_UPDATED",chair,f"Post-operation review {code} updated"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/review/close")
def close_post_operation_review(operation_id:int):
    p=request.get_json(silent=True) or {};actor=str(p.get("closed_by","TEST DIRECTOR")).strip();stamp=utc_now()
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        review=db.execute("SELECT * FROM post_operation_reviews WHERE operation_id=?",(operation_id,)).fetchone()
        if operation["current_stage"]!="REVIEW" or not review or review["state"]!="DRAFT":return jsonify(error="an active draft post-operation review is required"),409
        objectives=[dict(x) for x in db.execute("SELECT * FROM objective_assessments WHERE review_id=?",(review["id"],))];evidence=[dict(x) for x in db.execute("SELECT * FROM closeout_evidence_items WHERE review_id=?",(review["id"],))];actions=[dict(x) for x in db.execute("SELECT * FROM corrective_actions WHERE review_id=?",(review["id"],))]
        criteria=json.loads(operation["success_criteria_json"] or "[]");assessed={x["objective_text"] for x in objectives if x["assessment"] in {"MET","PARTIAL","NOT_MET"}}
        missing_objectives=[x for x in criteria if x not in assessed]
        if missing_objectives:return jsonify(error="success criteria are not assessed: "+", ".join(missing_objectives)),409
        expected={x["code"] for x in closeout_evidence_catalog(operation["operation_type"])};indexed={x["item_code"]:x for x in evidence};missing=sorted(code for code in expected if code not in indexed or indexed[code]["status"]!="VERIFIED")
        if missing:return jsonify(error="required evidence is not verified: "+", ".join(missing)),409
        open_actions=sorted(x["action_code"] for x in actions if x["status"]=="OPEN")
        if open_actions:return jsonify(error="corrective actions require closure or controlled transfer: "+", ".join(open_actions)),409
        if not review["overall_conclusion"] or not review["lessons_learned"] or not review["evidence_package_reference"] or not valid_sha256(review["evidence_package_sha256"]):return jsonify(error="conclusion, lessons learned and a controlled evidence package with SHA-256 are required"),409
        release=db.execute("SELECT outcome,outcome_summary,release_sha256 FROM execution_releases WHERE operation_id=?",(operation_id,)).fetchone()
        canonical={"schema":"SMTCS-POST-OPERATION-CLOSURE/1","operation":operation["code"],"execution":dict(release),"review":{"code":review["review_code"],"conclusion":review["overall_conclusion"],"lessons":review["lessons_learned"],"package":review["evidence_package_reference"],"package_sha256":review["evidence_package_sha256"]},"objectives":[{k:x[k] for k in ("objective_code","objective_text","assessment","evidence_reference","rationale")} for x in sorted(objectives,key=lambda y:y["objective_code"])],"evidence":[{k:x[k] for k in ("item_code","status","reference","sha256")} for x in sorted(evidence,key=lambda y:y["item_code"])],"actions":[{k:x[k] for k in ("action_code","severity","owner","status","closure_evidence","transfer_reference")} for x in sorted(actions,key=lambda y:y["action_code"])]}
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        db.execute("UPDATE post_operation_reviews SET state='CLOSED',closure_sha256=?,closed_at=?,closed_by=?,updated_at=? WHERE id=?",(digest,stamp,actor,stamp,review["id"]))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner=?,updated_at=? WHERE operation_id=? AND section_key='REVIEW'",(actor,stamp,operation_id))
        db.execute("UPDATE operation_registry SET current_stage='CLOSED',status='CLOSED',updated_at=? WHERE id=?",(stamp,operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",(operation_id,stamp,"OPERATION_CLOSED",actor,f"Operation closed with immutable closure SHA-256 {digest}"))
    return jsonify(ok=True,sha256=digest,url=url_for("operations.operation_detail",operation_id=operation_id))


@operations.post("/api/ops/<int:operation_id>/rehearsal")
def save_rehearsal(operation_id:int):
    p=request.get_json(silent=True) or {};checkpoints=p.get("checkpoints",[]);anomalies=p.get("anomalies",[])
    code=str(p.get("rehearsal_code","")).strip().upper();kind=str(p.get("rehearsal_type","DRY_RUN")).strip().upper()
    source=str(p.get("source_mode","SIMULATION")).strip().upper();conductor=str(p.get("conductor","")).strip();scheduled=str(p.get("scheduled_at","")).strip()
    if not valid_code(code) or kind not in {"TABLETOP","DRY_RUN","WET_DRESS"} or source!="SIMULATION" or not conductor or not scheduled:
        return jsonify(error="rehearsal code, type, conductor, schedule and SIMULATION source mode are required"),400
    if not isinstance(checkpoints,list) or not isinstance(anomalies,list):return jsonify(error="checkpoints and anomalies must be lists"),400
    checkpoint_rows=[];errors=[]
    for index,x in enumerate(checkpoints,1):
        ccode=str(x.get("checkpoint_code","")).strip().upper();name=str(x.get("name","")).strip();phase=str(x.get("phase","")).strip().upper()
        role=str(x.get("responsible_role","")).strip().upper();objective=str(x.get("objective","")).strip();expected=str(x.get("expected_result","")).strip()
        result=str(x.get("result","PENDING")).strip().upper();observed=str(x.get("observed_result","")).strip();evidence=str(x.get("evidence_reference","")).strip()
        try:response=float(x["response_time_seconds"]) if x.get("response_time_seconds") not in {None,""} else None
        except (TypeError,ValueError):errors.append(f"checkpoint {ccode or index} has invalid response time");response=None
        critical=bool(x.get("critical",True))
        if not valid_code(ccode) or not name or not phase or not role or not objective or not expected or result not in {"PENDING","PASS","FAIL","BLOCKED"}:errors.append(f"checkpoint {index} has incomplete controlled fields")
        if result in {"PASS","FAIL","BLOCKED"} and (not observed or not evidence):errors.append(f"{ccode or index} requires observed result and evidence")
        if response is not None and response<0:errors.append(f"{ccode or index} response time cannot be negative")
        checkpoint_rows.append((ccode,name,phase,role,objective,expected,int(critical),result,observed or None,response,evidence or None,str(x.get("notes","")).strip()))
    anomaly_rows=[]
    for index,x in enumerate(anomalies,1):
        acode=str(x.get("anomaly_code","")).strip().upper();title=str(x.get("title","")).strip();severity=str(x.get("severity","")).strip().upper()
        owner=str(x.get("owner","")).strip();status=str(x.get("status","OPEN")).strip().upper();retest=bool(x.get("requires_retest",False))
        disposition=str(x.get("disposition","")).strip();evidence=str(x.get("evidence_reference","")).strip()
        if not valid_code(acode) or not title or severity not in {"LOW","MEDIUM","HIGH","CRITICAL"} or not owner or status not in {"OPEN","CLOSED","ACCEPTED"}:errors.append(f"anomaly {index} has incomplete controlled fields")
        if status in {"CLOSED","ACCEPTED"} and (not disposition or not evidence):errors.append(f"{acode or index} requires disposition and evidence")
        anomaly_rows.append((acode,title,severity,owner,status,int(retest),disposition or None,evidence or None,str(x.get("notes","")).strip()))
    if errors:return jsonify(error="; ".join(errors)),400
    stamp=utc_now()
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="REHEARSAL":return jsonify(error="rehearsal can only be edited during the REHEARSAL stage"),409
        readiness=db.execute("SELECT state,canonical_sha256 FROM readiness_reviews WHERE operation_id=?",(operation_id,)).fetchone()
        baseline=db.execute("SELECT canonical_sha256 FROM configuration_baselines WHERE operation_id=?",(operation_id,)).fetchone()
        procedure=db.execute("SELECT canonical_sha256 FROM operation_procedures WHERE operation_id=?",(operation_id,)).fetchone()
        if not readiness or readiness["state"]!="APPROVED":return jsonify(error="an approved readiness review is required"),409
        staffing=db.execute("SELECT id FROM staffing_plans WHERE operation_id=? AND state='APPROVED'",(operation_id,)).fetchone()
        assigned={x["role_code"] for x in db.execute("SELECT role_code FROM operation_role_assignments WHERE staffing_plan_id=?",(staffing["id"],))} if staffing else set()
        unknown_roles=sorted({x[3] for x in checkpoint_rows}-assigned)
        if unknown_roles:return jsonify(error="rehearsal checkpoints reference unassigned roles: "+", ".join(unknown_roles)),409
        existing=db.execute("SELECT * FROM rehearsal_campaigns WHERE operation_id=?",(operation_id,)).fetchone()
        if existing and existing["state"]=="COMPLETED":return jsonify(error="completed rehearsal records are immutable"),409
        db.execute("""INSERT INTO rehearsal_campaigns(operation_id,rehearsal_code,rehearsal_type,source_mode,state,conductor,scheduled_at,baseline_sha256,procedure_sha256,result,created_at,updated_at)
            VALUES(?,?,?,?,'DRAFT',?,?,?,?, 'PENDING',?,?) ON CONFLICT(operation_id) DO UPDATE SET rehearsal_code=excluded.rehearsal_code,rehearsal_type=excluded.rehearsal_type,
            source_mode=excluded.source_mode,conductor=excluded.conductor,scheduled_at=excluded.scheduled_at,baseline_sha256=excluded.baseline_sha256,procedure_sha256=excluded.procedure_sha256,updated_at=excluded.updated_at""",
            (operation_id,code,kind,source,conductor,scheduled,baseline["canonical_sha256"],procedure["canonical_sha256"],stamp,stamp))
        campaign_id=db.execute("SELECT id FROM rehearsal_campaigns WHERE operation_id=?",(operation_id,)).fetchone()["id"]
        db.execute("DELETE FROM rehearsal_checkpoints WHERE campaign_id=?",(campaign_id,));db.execute("DELETE FROM rehearsal_anomalies WHERE campaign_id=?",(campaign_id,))
        for row in checkpoint_rows:db.execute("""INSERT INTO rehearsal_checkpoints(campaign_id,checkpoint_code,name,phase,responsible_role,objective,expected_result,critical,result,observed_result,response_time_seconds,evidence_reference,notes,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(campaign_id,*row,stamp))
        for row in anomaly_rows:db.execute("""INSERT INTO rehearsal_anomalies(campaign_id,anomaly_code,title,severity,owner,status,requires_retest,disposition,evidence_reference,notes,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(campaign_id,*row,stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",(operation_id,stamp,"REHEARSAL_UPDATED",conductor,f"Simulation rehearsal {code} updated with {len(checkpoint_rows)} checkpoints and {len(anomaly_rows)} anomalies"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/rehearsal/complete")
def complete_rehearsal(operation_id:int):
    p=request.get_json(silent=True) or {};actor=str(p.get("completed_by","TEST DIRECTOR")).strip() or "TEST DIRECTOR";summary=str(p.get("summary","")).strip();stamp=utc_now()
    if not summary:return jsonify(error="rehearsal completion requires an outcome summary"),400
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="REHEARSAL":return jsonify(error="REHEARSAL is not the active workflow stage"),409
        campaign=db.execute("SELECT * FROM rehearsal_campaigns WHERE operation_id=?",(operation_id,)).fetchone()
        if not campaign:return jsonify(error="save the rehearsal record first"),409
        if campaign["source_mode"]!="SIMULATION":return jsonify(error="rehearsal must run in explicit SIMULATION mode"),409
        checkpoints=[dict(x) for x in db.execute("SELECT * FROM rehearsal_checkpoints WHERE campaign_id=? ORDER BY checkpoint_code",(campaign["id"],))]
        present={x["checkpoint_code"] for x in checkpoints};missing=sorted(rehearsal_requirements(operation["operation_type"])-present)
        if missing:return jsonify(error="mandatory rehearsal checkpoints are missing: "+", ".join(missing)),409
        failed=sorted(x["checkpoint_code"] for x in checkpoints if x["critical"] and x["result"]!="PASS")
        if failed:return jsonify(error="critical rehearsal checkpoints did not pass: "+", ".join(failed)),409
        anomalies=[dict(x) for x in db.execute("SELECT * FROM rehearsal_anomalies WHERE campaign_id=? ORDER BY anomaly_code",(campaign["id"],))]
        open_items=sorted(x["anomaly_code"] for x in anomalies if x["status"]=="OPEN" or (x["requires_retest"] and x["status"]!="CLOSED"))
        if open_items:return jsonify(error="rehearsal anomalies require closure or retest: "+", ".join(open_items)),409
        unsafe=sorted(x["anomaly_code"] for x in anomalies if x["severity"]=="CRITICAL" and x["status"]=="ACCEPTED")
        if unsafe:return jsonify(error="critical rehearsal anomalies cannot be accepted: "+", ".join(unsafe)),409
        canonical={"schema":"SMTCS-REHEARSAL/1","operation":operation["code"],"campaign":{"code":campaign["rehearsal_code"],"type":campaign["rehearsal_type"],"source_mode":campaign["source_mode"],"baseline":campaign["baseline_sha256"],"procedure":campaign["procedure_sha256"]},
                   "checkpoints":[{k:x[k] for k in ("checkpoint_code","responsible_role","critical","result","observed_result","response_time_seconds","evidence_reference")} for x in checkpoints],
                   "anomalies":[{k:x[k] for k in ("anomaly_code","severity","owner","status","requires_retest","disposition","evidence_reference")} for x in anomalies],"summary":summary}
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        db.execute("UPDATE rehearsal_campaigns SET state='COMPLETED',result='PASS',summary=?,canonical_sha256=?,completed_at=?,completed_by=?,updated_at=? WHERE id=?",(summary,digest,stamp,actor,stamp,campaign["id"]))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner=?,updated_at=? WHERE operation_id=? AND section_key='REHEARSAL'",(actor,stamp,operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',updated_at=? WHERE operation_id=? AND section_key='EXECUTION'",(stamp,operation_id))
        db.execute("UPDATE operation_registry SET current_stage='EXECUTION',status='READY',updated_at=? WHERE id=?",(stamp,operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",(operation_id,stamp,"REHEARSAL_COMPLETED",actor,f"Simulation rehearsal passed with SHA-256 {digest}; Live Execution unlocked"))
    return jsonify(ok=True,sha256=digest,url=url_for("operations.operation_detail",operation_id=operation_id))


@operations.post("/api/ops/<int:operation_id>/readiness")
def save_readiness(operation_id:int):
    p=request.get_json(silent=True) or {};gates=p.get("gates",[]);findings=p.get("findings",[])
    code=str(p.get("review_code","")).strip().upper();review_type=str(p.get("review_type","TRR")).strip().upper()
    chair=str(p.get("review_chair","")).strip();planned=str(p.get("planned_date","")).strip()
    if not valid_code(code) or review_type not in {"TRR","FRR","LRR"} or not chair or not planned:return jsonify(error="review code, type, chair and planned date are required"),400
    if not isinstance(gates,list) or not isinstance(findings,list):return jsonify(error="gates and findings must be lists"),400
    gate_rows=[];errors=[]
    catalog={x["code"]:x for x in readiness_gate_catalog("")}
    for index,x in enumerate(gates,1):
        gate=str(x.get("gate_code","")).strip().upper();status=str(x.get("status","PENDING")).strip().upper()
        evidence=str(x.get("evidence_reference","")).strip();reviewer=str(x.get("reviewer","")).strip()
        waiver_reason=str(x.get("waiver_reason","")).strip();waiver_authority=str(x.get("waiver_authority","")).strip()
        if status not in {"PENDING","GO","NO_GO","WAIVER"}:errors.append(f"gate {gate or index} has invalid status")
        if status in {"GO","NO_GO","WAIVER"} and (not evidence or not reviewer):errors.append(f"gate {gate or index} requires evidence and reviewer")
        if status=="WAIVER" and (not waiver_reason or not waiver_authority):errors.append(f"gate {gate or index} waiver requires reason and authority")
        gate_rows.append((gate,status,evidence,reviewer,utc_now() if status!="PENDING" else None,waiver_reason or None,waiver_authority or None,str(x.get("notes","")).strip()))
    finding_rows=[]
    for index,x in enumerate(findings,1):
        fcode=str(x.get("finding_code","")).strip().upper();title=str(x.get("title","")).strip();severity=str(x.get("severity","")).strip().upper()
        owner=str(x.get("owner","")).strip();status=str(x.get("status","OPEN")).strip().upper();due=str(x.get("due_date","")).strip()
        disposition=str(x.get("disposition","")).strip();authority=str(x.get("acceptance_authority","")).strip()
        if not valid_code(fcode) or not title or severity not in {"LOW","MEDIUM","HIGH","CRITICAL"} or not owner or status not in {"OPEN","CLOSED","ACCEPTED"} or not due:errors.append(f"finding {index} has incomplete controlled fields")
        if status in {"CLOSED","ACCEPTED"} and not disposition:errors.append(f"{fcode or index} requires a disposition")
        if status=="ACCEPTED" and not authority:errors.append(f"{fcode or index} accepted risk requires acceptance authority")
        finding_rows.append((fcode,title,severity,owner,status,due,disposition or None,authority or None,str(x.get("notes","")).strip()))
    if errors:return jsonify(error="; ".join(errors)),400
    stamp=utc_now()
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="READINESS":return jsonify(error="readiness review can only be edited during the READINESS stage"),409
        video=db.execute("SELECT state FROM video_recording_plans WHERE operation_id=?",(operation_id,)).fetchone()
        if not video or video["state"]!="APPROVED":return jsonify(error="an approved video evidence plan is required"),409
        expected={x["code"]:x for x in readiness_gate_catalog(operation["operation_type"])}
        unknown=sorted({x[0] for x in gate_rows}-set(expected))
        if unknown:return jsonify(error="unsupported readiness gates: "+", ".join(unknown)),400
        existing=db.execute("SELECT * FROM readiness_reviews WHERE operation_id=?",(operation_id,)).fetchone()
        if existing and existing["state"]=="APPROVED":return jsonify(error="approved readiness reviews are immutable"),409
        db.execute("""INSERT INTO readiness_reviews(operation_id,review_code,review_type,state,review_chair,planned_date,final_decision,created_at,updated_at)
            VALUES(?,?,?,'DRAFT',?,?,'PENDING',?,?) ON CONFLICT(operation_id) DO UPDATE SET review_code=excluded.review_code,review_type=excluded.review_type,
            review_chair=excluded.review_chair,planned_date=excluded.planned_date,updated_at=excluded.updated_at""",(operation_id,code,review_type,chair,planned,stamp,stamp))
        review_id=db.execute("SELECT id FROM readiness_reviews WHERE operation_id=?",(operation_id,)).fetchone()["id"]
        db.execute("DELETE FROM readiness_gates WHERE review_id=?",(review_id,));db.execute("DELETE FROM readiness_findings WHERE review_id=?",(review_id,))
        for gate,status,evidence,reviewer,reviewed_at,reason,authority,notes in gate_rows:
            meta=expected[gate];db.execute("""INSERT INTO readiness_gates(review_id,gate_code,name,owner_role,required,status,evidence_reference,reviewer,reviewed_at,waiver_reason,waiver_authority,notes,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(review_id,gate,meta["name"],meta["owner_role"],int(meta["required"]),status,evidence,reviewer,reviewed_at,reason,authority,notes,stamp))
        for row in finding_rows:db.execute("""INSERT INTO readiness_findings(review_id,finding_code,title,severity,owner,status,due_date,disposition,acceptance_authority,notes,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(review_id,*row,stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",(operation_id,stamp,"READINESS_UPDATED",chair,f"{review_type} readiness package updated with {len(gate_rows)} gates and {len(finding_rows)} findings"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/readiness/approve")
def approve_readiness(operation_id:int):
    p=request.get_json(silent=True) or {};actor=str(p.get("approved_by","TEST DIRECTOR")).strip() or "TEST DIRECTOR";rationale=str(p.get("decision_rationale","")).strip();stamp=utc_now()
    if not rationale:return jsonify(error="final Go decision requires documented rationale"),400
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="READINESS":return jsonify(error="READINESS is not the active workflow stage"),409
        review=db.execute("SELECT * FROM readiness_reviews WHERE operation_id=?",(operation_id,)).fetchone()
        if not review:return jsonify(error="save the readiness review first"),409
        gates=[dict(x) for x in db.execute("SELECT * FROM readiness_gates WHERE review_id=? ORDER BY gate_code",(review["id"],))]
        expected={x["code"] for x in readiness_gate_catalog(operation["operation_type"])};present={x["gate_code"] for x in gates}
        missing=sorted(expected-present)
        if missing:return jsonify(error="mandatory readiness gates are missing: "+", ".join(missing)),409
        blocked=sorted(x["gate_code"] for x in gates if x["required"] and x["status"] not in {"GO","WAIVER"})
        if blocked:return jsonify(error="readiness gates are not GO: "+", ".join(blocked)),409
        prohibited=sorted(x["gate_code"] for x in gates if x["status"]=="WAIVER" and x["gate_code"] in {"SAFETY","RANGE","AIRSPACE"})
        if prohibited:return jsonify(error="safety/range/airspace gates cannot be waived: "+", ".join(prohibited)),409
        findings=[dict(x) for x in db.execute("SELECT * FROM readiness_findings WHERE review_id=? ORDER BY finding_code",(review["id"],))]
        open_findings=sorted(x["finding_code"] for x in findings if x["status"]=="OPEN")
        if open_findings:return jsonify(error="open readiness findings block approval: "+", ".join(open_findings)),409
        critical_acceptance=sorted(x["finding_code"] for x in findings if x["severity"]=="CRITICAL" and x["status"]=="ACCEPTED")
        if critical_acceptance:return jsonify(error="critical findings cannot be accepted as residual risk: "+", ".join(critical_acceptance)),409
        canonical={"schema":"SMTCS-READINESS/1","operation":operation["code"],"review":{"code":review["review_code"],"type":review["review_type"],"chair":review["review_chair"]},
                   "gates":[{k:x[k] for k in ("gate_code","status","evidence_reference","reviewer","waiver_reason","waiver_authority")} for x in gates],
                   "findings":[{k:x[k] for k in ("finding_code","severity","owner","status","disposition","acceptance_authority")} for x in findings],"decision_rationale":rationale}
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        db.execute("UPDATE readiness_reviews SET state='APPROVED',final_decision='GO',decision_rationale=?,canonical_sha256=?,approved_at=?,approved_by=?,updated_at=? WHERE id=?",(rationale,digest,stamp,actor,stamp,review["id"]))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner=?,updated_at=? WHERE operation_id=? AND section_key='READINESS'",(actor,stamp,operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',updated_at=? WHERE operation_id=? AND section_key='REHEARSAL'",(stamp,operation_id))
        db.execute("UPDATE operation_registry SET current_stage='REHEARSAL',updated_at=? WHERE id=?",(stamp,operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",(operation_id,stamp,"READINESS_APPROVED",actor,f"{review['review_type']} GO decision recorded with SHA-256 {digest}; Rehearsal unlocked"))
    return jsonify(ok=True,sha256=digest,url=url_for("operations.operation_detail",operation_id=operation_id))


@operations.post("/api/ops/<int:operation_id>/video")
def save_video_plan(operation_id:int):
    p=request.get_json(silent=True) or {};views=p.get("views",[])
    code=str(p.get("manifest_code","")).strip().upper();revision=str(p.get("revision","")).strip().upper()
    time_source=str(p.get("master_time_source","")).strip();owner=str(p.get("evidence_owner","")).strip()
    try:window=int(p.get("recording_window_seconds"))
    except (TypeError,ValueError):window=0
    if not valid_code(code) or not revision or not time_source or not owner or window<10 or window>86400:
        return jsonify(error="manifest code, revision, time source, evidence owner and valid recording window are required"),400
    if not isinstance(views,list):return jsonify(error="views must be a list"),400
    normalized=[];errors=[]
    for index,x in enumerate(views,1):
        view_code=str(x.get("view_code","")).strip().upper();name=str(x.get("name","")).strip();purpose=str(x.get("purpose","")).strip()
        camera=str(x.get("camera_device_id","")).strip().upper();mode=str(x.get("record_mode","ISO")).strip().upper()
        resolution=str(x.get("resolution","")).strip();codec=str(x.get("codec","")).strip().upper()
        sync_method=str(x.get("time_sync_method","")).strip();sync=str(x.get("time_sync_status","NOT_VERIFIED")).strip().upper()
        signal=str(x.get("signal_test_status","NOT_TESTED")).strip().upper();recording=str(x.get("recording_test_status","NOT_TESTED")).strip().upper()
        primary=str(x.get("primary_storage","")).strip();backup=str(x.get("backup_storage","")).strip();loss=str(x.get("loss_action","")).strip()
        try:
            fps=int(x.get("fps"));bitrate=float(x.get("bitrate_mbps"));pre=int(x.get("pre_roll_seconds"));post=int(x.get("post_roll_seconds"));retention=int(x.get("retention_days"))
        except (TypeError,ValueError):errors.append(f"view {index} has invalid numeric recording parameters");continue
        mandatory=bool(x.get("mandatory",True));public_safe=bool(x.get("public_safe",False))
        if not valid_code(view_code) or not name or not purpose or not camera or not resolution or not codec or not sync_method or not primary or not backup or not loss:
            errors.append(f"view {index} is missing controlled evidence fields")
        if mode not in {"ISO","PROGRAM","BOTH"} or sync not in {"NOT_VERIFIED","VERIFIED","FAILED"} or signal not in {"NOT_TESTED","PASS","FAIL"} or recording not in {"NOT_TESTED","PASS","FAIL"}:
            errors.append(f"{view_code or index} has an invalid assurance status")
        if fps<1 or fps>1000 or bitrate<=0 or pre<0 or post<0 or retention<1:errors.append(f"{view_code or index} has invalid recording capacity values")
        if mandatory and primary.casefold()==backup.casefold():errors.append(f"{view_code or index} mandatory evidence requires independent primary and backup storage")
        estimate=round(bitrate*(window+pre+post)/8/1024,3)
        normalized.append((view_code,name,purpose,camera,int(mandatory),mode,resolution,fps,codec,bitrate,pre,post,sync_method,sync,signal,recording,primary,backup,retention,estimate,loss,int(public_safe),str(x.get("notes","")).strip()))
    if errors:return jsonify(error="; ".join(errors)),400
    stamp=utc_now()
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="VIDEO":return jsonify(error="video plan can only be edited during the VIDEO stage"),409
        instrument=db.execute("SELECT state FROM instrumentation_plans WHERE operation_id=?",(operation_id,)).fetchone()
        if not instrument or instrument["state"]!="APPROVED":return jsonify(error="an approved instrumentation plan is required"),409
        existing=db.execute("SELECT * FROM video_recording_plans WHERE operation_id=?",(operation_id,)).fetchone()
        if existing and existing["state"]=="APPROVED":return jsonify(error="approved video and recording plans are immutable"),409
        runtime_id=operation["runtime_operation_id"] or OPERATION_ID
        for row in normalized:
            camera=db.execute("SELECT * FROM devices WHERE operation_id=? AND id=? AND device_type='IP-CAMERA'",(runtime_id,row[3])).fetchone()
            if not camera:return jsonify(error=f"{row[0]} references a camera not registered in Engineering Setup"),409
        db.execute("""INSERT INTO video_recording_plans(operation_id,manifest_code,revision,state,master_time_source,recording_window_seconds,evidence_owner,notes,created_at,updated_at)
            VALUES(?,?,?,'DRAFT',?,?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET manifest_code=excluded.manifest_code,revision=excluded.revision,
            master_time_source=excluded.master_time_source,recording_window_seconds=excluded.recording_window_seconds,evidence_owner=excluded.evidence_owner,
            notes=excluded.notes,updated_at=excluded.updated_at""",(operation_id,code,revision,time_source,window,owner,str(p.get("notes","")).strip(),stamp,stamp))
        plan_id=db.execute("SELECT id FROM video_recording_plans WHERE operation_id=?",(operation_id,)).fetchone()["id"]
        db.execute("DELETE FROM camera_view_requirements WHERE plan_id=?",(plan_id,))
        for row in normalized:db.execute("""INSERT INTO camera_view_requirements(plan_id,view_code,name,purpose,camera_device_id,mandatory,record_mode,resolution,fps,codec,
            bitrate_mbps,pre_roll_seconds,post_roll_seconds,time_sync_method,time_sync_status,signal_test_status,recording_test_status,primary_storage,backup_storage,
            retention_days,estimated_storage_gb,loss_action,public_safe,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(plan_id,*row,stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id,stamp,"VIDEO_PLAN_UPDATED",owner,f"Video evidence manifest {code}/{revision} saved with {len(normalized)} views"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/video/approve")
def approve_video_plan(operation_id:int):
    actor=str((request.get_json(silent=True) or {}).get("approved_by","DATA & VIDEO LEAD")).strip() or "DATA & VIDEO LEAD";stamp=utc_now()
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="VIDEO":return jsonify(error="VIDEO is not the active workflow stage"),409
        plan=db.execute("SELECT * FROM video_recording_plans WHERE operation_id=?",(operation_id,)).fetchone()
        if not plan:return jsonify(error="save the video and recording plan first"),409
        baseline=db.execute("SELECT id FROM configuration_baselines WHERE operation_id=? AND state='RELEASED'",(operation_id,)).fetchone()
        baseline_ref=db.execute("SELECT reference,revision FROM baseline_items WHERE baseline_id=? AND item_type='CAMERA_MANIFEST'",(baseline["id"],)).fetchone() if baseline else None
        if not baseline_ref or baseline_ref["reference"].upper()!=plan["manifest_code"] or baseline_ref["revision"].upper()!=plan["revision"]:
            return jsonify(error="video manifest identity does not match the released CAMERA_MANIFEST baseline item"),409
        rows=[dict(x) for x in db.execute("SELECT * FROM camera_view_requirements WHERE plan_id=? ORDER BY view_code",(plan["id"],))]
        present={x["view_code"] for x in rows if x["mandatory"]}
        missing=sorted(video_view_requirements(operation["operation_type"])-present)
        if missing:return jsonify(error="mandatory camera views are missing: "+", ".join(missing)),409
        failed=sorted(x["view_code"] for x in rows if x["mandatory"] and (x["signal_test_status"]!="PASS" or x["recording_test_status"]!="PASS" or x["time_sync_status"]!="VERIFIED"))
        if failed:return jsonify(error="mandatory views have not passed signal, recording and time-sync verification: "+", ".join(failed)),409
        canonical={"schema":"SMTCS-VIDEO-EVIDENCE/1","operation":operation["code"],"manifest":{"code":plan["manifest_code"],"revision":plan["revision"],"time_source":plan["master_time_source"],"window":plan["recording_window_seconds"]},
                   "views":[{k:x[k] for k in ("view_code","camera_device_id","record_mode","resolution","fps","codec","bitrate_mbps","pre_roll_seconds","post_roll_seconds","time_sync_method","primary_storage","backup_storage","retention_days","loss_action","public_safe")} for x in rows]}
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        db.execute("UPDATE video_recording_plans SET state='APPROVED',canonical_sha256=?,approved_at=?,approved_by=?,updated_at=? WHERE id=?",(digest,stamp,actor,stamp,plan["id"]))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner=?,updated_at=? WHERE operation_id=? AND section_key='VIDEO'",(actor,stamp,operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',updated_at=? WHERE operation_id=? AND section_key='READINESS'",(stamp,operation_id))
        db.execute("UPDATE operation_registry SET current_stage='READINESS',updated_at=? WHERE id=?",(stamp,operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id,stamp,"VIDEO_PLAN_APPROVED",actor,f"Video evidence plan approved with SHA-256 {digest}; Readiness Review unlocked"))
    return jsonify(ok=True,sha256=digest,url=url_for("operations.operation_detail",operation_id=operation_id))


@operations.post("/api/ops/<int:operation_id>/instrumentation")
def save_instrumentation(operation_id: int):
    p = request.get_json(silent=True) or {}; measurements = p.get("measurements", [])
    code = str(p.get("plan_code", "")).strip().upper(); revision = str(p.get("revision", "")).strip().upper()
    time_source = str(p.get("time_source", "")).strip(); mode = str(p.get("acquisition_mode", "")).strip().upper()
    if not valid_code(code) or not revision or not time_source or mode not in {"LIVE_ETHERNET", "LOCAL_LOGGER", "HYBRID"}:
        return jsonify(error="plan code, revision, time source and valid acquisition mode are required"), 400
    if not isinstance(measurements, list): return jsonify(error="measurements must be a list"), 400
    normalized, errors = [], []
    for index, x in enumerate(measurements, 1):
        measurement_code = str(x.get("measurement_code", "")).strip().upper(); name = str(x.get("name", "")).strip()
        category = str(x.get("category", "")).strip().upper(); criticality = str(x.get("criticality", "REQUIRED")).strip().upper()
        device_id = str(x.get("device_id", "")).strip().upper(); channel_id = str(x.get("channel_id", "")).strip()
        unit = str(x.get("unit", "")).strip(); accuracy = str(x.get("required_accuracy", "")).strip()
        calibration = str(x.get("calibration_reference", "")).strip(); calibration_due = str(x.get("calibration_due", "")).strip()
        redundancy = str(x.get("redundancy", "NONE")).strip().upper(); e2e = str(x.get("e2e_status", "NOT_TESTED")).strip().upper()
        try:
            minimum=float(x.get("engineering_min")); maximum=float(x.get("engineering_max")); rate=int(x.get("sample_rate_hz"))
            warning=float(x["warning_limit"]) if x.get("warning_limit") not in {None,""} else None
            critical=float(x["critical_limit"]) if x.get("critical_limit") not in {None,""} else None
            abort=float(x["abort_limit"]) if x.get("abort_limit") not in {None,""} else None
        except (TypeError,ValueError): errors.append(f"measurement {index} has invalid numeric values"); continue
        if not valid_code(measurement_code) or not name or not category or not device_id or not channel_id or not unit or not accuracy or not calibration or not calibration_due:
            errors.append(f"measurement {index} is missing controlled engineering fields")
        if criticality not in {"REQUIRED", "SAFETY_CRITICAL", "OPTIONAL"} or redundancy not in {"NONE", "MONITORED", "DUAL", "TRIPLE"} or e2e not in {"NOT_TESTED", "PASS", "FAIL"}:
            errors.append(f"{measurement_code or index} has an invalid assurance status")
        if minimum >= maximum or rate < 1 or rate > 100000: errors.append(f"{measurement_code or index} has an invalid engineering range or sample rate")
        ordered = [v for v in (warning,critical,abort) if v is not None]
        if ordered != sorted(ordered) or any(v < minimum or v > maximum for v in ordered): errors.append(f"{measurement_code or index} limits must be ordered and within engineering range")
        normalized.append((measurement_code,name,category,criticality,device_id,channel_id,unit,minimum,maximum,rate,accuracy,
                           calibration,calibration_due,warning,critical,abort,redundancy,e2e,int(criticality!="OPTIONAL"),str(x.get("notes","")).strip()))
    if errors: return jsonify(error="; ".join(errors)), 400
    stamp=utc_now()
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="INSTRUMENTATION":return jsonify(error="instrumentation can only be edited during the INSTRUMENTATION stage"),409
        procedure=db.execute("SELECT state FROM operation_procedures WHERE operation_id=?",(operation_id,)).fetchone()
        if not procedure or procedure["state"]!="APPROVED":return jsonify(error="an approved procedure is required"),409
        existing=db.execute("SELECT * FROM instrumentation_plans WHERE operation_id=?",(operation_id,)).fetchone()
        if existing and existing["state"]=="APPROVED":return jsonify(error="approved instrumentation plans are immutable"),409
        runtime_id=operation["runtime_operation_id"] or OPERATION_ID
        for row in normalized:
            device=db.execute("SELECT * FROM devices WHERE operation_id=? AND id=?",(runtime_id,row[4])).fetchone()
            channel=db.execute("""SELECT c.*,COALESCE(l.enabled,1) enabled FROM channels c LEFT JOIN channel_lifecycle l ON l.operation_id=c.operation_id AND l.channel_id=c.id
                WHERE c.operation_id=? AND c.id=?""",(runtime_id,row[5])).fetchone()
            if not device or not channel:return jsonify(error=f"{row[0]} references an unknown device or channel"),409
            if channel["source_id"]!=row[4]:return jsonify(error=f"{row[0]} channel is not sourced by selected device"),409
            if not channel["enabled"]:return jsonify(error=f"{row[0]} channel is archived or disabled"),409
            if channel["unit"]!=row[6]:return jsonify(error=f"{row[0]} unit does not match channel registry ({channel['unit']})"),409
            if row[9]>channel["sample_rate"]:return jsonify(error=f"{row[0]} requested rate exceeds configured channel rate"),409
        db.execute("""INSERT INTO instrumentation_plans(operation_id,plan_code,revision,state,time_source,acquisition_mode,notes,created_at,updated_at)
            VALUES(?,?,?,'DRAFT',?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET plan_code=excluded.plan_code,revision=excluded.revision,
            time_source=excluded.time_source,acquisition_mode=excluded.acquisition_mode,notes=excluded.notes,updated_at=excluded.updated_at""",
            (operation_id,code,revision,time_source,mode,str(p.get("notes","")).strip(),stamp,stamp))
        plan_id=db.execute("SELECT id FROM instrumentation_plans WHERE operation_id=?",(operation_id,)).fetchone()["id"]
        db.execute("DELETE FROM measurement_requirements WHERE plan_id=?",(plan_id,))
        for row in normalized:db.execute("""INSERT INTO measurement_requirements(plan_id,measurement_code,name,category,criticality,device_id,channel_id,unit,
            engineering_min,engineering_max,sample_rate_hz,required_accuracy,calibration_reference,calibration_due,warning_limit,critical_limit,
            abort_limit,redundancy,e2e_status,required,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(plan_id,*row,stamp))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id,stamp,"INSTRUMENTATION_UPDATED","INSTRUMENTATION LEAD",f"Instrumentation plan {code}/{revision} saved with {len(normalized)} measurements"))
    return jsonify(ok=True)


@operations.post("/api/ops/<int:operation_id>/instrumentation/approve")
def approve_instrumentation(operation_id:int):
    actor=str((request.get_json(silent=True) or {}).get("approved_by","INSTRUMENTATION LEAD")).strip() or "INSTRUMENTATION LEAD";stamp=utc_now()
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"]!="INSTRUMENTATION":return jsonify(error="INSTRUMENTATION is not the active workflow stage"),409
        plan=db.execute("SELECT * FROM instrumentation_plans WHERE operation_id=?",(operation_id,)).fetchone()
        if not plan:return jsonify(error="save the instrumentation plan first"),409
        baseline=db.execute("SELECT id FROM configuration_baselines WHERE operation_id=? AND state='RELEASED'",(operation_id,)).fetchone()
        baseline_ref=db.execute("SELECT reference,revision FROM baseline_items WHERE baseline_id=? AND item_type='CHANNEL_MAP'",(baseline["id"],)).fetchone() if baseline else None
        if not baseline_ref or baseline_ref["reference"].upper()!=plan["plan_code"] or baseline_ref["revision"].upper()!=plan["revision"]:
            return jsonify(error="instrumentation plan identity does not match the released CHANNEL_MAP baseline item"),409
        rows=[dict(x) for x in db.execute("SELECT * FROM measurement_requirements WHERE plan_id=? ORDER BY measurement_code",(plan["id"],))]
        present={x["measurement_code"] for x in rows if x["required"]}
        missing=sorted(instrumentation_requirements(operation["operation_type"])-present)
        if missing:return jsonify(error="mandatory measurements are missing: "+", ".join(missing)),409
        untested=sorted(x["measurement_code"] for x in rows if x["required"] and x["e2e_status"]!="PASS")
        if untested:return jsonify(error="required measurements have not passed end-to-end test: "+", ".join(untested)),409
        overdue=sorted(x["measurement_code"] for x in rows if x["required"] and x["calibration_due"]<stamp[:10])
        if overdue:return jsonify(error="calibration is expired: "+", ".join(overdue)),409
        unsafe=sorted(x["measurement_code"] for x in rows if x["criticality"]=="SAFETY_CRITICAL" and x["abort_limit"] is None)
        if unsafe:return jsonify(error="safety-critical measurements require abort limits: "+", ".join(unsafe)),409
        canonical={"schema":"SMTCS-INSTRUMENTATION/1","operation":operation["code"],"plan":{"code":plan["plan_code"],"revision":plan["revision"],"time_source":plan["time_source"],"mode":plan["acquisition_mode"]},
                   "measurements":[{k:x[k] for k in ("measurement_code","device_id","channel_id","unit","engineering_min","engineering_max","sample_rate_hz","required_accuracy","calibration_reference","calibration_due","warning_limit","critical_limit","abort_limit","redundancy")} for x in rows]}
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        db.execute("UPDATE instrumentation_plans SET state='APPROVED',canonical_sha256=?,approved_at=?,approved_by=?,updated_at=? WHERE id=?",(digest,stamp,actor,stamp,plan["id"]))
        db.execute("UPDATE operation_workflow_sections SET status='COMPLETE',owner=?,updated_at=? WHERE operation_id=? AND section_key='INSTRUMENTATION'",(actor,stamp,operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',updated_at=? WHERE operation_id=? AND section_key='VIDEO'",(stamp,operation_id))
        db.execute("UPDATE operation_registry SET current_stage='VIDEO',updated_at=? WHERE id=?",(stamp,operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id,stamp,"INSTRUMENTATION_APPROVED",actor,f"Instrumentation plan approved with SHA-256 {digest}; Video & Recording unlocked"))
    return jsonify(ok=True,sha256=digest,url=url_for("operations.operation_detail",operation_id=operation_id))


def validate_procedure_steps(steps: list, assigned_roles: set[str]) -> tuple[list, list[str]]:
    normalized, errors, sequences, codes = [], [], set(), set()
    meaningful=lambda value:bool(value and value.strip().lower() not in {"-","n/a","na","none","tbd"})
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
        if not meaningful(instruction) or not meaningful(evidence): errors.append(f"{code or index} requires a meaningful instruction and expected evidence; placeholders such as '-' are not accepted")
        if responsible not in assigned_roles: errors.append(f"{code or index} responsible role is not assigned")
        if mode not in {"SELF", "TWO_PERSON", "AUTOMATED"}: errors.append(f"{code or index} has an invalid verification mode")
        if mode == "TWO_PERSON" and (not verifier or verifier not in assigned_roles or verifier == responsible):
            errors.append(f"{code or index} requires a different assigned verifier")
        if critical and (not meaningful(abort) or mode == "SELF"): errors.append(f"{code or index} safety-critical step requires an explicit abort/safe action and independent/automated verification")
        if step_type == "HOLD_POINT" and not meaningful(hold): errors.append(f"{code or index} hold point requires an explicit release condition")
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
        if not baseline_ref:
            return jsonify(error="released baseline has no PROCEDURE identity; create a new operation or controlled baseline revision"), 409
        expected_code=baseline_ref["reference"].strip().upper();expected_revision=baseline_ref["revision"].strip().upper()
        if expected_code != procedure["document_code"] or expected_revision != procedure["revision"]:
            return jsonify(error=f"procedure identity mismatch: baseline requires {expected_code} / {expected_revision}, but draft contains {procedure['document_code']} / {procedure['revision']}"), 409
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


def next_controlled_revision(value:str)->str:
    match=re.fullmatch(r"REV-([A-Z])",value.strip().upper())
    if match and match.group(1)!="Z":return f"REV-{chr(ord(match.group(1))+1)}"
    match=re.fullmatch(r"REV-(\d+)",value.strip().upper())
    if match:return f"REV-{int(match.group(1))+1}"
    return "REV-A"


@operations.post("/api/ops/<int:operation_id>/baseline/revise")
def revise_baseline(operation_id:int):
    p=request.get_json(silent=True) or {};reason=str(p.get("reason","")).strip();actor=str(p.get("requested_by","CONFIGURATION MANAGER")).strip() or "CONFIGURATION MANAGER";stamp=utc_now()
    if len(reason)<12:return jsonify(error="a specific revision reason of at least 12 characters is required"),400
    with connect() as db:
        operation=db.execute("SELECT * FROM operation_registry WHERE id=?",(operation_id,)).fetchone()
        if not operation:return jsonify(error="operation not found"),404
        if operation["current_stage"] not in {"TEAM","PROCEDURE"}:return jsonify(error="baseline rework is only permitted before procedure approval"),409
        baseline=db.execute("SELECT * FROM configuration_baselines WHERE operation_id=?",(operation_id,)).fetchone()
        procedure=db.execute("SELECT state FROM operation_procedures WHERE operation_id=?",(operation_id,)).fetchone()
        if not baseline or baseline["state"]!="RELEASED":return jsonify(error="a released baseline is required for controlled revision"),409
        if procedure and procedure["state"]=="APPROVED":return jsonify(error="procedure is already approved; use formal change control instead"),409
        items=[dict(x) for x in db.execute("SELECT * FROM baseline_items WHERE baseline_id=? ORDER BY item_type",(baseline["id"],))]
        snapshot={"schema":"SMTCS-BASELINE-SNAPSHOT/1","baseline":{k:baseline[k] for k in ("baseline_code","revision","canonical_sha256","released_at","released_by","notes")},"items":[{k:x[k] for k in ("item_type","reference","revision","required","verification_status","source","notes")} for x in items]}
        db.execute("INSERT INTO configuration_baseline_history(operation_id,baseline_code,revision,canonical_sha256,snapshot_json,superseded_reason,superseded_by,superseded_at) VALUES(?,?,?,?,?,?,?,?)",
                   (operation_id,baseline["baseline_code"],baseline["revision"],baseline["canonical_sha256"],json.dumps(snapshot,sort_keys=True),reason,actor,stamp))
        new_revision=next_controlled_revision(baseline["revision"])
        db.execute("UPDATE configuration_baselines SET revision=?,state='DRAFT',notes=?,canonical_sha256=NULL,released_at=NULL,released_by=NULL,updated_at=? WHERE id=?",
                   (new_revision,f"{baseline['notes'] or ''}\nREVISION REASON: {reason}".strip(),stamp,baseline["id"]))
        db.execute("UPDATE staffing_plans SET state='DRAFT',approved_at=NULL,approved_by=NULL,updated_at=? WHERE operation_id=?",(stamp,operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='LOCKED',blocker='Superseded baseline requires revalidation',updated_at=? WHERE operation_id=? AND sequence>3",(stamp,operation_id))
        db.execute("UPDATE operation_workflow_sections SET status='ACTIVE',blocker=NULL,owner=?,updated_at=? WHERE operation_id=? AND section_key='BASELINE'",(actor,stamp,operation_id))
        db.execute("UPDATE operation_registry SET current_stage='BASELINE',status='CONTROLLED REWORK',updated_at=? WHERE id=?",(stamp,operation_id))
        db.execute("INSERT INTO operation_activity(operation_id,occurred_at,activity_type,actor,message) VALUES(?,?,?,?,?)",
                   (operation_id,stamp,"BASELINE_REVISION_OPENED",actor,f"Baseline {baseline['baseline_code']}/{baseline['revision']} superseded; {new_revision} opened: {reason}"))
    return jsonify(ok=True,revision=new_revision,url=url_for("operations.baseline_builder",operation_id=operation_id))


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
        placeholders={"","-","UNASSIGNED","WORKING","TBD","N/A","NA","NONE"}
        invalid=sorted(x["item_type"] for x in items if x["required"] and (x["reference"].strip().upper() in placeholders or x["revision"].strip().upper() in placeholders or x["source"].strip().upper() in placeholders))
        if invalid:return jsonify(error="required baseline items contain placeholder identity values: "+", ".join(invalid)),409
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
