from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .audit_integrity import append_audit_record


SEVERITIES = {"P1", "P2", "P3", "P4"}
CATEGORIES = {
    "SAFETY",
    "PROPULSION",
    "INSTRUMENTATION",
    "VIDEO",
    "NETWORK",
    "PROCEDURE",
    "FACILITY",
    "OTHER",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_incident_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS incidents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        run_id INTEGER,
        incident_code TEXT UNIQUE,
        opened_at TEXT NOT NULL,
        severity TEXT NOT NULL,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        status TEXT NOT NULL,
        source_alarm_id INTEGER,
        owner TEXT NOT NULL,
        containment TEXT,
        root_cause TEXT,
        resolution TEXT,
        resolved_at TEXT,
        closed_at TEXT,
        updated_at TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_source_alarm
        ON incidents(operation_id,source_alarm_id)
        WHERE source_alarm_id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS incident_actions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        incident_id INTEGER NOT NULL,
        occurred_at TEXT NOT NULL,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        from_status TEXT NOT NULL,
        to_status TEXT NOT NULL,
        notes TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_incidents_operation
        ON incidents(operation_id,id DESC);
    """)


def _next_code(db: sqlite3.Connection, operation_id: str, stamp: str) -> str:
    day = stamp[:10].replace("-", "")
    count = db.execute(
        """SELECT count(*) FROM incidents
           WHERE operation_id=? AND incident_code LIKE ?""",
        (operation_id, f"INC-{day}-%"),
    ).fetchone()[0]
    return f"INC-{day}-{count + 1:03d}"


def create_incident(
    db: sqlite3.Connection,
    *,
    operation_id: str,
    severity: str,
    category: str,
    title: str,
    description: str,
    owner: str,
    source_alarm_id: int | None = None,
) -> dict:
    ensure_incident_schema(db)
    severity = severity.upper()
    category = category.upper()
    if severity not in SEVERITIES:
        raise ValueError("invalid incident severity")
    if category not in CATEGORIES:
        raise ValueError("invalid incident category")
    stamp = utc_now()
    run = db.execute(
        "SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    code = _next_code(db, operation_id, stamp)
    cursor = db.execute(
        """INSERT INTO incidents(
               operation_id,run_id,incident_code,opened_at,severity,category,
               title,description,status,source_alarm_id,owner,updated_at)
           VALUES(?,?,?,?,?,?,?,?,'OPEN',?,?,?)""",
        (
            operation_id,
            run["id"] if run else None,
            code,
            stamp,
            severity,
            category,
            title,
            description,
            source_alarm_id,
            owner,
            stamp,
        ),
    )
    incident_id = cursor.lastrowid
    action_cursor = db.execute(
        """INSERT INTO incident_actions(
               operation_id,incident_id,occurred_at,action,actor,
               from_status,to_status,notes)
           VALUES(?,?,?,'OPEN',?,'NEW','OPEN',?)""",
        (operation_id, incident_id, stamp, owner, description),
    )
    append_audit_record(
        db,
        operation_id=operation_id,
        run_id=run["id"] if run else None,
        record_type="INCIDENT_ACTION",
        record_id=str(action_cursor.lastrowid),
        payload={"incident_id": incident_id, "incident_code": code, "action": "OPEN", "from_status": "NEW", "to_status": "OPEN", "actor": owner, "notes": description},
        occurred_at=stamp,
    )
    row = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    return dict(row)


def synchronize_critical_alarms(db: sqlite3.Connection, operation_id: str) -> list[dict]:
    """Promote each active P1 alarm into one tracked operational incident."""
    ensure_incident_schema(db)
    created = []
    alarms = db.execute(
        """SELECT * FROM alarms
           WHERE operation_id=? AND priority='P1' AND state!='CLOSED'
           ORDER BY id""",
        (operation_id,),
    ).fetchall()
    for alarm in alarms:
        exists = db.execute(
            """SELECT 1 FROM incidents
               WHERE operation_id=? AND source_alarm_id=?""",
            (operation_id, alarm["id"]),
        ).fetchone()
        if not exists:
            created.append(
                create_incident(
                    db,
                    operation_id=operation_id,
                    severity="P1",
                    category="INSTRUMENTATION",
                    title=f"Critical alarm from {alarm['source']}",
                    description=alarm["message"],
                    owner="TEST DIRECTOR",
                    source_alarm_id=alarm["id"],
                )
            )
    return created


def apply_incident_action(
    db: sqlite3.Connection,
    *,
    operation_id: str,
    incident_id: int,
    action: str,
    actor: str,
    notes: str,
) -> dict:
    ensure_incident_schema(db)
    incident = db.execute(
        "SELECT * FROM incidents WHERE operation_id=? AND id=?",
        (operation_id, incident_id),
    ).fetchone()
    if not incident:
        raise LookupError("incident not found")

    action = action.upper()
    allowed = {
        "CONTAIN": ({"OPEN", "REOPENED"}, "CONTAINED"),
        "RESOLVE": ({"OPEN", "REOPENED", "CONTAINED"}, "RESOLVED"),
        "CLOSE": ({"RESOLVED"}, "CLOSED"),
        "REOPEN": ({"RESOLVED", "CLOSED"}, "REOPENED"),
    }
    if action not in allowed:
        raise ValueError("invalid incident action")
    from_states, target = allowed[action]
    if incident["status"] not in from_states:
        raise ValueError(
            f"{action} is not valid from incident state {incident['status']}"
        )
    if not notes.strip():
        raise ValueError("incident action notes are required")

    stamp = utc_now()
    fields = ["status=?", "updated_at=?"]
    values: list[object] = [target, stamp]
    if action == "CONTAIN":
        fields.append("containment=?")
        values.append(notes.strip())
    elif action == "RESOLVE":
        fields.extend(["resolution=?", "resolved_at=?"])
        values.extend([notes.strip(), stamp])
    elif action == "CLOSE":
        fields.append("closed_at=?")
        values.append(stamp)
    elif action == "REOPEN":
        fields.extend(["resolved_at=NULL", "closed_at=NULL"])
    values.extend([operation_id, incident_id])
    db.execute(
        f"UPDATE incidents SET {','.join(fields)} WHERE operation_id=? AND id=?",
        values,
    )
    action_cursor = db.execute(
        """INSERT INTO incident_actions(
               operation_id,incident_id,occurred_at,action,actor,
               from_status,to_status,notes)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            incident_id,
            stamp,
            action,
            actor,
            incident["status"],
            target,
            notes.strip(),
        ),
    )
    append_audit_record(
        db,
        operation_id=operation_id,
        run_id=incident["run_id"],
        record_type="INCIDENT_ACTION",
        record_id=str(action_cursor.lastrowid),
        payload={"incident_id": incident_id, "incident_code": incident["incident_code"], "action": action, "from_status": incident["status"], "to_status": target, "actor": actor, "notes": notes.strip()},
        occurred_at=stamp,
    )
    row = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    return dict(row)
