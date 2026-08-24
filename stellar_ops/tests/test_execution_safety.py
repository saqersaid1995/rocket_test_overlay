import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module


class ExecutionSafetyTests(unittest.TestCase):
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

    def test_command_id_is_idempotent_and_journaled_once(self):
        headers = {"X-Command-ID": "hold-command-001"}
        first = self.client.post(
            "/api/control/command",
            json={"action": "HOLD", "reason": "Range verification"},
            headers=headers,
        )
        second = self.client.post(
            "/api/control/command",
            json={"action": "HOLD", "reason": "Different duplicate payload"},
            headers=headers,
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["replayed"])
        self.assertEqual(second.get_json()["command_id"], "hold-command-001")
        with control_module.connect() as db:
            count = db.execute(
                "SELECT count(*) FROM command_journal WHERE command_id=?",
                ("hold-command-001",),
            ).fetchone()[0]
            operation = db.execute(
                "SELECT state,active_hold FROM operations WHERE id=?",
                (control_module.OPERATION_ID,),
            ).fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(operation["state"], "HOLD")
        self.assertEqual(operation["active_hold"], "Range verification")

    def test_rejected_command_is_recorded_with_reason(self):
        response = self.client.post(
            "/api/control/command",
            json={"action": "FIRE", "command_id": "invalid-fire-001"},
        )
        self.assertEqual(response.status_code, 409)
        with control_module.connect() as db:
            journal = db.execute(
                """SELECT outcome,from_state,to_state,reason,http_status
                   FROM command_journal WHERE command_id=?""",
                ("invalid-fire-001",),
            ).fetchone()
        self.assertEqual(journal["outcome"], "REJECTED")
        self.assertEqual(journal["from_state"], "CHECKOUT")
        self.assertEqual(journal["to_state"], "CHECKOUT")
        self.assertIn("not valid", journal["reason"])
        self.assertEqual(journal["http_status"], 409)

    def test_live_fire_revalidates_the_pinned_execution_release(self):
        with control_module.connect() as db:
            db.execute(
                "UPDATE operations SET state='COUNTDOWN',mode='LIVE' WHERE id=?",
                (control_module.OPERATION_ID,),
            )
            db.execute(
                "UPDATE runtime_context SET context_state='CLOSED' WHERE id=1"
            )

        response = self.client.post(
            "/api/control/command",
            json={"action": "FIRE", "command_id": "stale-release-fire-001"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("not RELEASED", response.get_json()["error"])
        with control_module.connect() as db:
            operation = db.execute(
                "SELECT state FROM operations WHERE id=?",
                (control_module.OPERATION_ID,),
            ).fetchone()
            journal = db.execute(
                "SELECT outcome,reason FROM command_journal WHERE command_id=?",
                ("stale-release-fire-001",),
            ).fetchone()
        self.assertEqual(operation["state"], "COUNTDOWN")
        self.assertEqual(journal["outcome"], "REJECTED")
        self.assertIn("not RELEASED", journal["reason"])

    def test_restart_during_countdown_enters_fail_safe_hold(self):
        with control_module.connect() as db:
            db.execute(
                "UPDATE operations SET state='COUNTDOWN' WHERE id=?",
                (control_module.OPERATION_ID,),
            )
            db.execute(
                "UPDATE runtime_boot SET boot_id='previous-process' WHERE id=1"
            )
        control_module.init_control_db()
        with control_module.connect() as db:
            operation = db.execute(
                """SELECT state,prior_state,active_hold,firing_started_monotonic
                   FROM operations WHERE id=?""",
                (control_module.OPERATION_ID,),
            ).fetchone()
            recovery = db.execute(
                """SELECT event_type,severity,message FROM events
                   WHERE operation_id=? AND event_type='RUNTIME_RECOVERY'
                   ORDER BY sequence DESC LIMIT 1""",
                (control_module.OPERATION_ID,),
            ).fetchone()
        self.assertEqual(operation["state"], "HOLD")
        self.assertEqual(operation["prior_state"], "CHECKOUT")
        self.assertIsNone(operation["firing_started_monotonic"])
        self.assertIn("restart detected", operation["active_hold"])
        self.assertEqual(recovery["severity"], "CRITICAL")


if __name__ == "__main__":
    unittest.main()
