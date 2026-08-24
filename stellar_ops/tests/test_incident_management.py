import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module


class IncidentManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = control_module.CONTROL_DB
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.client.get("/ops")

    def tearDown(self):
        control_module.CONTROL_DB = self.original
        self.temp.cleanup()

    def test_manual_incident_requires_controlled_lifecycle(self):
        opened = self.client.post(
            "/api/control/incident",
            json={
                "severity": "P2",
                "category": "PROCEDURE",
                "title": "Hold-point verification discrepancy",
                "description": "Recorded verification does not match the released procedure.",
                "owner": "TEST DIRECTOR",
            },
        )
        self.assertEqual(opened.status_code, 201)
        incident = opened.get_json()["incident"]
        incident_id = incident["id"]
        self.assertTrue(incident["incident_code"].startswith("INC-"))
        self.assertEqual(incident["status"], "OPEN")

        invalid_close = self.client.post(
            f"/api/control/incident/{incident_id}/action",
            json={"action": "CLOSE", "notes": "Close immediately"},
        )
        self.assertEqual(invalid_close.status_code, 409)

        for action, expected in (
            ("CONTAIN", "CONTAINED"),
            ("RESOLVE", "RESOLVED"),
            ("CLOSE", "CLOSED"),
        ):
            response = self.client.post(
                f"/api/control/incident/{incident_id}/action",
                json={"action": action, "notes": f"{action} evidence recorded"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["incident"]["status"], expected)

        with control_module.connect() as db:
            actions = db.execute(
                """SELECT action FROM incident_actions
                   WHERE incident_id=? ORDER BY id""",
                (incident_id,),
            ).fetchall()
        self.assertEqual(
            [row["action"] for row in actions],
            ["OPEN", "CONTAIN", "RESOLVE", "CLOSE"],
        )

    def test_p1_alarm_creates_incident_and_holds_countdown(self):
        with control_module.connect() as db:
            run = db.execute(
                "SELECT id FROM test_runs WHERE active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            db.execute(
                "UPDATE operations SET state='COUNTDOWN' WHERE id=?",
                (control_module.OPERATION_ID,),
            )
            db.execute(
                """INSERT INTO alarms(
                       operation_id,opened_at,priority,source,message,state,run_id)
                   VALUES(?,datetime('now'),'P1','PT-01',
                          'Chamber pressure exceeded critical limit',
                          'ACTIVE_UNACKNOWLEDGED',?)""",
                (control_module.OPERATION_ID, run["id"]),
            )

        snapshot = self.client.get("/api/control/snapshot")
        self.assertEqual(snapshot.status_code, 200)
        body = snapshot.get_json()
        self.assertEqual(body["operation"]["state"], "HOLD")
        self.assertIn("P1 alarm", body["operation"]["active_hold"])
        self.assertEqual(len(body["incidents"]), 1)
        self.assertEqual(body["incidents"][0]["severity"], "P1")
        self.assertEqual(body["incidents"][0]["status"], "OPEN")

        second = self.client.get("/api/control/snapshot").get_json()
        self.assertEqual(len(second["incidents"]), 1)

    def test_active_p1_blocks_countdown_commit(self):
        with control_module.connect() as db:
            db.execute(
                """INSERT INTO alarms(
                       operation_id,opened_at,priority,source,message,state,run_id)
                   VALUES(?,datetime('now'),'P1','SYSTEM',
                          'Unresolved critical condition',
                          'ACTIVE_ACKNOWLEDGED',NULL)""",
                (control_module.OPERATION_ID,),
            )
            db.execute(
                "UPDATE stations SET decision='GO' WHERE operation_id=?",
                (control_module.OPERATION_ID,),
            )
            db.execute(
                """UPDATE procedure_steps SET status='COMPLETE'
                   WHERE operation_id=? AND sequence<=90""",
                (control_module.OPERATION_ID,),
            )
        response = self.client.post(
            "/api/control/command",
            json={"action": "COUNTDOWN", "command_id": "p1-block-001"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("active P1", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
