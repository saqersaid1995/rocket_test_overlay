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

    def test_procedure_validation_blocks_unsafe_steps_and_baseline_mismatch(self):
        operation_id = self.prepare_procedure_stage()
        page = self.client.get(f"/ops/{operation_id}/procedure")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Procedure Control", page.data)
        unsafe = self.procedure_payload(); unsafe["steps"][2]["verification_mode"] = "SELF"
        response = self.client.post(f"/api/ops/{operation_id}/procedure", json=unsafe)
        self.assertEqual(response.status_code, 400)
        self.assertIn("safety-critical", response.get_json()["error"])
        mismatch = self.procedure_payload("WRONG-PROCEDURE")
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/procedure", json=mismatch).status_code, 200)
        approval = self.client.post(f"/api/ops/{operation_id}/procedure/approve")
        self.assertEqual(approval.status_code, 409)
        self.assertIn("released configuration baseline", approval.get_json()["error"])

    def test_approved_procedure_is_hashed_locked_and_unlocks_instrumentation(self):
        operation_id = self.prepare_procedure_stage("QPROC-020")
        payload = self.procedure_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/procedure", json=payload).status_code, 200)
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

    def prepare_instrumentation_stage(self, code="QINST-010"):
        operation_id = self.prepare_procedure_stage(code)
        procedure = self.procedure_payload()
        self.assertEqual(self.client.post(f"/api/ops/{operation_id}/procedure", json=procedure).status_code, 200)
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


if __name__ == "__main__":
    unittest.main()
