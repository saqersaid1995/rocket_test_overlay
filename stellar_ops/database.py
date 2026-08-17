from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=10000")
    return db


def columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def add_column(db: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def ensure_migration_table(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")


def apply_once(db: sqlite3.Connection, version: int, name: str, stamp: str, migration) -> None:
    ensure_migration_table(db)
    if db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    migration(db)
    db.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)", (version,name,stamp))
