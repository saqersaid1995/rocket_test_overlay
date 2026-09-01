from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


def ensure_audit_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS audit_ledger(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        run_id INTEGER,
        occurred_at TEXT NOT NULL,
        record_type TEXT NOT NULL,
        record_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        entry_hash TEXT NOT NULL UNIQUE);
    CREATE INDEX IF NOT EXISTS idx_audit_ledger_operation
        ON audit_ledger(operation_id,sequence);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_ledger_record
        ON audit_ledger(record_type,record_id);
    """)


def append_audit_record(
    db: sqlite3.Connection,
    *,
    operation_id: str,
    run_id: int | None,
    record_type: str,
    record_id: str,
    payload: dict,
    occurred_at: str | None = None,
) -> dict:
    ensure_audit_schema(db)
    existing = db.execute(
        """SELECT * FROM audit_ledger
           WHERE record_type=? AND record_id=?""",
        (record_type, str(record_id)),
    ).fetchone()
    if existing:
        return dict(existing)

    stamp = occurred_at or utc_now()
    payload_sha = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    previous = db.execute(
        "SELECT entry_hash FROM audit_ledger ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    previous_hash = previous["entry_hash"] if previous else "GENESIS"
    material = "|".join(
        (
            previous_hash,
            operation_id,
            str(run_id or ""),
            stamp,
            record_type,
            str(record_id),
            payload_sha,
        )
    )
    entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    cursor = db.execute(
        """INSERT INTO audit_ledger(
               operation_id,run_id,occurred_at,record_type,record_id,
               payload_sha256,previous_hash,entry_hash)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            run_id,
            stamp,
            record_type,
            str(record_id),
            payload_sha,
            previous_hash,
            entry_hash,
        ),
    )
    row = db.execute(
        "SELECT * FROM audit_ledger WHERE sequence=?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)


def backfill_events(db: sqlite3.Connection) -> None:
    ensure_audit_schema(db)
    for row in db.execute("SELECT * FROM events ORDER BY sequence").fetchall():
        payload = dict(row)
        append_audit_record(
            db,
            operation_id=row["operation_id"],
            run_id=payload.get("run_id"),
            record_type="EVENT",
            record_id=str(row["sequence"]),
            payload=payload,
            occurred_at=row["occurred_at"],
        )


def protect_append_only_tables(db: sqlite3.Connection) -> None:
    """Database-level protection for records that must never be rewritten."""
    db.executescript("""
    CREATE TRIGGER IF NOT EXISTS protect_events_update
    BEFORE UPDATE ON events BEGIN
      SELECT RAISE(ABORT,'events are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS protect_events_delete
    BEFORE DELETE ON events BEGIN
      SELECT RAISE(ABORT,'events are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS protect_command_journal_update
    BEFORE UPDATE ON command_journal BEGIN
      SELECT RAISE(ABORT,'command journal is append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS protect_command_journal_delete
    BEFORE DELETE ON command_journal BEGIN
      SELECT RAISE(ABORT,'command journal is append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS protect_incident_actions_update
    BEFORE UPDATE ON incident_actions BEGIN
      SELECT RAISE(ABORT,'incident actions are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS protect_incident_actions_delete
    BEFORE DELETE ON incident_actions BEGIN
      SELECT RAISE(ABORT,'incident actions are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS protect_alarm_actions_update
    BEFORE UPDATE ON alarm_actions BEGIN
      SELECT RAISE(ABORT,'alarm actions are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS protect_alarm_actions_delete
    BEFORE DELETE ON alarm_actions BEGIN
      SELECT RAISE(ABORT,'alarm actions are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS protect_audit_ledger_update
    BEFORE UPDATE ON audit_ledger BEGIN
      SELECT RAISE(ABORT,'audit ledger is immutable');
    END;
    CREATE TRIGGER IF NOT EXISTS protect_audit_ledger_delete
    BEFORE DELETE ON audit_ledger BEGIN
      SELECT RAISE(ABORT,'audit ledger is immutable');
    END;
    """)


def initialize_audit_integrity(db: sqlite3.Connection) -> None:
    ensure_audit_schema(db)
    backfill_events(db)
    protect_append_only_tables(db)


def verify_audit_ledger(db: sqlite3.Connection) -> dict:
    ensure_audit_schema(db)
    previous_hash = "GENESIS"
    checked = 0
    for row in db.execute("SELECT * FROM audit_ledger ORDER BY sequence"):
        material = "|".join(
            (
                previous_hash,
                row["operation_id"],
                str(row["run_id"] or ""),
                row["occurred_at"],
                row["record_type"],
                row["record_id"],
                row["payload_sha256"],
            )
        )
        expected = hashlib.sha256(material.encode("utf-8")).hexdigest()
        if row["previous_hash"] != previous_hash or row["entry_hash"] != expected:
            return {
                "status": "FAILED",
                "valid": False,
                "checked_entries": checked,
                "failed_sequence": row["sequence"],
                "expected_hash": expected,
                "recorded_hash": row["entry_hash"],
            }
        previous_hash = row["entry_hash"]
        checked += 1
    return {
        "status": "VERIFIED",
        "valid": True,
        "checked_entries": checked,
        "head_hash": previous_hash,
        "verified_at": utc_now(),
    }
