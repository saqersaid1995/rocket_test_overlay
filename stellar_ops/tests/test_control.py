import tempfile
import unittest
import io
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


if __name__ == "__main__":
    unittest.main()
