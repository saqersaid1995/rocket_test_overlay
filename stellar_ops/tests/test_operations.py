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

    def test_training_operation_is_prefilled_and_every_workflow_page_is_navigable(self):
        register = self.client.get("/ops")
        self.assertEqual(register.status_code, 200)
        self.assertIn(b"DEMO-SF-001", register.data)
        with control_module.connect() as db:
            demo = db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()
        detail = self.client.get(f"/ops/{demo['id']}")
        self.assertIn(b"TRAINING / DEMONSTRATION RECORD", detail.data)
        for page in ("article", "baseline", "team", "procedure", "instrumentation", "video",
                     "readiness", "rehearsal", "execution", "review", "planning", "safety", "documents", "handbook", "briefing", "changes"):
            response = self.client.get(f"/ops/{demo['id']}/{page}")
            self.assertEqual(response.status_code, 200, page)
        planning = self.client.get(f"/ops/{demo['id']}/planning")
        self.assertIn(b"Integrated Preparation Plan", planning.data)
        self.assertIn(b"TRAINING EXAMPLE", planning.data)
        self.assertIn(b"INST-040", planning.data)
        self.assertIn(b"Nasser Al Rawahi", planning.data)

    def test_planning_generation_calculates_timeline_and_enforces_dependencies(self):
        response = self.client.post("/api/ops", json={
            "mission_id": 1, "code": "QPLAN-001", "title": "Planning workflow",
            "operation_type": "STATIC_FIRE", "site": "Test Site",
            "planned_start": "2026-10-01T08:00", "objective": "Validate planning controls",
            "success_criteria": ["Preparation accepted"], "owner": "Test Director"})
        self.assertEqual(response.status_code, 200)
        operation_id = response.get_json()["id"]
        generated = self.client.post(f"/api/ops/{operation_id}/planning/generate", json={})
        self.assertEqual(generated.status_code, 200)
        self.assertEqual(generated.get_json()["created"], 10)
        with control_module.connect() as db:
            task = db.execute("SELECT planned_start,due_at,assigned_person FROM operation_tasks WHERE operation_id=? AND task_code='CFG-010'", (operation_id,)).fetchone()
        self.assertTrue(task["planned_start"].startswith("2026-09-21T08:00"))
        self.assertTrue(task["due_at"].startswith("2026-09-24T08:00"))
        self.assertEqual(task["assigned_person"], "UNASSIGNED")
        blocked = self.client.post(f"/api/ops/{operation_id}/planning/tasks/PROP-020", json={
            "assigned_person": "Training Propulsion Lead", "status": "READY_FOR_REVIEW", "blocker": ""})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("CFG-010", blocked.get_json()["error"])

    def test_blocked_planning_task_requires_reason(self):
        self.assertEqual(self.client.get("/ops").status_code,200)
        with control_module.connect() as db:
            demo = db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()
        response = self.client.post(f"/api/ops/{demo['id']}/planning/tasks/INST-030", json={
            "assigned_person": "Nasser Al Rawahi", "status": "BLOCKED", "blocker": ""})
        self.assertEqual(response.status_code, 400)

    def test_training_work_packages_explain_departments_people_raci_and_evidence(self):
        self.assertEqual(self.client.get("/ops").status_code,200)
        with control_module.connect() as db:
            demo = db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()
        center = self.client.get(f"/ops/{demo['id']}/work-packages")
        self.assertEqual(center.status_code, 200)
        self.assertIn(b"Targeted Execution Pack Control", center.data)
        self.assertIn(b"RACI / INDEPENDENT VERIFICATION MATRIX", center.data)
        self.assertIn(b"Nasser Al Rawahi", center.data)
        department = self.client.get(f"/ops/{demo['id']}/work-packages/department/INST")
        self.assertEqual(department.status_code, 200)
        self.assertIn(b"Instrumentation Execution Pack", department.data)
        self.assertIn(b"Instrumentation installation checklist", department.data)
        person = self.client.get(f"/ops/{demo['id']}/work-packages/person/INST")
        self.assertEqual(person.status_code, 200)
        self.assertIn(b"Individual Execution Pack", person.data)
        self.assertIn(b"INDEPENDENT VERIFICATION QUEUE", person.data)

    def test_task_evidence_and_independent_review_issue_acceptance(self):
        response = self.client.post("/api/ops", json={
            "mission_id": 1, "code": "QPKG-001", "title": "Work package test",
            "operation_type": "STATIC_FIRE", "site": "Test Site", "planned_start": "2026-10-01T08:00",
            "objective": "Verify work package evidence flow", "success_criteria": ["Accepted evidence"], "owner": "TD"})
        operation_id = response.get_json()["id"]
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/planning/generate", json={}).status_code, 200)
        weak = self.client.post(f"/api/ops/{operation_id}/planning/tasks/CFG-010/evidence", json={
            "evidence_code": "EVD-CFG-10", "evidence_type": "CONTROLLED_DOCUMENT", "title": "Baseline",
            "reference": "CONFIG/BASELINE", "sha256": "short", "supplied_by": "Configuration Manager", "status": "VERIFIED"})
        self.assertEqual(weak.status_code, 400)
        evidence = self.client.post(f"/api/ops/{operation_id}/planning/tasks/CFG-010/evidence", json={
            "evidence_code": "EVD-CFG-10", "evidence_type": "CONTROLLED_DOCUMENT", "title": "Baseline",
            "reference": "CONFIG/BASELINE", "sha256": "a" * 64, "supplied_by": "Configuration Manager", "status": "VERIFIED"})
        self.assertEqual(evidence.status_code, 200)
        self_review = self.client.post(f"/api/ops/{operation_id}/planning/tasks/CFG-010/review", json={
            "reviewer_role": "PROP", "reviewer_name": "UNASSIGNED", "decision": "ACCEPTED", "finding": ""})
        self.assertEqual(self_review.status_code, 409)
        accepted = self.client.post(f"/api/ops/{operation_id}/planning/tasks/CFG-010/review", json={
            "reviewer_role": "PROP", "reviewer_name": "Independent Propulsion Reviewer", "decision": "ACCEPTED", "finding": ""})
        self.assertEqual(accepted.status_code, 200)
        with control_module.connect() as db:
            task = db.execute("SELECT status FROM operation_tasks WHERE operation_id=? AND task_code='CFG-010'", (operation_id,)).fetchone()
            reviews = db.execute("SELECT count(*) n FROM task_reviews WHERE operation_id=? AND task_code='CFG-010'", (operation_id,)).fetchone()["n"]
        self.assertEqual(task["status"], "ACCEPTED")
        self.assertEqual(reviews, 1)

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

    def prepare_identified_article(self, code="QBASE-010"):
        operation_id = self.create_operation(code)
        payload = {"article_class": "MOTOR_ASSEMBLY", "serial_number": "RNX71V-BASE-010",
                   "name": "Baseline Motor", "family": "RNX-71V", "configuration_revision": "REV-B",
                   "build_status": "INTEGRATED", "components": [
                       {"component_type": kind, "serial_or_lot": f"{kind}-010", "status": "VERIFIED"}
                       for kind in ("CASE", "NOZZLE", "PROPELLANT_BATCH", "IGNITER")]}
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/article", json=payload).status_code, 200)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/article/complete").status_code, 200)
        return operation_id

    def test_baseline_release_requires_every_mandatory_verified_item(self):
        operation_id = self.prepare_identified_article()
        page = self.client.get(f"/ops/{operation_id}/baseline")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Configuration Baseline", page.data)
        draft = {"baseline_code": "QBASE-010-CB", "revision": "REV-A", "items": [
            {"item_type": "PROCEDURE", "reference": "ETP-010", "revision": "REV-A", "source": "DOC", "verification_status": "VERIFIED"}]}
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/baseline", json=draft).status_code, 200)
        blocked = self.client.post(f"/api/ops/{operation_id}/baseline/release", json={"released_by": "CM"})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("CAMERA_MANIFEST", blocked.get_json()["error"])

    def test_released_baseline_is_hashed_immutable_and_unlocks_team(self):
        operation_id = self.prepare_identified_article("QBASE-020")
        required = ["PROCEDURE", "CHANNEL_MAP", "LIMIT_PROFILE", "DEVICE_MANIFEST", "CAMERA_MANIFEST", "SOFTWARE"]
        payload = {"baseline_code": "QBASE-020-CB", "revision": "REV-A", "items": [
            {"item_type": kind, "reference": f"REF-{kind}", "revision": "REV-A", "source": "CONTROLLED_RECORD", "verification_status": "VERIFIED"}
            for kind in required]}
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/baseline", json=payload).status_code, 200)
        released = self.client.post(f"/api/ops/{operation_id}/baseline/release", json={"released_by": "Configuration Manager"})
        self.assertEqual(released.status_code, 200)
        self.assertEqual(len(released.get_json()["sha256"]), 64)
        with control_module.connect() as db:
            op = db.execute("SELECT current_stage FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            baseline = db.execute("SELECT state,canonical_sha256 FROM configuration_baselines WHERE operation_id=?", (operation_id,)).fetchone()
            team = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='TEAM'", (operation_id,)).fetchone()
        self.assertEqual(op["current_stage"], "TEAM")
        self.assertEqual(baseline["state"], "RELEASED")
        self.assertEqual(team["status"], "ACTIVE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/baseline", json=payload).status_code, 409)

    def test_baseline_release_rejects_placeholder_controlled_identities(self):
        operation_id = self.prepare_identified_article("QBASE-PLACEHOLDER")
        required = ["PROCEDURE", "CHANNEL_MAP", "LIMIT_PROFILE", "DEVICE_MANIFEST", "CAMERA_MANIFEST", "SOFTWARE"]
        payload = {"baseline_code": "QBASE-PLACEHOLDER-CB", "revision": "REV-A", "items": [
            {"item_type": kind, "reference": "UNASSIGNED" if kind == "PROCEDURE" else f"REF-{kind}",
             "revision": "WORKING" if kind == "PROCEDURE" else "REV-A", "source": "CONTROLLED_RECORD", "verification_status": "VERIFIED"}
            for kind in required]}
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/baseline", json=payload).status_code, 200)
        blocked = self.client.post(f"/api/ops/{operation_id}/baseline/release")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("PROCEDURE", blocked.get_json()["error"])

    def test_document_export_center_and_draft_package_contract(self):
        self.assertEqual(self.client.get("/ops").status_code,200)
        with control_module.connect() as db:
            operation_id = db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()["id"]
        page = self.client.get(f"/ops/{operation_id}/documents")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Document Export Center", page.data)
        generated = self.client.post(f"/api/ops/{operation_id}/documents/generate", json={
            "scope_kind": "MASTER", "scope_key": "ALL", "state": "DRAFT",
            "generated_by": "Training Document Controller", "notes": "Workflow review copy"})
        self.assertEqual(generated.status_code, 200)
        with control_module.connect() as db:
            package = db.execute("SELECT * FROM document_packages WHERE id=?", (generated.get_json()["package_id"],)).fetchone()
            files = db.execute("SELECT * FROM generated_documents WHERE package_id=? ORDER BY document_type", (package["id"],)).fetchall()
        self.assertEqual(package["state"], "DRAFT")
        self.assertEqual(len(package["manifest_sha256"]), 64)
        self.assertEqual({x["document_type"] for x in files}, {"PDF", "XLSX", "ZIP"})
        for document in files:
            self.assertEqual(len(document["sha256"]), 64)
            self.assertGreater(document["byte_size"], 0)
            self.assertEqual(self.client.get(f"/ops/{operation_id}/documents/files/{document['id']}").status_code, 200)

    def test_released_document_package_respects_preflight_controls(self):
        self.assertEqual(self.client.get("/ops").status_code,200)
        with control_module.connect() as db:
            operation_id = db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()["id"]
        blocked = self.client.post(f"/api/ops/{operation_id}/documents/generate", json={
            "scope_kind": "MASTER", "state": "RELEASED", "generated_by": "Document Control"})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("blockers", blocked.get_json())

    def test_handbook_composer_saves_and_generates_immutable_draft(self):
        self.assertEqual(self.client.get("/ops").status_code, 200)
        with control_module.connect() as db:
            operation_id = db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()["id"]
        page = self.client.get(f"/ops/{operation_id}/handbook")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Operation Handbook Composer", page.data)
        saved = self.client.post(f"/api/ops/{operation_id}/handbook", json={
            "handbook_code":"DEMO-SF-001-OEH", "revision":"A", "title":"Training Operation Execution Handbook",
            "prepared_by":"Training Document Controller", "checked_by":"Training Test Director",
            "template_key":"TECHNICAL", "distribution_classification":"INTERNAL CONTROLLED", "notes":"Training issue",
            "chapters":[{"chapter_key":"COMMUNICATIONS","included":False,"custom_note":"Formal net plan to be supplied."}]})
        self.assertEqual(saved.status_code, 200)
        generated = self.client.post(f"/api/ops/{operation_id}/handbook/generate", json={"state":"DRAFT","generated_by":"Training Document Controller"})
        self.assertEqual(generated.status_code, 200)
        with control_module.connect() as db:
            revision = db.execute("SELECT * FROM handbook_revisions WHERE id=?",(generated.get_json()["revision_id"],)).fetchone()
        self.assertEqual(revision["state"],"DRAFT")
        self.assertEqual(len(revision["sha256"]),64)
        self.assertGreater(revision["byte_size"],1000)
        self.assertEqual(self.client.get(f"/ops/{operation_id}/handbook/files/{revision['id']}").status_code,200)

    def test_targeted_execution_pack_issue_delivery_and_acknowledgement(self):
        self.assertEqual(self.client.get("/ops").status_code,200)
        with control_module.connect() as db:
            operation_id=db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()["id"]
        page=self.client.get(f"/ops/{operation_id}/work-packages/person/PROP")
        self.assertEqual(page.status_code,200);self.assertIn(b"Individual Execution Pack",page.data)
        draft=self.client.post(f"/api/ops/{operation_id}/execution-packs/generate",json={"scope_kind":"PERSON","scope_key":"PROP","state":"DRAFT","issued_by":"Training Document Control"})
        self.assertEqual(draft.status_code,200,draft.get_json())
        released=self.client.post(f"/api/ops/{operation_id}/execution-packs/generate",json={"scope_kind":"PERSON","scope_key":"PROP","state":"RELEASED","issued_by":"Training Document Control"})
        self.assertEqual(released.status_code,200,released.get_json())
        issue_id=released.get_json()["issue_id"]
        delivered=self.client.post(f"/api/ops/{operation_id}/execution-packs/{issue_id}/delivery",json={"action":"DELIVER","actor":"Training Document Control"})
        self.assertEqual(delivered.status_code,200,delivered.get_json())
        acknowledged=self.client.post(f"/api/ops/{operation_id}/execution-packs/{issue_id}/delivery",json={"action":"ACKNOWLEDGE","actor":"Maha Al Hinai","note":"Brief reviewed"})
        self.assertEqual(acknowledged.status_code,200,acknowledged.get_json())
        with control_module.connect() as db: issue=db.execute("SELECT * FROM execution_pack_issues WHERE id=?",(issue_id,)).fetchone()
        self.assertEqual(issue["delivery_status"],"ACKNOWLEDGED");self.assertEqual(issue["acknowledged_by"],"Maha Al Hinai");self.assertEqual(len(issue["sha256"]),64)
        self.assertEqual(self.client.get(f"/ops/{operation_id}/execution-packs/files/{issue_id}").status_code,200)

    def test_day_of_operation_briefing_blocks_then_freezes_complete_signoff(self):
        self.assertEqual(self.client.get("/ops").status_code,200)
        with control_module.connect() as db: operation_id=db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()["id"]
        page=self.client.get(f"/ops/{operation_id}/briefing")
        self.assertEqual(page.status_code,200);self.assertIn(b"Day-of-Operation Briefing",page.data)
        blocked=self.client.post(f"/api/ops/{operation_id}/briefing/close",json={"closed_by":"Training Test Director"})
        self.assertEqual(blocked.status_code,409);self.assertIn("findings",blocked.get_json())
        with control_module.connect() as db:
            briefing=db.execute("SELECT id FROM operation_briefings WHERE operation_id=?",(operation_id,)).fetchone()
            roles=[x["role_code"] for x in db.execute("SELECT role_code FROM briefing_attendance WHERE briefing_id=?",(briefing["id"],))]
            pack_roles=[x["role_code"] for x in db.execute("SELECT role_code FROM briefing_attendance WHERE briefing_id=? AND pack_required=1",(briefing["id"],))]
        for role in pack_roles:
            issued=self.client.post(f"/api/ops/{operation_id}/execution-packs/generate",json={"scope_kind":"PERSON","scope_key":role,"state":"RELEASED","issued_by":"Training Document Control"})
            self.assertEqual(issued.status_code,200,issued.get_json());issue_id=issued.get_json()["issue_id"]
            self.assertEqual(self.client.post(f"/api/ops/{operation_id}/execution-packs/{issue_id}/delivery",json={"action":"DELIVER","actor":"Training Document Control"}).status_code,200)
            with control_module.connect() as db: person=db.execute("SELECT person_name FROM briefing_attendance WHERE briefing_id=? AND role_code=?",(briefing["id"],role)).fetchone()["person_name"]
            self.assertEqual(self.client.post(f"/api/ops/{operation_id}/execution-packs/{issue_id}/delivery",json={"action":"ACKNOWLEDGE","actor":person,"note":"Brief reviewed"}).status_code,200)
        saved=self.client.post(f"/api/ops/{operation_id}/briefing",json={
            "briefing_code":"DEMO-SF-001-DOB","scheduled_at":"2026-09-15T07:00","location":"Al Buraimi Training Stand","chair_role":"TD","recorder":"Training Recorder",
            "weather_summary":"Clear, 32 C, wind within training limits","site_status":"VERIFIED CLEAR","change_summary":"No changes since rehearsal","notes":"Training sign-off",
            "topics":[{"topic_key":key,"status":"BRIEFED","evidence_reference":f"DEMO/BRIEF/{key}","notes":"Reviewed"} for key,_,_ in __import__('stellar_ops.operations',fromlist=['BRIEFING_TOPICS']).BRIEFING_TOPICS],
            "attendees":[{"role_code":role,"attendance_status":"PRESENT","fit_for_duty":True,"comms_check":"PASS","concern_status":"CLEAR","concern":"","signed":True} for role in roles]})
        self.assertEqual(saved.status_code,200,saved.get_json())
        closed=self.client.post(f"/api/ops/{operation_id}/briefing/close",json={"closed_by":"Training Test Director"})
        self.assertEqual(closed.status_code,200,closed.get_json());self.assertEqual(len(closed.get_json()["sha256"]),64)
        immutable=self.client.post(f"/api/ops/{operation_id}/briefing",json={"notes":"changed"})
        self.assertEqual(immutable.status_code,409)

    def prepare_team_stage(self, code="QTEAM-010"):
        operation_id = self.prepare_identified_article(code)
        required = ["PROCEDURE", "CHANNEL_MAP", "LIMIT_PROFILE", "DEVICE_MANIFEST", "CAMERA_MANIFEST", "SOFTWARE"]
        payload = {"baseline_code": f"{code}-CB", "revision": "REV-A", "items": [
            {"item_type": kind, "reference": f"REF-{kind}", "revision": "REV-A", "source": "CONTROLLED_RECORD", "verification_status": "VERIFIED"}
            for kind in required]}
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/baseline", json=payload).status_code, 200)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/baseline/release").status_code, 200)
        return operation_id

    def staffing_payload(self, shared_rso=False):
        roles = ["TD", "RSO", "LCO", "PROP", "INST", "GND", "DATA"]
        return {"assignments": [{"role_code": role,
            "person_name": "Person TD" if shared_rso and role == "RSO" else f"Person {role}",
            "call_sign": role, "organization": "Stellar Kinetics", "contact_method": "Operations radio",
            "qualification_status": "CURRENT", "availability_status": "CONFIRMED"} for role in roles]}

    def test_team_approval_blocks_vacancies_and_authority_conflicts(self):
        operation_id = self.prepare_team_stage()
        page = self.client.get(f"/ops/{operation_id}/team")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Team & Authority", page.data)
        partial = self.staffing_payload(); partial["assignments"] = partial["assignments"][:2]
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/team", json=partial).status_code, 200)
        missing = self.client.post(f"/api/ops/{operation_id}/team/approve")
        self.assertEqual(missing.status_code, 409)
        self.assertIn("mandatory roles", missing.get_json()["error"])
        conflict_payload = self.staffing_payload(shared_rso=True)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/team", json=conflict_payload).status_code, 200)
        conflict = self.client.post(f"/api/ops/{operation_id}/team/approve")
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("RSO must be independent", conflict.get_json()["error"])

    def test_approved_team_is_locked_and_unlocks_procedure(self):
        operation_id = self.prepare_team_stage("QTEAM-020")
        payload = self.staffing_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/team", json=payload).status_code, 200)
        approved = self.client.post(f"/api/ops/{operation_id}/team/approve", json={"approved_by": "Test Director"})
        self.assertEqual(approved.status_code, 200)
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            plan = db.execute("SELECT state,approved_by FROM staffing_plans WHERE operation_id=?", (operation_id,)).fetchone()
            procedure = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='PROCEDURE'", (operation_id,)).fetchone()
        self.assertEqual(operation["current_stage"], "PROCEDURE")
        self.assertEqual(plan["state"], "APPROVED")
        self.assertEqual(procedure["status"], "ACTIVE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/team", json=payload).status_code, 409)

    def prepare_procedure_stage(self, code="QPROC-010"):
        operation_id = self.prepare_team_stage(code)
        payload = self.staffing_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/team", json=payload).status_code, 200)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/team/approve").status_code, 200)
        return operation_id

    def procedure_payload(self, code="REF-PROCEDURE"):
        phases = ["SITE", "PREPARATION", "COUNTDOWN", "EXECUTION", "SAFING", "CONTINGENCY"]
        types = ["VERIFY", "ACTION", "HOLD_POINT", "COMMAND", "VERIFY", "CONTINGENCY"]
        steps = []
        for i, (phase, kind) in enumerate(zip(phases, types), 1):
            critical = phase in {"COUNTDOWN", "EXECUTION", "CONTINGENCY"}
            steps.append({"sequence": i, "step_code": f"PROC-{i:02d}", "phase": phase, "step_type": kind,
                          "instruction": f"Execute controlled {phase.lower()} action", "responsible_role": "LCO" if critical else "GND",
                          "verification_mode": "TWO_PERSON" if critical else "SELF", "verifier_role": "RSO" if critical else "",
                          "expected_evidence": f"{phase} evidence recorded", "safety_critical": critical,
                          "hold_condition": "TD and RSO release" if kind == "HOLD_POINT" else "",
                          "abort_action": "Declare HOLD and safe ignition circuit" if critical else ""})
        return {"document_code": code, "revision": "REV-A", "title": "Static Fire Execution Procedure",
                "entry_conditions": "Approved baseline, staffed stations, and clear exclusion zone",
                "exit_conditions": "Motor safe, data secured, and site released",
                "abort_policy": "Any safety authority may call HOLD; RSO may terminate the operation", "steps": steps}

    def safety_payload(self):
        return {"safety_case_code":"STATIC-FIRE-SAFE","revision":"REV-A",
                "scope":"Static-fire preparation, countdown, execution, contingency and safing",
                "emergency_policy":"Any station may call HOLD; RSO controls release and ABORT authority.",
                "hazards":[{"hazard_code":"HZ-IGN-001","title":"Uncommanded ignition","category":"IGNITION",
                    "cause":"Firing energy reaches the igniter outside the authorised sequence","consequence":"Personnel injury and unintended motor firing",
                    "likelihood":3,"severity":5,"residual_likelihood":1,"residual_severity":5,"owner_role":"LCO","acceptance_authority":"RSO","status":"ACCEPTED",
                    "linked_steps":["PROC-03","PROC-04","PROC-06"],"notes":"Controlled firing-energy hazard",
                    "controls":[{"control_code":"CTL-IGN-01","control_type":"ENGINEERED","description":"Key isolation and removable firing-energy disconnect",
                        "verification_method":"Two-person isolation test","responsible_role":"LCO","evidence_required":"Signed isolation test","status":"VERIFIED"}]}],
                "resources":[{"resource_code":"PPE-EYE-01","resource_type":"PPE","description":"Impact-rated eye protection","quantity":4,"required":True,
                    "readiness_status":"READY","certification_reference":"PPE-ISSUE-LOG","owner_role":"GND","linked_steps":["PROC-01","PROC-02"],"notes":"Site PPE"}],
                "holds":[{"hold_code":"HOLD-PROC-03","step_code":"PROC-03","trigger_condition":"Any failed station poll or unsafe field condition",
                    "safe_state":"Countdown stopped and firing energy removed","release_criteria":"Cause corrected and affected stations return GO",
                    "call_authority":"ANY STATION","release_authority":"RSO","mandatory":True,"status":"VERIFIED"}]}

    def configure_safety_assurance(self, operation_id):
        saved=self.client.post(f"/api/ops/{operation_id}/safety",json=self.safety_payload())
        self.assertEqual(saved.status_code,200,saved.get_json())
        approved=self.client.post(f"/api/ops/{operation_id}/safety/approve",json={"approved_by":"RSO"})
        self.assertEqual(approved.status_code,200,approved.get_json())

    def test_procedure_validation_blocks_unsafe_steps_and_baseline_mismatch(self):
        operation_id = self.prepare_procedure_stage()
        page = self.client.get(f"/ops/{operation_id}/procedure")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Procedure Control", page.data)
        self.assertIn(b"RELEASED BASELINE REQUIRES", page.data)
        self.assertIn(b"REF-PROCEDURE", page.data)
        placeholder = self.procedure_payload(); placeholder["steps"][2]["abort_action"] = "-"
        response = self.client.post(f"/api/ops/{operation_id}/procedure", json=placeholder)
        self.assertEqual(response.status_code, 400)
        self.assertIn("explicit abort/safe action", response.get_json()["error"])
        unsafe = self.procedure_payload(); unsafe["steps"][2]["verification_mode"] = "SELF"
        response = self.client.post(f"/api/ops/{operation_id}/procedure", json=unsafe)
        self.assertEqual(response.status_code, 400)
        self.assertIn("safety-critical", response.get_json()["error"])
        mismatch = self.procedure_payload("WRONG-PROCEDURE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/procedure", json=mismatch).status_code, 200)
        approval = self.client.post(f"/api/ops/{operation_id}/procedure/approve")
        self.assertEqual(approval.status_code, 409)
        self.assertIn("baseline requires REF-PROCEDURE / REV-A", approval.get_json()["error"])
        self.assertIn("WRONG-PROCEDURE / REV-A", approval.get_json()["error"])

    def test_controlled_baseline_revision_preserves_history_and_invalidates_downstream_approval(self):
        operation_id = self.prepare_procedure_stage("QPROC-REWORK")
        revised = self.client.post(f"/api/ops/{operation_id}/baseline/revise", json={
            "reason": "Replace placeholder procedure identity with the controlled document reference",
            "requested_by": "Configuration Manager"})
        self.assertEqual(revised.status_code, 200)
        self.assertEqual(revised.get_json()["revision"], "REV-B")
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage,status FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            baseline = db.execute("SELECT state,revision,canonical_sha256 FROM configuration_baselines WHERE operation_id=?", (operation_id,)).fetchone()
            history = db.execute("SELECT revision,canonical_sha256,superseded_reason FROM configuration_baseline_history WHERE operation_id=?", (operation_id,)).fetchone()
            staffing = db.execute("SELECT state,approved_at FROM staffing_plans WHERE operation_id=?", (operation_id,)).fetchone()
        self.assertEqual(operation["current_stage"], "BASELINE")
        self.assertEqual(operation["status"], "CONTROLLED REWORK")
        self.assertEqual(baseline["state"], "DRAFT")
        self.assertEqual(baseline["revision"], "REV-B")
        self.assertIsNone(baseline["canonical_sha256"])
        self.assertEqual(history["revision"], "REV-A")
        self.assertEqual(len(history["canonical_sha256"]), 64)
        self.assertEqual(staffing["state"], "DRAFT")
        self.assertIsNone(staffing["approved_at"])

    def test_approved_procedure_is_hashed_locked_and_unlocks_instrumentation(self):
        operation_id = self.prepare_procedure_stage("QPROC-020")
        payload = self.procedure_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/procedure", json=payload).status_code, 200)
        blocked=self.client.post(f"/api/ops/{operation_id}/procedure/approve",json={"approved_by":"Test Director"})
        self.assertEqual(blocked.status_code,409)
        self.assertIn("Safety & Procedure Assurance",blocked.get_json()["error"])
        self.configure_safety_assurance(operation_id)
        approved = self.client.post(f"/api/ops/{operation_id}/procedure/approve", json={"approved_by": "Test Director"})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(len(approved.get_json()["sha256"]), 64)
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            procedure = db.execute("SELECT state,canonical_sha256 FROM operation_procedures WHERE operation_id=?", (operation_id,)).fetchone()
            instrumentation = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='INSTRUMENTATION'", (operation_id,)).fetchone()
        self.assertEqual(operation["current_stage"], "INSTRUMENTATION")
        self.assertEqual(procedure["state"], "APPROVED")
        self.assertEqual(instrumentation["status"], "ACTIVE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/procedure", json=payload).status_code, 409)

    def test_safety_assurance_requires_verified_controls_resources_and_formal_hold(self):
        operation_id=self.prepare_procedure_stage("QSAFE-010")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/procedure",json=self.procedure_payload()).status_code,200)
        page=self.client.get(f"/ops/{operation_id}/safety")
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Safety & Procedure Assurance",page.data)
        incomplete=self.safety_payload(); incomplete["hazards"][0]["controls"]=[]; incomplete["resources"][0]["readiness_status"]="NOT_READY"; incomplete["holds"]=[]
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/safety",json=incomplete).status_code,200)
        blocked=self.client.post(f"/api/ops/{operation_id}/safety/approve",json={"approved_by":"RSO"})
        self.assertEqual(blocked.status_code,409)
        findings=" ".join(blocked.get_json()["findings"])
        self.assertIn("no preventive or mitigating control",findings)
        self.assertIn("mandatory resource is not READY",findings)
        self.assertIn("mandatory operational HOLD",findings)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/safety",json=self.safety_payload()).status_code,200)
        approved=self.client.post(f"/api/ops/{operation_id}/safety/approve",json={"approved_by":"RSO"})
        self.assertEqual(approved.status_code,200)
        self.assertEqual(len(approved.get_json()["sha256"]),64)

    def prepare_instrumentation_stage(self, code="QINST-010"):
        operation_id = self.prepare_procedure_stage(code)
        procedure = self.procedure_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/procedure", json=procedure).status_code, 200)
        self.configure_safety_assurance(operation_id)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/procedure/approve").status_code, 200)
        return operation_id

    def instrumentation_payload(self, e2e="PASS", plan_code="REF-CHANNEL_MAP"):
        rows = [
            ("CHAMBER_PRESSURE", "Chamber pressure", "PRESSURE", "SAFETY_CRITICAL", "PT-01", "motor.chamber_pressure", "bar", 0, 80, 1000, 55, 70, 75),
            ("THRUST", "Motor thrust", "FORCE", "REQUIRED", "LC-01", "motor.thrust", "N", 0, 650, 1000, 450, 550, 600),
            ("CASE_TEMPERATURE", "Case temperature", "TEMPERATURE", "REQUIRED", "TC-01", "motor.case_temperature", "°C", 0, 120, 10, 75, 95, 105),
            ("IGNITION_CONTINUITY", "Ignition continuity", "DISCRETE", "REQUIRED", "FC-01", "ignition.continuity", "state", 0, 1, 2, None, None, None),
        ]
        return {"plan_code": plan_code, "revision": "REV-A", "time_source": "TIME-01 / UTC", "acquisition_mode": "LIVE_ETHERNET",
                "measurements": [{"measurement_code": code, "name": name, "category": category, "criticality": criticality,
                    "device_id": device, "channel_id": channel, "unit": unit, "engineering_min": minimum,
                    "engineering_max": maximum, "sample_rate_hz": rate, "required_accuracy": "±1% FS",
                    "calibration_reference": f"CAL-{code}-001", "calibration_due": "2027-12-31",
                    "warning_limit": warning, "critical_limit": critical, "abort_limit": abort,
                    "redundancy": "MONITORED", "e2e_status": e2e} for code,name,category,criticality,device,channel,unit,minimum,maximum,rate,warning,critical,abort in rows]}

    def test_instrumentation_blocks_registry_mismatch_and_unverified_signal_chain(self):
        operation_id = self.prepare_instrumentation_stage()
        page = self.client.get(f"/ops/{operation_id}/instrumentation")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Instrumentation Plan", page.data)
        wrong = self.instrumentation_payload(); wrong["measurements"][0]["device_id"] = "LC-01"
        response = self.client.post(f"/api/ops/{operation_id}/instrumentation", json=wrong)
        self.assertEqual(response.status_code, 409)
        self.assertIn("not sourced", response.get_json()["error"])
        untested = self.instrumentation_payload("NOT_TESTED")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/instrumentation", json=untested).status_code, 200)
        approval = self.client.post(f"/api/ops/{operation_id}/instrumentation/approve")
        self.assertEqual(approval.status_code, 409)
        self.assertIn("end-to-end", approval.get_json()["error"])

    def test_approved_instrumentation_is_hashed_locked_and_unlocks_video(self):
        operation_id = self.prepare_instrumentation_stage("QINST-020")
        payload = self.instrumentation_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/instrumentation", json=payload).status_code, 200)
        approved = self.client.post(f"/api/ops/{operation_id}/instrumentation/approve", json={"approved_by": "Instrumentation Lead"})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(len(approved.get_json()["sha256"]), 64)
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            plan = db.execute("SELECT state,canonical_sha256 FROM instrumentation_plans WHERE operation_id=?", (operation_id,)).fetchone()
            video = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='VIDEO'", (operation_id,)).fetchone()
        self.assertEqual(operation["current_stage"], "VIDEO")
        self.assertEqual(plan["state"], "APPROVED")
        self.assertEqual(video["status"], "ACTIVE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/instrumentation", json=payload).status_code, 409)

    def prepare_video_stage(self, code="QVIDEO-010"):
        operation_id = self.prepare_instrumentation_stage(code)
        instrumentation = self.instrumentation_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/instrumentation", json=instrumentation).status_code, 200)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/instrumentation/approve").status_code, 200)
        return operation_id

    def video_plan_payload(self, assurance="PASS", manifest_code="REF-CAMERA_MANIFEST"):
        views = [("MOTOR_WIDE", "Motor wide", "Full test article and stand", "CAM-01"),
                 ("NOZZLE_CLOSE", "Nozzle close", "Nozzle and plume onset", "CAM-02")]
        return {"manifest_code": manifest_code, "revision": "REV-A", "master_time_source": "TIME-01 / UTC",
                "recording_window_seconds": 600, "evidence_owner": "Data & Video Lead", "views": [
                    {"view_code": code, "name": name, "purpose": purpose, "camera_device_id": camera, "mandatory": True,
                     "record_mode": "ISO", "resolution": "1920x1080", "fps": 30, "codec": "H264", "bitrate_mbps": 8,
                     "pre_roll_seconds": 30, "post_roll_seconds": 120, "time_sync_method": "NTP / embedded UTC",
                     "time_sync_status": "VERIFIED" if assurance == "PASS" else "NOT_VERIFIED",
                     "signal_test_status": assurance, "recording_test_status": assurance,
                     "primary_storage": "RECORDER-A", "backup_storage": "NAS-EVIDENCE", "retention_days": 365,
                     "loss_action": "Call HOLD and assess evidence impact", "public_safe": False}
                    for code,name,purpose,camera in views]}

    def test_video_plan_blocks_unknown_camera_and_unverified_evidence_chain(self):
        operation_id = self.prepare_video_stage()
        page = self.client.get(f"/ops/{operation_id}/video")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Video & Recording Plan", page.data)
        unknown = self.video_plan_payload(); unknown["views"][0]["camera_device_id"] = "CAM-99"
        response = self.client.post(f"/api/ops/{operation_id}/video", json=unknown)
        self.assertEqual(response.status_code, 409)
        self.assertIn("not registered", response.get_json()["error"])
        untested = self.video_plan_payload("NOT_TESTED")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/video", json=untested).status_code, 200)
        approval = self.client.post(f"/api/ops/{operation_id}/video/approve")
        self.assertEqual(approval.status_code, 409)
        self.assertIn("time-sync", approval.get_json()["error"])

    def test_approved_video_plan_is_hashed_locked_and_unlocks_readiness(self):
        operation_id = self.prepare_video_stage("QVIDEO-020")
        payload = self.video_plan_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/video", json=payload).status_code, 200)
        approved = self.client.post(f"/api/ops/{operation_id}/video/approve", json={"approved_by": "Data & Video Lead"})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(len(approved.get_json()["sha256"]), 64)
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            plan = db.execute("SELECT id,state,canonical_sha256 FROM video_recording_plans WHERE operation_id=?", (operation_id,)).fetchone()
            readiness = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='READINESS'", (operation_id,)).fetchone()
            storage = db.execute("SELECT estimated_storage_gb FROM camera_view_requirements WHERE plan_id=?", (plan["id"],)).fetchall()
        self.assertEqual(operation["current_stage"], "READINESS")
        self.assertEqual(plan["state"], "APPROVED")
        self.assertEqual(readiness["status"], "ACTIVE")
        self.assertTrue(all(row["estimated_storage_gb"] > 0 for row in storage))
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/video", json=payload).status_code, 409)

    def prepare_readiness_stage(self, code="QREADY-010"):
        operation_id = self.prepare_video_stage(code)
        video = self.video_plan_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/video", json=video).status_code, 200)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/video/approve").status_code, 200)
        return operation_id

    def readiness_payload(self):
        gates = ["CONFIGURATION", "STAFFING", "PROCEDURE", "INSTRUMENTATION", "VIDEO", "SAFETY", "SITE"]
        return {"review_code": "QREADY-TRR", "review_type": "TRR", "review_chair": "Test Director",
                "planned_date": "2026-09-01", "gates": [{"gate_code": gate, "status": "GO",
                    "evidence_reference": f"EVIDENCE-{gate}", "reviewer": f"Reviewer {gate}"} for gate in gates], "findings": []}

    def test_readiness_blocks_open_findings_and_nonwaivable_safety_gate(self):
        operation_id = self.prepare_readiness_stage()
        page = self.client.get(f"/ops/{operation_id}/readiness")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Readiness Review", page.data)
        payload = self.readiness_payload()
        payload["findings"] = [{"finding_code": "RF-01", "title": "Open safety action", "severity": "HIGH",
                                "owner": "RSO", "status": "OPEN", "due_date": "2026-08-30"}]
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/readiness", json=payload).status_code, 200)
        blocked = self.client.post(f"/api/ops/{operation_id}/readiness/approve", json={"decision_rationale": "All gates reviewed"})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("open readiness findings", blocked.get_json()["error"])
        payload["findings"] = []
        safety = next(g for g in payload["gates"] if g["gate_code"] == "SAFETY")
        safety.update(status="WAIVER", waiver_reason="Pending confirmation", waiver_authority="Test Director")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/readiness", json=payload).status_code, 200)
        blocked = self.client.post(f"/api/ops/{operation_id}/readiness/approve", json={"decision_rationale": "Proceed with waiver"})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("cannot be waived", blocked.get_json()["error"])

    def test_approved_readiness_is_hashed_locked_and_unlocks_rehearsal(self):
        operation_id = self.prepare_readiness_stage("QREADY-020")
        payload = self.readiness_payload()
        payload["findings"] = [{"finding_code": "RF-02", "title": "Label correction", "severity": "LOW",
                                "owner": "Ground Operations", "status": "CLOSED", "due_date": "2026-08-30",
                                "disposition": "Label replaced and independently inspected"}]
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/readiness", json=payload).status_code, 200)
        approved = self.client.post(f"/api/ops/{operation_id}/readiness/approve", json={
            "approved_by": "Test Director", "decision_rationale": "All mandatory gates are GO and the only finding is verified closed."})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(len(approved.get_json()["sha256"]), 64)
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            review = db.execute("SELECT state,final_decision FROM readiness_reviews WHERE operation_id=?", (operation_id,)).fetchone()
            rehearsal = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='REHEARSAL'", (operation_id,)).fetchone()
        self.assertEqual(operation["current_stage"], "REHEARSAL")
        self.assertEqual(review["final_decision"], "GO")
        self.assertEqual(rehearsal["status"], "ACTIVE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/readiness", json=payload).status_code, 409)

    def prepare_rehearsal_stage(self, code="QREH-010"):
        operation_id = self.prepare_readiness_stage(code)
        readiness = self.readiness_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/readiness", json=readiness).status_code, 200)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/readiness/approve", json={
            "decision_rationale": "All gates are GO and no open findings remain."}).status_code, 200)
        return operation_id

    def rehearsal_payload(self, result="PASS"):
        rows = [
            ("FULL_SEQUENCE", "Full sequence", "SEQUENCE", "TD"),
            ("COMM_CHECK", "Communications", "COMMS", "TD"),
            ("HOLD_RESPONSE", "Hold response", "CONTINGENCY", "RSO"),
            ("ABORT_RESPONSE", "Abort response", "CONTINGENCY", "RSO"),
            ("DATA_RECORDING", "Data recording", "DATA", "INST"),
            ("VIDEO_RECORDING", "Video recording", "VIDEO", "DATA"),
        ]
        return {"rehearsal_code": "QREH-DRYRUN", "rehearsal_type": "DRY_RUN", "source_mode": "SIMULATION",
                "conductor": "Test Director", "scheduled_at": "2026-09-02T08:00", "checkpoints": [
                    {"checkpoint_code": code, "name": name, "phase": phase, "responsible_role": role,
                     "objective": f"Exercise {name.lower()}", "expected_result": "Controlled response within procedure",
                     "critical": True, "result": result, "observed_result": f"{name} observed",
                     "response_time_seconds": 2.5, "evidence_reference": f"REH-EVIDENCE-{code}"}
                    for code,name,phase,role in rows], "anomalies": []}

    def test_rehearsal_blocks_failed_checkpoint_and_open_retest_anomaly(self):
        operation_id = self.prepare_rehearsal_stage()
        page = self.client.get(f"/ops/{operation_id}/rehearsal")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Rehearsal Control", page.data)
        failed = self.rehearsal_payload(); failed["checkpoints"][2]["result"] = "FAIL"
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/rehearsal", json=failed).status_code, 200)
        completion = self.client.post(f"/api/ops/{operation_id}/rehearsal/complete", json={"summary": "Hold response failed."})
        self.assertEqual(completion.status_code, 409)
        self.assertIn("did not pass", completion.get_json()["error"])
        payload = self.rehearsal_payload()
        payload["anomalies"] = [{"anomaly_code": "RA-01", "title": "Delayed abort acknowledgement", "severity": "HIGH",
                                 "owner": "LCO", "status": "OPEN", "requires_retest": True}]
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/rehearsal", json=payload).status_code, 200)
        completion = self.client.post(f"/api/ops/{operation_id}/rehearsal/complete", json={"summary": "Sequence passed with anomaly."})
        self.assertEqual(completion.status_code, 409)
        self.assertIn("closure or retest", completion.get_json()["error"])

    def test_completed_rehearsal_is_hashed_locked_and_unlocks_execution(self):
        operation_id = self.prepare_rehearsal_stage("QREH-020")
        payload = self.rehearsal_payload()
        payload["anomalies"] = [{"anomaly_code": "RA-02", "title": "Call sign correction", "severity": "LOW",
                                 "owner": "TD", "status": "CLOSED", "requires_retest": False,
                                 "disposition": "Call sign corrected and checkpoint repeated", "evidence_reference": "REH-RETEST-02"}]
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/rehearsal", json=payload).status_code, 200)
        completed = self.client.post(f"/api/ops/{operation_id}/rehearsal/complete", json={
            "completed_by": "Test Director", "summary": "Full sequence, HOLD, ABORT, data and video paths passed in simulation."})
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(len(completed.get_json()["sha256"]), 64)
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage,status FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            campaign = db.execute("SELECT state,result FROM rehearsal_campaigns WHERE operation_id=?", (operation_id,)).fetchone()
            execution = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='EXECUTION'", (operation_id,)).fetchone()
        self.assertEqual(operation["current_stage"], "EXECUTION")
        self.assertEqual(operation["status"], "READY")
        self.assertEqual(campaign["result"], "PASS")
        self.assertEqual(execution["status"], "ACTIVE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/rehearsal", json=payload).status_code, 409)

    def prepare_execution_stage(self, code="QEXEC-010"):
        operation_id = self.prepare_rehearsal_stage(code)
        rehearsal = self.rehearsal_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/rehearsal", json=rehearsal).status_code, 200)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/rehearsal/complete", json={
            "summary": "All mandatory rehearsal paths passed in simulation."}).status_code, 200)
        self.assertEqual(self.client.get(f"/ops/{operation_id}/briefing").status_code,200)
        with control_module.connect() as db:
            db.execute("UPDATE operation_briefings SET state='CLOSED',canonical_sha256=?,closed_at=?,closed_by=? WHERE operation_id=?",("d"*64,"2026-09-03T07:00:00Z","Test Director",operation_id))
        return operation_id

    def execution_release_payload(self):
        gates = ["CONFIG_UNCHANGED", "READINESS_CURRENT", "REHEARSAL_VALID", "CREW_PRESENT", "SITE_CLEAR",
                 "TELEMETRY_LIVE", "RECORDING_ACTIVE", "VIDEO_ACTIVE", "IGNITION_SAFE"]
        return {"release_code": "QEXEC-LIVE", "source_mode": "LIVE", "planned_start": "2026-09-03T08:00",
                "valid_until": "2027-09-03T10:00", "gates": [{"gate_code": gate, "status": "GO",
                    "evidence_reference": f"LIVE-EVIDENCE-{gate}", "verified_by": f"Verifier {gate}"} for gate in gates]}

    def execution_authorizations(self):
        return {"authorizations": [{"role_code": role, "person_name": f"Person {role}", "decision": "GO",
                                     "attestation": f"I confirm {role} conditions and authority for this LIVE operation."}
                                    for role in ("TD", "RSO", "LCO")]}

    def test_execution_release_requires_live_runtime_and_independent_authorities(self):
        operation_id = self.prepare_execution_stage()
        page = self.client.get(f"/ops/{operation_id}/execution")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Execution Release", page.data)
        payload = self.execution_release_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/execution", json=payload).status_code, 200)
        with control_module.connect() as db: db.execute("UPDATE operation_briefings SET state='DRAFT',canonical_sha256=NULL WHERE operation_id=?",(operation_id,))
        briefing_blocked=self.client.post(f"/api/ops/{operation_id}/execution/release",json=self.execution_authorizations())
        self.assertEqual(briefing_blocked.status_code,409);self.assertIn("Day-of-Operation Briefing",briefing_blocked.get_json()["error"])
        with control_module.connect() as db: db.execute("UPDATE operation_briefings SET state='CLOSED',canonical_sha256=? WHERE operation_id=?",("d"*64,operation_id))
        blocked = self.client.post(f"/api/ops/{operation_id}/execution/release", json=self.execution_authorizations())
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("must be LIVE", blocked.get_json()["error"])
        with control_module.connect() as db:
            db.execute("UPDATE operations SET mode='LIVE' WHERE id=?", (control_module.OPERATION_ID,))
        wrong = self.execution_authorizations(); wrong["authorizations"][1]["person_name"] = "Person TD"
        blocked = self.client.post(f"/api/ops/{operation_id}/execution/release", json=wrong)
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("does not match", blocked.get_json()["error"])

    def test_released_execution_handoff_and_post_operation_closure(self):
        operation_id = self.prepare_execution_stage("QEXEC-020")
        payload = self.execution_release_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/execution", json=payload).status_code, 200)
        with control_module.connect() as db:
            db.execute("UPDATE operations SET mode='LIVE',state='CHECKOUT' WHERE id=?", (control_module.OPERATION_ID,))
        released = self.client.post(f"/api/ops/{operation_id}/execution/release", json=self.execution_authorizations())
        self.assertEqual(released.status_code, 200)
        self.assertEqual(len(released.get_json()["sha256"]), 64)
        self.assertEqual(released.get_json()["url"], "/workspace")
        premature = self.client.post(f"/api/ops/{operation_id}/execution/close", json={"outcome": "SUCCESS", "summary": "Test complete"})
        self.assertEqual(premature.status_code, 409)
        with control_module.connect() as db:
            db.execute("UPDATE operations SET state='POST_FIRE' WHERE id=?", (control_module.OPERATION_ID,))
        closed = self.client.post(f"/api/ops/{operation_id}/execution/close", json={
            "outcome": "SUCCESS", "summary": "Static fire completed and the article was declared safe.", "closed_by": "Test Director"})
        self.assertEqual(closed.status_code, 200)
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage,status FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            release = db.execute("SELECT state,outcome FROM execution_releases WHERE operation_id=?", (operation_id,)).fetchone()
            review = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='REVIEW'", (operation_id,)).fetchone()
        self.assertEqual(operation["current_stage"], "REVIEW")
        self.assertEqual(release["state"], "CLOSED")
        self.assertEqual(release["outcome"], "SUCCESS")
        self.assertEqual(review["status"], "ACTIVE")

    def prepare_review_stage(self, code="QREVIEW-010"):
        operation_id = self.prepare_execution_stage(code)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/execution", json=self.execution_release_payload()).status_code, 200)
        with control_module.connect() as db:
            db.execute("UPDATE operations SET mode='LIVE',state='CHECKOUT' WHERE id=?", (control_module.OPERATION_ID,))
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/execution/release", json=self.execution_authorizations()).status_code, 200)
        with control_module.connect() as db:
            db.execute("UPDATE operations SET state='POST_FIRE' WHERE id=?", (control_module.OPERATION_ID,))
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/execution/close", json={
            "outcome": "SUCCESS", "summary": "Static fire completed and the article was declared safe."}).status_code, 200)
        return operation_id

    def post_review_payload(self):
        evidence_codes = ["EXECUTION_RELEASE", "EVENT_LOG", "TELEMETRY_PACKAGE", "VIDEO_EVIDENCE",
                          "CONFIGURATION_BASELINE", "APPROVED_PROCEDURE", "READINESS_DECISION",
                          "REHEARSAL_RECORD", "SAFING_DECLARATION"]
        return {"review_code": "QREVIEW-POR", "review_chair": "Test Director", "review_date": "2026-09-04",
                "overall_conclusion": "The qualification objective was achieved within the approved configuration and limits.",
                "lessons_learned": "Retain the verified timing and evidence configuration for the next campaign.",
                "evidence_package_reference": "EVIDENCE/QREVIEW/FINAL", "evidence_package_sha256": "a" * 64,
                "objectives": [{"objective_code": "OBJ-01", "objective_text": "Hardware identity verified",
                                "assessment": "MET", "evidence_reference": "RESULTS/OBJECTIVE-01",
                                "rationale": "Serial identity and as-run configuration matched the released baseline."}],
                "evidence_items": [{"item_code": code, "name": code.replace("_", " ").title(), "required": True,
                                    "status": "VERIFIED", "reference": f"EVIDENCE/{code}", "sha256": "b" * 64,
                                    "disposition": ""} for code in evidence_codes], "corrective_actions": []}

    def test_post_operation_review_blocks_missing_evidence_and_open_actions(self):
        operation_id = self.prepare_review_stage()
        page = self.client.get(f"/ops/{operation_id}/review")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Review & Closure", page.data)
        payload = self.post_review_payload()
        telemetry = next(x for x in payload["evidence_items"] if x["item_code"] == "TELEMETRY_PACKAGE")
        telemetry.update(status="MISSING", reference="", sha256="", disposition="Recorder package awaiting controlled export")
        payload["corrective_actions"] = [{"action_code": "CA-01", "title": "Archive timing trace", "source": "Post-operation review",
                                           "severity": "HIGH", "owner": "Data Lead", "due_date": "2026-09-05",
                                           "status": "OPEN", "closure_evidence": "", "transfer_reference": "", "notes": ""}]
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/review", json=payload).status_code, 200)
        blocked = self.client.post(f"/api/ops/{operation_id}/review/close")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("TELEMETRY_PACKAGE", blocked.get_json()["error"])
        telemetry.update(status="VERIFIED", reference="EVIDENCE/TELEMETRY_PACKAGE", sha256="c" * 64, disposition="")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/review", json=payload).status_code, 200)
        blocked = self.client.post(f"/api/ops/{operation_id}/review/close")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("CA-01", blocked.get_json()["error"])

    def test_final_closure_is_hashed_immutable_and_completes_workflow(self):
        operation_id = self.prepare_review_stage("QREVIEW-020")
        payload = self.post_review_payload()
        payload["corrective_actions"] = [{"action_code": "CA-02", "title": "Carry improved camera marker forward", "source": "Lessons learned",
                                           "severity": "LOW", "owner": "Data Lead", "due_date": "2026-09-10",
                                           "status": "TRANSFERRED", "closure_evidence": "", "transfer_reference": "CAMPAIGN-BACKLOG-42", "notes": ""}]
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/review", json=payload).status_code, 200)
        closed = self.client.post(f"/api/ops/{operation_id}/review/close", json={"closed_by": "Test Director"})
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(len(closed.get_json()["sha256"]), 64)
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage,status FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            review = db.execute("SELECT state,closure_sha256 FROM post_operation_reviews WHERE operation_id=?", (operation_id,)).fetchone()
            section = db.execute("SELECT status FROM operation_workflow_sections WHERE operation_id=? AND section_key='REVIEW'", (operation_id,)).fetchone()
        self.assertEqual(operation["current_stage"], "CLOSED")
        self.assertEqual(operation["status"], "CLOSED")
        self.assertEqual(review["state"], "CLOSED")
        self.assertEqual(section["status"], "COMPLETE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/review", json=payload).status_code, 409)

    def test_operational_change_controls_approval_invalidation_and_verification(self):
        self.assertEqual(self.client.get("/ops").status_code, 200)
        with control_module.connect() as db:
            operation_id = db.execute("SELECT id FROM operation_registry WHERE code='DEMO-SF-001'").fetchone()["id"]
            # A released runtime must first be returned to controlled planning.
            db.execute("UPDATE execution_releases SET state='DRAFT',release_sha256=NULL,released_at=NULL WHERE operation_id=?", (operation_id,))

        created = self.client.post(f"/api/ops/{operation_id}/changes", json={
            "change_code": "CR-WX-001", "change_type": "CHANGE", "category": "WEATHER",
            "severity": "MAJOR", "title": "Revised wind operating constraint",
            "description": "Apply a revised wind limit to the planned operation window.",
            "reason": "Updated site forecast and range assessment.",
            "proposed_solution": "Revalidate readiness, briefing and execution release against the revised limit.",
            "requested_by": "Range Coordinator", "owner_role": "TD", "due_at": "2026-09-14T12:00",
            "implementation_plan": "Invalidate affected approvals and update their controlled evidence.",
            "verification_plan": "Independent review of each regenerated controlled record.",
            "impacts": []})
        self.assertEqual(created.status_code, 200)
        change_id = created.get_json()["id"]
        with control_module.connect() as db:
            domains = {row["domain_key"] for row in db.execute("SELECT domain_key FROM change_impacts WHERE change_id=?", (change_id,))}
        self.assertEqual(domains, {"READINESS", "BRIEFING", "EXECUTION"})

        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/changes/{change_id}/submit").status_code, 200)
        decisions = (("TD", "Aisha Al Harthy"), ("RSO", "Omar Al Balushi"), ("CM", "Training Configuration Manager"))
        for role, person in decisions:
            response = self.client.post(f"/api/ops/{operation_id}/changes/{change_id}/approve", json={
                "role_code": role, "person_name": person, "decision": "APPROVED",
                "rationale": f"{role} accepts the controlled impact and verification plan."})
            self.assertEqual(response.status_code, 200, role)
        self.assertEqual(response.get_json()["state"], "APPROVED")

        implemented = self.client.post(f"/api/ops/{operation_id}/changes/{change_id}/implement", json={"implemented_by": "Aisha Al Harthy"})
        self.assertEqual(implemented.status_code, 200)
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/changes/{change_id}/close", json={"closed_by": "Aisha Al Harthy"}).status_code, 409)
        briefing = self.client.get(f"/ops/{operation_id}/briefing")
        self.assertIn(b"CR-WX-001", briefing.data)
        with control_module.connect() as db:
            operation = db.execute("SELECT current_stage,status FROM operation_registry WHERE id=?", (operation_id,)).fetchone()
            readiness = db.execute("SELECT state,final_decision FROM readiness_reviews WHERE operation_id=?", (operation_id,)).fetchone()
            briefing_row = db.execute("SELECT state,canonical_sha256 FROM operation_briefings WHERE operation_id=?", (operation_id,)).fetchone()
        self.assertEqual((operation["current_stage"], operation["status"]), ("READINESS", "CONTROLLED CHANGE REWORK"))
        self.assertEqual((readiness["state"], readiness["final_decision"]), ("DRAFT", "PENDING"))
        self.assertEqual(briefing_row["state"], "DRAFT")
        self.assertIsNone(briefing_row["canonical_sha256"])

        for domain in sorted(domains):
            verified = self.client.post(f"/api/ops/{operation_id}/changes/{change_id}/verify", json={
                "domain_key": domain, "status": "VERIFIED", "verified_by": "Independent Reviewer",
                "notes": f"Regenerated {domain} record checked against CR-WX-001."})
            self.assertEqual(verified.status_code, 200, domain)
        closed = self.client.post(f"/api/ops/{operation_id}/changes/{change_id}/close", json={"closed_by": "Aisha Al Harthy"})
        self.assertEqual(closed.status_code, 200)
        with control_module.connect() as db:
            state = db.execute("SELECT state FROM operation_changes WHERE id=?", (change_id,)).fetchone()["state"]
        self.assertEqual(state, "CLOSED")


if __name__ == "__main__":
    unittest.main()
