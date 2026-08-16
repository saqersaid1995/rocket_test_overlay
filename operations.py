"""Stellar Kinetics operations module.

This blueprint is intentionally isolated from Rocket Overlay Studio. It owns its
routes, persistence, and templates under /operations.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
OPERATIONS_DB = BASE_DIR / "workspace" / "operations.db"

operations_bp = Blueprint("operations", __name__, url_prefix="/operations")

PROGRAM_STATUSES = ("Planning", "Active", "On Hold", "Completed", "Archived")
PROGRAM_TYPES = (
    "Launch Vehicle",
    "Propulsion",
    "Flight Demonstration",
    "Customer Mission",
    "Research & Development",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db() -> sqlite3.Connection:
    OPERATIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(OPERATIONS_DB)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_operations_db() -> None:
    with _db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS programs (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL,
                program_type TEXT NOT NULL,
                status TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT '',
                objective TEXT NOT NULL DEFAULT '',
                start_date TEXT,
                target_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            );

            CREATE TABLE IF NOT EXISTS operation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_program_status
                ON programs(status);
            CREATE INDEX IF NOT EXISTS idx_operation_events_entity
                ON operation_events(entity_type, entity_id, created_at DESC);
            """
        )


def _record_event(
    connection: sqlite3.Connection,
    program_id: str,
    action: str,
    summary: str,
) -> None:
    connection.execute(
        """
        INSERT INTO operation_events
            (entity_type, entity_id, action, summary, created_at)
        VALUES ('program', ?, ?, ?, ?)
        """,
        (program_id, action, summary, _now()),
    )


def _program_or_404(program_id: str) -> sqlite3.Row:
    with _db() as connection:
        program = connection.execute(
            "SELECT * FROM programs WHERE id = ?", (program_id,)
        ).fetchone()
    if program is None:
        abort(404)
    return program


def _form_values() -> dict[str, str | None]:
    values = {
        "code": request.form.get("code", "").strip().upper(),
        "name": request.form.get("name", "").strip(),
        "program_type": request.form.get("program_type", "").strip(),
        "status": request.form.get("status", "").strip(),
        "owner": request.form.get("owner", "").strip(),
        "objective": request.form.get("objective", "").strip(),
        "start_date": request.form.get("start_date", "").strip() or None,
        "target_date": request.form.get("target_date", "").strip() or None,
    }
    errors: list[str] = []
    if not values["code"]:
        errors.append("Program code is required.")
    if not values["name"]:
        errors.append("Program name is required.")
    if values["program_type"] not in PROGRAM_TYPES:
        errors.append("Select a valid program type.")
    if values["status"] not in PROGRAM_STATUSES[:-1]:
        errors.append("Select a valid program status.")
    if values["start_date"] and values["target_date"]:
        if values["target_date"] < values["start_date"]:
            errors.append("Target date cannot be earlier than start date.")
    return values, errors


@operations_bp.get("/")
def dashboard():
    init_operations_db()
    with _db() as connection:
        counts = {
            row["status"]: row["total"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS total FROM programs GROUP BY status"
            )
        }
        programs = connection.execute(
            """
            SELECT * FROM programs
            WHERE archived_at IS NULL
            ORDER BY
                CASE status
                    WHEN 'Active' THEN 1
                    WHEN 'Planning' THEN 2
                    WHEN 'On Hold' THEN 3
                    WHEN 'Completed' THEN 4
                    ELSE 5
                END,
                updated_at DESC
            LIMIT 6
            """
        ).fetchall()
        events = connection.execute(
            """
            SELECT e.*, p.code, p.name
            FROM operation_events e
            LEFT JOIN programs p ON p.id = e.entity_id
            ORDER BY e.created_at DESC
            LIMIT 8
            """
        ).fetchall()
    return render_template(
        "operations/dashboard.html",
        counts=counts,
        programs=programs,
        events=events,
        today=date.today().isoformat(),
    )


