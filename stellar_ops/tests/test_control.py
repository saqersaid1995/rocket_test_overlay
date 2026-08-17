import tempfile
import unittest
import io
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module


class StaticTestControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = control_module.CONTROL_DB
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        control_module.CONTROL_DB = self.original
        self.temp.cleanup()

    def test_console_and_snapshot_contract(self):
        response = self.client.get("/control")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"STELLAR MISSION & TEST CONTROL", response.data)
        snapshot = self.client.get("/api/control/snapshot").get_json()
        self.assertEqual(snapshot["operation"]["state"], "CHECKOUT")
        self.assertEqual(len(snapshot["stations"]), 7)
        self.assertEqual(len(snapshot["devices"]), 8)
        self.assertEqual(len(snapshot["steps"]), 14)
        workspace = self.client.get("/workspace")
        self.assertEqual(workspace.status_code,200)
        self.assertIn(b"MISSION CONTROL WORKSPACE",workspace.data)
        self.assertEqual(len(snapshot["workspaces"]),5)
        self.assertTrue(snapshot["runs"][0]["active"])
        health=self.client.get("/health")
        self.assertEqual(health.status_code,200)
        self.assertEqual(health.get_json()["database"]["journal_mode"],"WAL")

    def test_countdown_is_blocked_until_go_and_procedure_complete(self):
        blocked = self.client.post("/api/control/command", json={"action": "COUNTDOWN"})
        self.assertEqual(blocked.status_code, 409)
        snapshot = self.client.get("/api/control/snapshot").get_json()
        for station in snapshot["stations"]:
            self.client.post(f"/api/control/station/{station['code']}", json={"decision": "GO"})
        still_blocked = self.client.post("/api/control/command", json={"action": "COUNTDOWN"})
        self.assertEqual(still_blocked.status_code, 409)
        for sequence in (10, 20, 30, 40, 50, 60, 70, 80, 90):
            completed = self.client.post(f"/api/control/step/{sequence}/complete")
            self.assertEqual(completed.status_code, 200)
        accepted = self.client.post("/api/control/command", json={"action": "COUNTDOWN"})
        self.assertEqual(accepted.status_code, 200)

    def test_hold_resume_and_abort_are_audited(self):
        self.client.post("/api/control/command", json={"action": "HOLD", "reason": "Range review"})
        self.client.post("/api/control/command", json={"action": "RESUME"})
        self.client.post("/api/control/command", json={"action": "ABORT"})
        snapshot = self.client.get("/api/control/snapshot").get_json()
        self.assertEqual(snapshot["operation"]["state"], "ABORTED")
        kinds = {event["event_type"] for event in snapshot["events"]}
        self.assertTrue({"HOLD", "HOLD_RELEASE", "ABORT"}.issubset(kinds))

    def test_device_channel_and_replay_configuration(self):
        device = self.client.post("/api/control/device", json={
            "id": "DAQ-02", "name": "Qualification DAQ", "device_type": "DAQ",
            "adapter_type": "SIMULATOR", "endpoint": "SIM://qualification-daq", "required": True,
        })
        self.assertEqual(device.status_code, 200)
        tested = self.client.post("/api/control/device/DAQ-02/test")
        self.assertEqual(tested.status_code, 200)
        self.assertEqual(tested.get_json()["status"], "SIMULATED")
        channel = self.client.post("/api/control/channel", json={
            "id": "motor.pressure_2", "name": "Secondary chamber pressure", "unit": "bar",
            "source_id": "DAQ-02", "raw_field": "ai0", "slope": 1.25, "intercept": -0.04,
            "sample_rate": 1000, "stale_timeout_ms": 100, "warning": 55, "critical": 70,
            "required": True,
        })
        self.assertEqual(channel.status_code, 200)
        replay = self.client.post("/api/control/replay", data={
            "file": (io.BytesIO(b"time,pressure,thrust\n0,0,0\n0.01,2.1,14\n"), "test.csv")
        }, content_type="multipart/form-data")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()["row_count"], 2)
        snapshot = self.client.get("/api/control/snapshot").get_json()
        self.assertTrue(any(item["device_id"] == "DAQ-02" for item in snapshot["integrations"]))
        self.assertTrue(any(item["channel_id"] == "motor.pressure_2" for item in snapshot["channel_integrations"]))

    def test_replay_mode_transport_drives_runtime_telemetry(self):
        missing = self.client.post("/api/control/mode", json={"mode": "REPLAY"})
        self.assertEqual(missing.status_code, 409)
        replay = self.client.post("/api/control/replay", data={
            "file": (io.BytesIO(
                b"chamber_pressure,thrust,case_temperature,continuity\n0,0,28,0\n12.5,84,29,0\n"), "run.csv")
        }, content_type="multipart/form-data")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(self.client.post("/api/control/mode", json={"mode": "REPLAY"}).status_code, 200)
        self.assertEqual(self.client.post("/api/control/replay/control", json={"action": "SEEK", "cursor": 1}).status_code, 200)
        snapshot = self.client.get("/api/control/snapshot").get_json()
        self.assertEqual(snapshot["telemetry"]["source_mode"], "REPLAY")
        self.assertEqual(snapshot["telemetry"]["pressure"], 12.5)
        self.assertEqual(snapshot["telemetry"]["channels"]["motor.thrust"]["quality"], "REPLAY")

    def test_live_mode_reports_quality_and_requires_recording_for_countdown(self):
        self.client.get("/api/control/snapshot")
        self.assertEqual(self.client.post("/api/control/mode", json={"mode": "LIVE"}).status_code, 200)
        disconnected = self.client.get("/api/control/snapshot").get_json()
        self.assertEqual(disconnected["telemetry"]["meta"]["status"], "NO_DEVICE")
        self.assertEqual(disconnected["telemetry"]["channels"]["motor.thrust"]["quality"], "DISCONNECTED")
        live_devices = {item["id"]: item for item in disconnected["devices"]}
        self.assertEqual(live_devices["DAQ-01"]["health"], "NOT_CONNECTED")
        self.assertEqual(live_devices["PT-01"]["health"], "DISCONNECTED")
        self.assertEqual(live_devices["CAM-01"]["health"], "NOT_CONNECTED")
        self.assertNotIn("SIMULATED", {item["health"] for item in disconnected["devices"]})

        registered = self.client.post("/api/control/device", json={
            "id":"ESP-DAQ-01","name":"Ethernet gateway","device_type":"DAQ","adapter_type":"SMTCS_EDGE_TCP",
            "endpoint":"127.0.0.1:9100","required":True})
        self.assertEqual(registered.status_code,200)
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        channels = {"chamber_pressure": [61.2], "thrust": [430.0], "case_temperature": [42.0], "continuity": [0]}
        with control_module.connect() as db:
            db.execute("""INSERT INTO edge_sessions(device_id,boot_id,remote_addr,firmware,connected_at,last_seen,last_sequence,total_samples,sequence_gaps,status)
                VALUES('ESP-DAQ-01','boot-test','127.0.0.1:4000','test',?,?,0,1,0,'STREAMING')""", (stamp, stamp))
            db.execute("""INSERT INTO edge_batches(device_id,boot_id,sequence,received_at,first_sample_us,sample_period_us,sample_count,channels_json)
                VALUES('ESP-DAQ-01','boot-test',0,?,0,1000,1,?)""", (stamp, json.dumps(channels)))
        live = self.client.get("/api/control/snapshot").get_json()
        self.assertEqual(live["telemetry"]["pressure"], 61.2)
        self.assertTrue(all(item["quality"] == "GOOD" for item in live["telemetry"]["channels"].values()))

        for station in live["stations"]:
            self.client.post(f"/api/control/station/{station['code']}", json={"decision": "GO"})
        for sequence in (10, 20, 30, 40, 50, 60, 70, 80, 90):
            self.client.post(f"/api/control/step/{sequence}/complete")
        blocked = self.client.post("/api/control/command", json={"action": "COUNTDOWN"})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("recording", blocked.get_json()["error"])
        self.assertEqual(self.client.post("/api/control/recording", json={"action": "START"}).status_code, 200)
        # The real edge gateway continuously refreshes last_seen while streaming. Refresh the
        # synthetic session immediately before the commit check so this test represents a live
        # source rather than a single batch that correctly becomes STALE during procedure setup.
        fresh_stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with control_module.connect() as db:
            db.execute("""UPDATE edge_sessions SET last_seen=?,status='STREAMING'
                WHERE device_id='ESP-DAQ-01' AND boot_id='boot-test'""", (fresh_stamp,))
            db.execute("""UPDATE edge_batches SET received_at=?
                WHERE device_id='ESP-DAQ-01' AND boot_id='boot-test' AND sequence=0""", (fresh_stamp,))
        accepted = self.client.post("/api/control/command", json={"action": "COUNTDOWN"})
        self.assertEqual(accepted.status_code, 200)

    def test_source_cannot_change_during_recording(self):
        self.client.get("/api/control/snapshot")
        self.assertEqual(self.client.post("/api/control/recording", json={"action": "START"}).status_code, 200)
        blocked = self.client.post("/api/control/mode", json={"mode": "LIVE"})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("stop", blocked.get_json()["error"])

    def test_device_and_channel_archive_restore_lifecycle(self):
        self.client.get("/api/control/snapshot")
        blocked = self.client.post("/api/control/device/PT-01/state", json={"enabled": False})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("motor.chamber_pressure", blocked.get_json()["error"])
        self.assertEqual(self.client.post("/api/control/channel/motor.chamber_pressure/state", json={"enabled": False}).status_code, 200)
        self.assertEqual(self.client.post("/api/control/device/PT-01/state", json={"enabled": False}).status_code, 200)
        snapshot = self.client.get("/api/control/snapshot").get_json()
        devices = {item["id"]: item for item in snapshot["devices"]}
        channels = {item["id"]: item for item in snapshot["channels"]}
        self.assertFalse(devices["PT-01"]["enabled"])
        self.assertEqual(devices["PT-01"]["health"], "DISABLED")
        self.assertFalse(channels["motor.chamber_pressure"]["enabled"])
        self.assertNotIn("motor.chamber_pressure", snapshot["telemetry"]["channels"])
        cannot_restore = self.client.post("/api/control/channel/motor.chamber_pressure/state", json={"enabled": True})
        self.assertEqual(cannot_restore.status_code, 409)
        self.assertEqual(self.client.post("/api/control/device/PT-01/state", json={"enabled": True}).status_code, 200)
        self.assertEqual(self.client.post("/api/control/channel/motor.chamber_pressure/state", json={"enabled": True}).status_code, 200)

    def test_configuration_validation_and_recording_guard(self):
        invalid_camera = self.client.post("/api/control/device", json={
            "id":"CAM-03","name":"Pad view","device_type":"IP-CAMERA","adapter_type":"MODBUS_TCP",
            "endpoint":"10.0.0.5:502","required":True})
        self.assertEqual(invalid_camera.status_code, 400)
        invalid_limit = self.client.post("/api/control/channel", json={
            "id":"motor.bad","name":"Bad limits","unit":"bar","source_id":"PT-01","raw_field":"bad",
            "slope":1,"intercept":0,"sample_rate":10,"stale_timeout_ms":100,"warning":80,"critical":70})
        self.assertEqual(invalid_limit.status_code, 400)
        self.assertEqual(self.client.post("/api/control/recording",json={"action":"START"}).status_code,200)
        blocked = self.client.post("/api/control/device/PT-01/state",json={"enabled":False})
        self.assertEqual(blocked.status_code,409)
        self.assertIn("recording",blocked.get_json()["error"])

    def test_workspace_run_and_alarm_workflows(self):
        saved=self.client.post("/api/control/workspace",json={"name":"Qualification Wall","console_role":"TEST DIRECTOR","layout":[{"panel":"mission","span":1},{"panel":"telemetry","span":2},{"panel":"alarms","span":1}]})
        self.assertEqual(saved.status_code,200)
        invalid=self.client.post("/api/control/workspace",json={"name":"Unsafe","console_role":"TEST DIRECTOR","layout":[{"panel":"unknown","span":1}]})
        self.assertEqual(invalid.status_code,400)
        created=self.client.post("/api/control/run",json={"code":"RUN-SRM-2026-002","title":"Development static fire","test_article":"RNX-71V / SN-002","configuration_revision":"REV-B","propellant_batch":"B-006"})
        self.assertEqual(created.status_code,200)
        run_id=created.get_json()["id"]
        self.assertEqual(self.client.post(f"/api/control/run/{run_id}/activate").status_code,200)
        snapshot=self.client.get("/api/control/snapshot").get_json()
        self.assertEqual(next(r for r in snapshot["runs"] if r["active"])["code"],"RUN-SRM-2026-002")

        self.assertEqual(self.client.post("/api/control/mode",json={"mode":"LIVE"}).status_code,200)
        alarm_snapshot=self.client.get("/api/control/snapshot").get_json()
        alarm=alarm_snapshot["alarms"][0]
        ack=self.client.post(f"/api/control/alarm/{alarm['id']}/action",json={"action":"ACKNOWLEDGE"})
        self.assertEqual(ack.status_code,200)
        close=self.client.post(f"/api/control/alarm/{alarm['id']}/action",json={"action":"CLOSE","reason":"operator request"})
        self.assertEqual(close.status_code,409)

    def test_database_migration_and_evidence_package_integrity(self):
        self.client.get("/api/control/snapshot")
        with control_module.connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(),"wal")
            migration=db.execute("SELECT name FROM schema_migrations WHERE version=1").fetchone()
            self.assertIsNotNone(migration)
            active_run=db.execute("SELECT id FROM test_runs WHERE active=1").fetchone()[0]
        started=self.client.post("/api/control/recording",json={"action":"START"})
        self.assertEqual(started.status_code,200)
        stopped=self.client.post("/api/control/recording",json={"action":"STOP"})
        self.assertEqual(stopped.status_code,200)
        evidence=stopped.get_json()["evidence"]
        manifest_path=Path(evidence["manifest_path"])
        self.assertTrue(manifest_path.exists())
        self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(),evidence["sha256"])
        manifest=json.loads(manifest_path.read_text())
        self.assertEqual(manifest["run"]["id"],active_run)
        telemetry_path=manifest_path.parent/manifest["telemetry"]["file"]
        self.assertTrue(telemetry_path.exists())
        self.assertEqual(hashlib.sha256(telemetry_path.read_bytes()).hexdigest(),manifest["telemetry"]["file_sha256"])
        with control_module.connect() as db:
            package=db.execute("SELECT * FROM evidence_packages WHERE id=?",(evidence["package_id"],)).fetchone()
        self.assertEqual(package["state"],"SEALED")
        self.assertEqual(package["manifest_sha256"],evidence["sha256"])


if __name__ == "__main__":
    unittest.main()
