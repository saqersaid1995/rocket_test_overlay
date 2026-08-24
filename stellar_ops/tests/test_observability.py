import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module


class ObservabilityTests(unittest.TestCase):
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

    def test_responses_carry_correlation_and_server_timing(self):
        response = self.client.get(
            "/health/live",
            headers={"X-Request-ID": "operator-console-request-001"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["X-Request-ID"],
            "operator-console-request-001",
        )
        self.assertIn("app;dur=", response.headers["Server-Timing"])

    def test_self_test_records_component_results(self):
        initial = self.client.post("/api/control/diagnostics/self-test", json={})
        self.assertEqual(initial.status_code, 200)
        result = initial.get_json()["diagnostic"]
        self.assertEqual(result["overall_status"], "WARN")
        self.assertEqual(
            next(
                check
                for check in result["checks"]
                if check["code"] == "RECOVERY_BACKUP"
            )["status"],
            "WARN",
        )

        backup = self.client.post(
            "/api/control/backups",
            json={"reason": "Diagnostic recovery baseline"},
        )
        self.assertEqual(backup.status_code, 201)
        verified = self.client.post(
            "/api/control/diagnostics/self-test",
            json={"actor": "TEST DIRECTOR"},
        )
        self.assertEqual(verified.status_code, 200)
        result = verified.get_json()["diagnostic"]
        self.assertEqual(result["overall_status"], "PASS")
        self.assertTrue(all(check["status"] == "PASS" for check in result["checks"]))

        snapshot = self.client.get("/api/control/snapshot").get_json()
        self.assertEqual(snapshot["latest_diagnostic"]["overall_status"], "PASS")
        self.assertGreaterEqual(len(snapshot["latest_diagnostic"]["checks"]), 6)

    def test_diagnostics_are_blocked_during_countdown(self):
        with control_module.connect() as db:
            db.execute(
                "UPDATE operations SET state='COUNTDOWN' WHERE id=?",
                (control_module.OPERATION_ID,),
            )
        response = self.client.post("/api/control/diagnostics/self-test", json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn("CHECKOUT or HOLD", response.get_json()["error"])

    def test_sensitive_prometheus_endpoint_is_not_public(self):
        self.assertEqual(self.client.get("/metrics").status_code, 404)


if __name__ == "__main__":
    unittest.main()
