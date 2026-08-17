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

    def create_operation(self, code="QSF-010", operation_type="STATIC_FIRE"):
        response = self.client.post("/api/ops", json={"mission_id": 1, "code": code, "title": "Article workflow",
            "operation_type": operation_type, "site": "Test Site", "objective": "Identify test hardware",
            "success_criteria": ["Hardware identity verified"], "owner": "Test Director"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["id"]

    def test_static_fire_article_requires_complete_component_genealogy(self):
        operation_id = self.create_operation()
        article = {"article_class": "MOTOR_ASSEMBLY", "serial_number": "RNX71V-SN-010",
                   "name": "Qualification Motor", "family": "RNX-71V", "configuration_revision": "REV-B",
                   "build_status": "INTEGRATED", "components": [
                       {"component_type": "CASE", "serial_or_lot": "CASE-010", "status": "VERIFIED"},
                       {"component_type": "NOZZLE", "serial_or_lot": "NZL-010", "status": "INSTALLED"},
                   ]}
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/article", json=article).status_code, 200)
        blocked = self.client.post(f"/api/ops/{operation_id}/article/complete")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("IGNITER", blocked.get_json()["error"])
        article["components"] += [
            {"component_type": "PROPELLANT_BATCH", "serial_or_lot": "RNX-BATCH-010", "status": "ASSIGNED"},
            {"component_type": "IGNITER", "serial_or_lot": "IGN-010", "status": "INSTALLED"},
        ]
        self.client.post(f"/api/ops/{operation_id}/article", json=article)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/article/complete").status_code, 200)
        with control_module.connect() as db:
            op = db.execute("SELECT current_stage FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            article_row = db.execute("SELECT state FROM test_articles WHERE operation_id=?", (operation_id,)).fetchone()
            baseline = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='BASELINE'", (operation_id,)).fetchone()
        self.assertEqual(op["current_stage"], "BASELINE")
        self.assertEqual(article_row["state"], "IDENTIFIED")
        self.assertEqual(baseline["status"], "ACTIVE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/article", json=article).status_code, 409)

    def test_launch_article_uses_vehicle_specific_requirements(self):
        operation_id = self.create_operation("QLAUNCH-010", "ROCKET_LAUNCH")
        page = self.client.get(f"/ops/{operation_id}/article")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"FLIGHT_VEHICLE", page.data)
        payload = {"article_class": "FLIGHT_VEHICLE", "serial_number": "QSRM-FV-010", "name": "QualSRM",
                   "family": "QualSRM", "configuration_revision": "REV-A", "components": [
                       {"component_type": "PROPULSION", "serial_or_lot": "MTR-010"}]}
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/article", json=payload).status_code, 200)
        blocked = self.client.post(f"/api/ops/{operation_id}/article/complete")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("AVIONICS", blocked.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
