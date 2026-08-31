from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .database import connect_database


DEFAULT_DEVICE_ID = "PT-01"
DEFAULT_ENDPOINT = "http://192.168.1.50/reading"
DEFAULT_POLL_INTERVAL_S = 0.5


@dataclass(frozen=True)
class HttpTelemetryConfig:
    device_id: str
    endpoint: str
    firmware: str = "esp32-pressure-http/1"
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    stale_after_s: float = 2.0
    timeout_s: float = 0.4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def configured_endpoint() -> str:
    return os.environ.get("STELLAR_OPS_ESP32_PRESSURE_ENDPOINT", DEFAULT_ENDPOINT).strip()


def configured_device_id() -> str:
    return os.environ.get("STELLAR_OPS_ESP32_PRESSURE_DEVICE", DEFAULT_DEVICE_ID).strip() or DEFAULT_DEVICE_ID


def configured_poll_interval_s() -> float:
    try:
        value = float(os.environ.get("STELLAR_OPS_ESP32_PRESSURE_POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL_S)))
    except ValueError:
        value = DEFAULT_POLL_INTERVAL_S
    return min(5.0, max(0.1, value))


def _normalize_endpoint(endpoint: str) -> str:
    value = endpoint.strip()
    if not value:
        raise ValueError("HTTP telemetry endpoint is required")
    if "://" not in value:
        value = f"http://{value}"
    if not value.rstrip("/").endswith("/reading"):
        value = value.rstrip("/") + "/reading"
    return value


def _ensure_edge_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS edge_sessions(
          device_id TEXT NOT NULL, boot_id TEXT NOT NULL, remote_addr TEXT NOT NULL,
          firmware TEXT, connected_at TEXT NOT NULL, disconnected_at TEXT,
          last_seen TEXT NOT NULL, last_sequence INTEGER, total_samples INTEGER NOT NULL DEFAULT 0,
          sequence_gaps INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
          PRIMARY KEY(device_id,boot_id));
        CREATE TABLE IF NOT EXISTS edge_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, boot_id TEXT NOT NULL,
          sequence INTEGER NOT NULL, received_at TEXT NOT NULL, first_sample_us INTEGER NOT NULL,
          sample_period_us INTEGER NOT NULL, sample_count INTEGER NOT NULL,
          channels_json TEXT NOT NULL, UNIQUE(device_id,boot_id,sequence));
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(edge_batches)")}
    if "run_id" not in columns:
        db.execute("ALTER TABLE edge_batches ADD COLUMN run_id INTEGER")


def ensure_esp32_pressure_integration(db_path, operation_id: str) -> dict:
    """Make the existing PT-01 device a live ESP32 HTTP telemetry source.

    This does not change the ESP32 firmware or its SD logging. It only tells
    Stellar Ops how to reach the already-running `/reading` endpoint and maps
    its JSON fields into the existing telemetry channel model.
    """
    endpoint = _normalize_endpoint(configured_endpoint())
    device_id = configured_device_id()
    db = connect_database(db_path)
    try:
        _ensure_edge_schema(db)
        device = db.execute(
            "SELECT 1 FROM devices WHERE operation_id=? AND id=?", (operation_id, device_id)
        ).fetchone()
        if not device:
            raise RuntimeError(f"Configured pressure device {device_id} does not exist in Device Registry")

        db.execute(
            """UPDATE devices SET protocol='HTTP-JSON',endpoint=?
               WHERE operation_id=? AND id=?""",
            (endpoint, operation_id, device_id),
        )
        db.execute(
            """INSERT INTO device_integrations(
                 operation_id,device_id,adapter_type,config_json,enabled,last_test_at,last_test_status,last_test_message)
               VALUES(?,?, 'HTTP_JSON', ?,1,NULL,'NOT_TESTED',NULL)
               ON CONFLICT(operation_id,device_id) DO UPDATE SET
                 adapter_type='HTTP_JSON',config_json=excluded.config_json,enabled=1""",
            (operation_id, device_id, json.dumps({"endpoint": endpoint}, separators=(",", ":"))),
        )

        # Preserve all existing pressure calibration behavior in Stellar Ops;
        # map only the raw field name to the JSON field produced by the ESP32.
        db.execute(
            """UPDATE channel_integrations SET raw_field='pressure_bar'
               WHERE operation_id=? AND channel_id='motor.chamber_pressure'""",
            (operation_id,),
        )
        # PT-01 is now the actual source for chamber pressure.
        db.execute(
            """UPDATE channels SET source_id=?, quality='NO_DATA'
               WHERE operation_id=? AND id='motor.chamber_pressure'""",
            (device_id, operation_id),
        )
        db.commit()
    finally:
        db.close()

    ensure_http_poller(
        db_path,
        device_id=device_id,
        endpoint=endpoint,
        poll_interval_s=configured_poll_interval_s(),
    )
    return {
        "device_id": device_id,
        "endpoint": endpoint,
        "adapter_type": "HTTP_JSON",
        "poll_interval_s": configured_poll_interval_s(),
    }


def _fetch_reading(config: HttpTelemetryConfig) -> dict:
    endpoint = _normalize_endpoint(config.endpoint)
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=config.timeout_s) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP telemetry returned {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("HTTP telemetry payload must be a JSON object")
    for key in ("pressure", "voltage", "time"):
        if key not in payload:
            raise ValueError(f"HTTP telemetry payload is missing {key}")
    return payload


class HttpTelemetryPoller:
    def __init__(self, db_path, config: HttpTelemetryConfig):
        self.db_path = db_path
        self.config = config
        self.boot_id = f"http-{uuid.uuid4().hex[:12]}"
        self.sequence = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"http-telemetry-{self.config.device_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _database(self) -> sqlite3.Connection:
        db = connect_database(self.db_path)
        _ensure_edge_schema(db)
        return db

    def _mark(self, db: sqlite3.Connection, status: str, remote: str, *, sample=False) -> None:
        stamp = utc_now()
        db.execute(
            """INSERT INTO edge_sessions(device_id,boot_id,remote_addr,firmware,connected_at,last_seen,status)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(device_id,boot_id) DO UPDATE SET remote_addr=excluded.remote_addr,
               firmware=excluded.firmware,last_seen=excluded.last_seen,
               disconnected_at=CASE WHEN excluded.status='DISCONNECTED' THEN excluded.last_seen ELSE NULL END,
               status=excluded.status""",
            (self.config.device_id, self.boot_id, remote, self.config.firmware, stamp, stamp, status),
        )
        if sample:
            db.execute(
                """UPDATE edge_sessions SET last_sequence=?, total_samples=total_samples+1,
                   status='STREAMING', last_seen=?, disconnected_at=NULL
                   WHERE device_id=? AND boot_id=?""",
                (self.sequence, stamp, self.config.device_id, self.boot_id),
            )

    def _store(self, db: sqlite3.Connection, payload: dict) -> None:
        pressure = float(payload["pressure"])
        voltage = float(payload["voltage"])
        elapsed = max(0.0, float(payload.get("time", 0.0)))
        active_run = None
        try:
            active_run = db.execute("SELECT id FROM test_runs WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            pass
        channels = {"pressure_bar": [pressure], "voltage_v": [voltage]}
        db.execute(
            """INSERT OR REPLACE INTO edge_batches(
                 device_id,boot_id,sequence,received_at,first_sample_us,sample_period_us,sample_count,channels_json,run_id)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                self.config.device_id,
                self.boot_id,
                self.sequence,
                utc_now(),
                int(elapsed * 1_000_000),
                max(1, int(self.config.poll_interval_s * 1_000_000)),
                1,
                json.dumps(channels, separators=(",", ":")),
                active_run["id"] if active_run else None,
            ),
        )
        self._mark(db, "STREAMING", _normalize_endpoint(self.config.endpoint), sample=True)
        db.commit()
        self.sequence += 1

    def _run(self) -> None:
        db = self._database()
        remote = _normalize_endpoint(self.config.endpoint)
        try:
            self._mark(db, "CONNECTING", remote)
            db.commit()
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    payload = _fetch_reading(self.config)
                    self._store(db, payload)
                except (OSError, ValueError, RuntimeError, urllib.error.URLError):
                    self._mark(db, "DISCONNECTED", remote)
                    db.commit()
                delay = max(0.05, self.config.poll_interval_s - (time.monotonic() - started))
                self._stop.wait(delay)
        finally:
            self._mark(db, "DISCONNECTED", remote)
            db.commit()
            db.close()


_POLLER_LOCK = threading.RLock()
_POLLERS: dict[str, HttpTelemetryPoller] = {}


def ensure_http_poller(db_path, *, device_id: str, endpoint: str, poll_interval_s: float = DEFAULT_POLL_INTERVAL_S) -> HttpTelemetryPoller:
    key = f"{device_id}|{_normalize_endpoint(endpoint)}"
    with _POLLER_LOCK:
        poller = _POLLERS.get(key)
        if poller is None:
            poller = HttpTelemetryPoller(
                db_path,
                HttpTelemetryConfig(device_id=device_id, endpoint=endpoint, poll_interval_s=poll_interval_s),
            )
            _POLLERS[key] = poller
        poller.start()
        return poller


def stop_all_http_pollers() -> None:
    with _POLLER_LOCK:
        for poller in list(_POLLERS.values()):
            poller.stop()
        _POLLERS.clear()
