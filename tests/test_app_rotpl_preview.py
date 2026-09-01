from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pandas as pd

import app as webapp
from rotpl_registry import ResolvedTemplate


class _Registry:
    def __init__(self, resolved: ResolvedTemplate):
        self.resolved = resolved
        self.calls: list[tuple[str | None, str | None]] = []

    def resolve(
        self, template_id: str | None = None, version: str | None = None
    ) -> ResolvedTemplate:
        self.calls.append((template_id, version))
        return self.resolved

    def get(self, template_id: str, version: str) -> dict[str, object]:
        return {
            "id": template_id,
            "version": version,
            "validation": {
                "valid": True,
                "activatable": True,
                "errors": [],
                "blocked_reasons": [],
            },
        }


class RotplPreviewIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="app-rotpl-preview-")
        self.root = Path(self.temporary.name)
        self.preview_id = uuid.uuid4().hex
        frame = pd.DataFrame({
            "Time(s)": [0.0, 0.5, 1.0],
            "Pressure(bar)": [0.0, 5.0, 10.0],
        })
        now = time.monotonic()
        with webapp.preview_sessions_lock:
            webapp.preview_sessions[self.preview_id] = {
                "frame": frame,
                "created_at": now,
                "touched_at": now,
                "logo_path": None,
                "logo_filename": None,
                "logo_revision": 0,
                "cache_lock": threading.RLock(),
                "telemetry_cache": {},
                "assets_cache": {},
                "template_cache": {},
            }
        self.original_registry = webapp.template_registry
        self.client = webapp.app.test_client()
        self.created_job_ids: list[str] = []

    def tearDown(self) -> None:
        with webapp.preview_sessions_lock:
            session = webapp.preview_sessions.pop(self.preview_id, None)
        if session is not None:
            webapp.remove_preview_artifacts(self.preview_id, session)
        webapp.template_registry = self.original_registry
        with webapp.jobs_lock:
            for job_id in self.created_job_ids:
                webapp.jobs.pop(job_id, None)
        self.temporary.cleanup()

    def _package(self) -> ResolvedTemplate:
        package = self.root / "files"
        package.mkdir()
        manifest = {
            "schema": "rocket-overlay-template",
            "schema_version": "1.0.0",
            "id": "test.preview",
            "template_version": "1.0.0",
            "canvas": {
                "width": 1920,
                "height": 1080,
                "alpha_mode": "straight",
            },
            "entry": "layout.json",
            "fonts": [],
            "required_bindings": ["telemetry.pressure.normalized"],
        }
        layout = {
            "schema": "rocket-overlay-layout",
            "canvas": {"width": 1920, "height": 1080},
            "elements": [
                {
                    "id": "pressure",
                    "type": "bar_gauge",
                    "z": 30,
                    "x": 0,
                    "y": 0,
                    "w": 1920,
                    "h": 1080,
                    "bind": "telemetry.pressure.normalized",
                    "color": "#FF0000",
                }
            ],
        }
        (package / "manifest.json").write_text(json.dumps(manifest), "utf-8")
        (package / "layout.json").write_text(json.dumps(layout), "utf-8")
        package_archive = self.root / "package.rotpl"
        package_archive.write_bytes(b"fixture")
        digest = "a" * 64
        return ResolvedTemplate(
            id="test.preview",
            version="1.0.0",
            sha256=digest,
            files_path=package,
            package_path=package_archive,
            manifest_path=package / "manifest.json",
            layout_path=package / "layout.json",
        )

    @staticmethod
    def _assets() -> SimpleNamespace:
        return SimpleNamespace(
            p_peak=10.0,
            f_peak=0.0,
            t_end=1.0,
            has_thrust=False,
            logo=None,
        )

    def _payload(self, sha256: str = "a" * 64) -> dict[str, object]:
        return {
            "template_id": "test.preview",
            "template_version": "1.0.0",
            "sha256": sha256,
            "broadcast_theme": "launch",
            "time": 1.0,
            "time_column": "Time(s)",
            "pressure_column": "Pressure(bar)",
            "thrust_column": "__none__",
            "telemetry_zero_s": 0.0,
            "width": 640,
            "height": 360,
            "title": "RNX-TEST",
            "subtitle": "STATIC FIRE TEST",
        }

    def test_custom_template_renders_master_png_and_reuses_verified_renderer(self) -> None:
        registry = _Registry(self._package())
        webapp.template_registry = registry
        with patch.object(
            webapp.broadcast_overlay, "build_assets", return_value=self._assets()
        ):
            first = self.client.post(
                f"/api/previews/{self.preview_id}/render", json=self._payload()
            )
            second = self.client.post(
                f"/api/previews/{self.preview_id}/render", json=self._payload()
            )

        self.assertEqual(first.status_code, 200, first.get_json(silent=True))
        self.assertEqual(second.status_code, 200, second.get_json(silent=True))
        image = cv2.imdecode(
            np.frombuffer(first.data, dtype=np.uint8), cv2.IMREAD_UNCHANGED
        )
        self.assertEqual(image.shape, (1080, 1920, 4))
        np.testing.assert_array_equal(image[100, 100], [0, 0, 255, 255])
        np.testing.assert_array_equal(image[100, 1900], [0, 0, 0, 0])
        self.assertEqual(first.headers["X-Preview-Width"], "1920")
        self.assertEqual(first.headers["X-Preview-Height"], "1080")
        self.assertEqual(
            first.headers["X-Preview-Template-Renderer"],
            "rotpl-declarative-v1",
        )
        self.assertEqual(first.headers["X-Preview-Template-Cache"], "miss")
        self.assertEqual(second.headers["X-Preview-Template-Cache"], "hit")
        self.assertEqual(registry.calls, [
            ("test.preview", "1.0.0"),
            ("test.preview", "1.0.0"),
        ])

    def test_sha_mismatch_is_rejected_before_rendering(self) -> None:
        registry = _Registry(self._package())
        webapp.template_registry = registry
        with patch.object(
            webapp.broadcast_overlay, "build_assets", return_value=self._assets()
        ) as build_assets:
            response = self.client.post(
                f"/api/previews/{self.preview_id}/render",
                json=self._payload("b" * 64),
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("digest", response.get_json()["error"])
        build_assets.assert_not_called()

    def test_builtin_theme_keeps_existing_compositor_and_requested_size(self) -> None:
        overlay = np.zeros((360, 640, 4), dtype=np.uint8)
        with (
            patch.object(
                webapp.broadcast_overlay, "build_assets", return_value=self._assets()
            ),
            patch.object(
                webapp.broadcast_overlay,
                "compose_overlay_bgra",
                return_value=overlay,
            ) as compose,
        ):
            response = self.client.post(
                f"/api/previews/{self.preview_id}/render",
                json={
                    "broadcast_theme": "launch",
                    "time": 0.5,
                    "time_column": "Time(s)",
                    "pressure_column": "Pressure(bar)",
                    "thrust_column": "__none__",
                    "telemetry_zero_s": 0.0,
                    "width": 640,
                    "height": 360,
                },
            )
        self.assertEqual(response.status_code, 200, response.get_json(silent=True))
        compose.assert_called_once()
        self.assertEqual(response.headers["X-Preview-Width"], "640")
        self.assertEqual(response.headers["X-Preview-Height"], "360")
        self.assertEqual(response.headers["X-Preview-Template-Renderer"], "hardcoded")

    def test_export_job_pins_exact_template_identity_and_master_resolution(self) -> None:
        resolved = self._package()
        registry = _Registry(resolved)
        webapp.template_registry = registry
        video_path = self.root / "source.mp4"
        data_path = self.root / "telemetry.csv"
        video_path.write_bytes(b"video-fixture")
        data_path.write_text("Time(s),Pressure(bar)\n0,0\n1,10\n", "utf-8")
        captured: dict[str, object] = {}

        class FakeCapture:
            def get(self, field: int) -> float:
                return {
                    cv2.CAP_PROP_FRAME_WIDTH: 1280.0,
                    cv2.CAP_PROP_FRAME_HEIGHT: 720.0,
                    cv2.CAP_PROP_FPS: 30.0,
                }.get(field, 0.0)

            def release(self) -> None:
                pass

        class FakeThread:
            def __init__(self, target, args, daemon=False):
                captured["target"] = target
                captured["args"] = args
                captured["daemon"] = daemon

            def start(self) -> None:
                captured["started"] = True

        def fake_upload(field, _job_dir, _extensions, required=True):
            if field == "video":
                return video_path
            if field == "data":
                return data_path
            return None

        upload_root = self.root / "uploads"
        output_root = self.root / "outputs"
        with (
            patch.object(webapp, "UPLOAD_DIR", upload_root),
            patch.object(webapp, "OUTPUT_DIR", output_root),
            patch.object(webapp, "consume_upload", side_effect=fake_upload),
            patch.object(webapp.cv2, "VideoCapture", return_value=FakeCapture()),
            patch.object(webapp.threading, "Thread", FakeThread),
        ):
            response = self.client.post(
                "/api/jobs",
                data={
                    "template_id": resolved.id,
                    "template_version": resolved.version,
                    "sha256": resolved.sha256,
                    "resolution": "1920x1080",
                    "broadcast_theme": "launch",
                    "time_column": "Time(s)",
                    "pressure_column": "Pressure(bar)",
                    "thrust_column": "__none__",
                    "title": "MISSION DUQM-3 QUALIFICATION",
                    "subtitle": "WEKA ENGINE · STATIC FIRE",
                    "coordinates_text": "19°39'N 57°42'E",
                    "oxidizer": "LOX",
                    "fuel": "PROPANE",
                    "ablative_material": "PHENOLIC ABLATIVE",
                },
            )

        self.assertEqual(response.status_code, 202, response.get_json(silent=True))
        payload = response.get_json()
        self.created_job_ids.append(payload["id"])
        cfg = captured["args"][1]
        self.assertTrue(captured["started"])
        self.assertEqual((cfg.width, cfg.height), (1920, 1080))
        self.assertEqual(cfg.template_id, resolved.id)
        self.assertEqual(cfg.template_version, resolved.version)
        self.assertEqual(cfg.template_sha256, resolved.sha256)
        self.assertEqual(cfg.template_path, resolved.files_path)
        self.assertEqual(cfg.coordinates_text, "19°39'N 57°42'E")
        self.assertEqual(cfg.oxidizer, "LOX")
        self.assertEqual(cfg.fuel, "PROPANE")
        self.assertEqual(cfg.ablative_material, "PHENOLIC ABLATIVE")
        self.assertEqual(payload["template"]["sha256"], resolved.sha256)


if __name__ == "__main__":
    unittest.main()
