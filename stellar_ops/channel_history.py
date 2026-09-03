from __future__ import annotations

import threading
import time
from pathlib import Path

from .database import connect_database
from .telemetry_runtime import live_snapshot

# =====================================================================
# Phase 0 — channel time-series storage
# =====================================================================
#
# The live snapshot endpoints are polled by every open browser tab,
# every 0.5-2 seconds, so writing history directly from that read path
# would flood the database with near-duplicate rows and couple the
# recording rate to however many clients happen to be watching.
#
# Instead a single background thread samples the same live_snapshot()
# used by the UI at a fixed, predictable rate, independent of how many
# clients are polling. This is the foundation the graph, gauge history,
# and replay-from-live-data features (later phases) read from.
# =====================================================================

SAMPLE_INTERVAL_S = 1.0
RETENTION_HOURS = 24
PRUNE_EVERY_N_SAMPLES = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_history(
 operation_id TEXT NOT NULL, channel_id TEXT NOT NULL,
 ts REAL NOT NULL, value REAL, quality TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_channel_history_lookup
 ON channel_history(operation_id, channel_id, ts);
"""


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)


_SAMPLER_LOCK = threading.RLock()
_SAMPLER_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def _sample_loop(db_path: Path, operation_id: str) -> None:
    db = connect_database(db_path)
    ensure_schema(db)
    db.commit()
    count = 0
    while not _STOP.is_set():
        try:
            snapshot = live_snapshot(db, operation_id)
            now = time.time()
            rows = [
                (operation_id, channel_id, now, data.get("value"), data.get("quality"))
                for channel_id, data in snapshot.get("channels", {}).items()
            ]
            if rows:
                db.executemany(
                    "INSERT INTO channel_history(operation_id,channel_id,ts,value,quality) "
                    "VALUES(?,?,?,?,?)",
                    rows,
                )
                db.commit()
            count += 1
            if count % PRUNE_EVERY_N_SAMPLES == 0:
                cutoff = now - RETENTION_HOURS * 3600
                db.execute("DELETE FROM channel_history WHERE ts < ?", (cutoff,))
                db.commit()
        except Exception:
            # A single failed sample (e.g. transient DB lock) must never
            # kill the sampler thread -- skip this tick and try again.
            pass
        _STOP.wait(SAMPLE_INTERVAL_S)


def ensure_channel_history_sampler(db_path: Path, operation_id: str) -> dict:
    """Start the channel history sampler once per process, idempotently."""
    global _SAMPLER_THREAD

    with _SAMPLER_LOCK:
        if _SAMPLER_THREAD is not None and _SAMPLER_THREAD.is_alive():
            return {"status": "RUNNING", "interval_s": SAMPLE_INTERVAL_S}

        _STOP.clear()
        thread = threading.Thread(
            target=_sample_loop,
            args=(db_path, operation_id),
            name="stellar-ops-channel-history-sampler",
            daemon=True,
        )
        thread.start()
        _SAMPLER_THREAD = thread
        return {"status": "RUNNING", "interval_s": SAMPLE_INTERVAL_S}


def stop_channel_history_sampler() -> None:
    global _SAMPLER_THREAD
    with _SAMPLER_LOCK:
        _STOP.set()
        if _SAMPLER_THREAD is not None:
            _SAMPLER_THREAD.join(timeout=2.0)
        _SAMPLER_THREAD = None


def query_history(db, operation_id: str, channel_id: str, since: float, until: float) -> list[dict]:
    """Return recorded samples for one channel between two unix timestamps."""
    ensure_schema(db)
    rows = db.execute(
        "SELECT ts,value,quality FROM channel_history "
        "WHERE operation_id=? AND channel_id=? AND ts BETWEEN ? AND ? "
        "ORDER BY ts",
        (operation_id, channel_id, since, until),
    ).fetchall()
    return [{"ts": row["ts"], "value": row["value"], "quality": row["quality"]} for row in rows]
