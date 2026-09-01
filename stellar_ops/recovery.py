from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


BACKUP_SUFFIX = ".sqlite3"
DEFAULT_RETENTION = 20


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_name(value: str) -> str:
    name = Path(value).name
    if name != value or not name.endswith(BACKUP_SUFFIX):
        raise ValueError("invalid backup name")
    return name


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_root(database_path: Path) -> Path:
    configured = os.environ.get("STELLAR_OPS_BACKUPS")
    root = Path(configured) if configured else database_path.parent / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_recovery_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS backup_records(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backup_name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL,
        reason TEXT NOT NULL,
        database_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        quick_check TEXT NOT NULL,
        audit_head_hash TEXT NOT NULL,
        audit_entries INTEGER NOT NULL,
        state TEXT NOT NULL);
    """)


def _verify_audit_readonly(db: sqlite3.Connection) -> dict:
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "audit_ledger" not in tables:
        return {"valid": False, "status": "MISSING", "checked_entries": 0}
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
                "valid": False,
                "status": "FAILED",
                "checked_entries": checked,
                "failed_sequence": row["sequence"],
            }
        previous_hash = row["entry_hash"]
        checked += 1
    return {
        "valid": True,
        "status": "VERIFIED",
        "checked_entries": checked,
        "head_hash": previous_hash,
    }


def verify_backup(path: Path, expected_sha256: str | None = None) -> dict:
    if not path.is_file():
        raise FileNotFoundError("backup file not found")
    digest = sha256_file(path)
    if expected_sha256 and digest != expected_sha256:
        return {
            "valid": False,
            "status": "CHECKSUM_FAILED",
            "sha256": digest,
            "expected_sha256": expected_sha256,
        }
    uri = f"file:{path.resolve()}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        quick_check = db.execute("PRAGMA quick_check").fetchone()[0]
        audit = _verify_audit_readonly(db)
    finally:
        db.close()
    valid = quick_check == "ok" and audit["valid"]
    return {
        "valid": valid,
        "status": "VERIFIED" if valid else "FAILED",
        "sha256": digest,
        "bytes": path.stat().st_size,
        "quick_check": quick_check,
        "audit": audit,
    }


def create_backup(
    source_db: sqlite3.Connection,
    *,
    database_path: Path,
    created_by: str,
    reason: str,
    retention: int = DEFAULT_RETENTION,
) -> dict:
    ensure_recovery_schema(source_db)
    stamp = utc_now()
    token = stamp.replace(":", "").replace("-", "").replace("+", "_").replace(".", "")
    name = f"stellar-ops-{token}{BACKUP_SUFFIX}"
    root = backup_root(database_path)
    target = root / name
    temporary = root / f".{name}.partial"

    target_db = sqlite3.connect(temporary)
    try:
        source_db.backup(target_db)
        target_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        target_db.close()
    os.replace(temporary, target)

    verification = verify_backup(target)
    if not verification["valid"]:
        target.unlink(missing_ok=True)
        raise RuntimeError("backup verification failed")

    manifest = {
        "backup_name": name,
        "created_at": stamp,
        "created_by": created_by,
        "reason": reason,
        "database_bytes": verification["bytes"],
        "sha256": verification["sha256"],
        "quick_check": verification["quick_check"],
        "audit_head_hash": verification["audit"]["head_hash"],
        "audit_entries": verification["audit"]["checked_entries"],
        "state": "VERIFIED",
    }
    manifest_path = target.with_suffix(target.suffix + ".json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    source_db.execute(
        """INSERT INTO backup_records(
               backup_name,created_at,created_by,reason,database_bytes,
               sha256,quick_check,audit_head_hash,audit_entries,state)
           VALUES(?,?,?,?,?,?,?,?,?,'VERIFIED')""",
        (
            name,
            stamp,
            created_by,
            reason,
            verification["bytes"],
            verification["sha256"],
            verification["quick_check"],
            verification["audit"]["head_hash"],
            verification["audit"]["checked_entries"],
        ),
    )
    enforce_retention(root, max(1, retention))
    return manifest


def enforce_retention(root: Path, retain: int) -> None:
    backups = sorted(root.glob(f"*{BACKUP_SUFFIX}"), key=lambda p: p.stat().st_mtime)
    for obsolete in backups[:-retain]:
        obsolete.unlink(missing_ok=True)
        obsolete.with_suffix(obsolete.suffix + ".json").unlink(missing_ok=True)


def list_backups(database_path: Path) -> list[dict]:
    root = backup_root(database_path)
    result = []
    for path in sorted(
        root.glob(f"*{BACKUP_SUFFIX}"),
        key=lambda candidate: candidate.stat().st_mtime,
        reverse=True,
    ):
        manifest_path = path.with_suffix(path.suffix + ".json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {
                "backup_name": path.name,
                "created_at": datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).isoformat(),
                "state": "MANIFEST_MISSING",
            }
        result.append(manifest)
    return result


def restore_backup(
    *,
    database_path: Path,
    backup_name: str,
    confirmation: str,
) -> dict:
    """Offline-only restore with exact confirmation and automatic rollback copy."""
    name = safe_name(backup_name)
    if confirmation != f"RESTORE {name}":
        raise ValueError(f"confirmation must exactly match: RESTORE {name}")
    source = backup_root(database_path) / name
    manifest_path = source.with_suffix(source.suffix + ".json")
    if not manifest_path.is_file():
        raise ValueError("backup manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verification = verify_backup(source, manifest.get("sha256"))
    if not verification["valid"]:
        raise ValueError("backup verification failed; restore refused")

    rollback = database_path.with_name(
        f"{database_path.name}.pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    if database_path.exists():
        shutil.copy2(database_path, rollback)
    temporary = database_path.with_suffix(database_path.suffix + ".restore")
    shutil.copy2(source, temporary)
    os.replace(temporary, database_path)
    for suffix in ("-wal", "-shm"):
        database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)
    return {
        "restored": True,
        "backup_name": name,
        "database_path": str(database_path),
        "rollback_path": str(rollback) if rollback.exists() else None,
        "sha256": verification["sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline Stellar Ops verified backup restore"
    )
    parser.add_argument("restore", choices=["restore"])
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    try:
        result = restore_backup(
            database_path=args.database,
            backup_name=args.backup,
            confirmation=args.confirm,
        )
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        print(f"RESTORE REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
