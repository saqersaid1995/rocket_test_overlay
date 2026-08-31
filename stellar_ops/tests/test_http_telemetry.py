import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from stellar_ops.database import connect_database
from stellar_ops.http_telemetry import (
    HttpTelemetryConfig,
    HttpTelemetryPoller,
    ensure_esp32_pressure_integration,
    stop_all_http_pollers,
)


class ReadingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/reading":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"pressure": 12.345, "voltage": 0.873, "time": 1.25}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class HttpTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ReadingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        stop_all_http_pollers()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_poller_normalizes_reading_into_edge_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.db"
            poller = HttpTelemetryPoller(
                path,
                HttpTelemetryConfig(
                    device_id="PT-01",
                    endpoint=f"http://127.0.0.1:{self.server.server_port}/reading",
                    poll_interval_s=0.1,
                    timeout_s=0.5,
                ),
            )
            poller.start()
            deadline = time.time() + 2
            batch = None
            while time.time() < deadline:
                with connect_database(path) as db:
                    try:
                        batch = db.execute("SELECT * FROM edge_batches ORDER BY id DESC LIMIT 1").fetchone()
                    except Exception:
                        batch = None
                if batch:
                    break
                time.sleep(0.05)
            poller.stop()
            self.assertIsNotNone(batch)
            channels = json.loads(batch["channels_json"])
            self.assertEqual(channels["pressure_bar"], [12.345])
            self.assertEqual(channels["voltage_v"], [0.873])

    def test_registry_binding_maps_existing_pressure_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.db"
            with connect_database(path) as db:
                db.executescript(
                    """
                    CREATE TABLE devices(operation_id TEXT,id TEXT,name TEXT,device_type TEXT,protocol TEXT,endpoint TEXT,
                      health TEXT,recording TEXT,required INTEGER,PRIMARY KEY(operation_id,id));
                    CREATE TABLE device_integrations(operation_id TEXT,device_id TEXT,adapter_type TEXT,config_json TEXT,
                      enabled INTEGER,last_test_at TEXT,last_test_status TEXT,last_test_message TEXT,
                      PRIMARY KEY(operation_id,device_id));
                    CREATE TABLE channels(operation_id TEXT,id TEXT,name TEXT,unit TEXT,source_id TEXT,quality TEXT,
                      warning REAL,critical REAL,sample_rate INTEGER,PRIMARY KEY(operation_id,id));
                    CREATE TABLE channel_integrations(operation_id TEXT,channel_id TEXT,raw_field TEXT,
                      calibration_slope REAL,calibration_intercept REAL,stale_timeout_ms INTEGER,
                      required_for_commit INTEGER,PRIMARY KEY(operation_id,channel_id));
                    CREATE TABLE test_runs(id INTEGER PRIMARY KEY,active INTEGER);
                    """
                )
                db.execute("INSERT INTO devices VALUES(?,?,?,?,?,?,?,?,?)",
                           ("OP","PT-01","Chamber Pressure","PRESSURE","ANALOG-DAQ","DAQ-01/AI-01","SIMULATED","N/A",1))
                db.execute("INSERT INTO device_integrations VALUES(?,?,?,?,?,?,?,?)",
                           ("OP","PT-01","SIMULATOR","{}",1,None,"NOT_TESTED",None))
                db.execute("INSERT INTO channels VALUES(?,?,?,?,?,?,?,?,?)",
                           ("OP","motor.chamber_pressure","Chamber pressure","bar","PT-01","SIMULATED",55.0,70.0,1000))
                db.execute("INSERT INTO channel_integrations VALUES(?,?,?,?,?,?,?)",
                           ("OP","motor.chamber_pressure","chamber_pressure",1.0,0.0,100,1))
                db.commit()

            endpoint = f"http://127.0.0.1:{self.server.server_port}/reading"
            with patch.dict(os.environ, {"STELLAR_OPS_ESP32_PRESSURE_ENDPOINT": endpoint}, clear=False):
                result = ensure_esp32_pressure_integration(path, "OP")
            self.assertEqual(result["physical_transport"], "HTTP_JSON")
            with connect_database(path) as db:
                device = db.execute("SELECT protocol,endpoint FROM devices WHERE operation_id='OP' AND id='PT-01'").fetchone()
                integration = db.execute("SELECT adapter_type,config_json FROM device_integrations WHERE operation_id='OP' AND device_id='PT-01'").fetchone()
                channel = db.execute("SELECT sample_rate FROM channels WHERE operation_id='OP' AND id='motor.chamber_pressure'").fetchone()
                mapping = db.execute("SELECT raw_field,stale_timeout_ms FROM channel_integrations WHERE operation_id='OP' AND channel_id='motor.chamber_pressure'").fetchone()
            self.assertEqual(device["protocol"], "HTTP-JSON")
            self.assertEqual(device["endpoint"], endpoint)
            self.assertEqual(integration["adapter_type"], "SMTCS_EDGE_TCP")
            self.assertEqual(mapping["raw_field"], "pressure_bar")
            self.assertGreaterEqual(mapping["stale_timeout_ms"], 1000)
            self.assertGreaterEqual(channel["sample_rate"], 1)


if __name__ == "__main__":
    unittest.main()
