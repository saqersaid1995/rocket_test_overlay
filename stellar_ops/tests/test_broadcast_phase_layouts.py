import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module
from stellar_ops.broadcast_telemetry import install_bundled_packages


def public_rotpl(template_id="phase-layout", version="1.0.0"):
    stream = io.BytesIO()
    manifest = {
        "template_id": template_id,
        "name": "Phase Layout",
        "version": version,
        "canvas": "1920x1080@30",
        "required_channels": ["mission.elapsed_time", "position.altitude_agl"],
        "optional_channels": ["velocity.speed_3d", "attitude.pitch"],
    }
    layout = {
        "elements": [
            {"id": "altitude", "type": "numeric", "binding": "position.altitude_agl"},
            {"id": "clock", "type": "clock", "binding": "mission.elapsed_time"},
        ]
    }
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("layout.json", json.dumps(layout))
    stream.seek(0)
    return stream


class BroadcastPhaseLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = control_module.CONTROL_DB
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        control_module.CONTROL_DB = self.original
        self.temp.cleanup()

    def upload(self):
        response = self.client.post(
            "/api/media/overlay-package",
            data={"package": (public_rotpl(), "phase-layout.rotpl")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["public_safe"])
        return response.get_json()["package_id"]

    def test_real_bundled_studio_template_is_public_selectable_and_phase_bound(self):
        # Build the isolated schema, then exercise the exact .rotpl committed
        # under UPLOAD_TEMPLATE_HERE rather than a synthetic fixture.
        self.client.get("/api/media/snapshot")
        with control_module.connect() as db:
            imported = install_bundled_packages(
                db, control_module.OPERATION_ID, "2026-08-26T00:00:00.000+00:00"
            )
        self.assertEqual(len(imported), 1)

        state = self.client.get("/api/media/snapshot").get_json()
        self.assertEqual(len(state["overlay_packages"]), 1)
        package = state["overlay_packages"][0]
        self.assertTrue(package["public_safe"], package)
        self.assertEqual(package["state"], "VALIDATED")

        checkout = next(
            item for item in state["phase_overlay_assignments"]
            if item["phase"] == "CHECKOUT"
        )
        self.assertEqual(checkout["package_id"], package["id"])
        self.assertEqual(state["overlay_selection"]["mode"], "AUTO")
        self.assertEqual(state["overlay_selection"]["active_package_id"], package["id"])

        manual = self.client.post(
            "/api/media/overlay-selection",
            json={"mode": "MANUAL", "package_id": package["id"]},
        )
        self.assertEqual(manual.status_code, 200, manual.get_json())
        selected = self.client.get("/api/media/snapshot").get_json()["overlay_selection"]
        self.assertEqual(selected["active_package_id"], package["id"])
        self.assertEqual(selected["package"]["template_id"], package["template_id"])

        preview = self.client.get(
            f"/api/media/overlay-preview/{package['id']}.png"
            "?t=1.2&pressure=42&thrust=720&mode=VIDEO&width=640"
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.mimetype, "image/png")
        self.assertTrue(preview.data.startswith(bytes.fromhex("89504e470d0a1a0a")))
        self.assertGreater(len(preview.data), 20_000)

        transparent = self.client.get(
            f"/api/media/overlay-preview/{package['id']}.png"
            "?t=-2&pressure=0&thrust=0&mode=OVERLAY&width=640"
        )
        self.assertEqual(transparent.status_code, 200)
        self.assertTrue(transparent.data.startswith(bytes.fromhex("89504e47")))

    def test_public_layout_can_be_assigned_to_a_mission_phase(self):
        package_id = self.upload()
        response = self.client.post(
            "/api/media/phase-overlay",
            json={"phase": "LIFTOFF", "package_id": package_id, "transition": "DISSOLVE"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        state = self.client.get("/api/media/snapshot").get_json()
        liftoff = next(item for item in state["phase_overlay_assignments"] if item["phase"] == "LIFTOFF")
        self.assertEqual(liftoff["package_id"], package_id)
        self.assertEqual(liftoff["transition"], "DISSOLVE")

    def test_auto_and_manual_selection_modes_are_persisted(self):
        package_id = self.upload()
        manual = self.client.post(
            "/api/media/overlay-selection",
            json={"mode": "MANUAL", "package_id": package_id},
        )
        self.assertEqual(manual.status_code, 200, manual.get_json())
        state = self.client.get("/api/media/snapshot").get_json()
        self.assertEqual(state["overlay_selection"]["mode"], "MANUAL")
        self.assertEqual(state["overlay_selection"]["active_package_id"], package_id)

        automatic = self.client.post("/api/media/overlay-selection", json={"mode": "AUTO"})
        self.assertEqual(automatic.status_code, 200, automatic.get_json())
        state = self.client.get("/api/media/snapshot").get_json()
        self.assertEqual(state["overlay_selection"]["mode"], "AUTO")

    def test_invalid_phase_and_transition_are_rejected(self):
        package_id = self.upload()
        bad_phase = self.client.post(
            "/api/media/phase-overlay",
            json={"phase": "UNKNOWN", "package_id": package_id, "transition": "CUT"},
        )
        bad_transition = self.client.post(
            "/api/media/phase-overlay",
            json={"phase": "COUNTDOWN", "package_id": package_id, "transition": "WIPE"},
        )
        self.assertEqual(bad_phase.status_code, 400)
        self.assertEqual(bad_transition.status_code, 400)


if __name__ == "__main__":
    unittest.main()
