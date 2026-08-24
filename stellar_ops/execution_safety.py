from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

BOOT_ID = str(uuid.uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_execution_safety_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS runtime_boot(
        id INTEGER PRIMARY KEY CHECK(id=1),
        boot_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        reconciled_state TEXT);
    CREATE TABLE IF NOT EXISTS command_journal(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        command_id TEXT NOT NULL UNIQUE,
        requested_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        action TEXT NOT NULL,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT,
        http_status INTEGER NOT NULL,
        response_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_command_journal_operation
        ON command_journal(operation_id,id DESC);
    """)


def reconcile_runtime_boot(db: sqlite3.Connection, operation_id: str) -> dict:
    """Fail safe when a process restart interrupts a transient execution state."""
    ensure_execution_safety_schema(db)
    previous = db.execute("SELECT * FROM runtime_boot WHERE id=1").fetchone()
    if previous and previous["boot_id"] == BOOT_ID:
        return {"reconciled": False, "boot_id": BOOT_ID}

    operation = db.execute(
        "SELECT state FROM operations WHERE id=?", (operation_id,)
    ).fetchone()
    previous_state = operation["state"] if operation else None
    reconciled_state = previous_state
    interrupted = previous_state in {"COUNTDOWN", "FIRING"}
    stamp = utc_now()

    if interrupted:
        reason = (
            f"Server restart detected during {previous_state}; "
            "execution placed in fail-safe HOLD"
        )
        db.execute(
            """UPDATE operations
               SET state='HOLD',prior_state='CHECKOUT',active_hold=?,
                   firing_started_monotonic=NULL,updated_at=?
               WHERE id=?""",
            (reason, stamp, operation_id),
        )
        run = db.execute(
            "SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        db.execute(
            """INSERT INTO events(
                   operation_id,occurred_at,event_type,source,severity,message,run_id)
               VALUES(?,?,'RUNTIME_RECOVERY','SYSTEM','CRITICAL',?,?)""",
            (operation_id, stamp, reason, run["id"] if run else None),
        )
        reconciled_state = "HOLD"

    db.execute(
        """INSERT INTO runtime_boot(id,boot_id,started_at,reconciled_state)
           VALUES(1,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             boot_id=excluded.boot_id,
             started_at=excluded.started_at,
             reconciled_state=excluded.reconciled_state""",
        (BOOT_ID, stamp, reconciled_state),
    )
    return {
        "reconciled": interrupted,
        "boot_id": BOOT_ID,
        "previous_state": previous_state,
        "state": reconciled_state,
    }


def command_id_from_request(header_value: str | None, body_value: str | None) -> str:
    value = (header_value or body_value or "").strip()
    return value[:128] if value else str(uuid.uuid4())


def previous_command(db: sqlite3.Connection, command_id: str) -> tuple[dict, int] | None:
    ensure_execution_safety_schema(db)
    row = db.execute(
        "SELECT response_json,http_status FROM command_journal WHERE command_id=?",
        (command_id,),
    ).fetchone()
    if not row:
        return None
    payload = json.loads(row["response_json"])
    payload["replayed"] = True
    return payload, row["http_status"]


def record_command(
    db: sqlite3.Connection,
    *,
    operation_id: str,
    command_id: str,
    action: str,
    from_state: str,
    to_state: str,
    outcome: str,
    reason: str | None,
    http_status: int,
    response: dict,
) -> None:
    ensure_execution_safety_schema(db)
    stamp = utc_now()
    payload = dict(response)
    payload["command_id"] = command_id
    db.execute(
        """INSERT INTO command_journal(
               operation_id,command_id,requested_at,completed_at,action,
               from_state,to_state,outcome,reason,http_status,response_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            command_id,
            stamp,
            stamp,
            action,
            from_state,
            to_state,
            outcome,
            reason,
            http_status,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        ),
    )
