import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module


class InformationArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = control_module.CONTROL_DB
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        control_module.CONTROL_DB = self.original
        self.temp.cleanup()

    def test_operations_home_presents_five_lifecycle_phases(self):
        response = self.client.get("/ops")
        self.assertEqual(response.status_code, 200)
        for label in (
            b"DEFINITION",
            b"PREPARATION",
            b"ASSURANCE",
            b"EXECUTION PREP",
            b"CLOSE-OUT",
        ):
            self.assertIn(label, response.data)

    def test_system_configuration_is_not_presented_as_execution_console(self):
        response = self.client.get("/control")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SYSTEM CONFIGURATION", response.data)
        self.assertIn(b'DEVICE REGISTRY', response.data)
        self.assertIn(b'id="device-setup" class="panel-view active"', response.data)
        self.assertNotIn(b'data-panel="conduct"', response.data)

    def test_mission_control_links_to_system_configuration(self):
        response = self.client.get("/workspace")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MISSION CONTROL WORKSPACE", response.data)
        self.assertIn(b"SYSTEM CONFIGURATION", response.data)


    def test_workspace_camera_fullscreen_binding_uses_multi_selector(self):
        script = (
            Path(__file__).resolve().parents[1] / "static" / "workspace.js"
        ).read_text(encoding="utf-8")
        self.assertIn("$('[data-camera-full]').forEach", script)
        self.assertNotIn(";$('[data-camera-full]').forEach", script)


    def test_workspace_uses_accessible_dialogs_instead_of_native_prompts(self):
        static_root = Path(__file__).resolve().parents[1] / "static"
        script = (static_root / "workspace.js").read_text(encoding="utf-8")
        self.assertNotIn("prompt(", script)
        self.assertIn("requestText({", script)

        response = self.client.get("/workspace")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="text-entry-dialog"', response.data)
        self.assertIn(b'aria-label="Incident severity"', response.data)
        self.assertIn(b'aria-label="Incident description"', response.data)


if __name__ == "__main__":
    unittest.main()
