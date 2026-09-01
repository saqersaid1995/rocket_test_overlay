from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from flask import g, request

from .audit_integrity import verify_audit_ledger
from .recovery import list_backups


PROCESS_STARTED_MONOTONIC = time.monotonic()
PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
_LOCK = threading.Lock()
_REQUESTS = defaultdict(int)
_ERRORS = defaultdict(int)
_DURATION_SECONDS = defaultdict(float)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_observability_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS diagnostic_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        initiated_by TEXT NOT NULL,
        overall_status TEXT NOT NULL,
        checks_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_diagnostic_runs_operation
        ON diagnostic_runs(operation_id,id DESC);
    """)


def begin_request() -> None:
    g.request_started_monotonic = time.monotonic()
    supplied = request.headers.get("X-Request-ID", "").strip()
    g.request_id = supplied[:128] if supplied else str(uuid.uuid4())


def finish_request(response):
    elapsed = max(
        0.0,
        time.monotonic() - getattr(g, "request_started_monotonic", time.monotonic()),
    )
    route = request.url_rule.rule if request.url_rule else "unmatched"
    method_route = f"{request.method} {route}"
    with _LOCK:
        _REQUESTS[method_route] += 1
        _DURATION_SECONDS[method_route] += elapsed
        if response.status_code >= 400:
            _ERRORS[method_route] += 1
    response.headers["X-Request-ID"] = getattr(g, "request_id", "")
    response.headers["Server-Timing"] = f"app;dur={elapsed * 1000:.2f}"
    return response


def process_metrics() -> dict:
    with _LOCK:
        return {
            "started_at": PROCESS_STARTED_AT,
            "uptime_seconds": round(time.monotonic() - PROCESS_STARTED_MONOTONIC, 3),
            "requests": dict(_REQUESTS),
            "errors": dict(_ERRORS),
            "duration_seconds": dict(_DURATION_SECONDS),
        }


def _prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus_metrics(db: sqlite3.Connection, operation_id: str) -> str:
    metrics = process_metrics()
    lines = [
        "# HELP stellar_ops_process_uptime_seconds Process uptime in seconds.",
        "# TYPE stellar_ops_process_uptime_seconds gauge",
        f"stellar_ops_process_uptime_seconds {metrics['uptime_seconds']}",
        "# HELP stellar_ops_http_requests_total HTTP requests by route.",
        "# TYPE stellar_ops_http_requests_total counter",
    ]
    for route, count in sorted(metrics["requests"].items()):
        label = _prom_label(route)
        lines.append(f'stellar_ops_http_requests_total{{route="{label}"}} {count}')
        lines.append(
            f'stellar_ops_http_errors_total{{route="{label}"}} '
            f'{metrics["errors"].get(route, 0)}'
        )
        lines.append(
            f'stellar_ops_http_request_duration_seconds_sum{{route="{label}"}} '
            f'{metrics["duration_seconds"].get(route, 0.0):.6f}'
        )

    operation = db.execute(
        "SELECT state,mode FROM operations WHERE id=?", (operation_id,)
    ).fetchone()
    active_alarms = db.execute(
        "SELECT count(*) FROM alarms WHERE operation_id=? AND state!='CLOSED'",
        (operation_id,),
    ).fetchone()[0]
    p1_alarms = db.execute(
        """SELECT count(*) FROM alarms
           WHERE operation_id=? AND state!='CLOSED' AND priority='P1'""",
        (operation_id,),
    ).fetchone()[0]
    incidents = db.execute(
        """SELECT count(*) FROM incidents
           WHERE operation_id=? AND status!='CLOSED'""",
        (operation_id,),
    ).fetchone()[0]
    audit = verify_audit_ledger(db)
    lines.extend(
        (
            "# TYPE stellar_ops_active_alarms gauge",
            f"stellar_ops_active_alarms {active_alarms}",
            "# TYPE stellar_ops_active_p1_alarms gauge",
            f"stellar_ops_active_p1_alarms {p1_alarms}",
            "# TYPE stellar_ops_open_incidents gauge",
            f"stellar_ops_open_incidents {incidents}",
            "# TYPE stellar_ops_audit_integrity gauge",
            f"stellar_ops_audit_integrity {1 if audit['valid'] else 0}",
            "# TYPE stellar_ops_audit_entries gauge",
            f"stellar_ops_audit_entries {audit['checked_entries']}",
        )
    )
    if operation:
        lines.append(
            'stellar_ops_operation_info'
            f'{{state="{_prom_label(operation["state"])}",'
            f'mode="{_prom_label(operation["mode"])}"}} 1'
        )
    return "\n".join(lines) + "\n"


def run_self_test(
    db: sqlite3.Connection,
    *,
    operation_id: str,
    database_path: Path,
    initiated_by: str,
) -> dict:
    ensure_observability_schema(db)
    started = utc_now()
    checks = []

    quick_check = db.execute("PRAGMA quick_check").fetchone()[0]
    checks.append(
        {
            "code": "DATABASE_INTEGRITY",
            "status": "PASS" if quick_check == "ok" else "FAIL",
            "detail": quick_check,
        }
    )

    audit = verify_audit_ledger(db)
    checks.append(
        {
            "code": "AUDIT_CHAIN",
            "status": "PASS" if audit["valid"] else "FAIL",
            "detail": f"{audit['checked_entries']} entries · {audit['status']}",
        }
    )

    run = db.execute(
        """SELECT code,status FROM test_runs
           WHERE operation_id=? AND active=1 ORDER BY id DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    checks.append(
        {
            "code": "ACTIVE_TEST_RUN",
            "status": "PASS" if run else "FAIL",
            "detail": f"{run['code']} · {run['status']}" if run else "No active Test Run",
        }
    )

    context = db.execute("SELECT * FROM runtime_context WHERE id=1").fetchone()
    context_ok = bool(context and context["active_run_id"])
    checks.append(
        {
            "code": "RUNTIME_CONTEXT",
            "status": "PASS" if context_ok else "FAIL",
            "detail": (
                f"{context['context_state']} · {context['operation_code']}"
                if context
                else "Runtime context missing"
            ),
        }
    )

    disk = shutil.disk_usage(database_path.parent)
    free_percent = round(disk.free / disk.total * 100, 1)
    checks.append(
        {
            "code": "DISK_CAPACITY",
            "status": "PASS" if free_percent >= 10 else (
                "WARN" if free_percent >= 5 else "FAIL"
            ),
            "detail": f"{free_percent}% free",
        }
    )

    backup_count = len(list_backups(database_path))
    checks.append(
        {
            "code": "RECOVERY_BACKUP",
            "status": "PASS" if backup_count else "WARN",
            "detail": f"{backup_count} verified backup record(s)",
        }
    )

    migrations = db.execute(
        "SELECT max(version) FROM schema_migrations"
    ).fetchone()[0]
    checks.append(
        {
            "code": "SCHEMA_VERSION",
            "status": "PASS" if migrations and migrations >= 7 else "FAIL",
            "detail": f"migration {migrations or 0}",
        }
    )

    overall = (
        "FAIL"
        if any(item["status"] == "FAIL" for item in checks)
        else "WARN"
        if any(item["status"] == "WARN" for item in checks)
        else "PASS"
    )
    completed = utc_now()
    cursor = db.execute(
        """INSERT INTO diagnostic_runs(
               operation_id,started_at,completed_at,initiated_by,
               overall_status,checks_json)
           VALUES(?,?,?,?,?,?)""",
        (
            operation_id,
            started,
            completed,
            initiated_by,
            overall,
            json.dumps(checks, separators=(",", ":"), sort_keys=True),
        ),
    )
    return {
        "id": cursor.lastrowid,
        "started_at": started,
        "completed_at": completed,
        "initiated_by": initiated_by,
        "overall_status": overall,
        "checks": checks,
    }
