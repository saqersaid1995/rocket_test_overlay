import tempfile
import unittest
from pathlib import Path

from stellar_ops.execution_packs import create_execution_pack, validate_execution_pack


class ExecutionPackTests(unittest.TestCase):
    def operation(self):
        task={"task_code":"SAFE-010","title":"Exclusion-zone verification","phase":"SITE","description":"Verify the site is clear.","due_at":"2026-08-20T07:00:00Z","accountable_role":"TD","verifier_role":"LCO","responsible_role":"RSO","assigned_person":"Training RSO","status":"ACCEPTED","safety_critical":1,"required_inputs":"Site map","acceptance_criteria":"Zone clear","required_evidence":"Signed checklist","blocker":"","evidence":[]}
        return {"code":"DEMO-1","title":"Training Static Fire","planned_start":"2026-08-20T08:00:00Z","baseline":{"state":"RELEASED"},"staffing":{"state":"APPROVED","assignments":[]},"procedure":{"state":"APPROVED","steps":[]},"safety_case":{"state":"APPROVED","emergency_policy":"Any station may call HOLD."},"hazards":[],"planning_tasks":[task]}

    def test_named_recipient_and_release_controls(self):
        op=self.operation(); scope={"role_code":"RSO","person_name":"Training RSO","qualification_status":"CURRENT"}
        self.assertTrue(validate_execution_pack(op,"PERSON",scope,op["planning_tasks"],True)["release_ready"])
        scope["qualification_status"]="EXPIRED"
        self.assertFalse(validate_execution_pack(op,"PERSON",scope,op["planning_tasks"],True)["release_ready"])

    def test_targeted_pdf_is_created(self):
        op=self.operation(); scope={"role_code":"RSO","person_name":"Training RSO","qualification_status":"CURRENT","call_sign":"RANGE","authority_scope":"STOP WORK","contact_method":"RADIO"}
        metadata={"pack_code":"DEMO-1-ROLE-RSO-EP","issue":1,"state":"DRAFT","issued_by":"DOCUMENT CONTROL","issued_at":"2026-08-19T08:00:00Z"}
        with tempfile.TemporaryDirectory() as directory:
            result=create_execution_pack(Path(directory),op,"PERSON",scope,op["planning_tasks"],[],metadata)
            self.assertGreater(result["byte_size"],1000);self.assertEqual(len(result["sha256"]),64)

if __name__=="__main__":unittest.main()
