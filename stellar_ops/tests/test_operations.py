import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module


class OperationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = control_module.CONTROL_DB
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        control_module.CONTROL_DB = self.original
        self.temp.cleanup()

    def test_root_is_operations_home_and_seeded_operation_has_workflow(self):
        root = self.client.get("/")
        self.assertEqual(root.status_code, 302)
        self.assertTrue(root.location.endswith("/ops"))
        home = self.client.get("/ops")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"Mission & Operation Control", home.data)
        self.assertIn(b"QST-001", home.data)
        detail = self.client.get("/ops/1")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"OPERATION WORKFLOW", detail.data)
        self.assertIn(b"Test Article / Vehicle", detail.data)

    def test_builder_creates_controlled_operation_and_unlocks_article(self):
        response = self.client.post("/api/ops", json={
            "mission_id": 1, "code": "QSF-002", "title": "Secondary qualification static fire",
            "operation_type": "STATIC_FIRE", "site": "Al Buraimi Test Site",
            "objective": "Verify the revised insulation and nozzle interface.",
            "success_criteria": ["Stable ignition", "No structural leakage"],
            "owner": "Test Director", "risk_class": "HAZARDOUS",
        })
        self.assertEqual(response.status_code, 200)
        detail = self.client.get(response.get_json()["url"])
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"QSF-002", detail.data)
        with control_module.connect() as db:
            sections = db.execute("SELECT section_key,status FROM operation_workflow_sections WHERE operation_id=? ORDER BY sequence", (response.get_json()["id"],)).fetchall()
        self.assertEqual(sections[0]["status"], "COMPLETE")
        self.assertEqual(sections[1]["section_key"], "ARTICLE")
        self.assertEqual(sections[1]["status"], "ACTIVE")
        self.assertTrue(all(row["status"] == "LOCKED" for row in sections[2:]))

    def test_builder_rejects_incomplete_or_duplicate_identity(self):
        self.assertEqual(self.client.post("/api/ops", json={"code": "bad"}).status_code, 400)
        payload = {"mission_id": 1, "code": "QSF-003", "title": "Test", "operation_type": "STATIC_FIRE",
                   "site": "Site", "objective": "Objective", "success_criteria": ["Criterion"], "owner": "TD"}
        self.assertEqual(self.client.post("/api/ops", json=payload).status_code, 200)
        self.assertEqual(self.client.post("/api/ops", json=payload).status_code, 409)


if __name__ == "__main__":
    unittest.main()
