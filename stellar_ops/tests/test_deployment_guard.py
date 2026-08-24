import os
import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module


class DeploymentGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = control_module.CONTROL_DB
        self.original_secret = app.config["SECRET_KEY"]
        self.original_env = {
            key: os.environ.get(key)
            for key in (
                "STELLAR_OPS_ENV",
                "STELLAR_OPS_MAINTENANCE",
                "STELLAR_OPS_PUBLIC_URL",
                "STELLAR_OPS_BACKUPS",
                "STELLAR_OPS_COMMIT",
                "FLASK_DEBUG",
            )
        }
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        os.environ["STELLAR_OPS_ENV"] = "DEVELOPMENT"
        os.environ.pop("STELLAR_OPS_MAINTENANCE", None)
        self.client = app.test_client()
        self.client.get("/ops")

    def tearDown(self):
        control_module.CONTROL_DB = self.original_db
        app.config["SECRET_KEY"] = self.original_secret
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_security_and_no_cache_headers_are_present(self):
        response = self.client.get("/health")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors", response.headers["Content-Security-Policy"])
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_maintenance_mode_blocks_mutations_but_not_reads(self):
        os.environ["STELLAR_OPS_MAINTENANCE"] = "1"
        blocked = self.client.post(
            "/api/control/command",
            json={"action": "HOLD", "command_id": "maintenance-hold"},
        )
        self.assertEqual(blocked.status_code, 503)
        self.assertIn("maintenance mode", blocked.get_json()["error"])
        self.assertEqual(self.client.get("/api/control/snapshot").status_code, 200)

    def test_insecure_production_configuration_is_fail_closed(self):
        os.environ["STELLAR_OPS_ENV"] = "PRODUCTION"
        os.environ["STELLAR_OPS_COMMIT"] = "abcdef1234567890"
        os.environ["STELLAR_OPS_PUBLIC_URL"] = "https://ops.example.invalid"
        app.config["SECRET_KEY"] = "short"

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 503)
        deployment = health.get_json()["deployment"]
        self.assertEqual(deployment["status"], "BLOCKED")
        self.assertFalse(deployment["production_authorized"])

        mutation = self.client.post(
            "/api/control/command",
            json={"action": "HOLD", "command_id": "production-hold"},
        )
        self.assertEqual(mutation.status_code, 503)
        self.assertIn("production mutation refused", mutation.get_json()["error"])

    def test_development_remains_usable_with_explicit_warnings(self):
        app.config["SECRET_KEY"] = "development-only-change-me"
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertIn(
            health.get_json()["deployment"]["status"],
            {"WARN", "BLOCKED"},
        )
        command = self.client.post(
            "/api/control/command",
            json={
                "action": "HOLD",
                "reason": "Development guard verification",
                "command_id": "development-hold",
            },
        )
        self.assertEqual(command.status_code, 200)


if __name__ == "__main__":
    unittest.main()
