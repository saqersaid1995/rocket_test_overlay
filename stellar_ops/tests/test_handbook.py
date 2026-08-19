import tempfile
import unittest
from pathlib import Path

from stellar_ops.handbook import create_handbook_file, default_chapters, validate_handbook


class HandbookTests(unittest.TestCase):
    def operation(self):
        return {"code":"DEMO-1","title":"Training Static Fire","mission_name":"Training","site":"Stand A","planned_start":"2026-08-20T08:00:00Z","owner":"TD","risk_class":"HAZARDOUS","objective":"Train team","success_criteria":["Safe completion"],
                "baseline":{"state":"RELEASED","canonical_sha256":"a"*64,"items":[]},"staffing":{"state":"APPROVED","assignments":[]},
                "procedure":{"state":"APPROVED","canonical_sha256":"b"*64,"steps":[]},"safety_case":{"state":"APPROVED","canonical_sha256":"c"*64,"emergency_policy":"Call HOLD and safe the test stand."},"hazards":[],
                "instrumentation":{"state":"APPROVED","measurements":[]},"video_plan":{"state":"APPROVED","views":[]},"readiness":{"decision":"GO"},"planning_tasks":[]}

    def config(self):
        return {"handbook_code":"DEMO-1-OEH","revision":"A","title":"Operation Execution Handbook","template_key":"TECHNICAL","state":"DRAFT","distribution_classification":"INTERNAL CONTROLLED","prepared_by":"DOCUMENT CONTROL","checked_by":"TD","approved_by":None,"generated_at":"2026-08-19T12:00:00Z"}

    def test_mandatory_chapters_cannot_be_omitted(self):
        op=self.operation(); chapters=default_chapters(op); chapters[0]["included"]=0
        self.assertFalse(validate_handbook(op,self.config(),chapters)["release_ready"])

    def test_draft_pdf_is_created_with_digest(self):
        op=self.operation(); chapters=default_chapters(op)
        with tempfile.TemporaryDirectory() as directory:
            result=create_handbook_file(Path(directory),op,self.config(),chapters)
            self.assertGreater(result["byte_size"],1000); self.assertEqual(len(result["sha256"]),64)

if __name__ == "__main__": unittest.main()
