"""Flask test-client integration tests for the Live Studio blueprint.

Mirrors the registry-shim + app.test_client() idioms already used in
tests/test_app_rotpl_preview.py. MJPEG streaming routes are read for a
bounded number of bytes and then closed, since the response body is an
infinite multipart stream by design.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import app as webapp
import live_session
from rotpl_registry import ResolvedTemplate


def _write_test_video(path: Path, frames: int = 6, size: tuple[int, int] = (64, 48)) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, size)
    assert writer.isOpened()
    for index in range(frames):
        writer.write(np.full((size[1], size[0], 3), (10 + index, 20, 30), dtype=np.uint8))
    writer.release()


class _Registry:
    """Minimal registry shim exposing the same surface live_session.py uses."""

    def __init__(self, resolved: ResolvedTemplate):
        self.resolved = resolved

    def resolve(self, template_id=None, version=None) -> ResolvedTemplate:
        return self.resolved

    def get(self, template_id: str, version: str):
        return {
            "id": template_id,
            "version": version,
            "validation": {"valid": True, "activatable": True, "errors": [], "blocked_reasons": []},
        }


class LiveRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = webapp.app.test_client()
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.session_ids: list[str] = []

    def tearDown(self) -> None:
        for session_id in self.session_ids:
            live_session.live_sessions.end(session_id)

    def _create_session(self, **overrides) -> str:
        payload = {
            "mission_name": "Unit Test Static Fire",
            "mission_type": "static_fire",
            "t0_epoch": time.time() + 5.0,
            "max_wind_speed_mps": 10.0,
            "temp_min_c": -5.0,
            "temp_max_c": 40.0,
            "static_fire": {"expected_burn_duration_s": 8.0},
        }
        payload.update(overrides)
        response = self.client.post("/api/live/sessions", json=payload)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        session_id = response.get_json()["session"]["id"]
        self.session_ids.append(session_id)
        return session_id

    def _package(self) -> ResolvedTemplate:
        package = self.temp / "files"
        package.mkdir()
        manifest = {
            "schema": "rocket-overlay-template",
            "schema_version": "1.0.0",
            "id": "test.live",
            "template_version": "1.0.0",
            "canvas": {"width": 320, "height": 240, "alpha_mode": "straight"},
            "entry": "layout.json",
            "fonts": [],
            "required_bindings": ["telemetry.pressure.normalized"],
        }
        layout = {
            "schema": "rocket-overlay-layout",
            "canvas": {"width": 320, "height": 240},
            "elements": [
                {
                    "id": "pressure", "type": "bar_gauge", "z": 30,
                    "x": 0, "y": 0, "w": 320, "h": 240,
                    "bind": "telemetry.pressure.normalized", "color": "#FF0000",
                }
            ],
        }
        (package / "manifest.json").write_text(json.dumps(manifest), "utf-8")
        (package / "layout.json").write_text(json.dumps(layout), "utf-8")
        return ResolvedTemplate(
            id="test.live", version="1.0.0", sha256="b" * 64,
            files_path=package, package_path=self.temp / "package.rotpl",
            manifest_path=package / "manifest.json", layout_path=package / "layout.json",
        )

    # -- session lifecycle --------------------------------------------------

    def test_create_session_requires_mission_name_and_t0(self):
        response = self.client.post("/api/live/sessions", json={"mission_type": "static_fire"})
        self.assertEqual(response.status_code, 400)

    def test_create_static_fire_session_and_fetch_state(self):
        session_id = self._create_session()
        response = self.client.get(f"/api/live/{session_id}/state")
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["mission_type"], "static_fire")
        self.assertFalse(state["armed"])
        weather = next(item for item in state["checklist"] if item["id"] == "weather_hold")
        self.assertEqual(weather["state"], "pending")

    def test_create_launch_session_with_launch_details(self):
        session_id = self._create_session(
            mission_type="launch",
            launch={"vehicle_type": "TestBird-1", "target_altitude_km": 200.0},
        )
        response = self.client.get(f"/api/live/{session_id}/state")
        self.assertEqual(response.get_json()["mission_type"], "launch")

    def test_unknown_session_returns_404(self):
        response = self.client.get("/api/live/does-not-exist/state")
        self.assertEqual(response.status_code, 404)

    # -- checklist / hold / resume / abort -----------------------------------

    def test_checklist_toggle_and_hold_resume_abort(self):
        session_id = self._create_session()
        response = self.client.post(
            f"/api/live/{session_id}/checklist/safety_officer", json={"state": "go"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["item"]["state"], "go")

        response = self.client.post(f"/api/live/{session_id}/checklist/weather_hold", json={"state": "go"})
        self.assertEqual(response.status_code, 400)  # auto item, not manually settable

        response = self.client.post(f"/api/live/{session_id}/hold")
        self.assertTrue(response.get_json()["session"]["manual_hold"])

        response = self.client.post(f"/api/live/{session_id}/resume")
        self.assertFalse(response.get_json()["session"]["manual_hold"])

        response = self.client.post(f"/api/live/{session_id}/abort")
        self.assertTrue(response.get_json()["session"]["aborted"])

    # -- telemetry ingestion --------------------------------------------------

    def test_telemetry_ingestion_updates_state(self):
        session_id = self._create_session()
        response = self.client.post(
            f"/api/live/{session_id}/telemetry",
            json={"channels": {"pressure": 12.5, "wind_speed": 3.0}},
        )
        self.assertEqual(response.status_code, 200)
        state = self.client.get(f"/api/live/{session_id}/state").get_json()
        self.assertEqual(state["telemetry"]["latest"]["pressure"], 12.5)

    def test_telemetry_rejects_bad_channel_name_and_non_numeric_value(self):
        session_id = self._create_session()
        response = self.client.post(
            f"/api/live/{session_id}/telemetry", json={"channels": {"Bad Name!": 1.0}}
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            f"/api/live/{session_id}/telemetry", json={"channels": {"pressure": "not-a-number"}}
        )
        self.assertEqual(response.status_code, 400)

    # -- cameras + MJPEG streaming -------------------------------------------

    def test_attach_camera_and_stream_raw_mjpeg(self):
        session_id = self._create_session()
        video_path = self.temp / "cam.mp4"
        _write_test_video(video_path)
        response = self.client.post(
            f"/api/live/{session_id}/camera/1", json={"uri": str(video_path)}
        )
        self.assertEqual(response.status_code, 200)

        deadline = time.monotonic() + 5.0
        connected = False
        while time.monotonic() < deadline:
            state = self.client.get(f"/api/live/{session_id}/state").get_json()
            if state["cameras"]["1"]["connected"]:
                connected = True
                break
            time.sleep(0.1)
        self.assertTrue(connected)

        stream_response = self.client.get(f"/api/live/{session_id}/camera/1/stream")
        self.assertEqual(stream_response.status_code, 200)
        self.assertIn("multipart/x-mixed-replace", stream_response.content_type)
        chunk = next(stream_response.response)
        self.assertIn(b"Content-Type: image/jpeg", chunk)
        stream_response.close()

    def test_program_stream_requires_armed_session(self):
        session_id = self._create_session()
        response = self.client.get(f"/api/live/{session_id}/program/stream")
        self.assertEqual(response.status_code, 409)

    def test_armed_session_program_stream_serves_standby_before_broadcast(self):
        session_id = self._create_session()
        self.client.post(f"/api/live/{session_id}/arm")
        deadline = time.monotonic() + 3.0
        chunk = b""
        stream_response = self.client.get(f"/api/live/{session_id}/program/stream")
        try:
            while time.monotonic() < deadline and not chunk:
                chunk = next(stream_response.response)
            self.assertIn(b"Content-Type: image/jpeg", chunk)
        finally:
            stream_response.close()

    # -- active template selection reuses the ROTPL registry ------------------

    def test_select_active_template_resolves_via_registry(self):
        session_id = self._create_session()
        resolved = self._package()
        with patch.object(live_session, "template_registry", _Registry(resolved)):
            response = self.client.post(
                f"/api/live/{session_id}/template",
                json={
                    "template_id": resolved.id,
                    "template_version": resolved.version,
                    "sha256": resolved.sha256,
                },
            )
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