@operations_bp.get("/programs")
def programs():
    init_operations_db()
    status = request.args.get("status", "").strip()
    search = request.args.get("q", "").strip()
    conditions = ["1 = 1"]
    parameters: list[str] = []
    if status == "Archived":
        conditions.append("archived_at IS NOT NULL")
    else:
        conditions.append("archived_at IS NULL")
        if status in PROGRAM_STATUSES:
            conditions.append("status = ?")
            parameters.append(status)
    if search:
        conditions.append("(code LIKE ? OR name LIKE ? OR owner LIKE ?)")
        token = f"%{search}%"
        parameters.extend((token, token, token))
    with _db() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM programs
            WHERE {' AND '.join(conditions)}
            ORDER BY updated_at DESC
            """,
            parameters,
        ).fetchall()
    return render_template(
        "operations/programs.html",
        programs=rows,
        statuses=PROGRAM_STATUSES,
        selected_status=status,
        search=search,
    )


@operations_bp.route("/programs/new", methods=["GET", "POST"])
def new_program():
    values = {
        "code": "",
        "name": "",
        "program_type": "Launch Vehicle",
        "status": "Planning",
        "owner": "",
        "objective": "",
        "start_date": "",
        "target_date": "",
    }
    errors: list[str] = []
    if request.method == "POST":
        values, errors = _form_values()
        if not errors:
            program_id = uuid.uuid4().hex
            now = _now()
            try:
                with _db() as connection:
                    connection.execute(
                        """
                        INSERT INTO programs (
                            id, code, name, program_type, status, owner,
                            objective, start_date, target_date,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            program_id, values["code"], values["name"],
                            values["program_type"], values["status"],
                            values["owner"], values["objective"],
                            values["start_date"], values["target_date"], now, now,
                        ),
                    )
                    _record_event(
                        connection, program_id, "created",
                        f"Program {values['code']} created",
                    )
            except sqlite3.IntegrityError:
                errors.append("This program code is already in use.")
            else:
                flash("Program created successfully.", "success")
                return redirect(
                    url_for("operations.program_detail", program_id=program_id)
                )
    return render_template(
        "operations/program_form.html",
        title="Create program",
        values=values,
        errors=errors,
        program_types=PROGRAM_TYPES,
        statuses=PROGRAM_STATUSES[:-1],
    )


@operations_bp.get("/programs/<program_id>")
def program_detail(program_id: str):
    program = _program_or_404(program_id)
    with _db() as connection:
        events = connection.execute(
            """
            SELECT * FROM operation_events
            WHERE entity_type = 'program' AND entity_id = ?
            ORDER BY created_at DESC
            """,
            (program_id,),
        ).fetchall()
    return render_template(
        "operations/program_detail.html", program=program, events=events
    )


@operations_bp.route("/programs/<program_id>/edit", methods=["GET", "POST"])
def edit_program(program_id: str):
    program = _program_or_404(program_id)
    values = dict(program)
    errors: list[str] = []
    if request.method == "POST":
        values, errors = _form_values()
        if not errors:
            now = _now()
            try:
                with _db() as connection:
                    connection.execute(
                        """
                        UPDATE programs
                        SET code = ?, name = ?, program_type = ?, status = ?,
                            owner = ?, objective = ?, start_date = ?,
                            target_date = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            values["code"], values["name"], values["program_type"],
                            values["status"], values["owner"], values["objective"],
                            values["start_date"], values["target_date"], now,
                            program_id,
                        ),
                    )
                    _record_event(
                        connection, program_id, "updated",
                        f"Program {values['code']} updated",
                    )
            except sqlite3.IntegrityError:
                errors.append("This program code is already in use.")
            else:
                flash("Program updated successfully.", "success")
                return redirect(
                    url_for("operations.program_detail", program_id=program_id)
                )
    return render_template(
        "operations/program_form.html",
        title="Edit program",
        values=values,
        errors=errors,
        program_types=PROGRAM_TYPES,
        statuses=PROGRAM_STATUSES[:-1],
        program=program,
    )


@operations_bp.post("/programs/<program_id>/archive")
def archive_program(program_id: str):
    program = _program_or_404(program_id)
    if program["archived_at"] is None:
        now = _now()
        with _db() as connection:
            connection.execute(
                """
                UPDATE programs
                SET status = 'Archived', archived_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, program_id),
            )
            _record_event(
                connection, program_id, "archived",
                f"Program {program['code']} archived",
            )
        flash("Program archived.", "success")
    return redirect(url_for("operations.programs"))
