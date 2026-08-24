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


    def test_workspace_multi_element_bindings_use_query_selector_all(self):
        script = (
            Path(__file__).resolve().parents[1] / "static" / "workspace.js"
        ).read_text(encoding="utf-8")
        for selector in (
            "data-camera-full",
            "data-run-diagnostics",
            "data-create-backup",
            "data-alarm-detail",
            "data-incident-detail",
            "data-close",
        ):
            self.assertIn(f"$('[{selector}]').forEach", script)
            self.assertNotIn(f"$('[{selector}]').forEach", script)


    def test_lifecycle_navigation_matches_operational_dependency_order(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "partials"
            / "ops_lifecycle_nav.html"
        ).read_text(encoding="utf-8")
        expected = [
            "/planning",
            "/article",
            "/baseline",
            "/team",
            "/procedure",
            "/safety",
            "/instrumentation",
            "/video",
            "/handbook",
            "/work-packages",
            "/readiness",
            "/briefing",
            "/rehearsal",
            "/execution",
            "/review",
        ]
        positions = [template.index(route) for route in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("CROSS-LIFECYCLE CONTROL", template)

    def test_mission_control_entry_requires_released_runtime_context(self):
        response = self.client.get("/ops/1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MISSION CONTROL LOCKED", response.data)
        self.assertNotIn(b'>OPEN MISSION CONTROL</a>', response.data)


    def test_workspace_diagnostics_control_is_wired(self):
        script = (
            Path(__file__).resolve().parents[1] / "static" / "workspace.js"
        ).read_text(encoding="utf-8")
        self.assertIn("[data-run-diagnostics]", script)
        self.assertIn("/api/control/diagnostics/self-test", script)


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
