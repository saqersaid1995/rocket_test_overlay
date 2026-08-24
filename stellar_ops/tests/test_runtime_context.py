import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module
from stellar_ops.runtime_context import activate_released_operation, get_runtime_context


class RuntimeContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = control_module.CONTROL_DB
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.assertEqual(self.client.get("/ops").status_code, 200)

    def tearDown(self):
        control_module.CONTROL_DB = self.original
        self.temp.cleanup()

    def activate_demo_release(self):
        with control_module.connect() as db:
            operation = db.execute(
                "SELECT * FROM operation_registry WHERE code='DEMO-SF-001'"
            ).fetchone()
            release = db.execute(
                "SELECT * FROM execution_releases WHERE operation_id=?",
                (operation["id"],),
            ).fetchone()
            self.assertIsNotNone(release)
            return activate_released_operation(
                db,
                runtime_operation_id=control_module.OPERATION_ID,
                registry_operation=operation,
                execution_release=release,
                release_sha256=release["release_sha256"] or "a" * 64,
            )

    def test_release_creates_and_pins_a_run(self):
        activated = self.activate_demo_release()
        with control_module.connect() as db:
            context = get_runtime_context(db)
            run = db.execute(
                "SELECT * FROM test_runs WHERE id=?", (context["active_run_id"],)
            ).fetchone()
            runtime = db.execute(
                "SELECT * FROM operations WHERE id=?",
                (control_module.OPERATION_ID,),
            ).fetchone()

        self.assertEqual(context["context_state"], "RELEASED")
        self.assertEqual(context["registry_operation_id"], activated["registry_operation_id"])
        self.assertEqual(context["execution_release_id"], activated["execution_release_id"])
        self.assertEqual(run["registry_operation_id"], activated["registry_operation_id"])
        self.assertEqual(run["execution_release_id"], activated["execution_release_id"])
        self.assertEqual(run["release_sha256"], activated["release_sha256"])
        self.assertTrue(run["active"])
        self.assertEqual(runtime["code"], "DEMO-SF-001")
        self.assertEqual(runtime["mode"], "LIVE")
        self.assertEqual(runtime["state"], "CHECKOUT")

    def test_snapshot_exposes_same_operation_release_and_run(self):
        activated = self.activate_demo_release()
        snapshot = self.client.get("/api/control/snapshot").get_json()
        self.assertEqual(
            snapshot["runtime_context"]["registry_operation_id"],
            activated["registry_operation_id"],
        )
        self.assertEqual(
            snapshot["runtime_context"]["active_run_id"],
            next(run["id"] for run in snapshot["runs"] if run["active"]),
        )
        self.assertEqual(snapshot["operation"]["code"], "DEMO-SF-001")

    def test_released_context_blocks_manual_run_changes(self):
        activated = self.activate_demo_release()
        created = self.client.post(
            "/api/control/run",
            json={"code": "MANUAL-001", "title": "Manual", "test_article": "Manual"},
        )
        self.assertEqual(created.status_code, 409)

        with control_module.connect() as db:
            legacy = db.execute(
                "SELECT id FROM test_runs WHERE id!=? ORDER BY id LIMIT 1",
                (activated["run_id"],),
            ).fetchone()
        blocked = self.client.post(f"/api/control/run/{legacy['id']}/activate")
        self.assertEqual(blocked.status_code, 409)


if __name__ == "__main__":
    unittest.main()
