import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
