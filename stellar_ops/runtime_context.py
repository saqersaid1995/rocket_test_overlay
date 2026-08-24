from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_runtime_context_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS runtime_context(
        id INTEGER PRIMARY KEY CHECK(id=1),
        registry_operation_id INTEGER,
        runtime_operation_id TEXT NOT NULL,
        execution_release_id INTEGER,
        active_run_id INTEGER,
        context_state TEXT NOT NULL,
        operation_code TEXT NOT NULL,
        release_code TEXT,
        release_sha256 TEXT,
        activated_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")


def get_runtime_context(db: sqlite3.Connection) -> dict | None:
    ensure_runtime_context_schema(db)
    row = db.execute("SELECT * FROM runtime_context WHERE id=1").fetchone()
    return dict(row) if row else None


def ensure_development_context(db: sqlite3.Connection, runtime_operation_id: str) -> None:
    """Preserve the seeded single-operator training baseline until a release is activated."""
    ensure_runtime_context_schema(db)
    if db.execute("SELECT 1 FROM runtime_context WHERE id=1").fetchone():
        return
    operation = db.execute(
        "SELECT code FROM operations WHERE id=?", (runtime_operation_id,)
    ).fetchone()
    run = db.execute(
        "SELECT id FROM test_runs WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1",
        (runtime_operation_id,),
    ).fetchone()
    stamp = utc_now()
    db.execute(
        """INSERT INTO runtime_context(
            id,registry_operation_id,runtime_operation_id,execution_release_id,
            active_run_id,context_state,operation_code,release_code,release_sha256,
            activated_at,updated_at)
           VALUES(1,NULL,?,NULL,?,'DEVELOPMENT',?,NULL,NULL,?,?)""",
        (
            runtime_operation_id,
            run["id"] if run else None,
            operation["code"] if operation else runtime_operation_id,
            stamp,
            stamp,
        ),
    )


def activate_released_operation(
    db: sqlite3.Connection,
    *,
    runtime_operation_id: str,
    registry_operation: sqlite3.Row,
    execution_release: sqlite3.Row,
    release_sha256: str,
) -> dict:
    """Pin one released Operations record and one new Test Run to Mission Control."""
    ensure_runtime_context_schema(db)
    stamp = utc_now()
    article = db.execute(
        "SELECT * FROM test_articles WHERE operation_id=?",
        (registry_operation["id"],),
    ).fetchone()
    baseline = db.execute(
        "SELECT * FROM configuration_baselines WHERE operation_id=?",
        (registry_operation["id"],),
    ).fetchone()
    procedure = db.execute(
        "SELECT * FROM operation_procedures WHERE operation_id=?",
        (registry_operation["id"],),
    ).fetchone()

    existing = db.execute(
        """SELECT count(*) AS count FROM test_runs
           WHERE registry_operation_id=?""",
        (registry_operation["id"],),
    ).fetchone()["count"]
    run_code = f"{registry_operation['code']}-RUN-{existing + 1:03d}"
    test_article = (
        f"{article['family']} / {article['serial_number']}"
        if article
        else "ARTICLE NOT ASSIGNED"
    )
    configuration_revision = (
        f"{baseline['baseline_code']} / {baseline['revision']}"
        if baseline
        else (article["configuration_revision"] if article else "UNASSIGNED")
    )

    db.execute("UPDATE test_runs SET active=0 WHERE active=1")
    cursor = db.execute(
        """INSERT INTO test_runs(
            operation_id,code,title,test_article,configuration_revision,
            propellant_batch,status,created_at,activated_at,notes,active,
            registry_operation_id,execution_release_id,release_sha256,
            procedure_code,procedure_revision)
           VALUES(?,?,?,?,?,NULL,'RELEASED',?,?,?,1,?,?,?,?,?)""",
        (
            runtime_operation_id,
            run_code,
            registry_operation["title"],
            test_article,
            configuration_revision,
            stamp,
            stamp,
            f"Created from controlled execution release {execution_release['release_code']}",
            registry_operation["id"],
            execution_release["id"],
            release_sha256,
            procedure["document_code"] if procedure else None,
            procedure["revision"] if procedure else None,
        ),
    )
    run_id = cursor.lastrowid

    db.execute(
        """UPDATE operations
           SET code=?,title=?,operation_type=?,mode=?,state='CHECKOUT',
               prior_state=NULL,active_hold=NULL,firing_started_monotonic=NULL,
               updated_at=?
           WHERE id=?""",
        (
            registry_operation["code"],
            registry_operation["title"],
            registry_operation["operation_type"],
            execution_release["source_mode"],
            stamp,
            runtime_operation_id,
        ),
    )
    db.execute(
        """INSERT INTO runtime_context(
            id,registry_operation_id,runtime_operation_id,execution_release_id,
            active_run_id,context_state,operation_code,release_code,release_sha256,
            activated_at,updated_at)
           VALUES(1,?,?,?,?, 'RELEASED',?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             registry_operation_id=excluded.registry_operation_id,
             runtime_operation_id=excluded.runtime_operation_id,
             execution_release_id=excluded.execution_release_id,
             active_run_id=excluded.active_run_id,
             context_state=excluded.context_state,
             operation_code=excluded.operation_code,
             release_code=excluded.release_code,
             release_sha256=excluded.release_sha256,
             activated_at=excluded.activated_at,
             updated_at=excluded.updated_at""",
        (
            registry_operation["id"],
            runtime_operation_id,
            execution_release["id"],
            run_id,
            registry_operation["code"],
            execution_release["release_code"],
            release_sha256,
            stamp,
            stamp,
        ),
    )
    db.execute(
        "UPDATE operation_registry SET runtime_operation_id=?,updated_at=? WHERE id=?",
        (runtime_operation_id, stamp, registry_operation["id"]),
    )
    return {
        "runtime_operation_id": runtime_operation_id,
        "registry_operation_id": registry_operation["id"],
        "execution_release_id": execution_release["id"],
        "run_id": run_id,
        "run_code": run_code,
        "release_code": execution_release["release_code"],
        "release_sha256": release_sha256,
    }


def close_runtime_context(db: sqlite3.Connection, registry_operation_id: int) -> None:
    ensure_runtime_context_schema(db)
    stamp = utc_now()
    db.execute(
        """UPDATE runtime_context SET context_state='CLOSED',updated_at=?
           WHERE id=1 AND registry_operation_id=?""",
        (stamp, registry_operation_id),
    )
    db.execute(
        """UPDATE test_runs SET active=0,status='CLOSED',closed_at=?
           WHERE id=(SELECT active_run_id FROM runtime_context WHERE id=1
                    AND registry_operation_id=?)""",
        (stamp, registry_operation_id),
    )
