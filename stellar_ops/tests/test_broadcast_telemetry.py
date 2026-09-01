import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module


def rotpl(manifest, layout=None, extras=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("layout.json", json.dumps(layout or {"elements": []}))
        for name, value in (extras or {}).items():
            archive.writestr(name, value)
    stream.seek(0)
    return stream


class BroadcastTelemetryFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = control_module.CONTROL_DB
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        control_module.CONTROL_DB = self.original
        self.temp.cleanup()

    def test_catalog_contains_flight_motor_weather_and_quality_channels(self):
        state = self.client.get("/api/media/snapshot").get_json()
        catalog = {item["channel_id"]: item for item in state["telemetry_catalog"]}
        for channel_id in (
            "position.altitude_agl",
            "velocity.speed_3d",
            "velocity.mach",
            "acceleration.g_load",
            "attitude.pitch",
            "motor.chamber_pressure",
            "motor.thrust",
            "weather.wind_speed",
            "mission.phase",
            "link.status",
        ):
            self.assertIn(channel_id, catalog)
        self.assertEqual(catalog["position.latitude"]["classification"], "INTERNAL")
        self.assertEqual(catalog["vehicle.arming_state"]["classification"], "RESTRICTED")

    def test_custom_channel_registration_requires_canonical_metadata(self):
        invalid = self.client.post("/api/media/telemetry-channel", json={
            "channel_id": "Bad Channel",
            "label": "Bad",
            "canonical_unit": "m",
        })
        self.assertEqual(invalid.status_code, 400)
        valid = self.client.post("/api/media/telemetry-channel", json={
            "channel_id": "payload.internal_temperature",
            "label": "Payload temperature",
            "canonical_unit": "degC",
            "category": "PAYLOAD",
            "data_type": "number",
            "classification": "INTERNAL",
            "source_kind": "MEASURED",
        })
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.get_json()["channel"]["channel_id"], "payload.internal_temperature")

    def test_generic_rotpl_package_upload_and_checksum(self):
        package = rotpl(
            {
                "template_id": "barq-public-launch",
                "name": "BARQ Public Launch",
                "version": "1.0.0",
                "canvas": "1920x1080@30",
                "required_channels": [
                    "mission.elapsed_time",
                    "position.altitude_agl",
                    "velocity.speed_3d",
                ],
                "optional_channels": ["velocity.mach", "attitude.pitch"],
            },
            {
                "elements": [
                    {"id": "altitude", "type": "numeric", "binding": "position.altitude_agl"},
                    {"id": "speed", "type": "numeric", "binding": {"channel": "velocity.speed_3d"}},
                ]
            },
        )
        response = self.client.post(
            "/api/media/overlay-package",
            data={"package": (package, "barq.rotpl")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertTrue(body["public_safe"])
        self.assertEqual(len(body["sha256"]), 64)
        saved = self.client.get("/api/media/snapshot").get_json()["overlay_packages"]
        self.assertEqual(saved[0]["template_id"], "barq-public-launch")
        self.assertNotIn("archive_blob", saved[0])

    def test_original_studio_rotpl_remains_compatible(self):
        package = rotpl(
            {
                "schema": "rocket-overlay-template",
                "id": "legacy-static-fire",
                "display_name": "Legacy Static Fire",
                "template_version": "1.2.0",
                "entry": "layout.json",
                "canvas": {"width": 1920, "height": 1080, "alpha_mode": "straight"},
                "required_bindings": [
                    "frame.mission_clock",
                    "telemetry.pressure.value",
                    "telemetry.thrust.value",
                ],
                "variables": {
                    "telemetry.pressure.formatted": {"default": "0.0"},
                    "telemetry.thrust.formatted": {"default": "0"},
                },
            },
            {
                "canvas": {"width": 1920, "height": 1080},
                "elements": [
                    {"id": "pressure", "type": "text", "bind": "telemetry.pressure.formatted"},
                    {"id": "thrust", "type": "text", "bind": "telemetry.thrust.formatted"},
                ],
            },
        )
        response = self.client.post(
            "/api/media/overlay-package",
            data={"package": (package, "legacy.rotpl")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        state = self.client.get("/api/media/snapshot").get_json()
        saved = state["overlay_packages"][0]
        self.assertIn("motor.chamber_pressure", saved["required_channels"])
        self.assertIn("motor.thrust", saved["required_channels"])
        self.assertIn("mission.elapsed_time", saved["required_channels"])

    def test_unknown_required_channel_and_unsafe_archive_are_rejected(self):
        unknown = rotpl({
            "template_id": "unknown-channel",
            "name": "Unknown",
            "version": "1.0.0",
            "canvas": "1920x1080",
            "required_channels": ["unknown.secret_value"],
        })
        response = self.client.post(
            "/api/media/overlay-package",
            data={"package": (unknown, "unknown.rotpl")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not registered", response.get_json()["error"])

        unsafe = rotpl(
            {
                "template_id": "unsafe-package",
                "name": "Unsafe",
                "version": "1.0.0",
                "canvas": "1920x1080",
                "required_channels": ["mission.elapsed_time"],
            },
            extras={"../escape.txt": "blocked"},
        )
        response = self.client.post(
            "/api/media/overlay-package",
            data={"package": (unsafe, "unsafe.rotpl")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("unsafe path", response.get_json()["error"])

    def test_internal_channel_marks_package_not_public_safe(self):
        package = rotpl({
            "template_id": "engineering-overlay",
            "name": "Engineering Overlay",
            "version": "1.0.0",
            "canvas": "1920x1080",
            "required_channels": ["position.latitude"],
        })
        response = self.client.post(
            "/api/media/overlay-package",
            data={"package": (package, "engineering.rotpl")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["public_safe"])

    def test_overlay_studio_page_is_available(self):
        response = self.client.get("/media/overlays")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Broadcast Overlay Studio", response.data)
        self.assertIn(b"SELECT .ROTPL PACKAGE", response.data)


if __name__ == "__main__":
    unittest.main()
