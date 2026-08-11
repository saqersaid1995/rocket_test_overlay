from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

import matplotlib
import cv2
import numpy as np
import pandas as pd
from PIL import Image

from rotpl_renderer import (
    RotplPackage,
    RotplRenderer,
    RotplValidationError,
    TemplateContext,
)
from rocket_overlay import (
    Config,
    RocketOverlayError,
    load_rotpl_template,
    render as render_video,
    validate_config_values,
)


ROOT = Path(__file__).resolve().parents[1]
SUPPLIED_PACKAGE = (
    ROOT
    / "workspace"
    / "template_packages"
    / "incoming"
    / "stellar-kinetics"
    / "stellar-kinetics.rotpl"
)
FONT_DIR = Path(matplotlib.get_data_path()) / "fonts" / "ttf"


class RotplRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rotpl-test-")
        self.temp = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_json(self, relative: str, value: object) -> None:
        target = self.temp / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding="utf-8")

    def _minimal_package(
        self, elements: list[dict], fonts: list[dict] | None = None
    ) -> Path:
        self._write_json(
            "manifest.json",
            {
                "schema": "rocket-overlay-template",
                "schema_version": "1.0.0",
                "id": "test.fixture",
                "template_version": "1.0.0",
                "canvas": {
                    "width": 200,
                    "height": 100,
                    "alpha_mode": "straight",
                },
                "entry": "layout.json",
                "fonts": fonts or [],
            },
        )
        self._write_json(
            "layout.json",
            {
                "schema": "rocket-overlay-layout",
                "canvas": {"width": 200, "height": 100},
                "elements": elements,
            },
        )
        return self.temp

    def _complete_supplied_package(self) -> RotplPackage:
        self.assertTrue(SUPPLIED_PACKAGE.exists())
        destination = self.temp / "supplied"
        destination.mkdir()
        with zipfile.ZipFile(SUPPLIED_PACKAGE) as archive:
            for member in archive.infolist():
                # The production loader performs the same traversal check.
                self.assertNotIn("..", Path(member.filename).parts)
                archive.extract(member, destination)
        manifest = json.loads((destination / "manifest.json").read_text("utf-8"))
        for declaration in manifest["fonts"]:
            target = destination / declaration["file"]
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            is_mono = declaration["font_id"].startswith("mono-")
            is_bold = declaration.get("weight", 400) >= 600
            if is_mono:
                source = FONT_DIR / (
                    "DejaVuSansMono-Bold.ttf" if is_bold else "DejaVuSansMono.ttf"
                )
            else:
                source = FONT_DIR / (
                    "DejaVuSans-Bold.ttf" if is_bold else "DejaVuSans.ttf"
                )
            shutil.copyfile(source, target)
        return RotplPackage.open(destination)

    def test_minimal_rect_matches_exact_golden_pixels(self) -> None:
        package = RotplPackage.open(
            self._minimal_package(
                [
                    {
                        "id": "golden_rect",
                        "type": "rect",
                        "z": 10,
                        "x": 10,
                        "y": 20,
                        "w": 30,
                        "h": 10,
                        "fill": "#FF0000",
                        "opacity": 1,
                    }
                ]
            )
        )
        actual = np.asarray(RotplRenderer(package).render({}), dtype=np.uint8)
        golden = np.zeros((100, 200, 4), dtype=np.uint8)
        # Pillow rectangles include their right and bottom coordinate.
        golden[20:31, 10:41] = (255, 0, 0, 255)
        np.testing.assert_array_equal(actual, golden)

    def test_missing_font_is_a_hard_validation_error(self) -> None:
        package_path = self._minimal_package(
            [
                {
                    "id": "title",
                    "type": "text",
                    "z": 1,
                    "text": "TEST",
                    "font_id": "display",
                }
            ],
            fonts=[
                {
                    "font_id": "display",
                    "file": "fonts/Required.ttf",
                    "family": "Required",
                    "weight": 400,
                }
            ],
        )
        with self.assertRaisesRegex(
            RotplValidationError, "Required template font is missing"
        ):
            RotplPackage.open(package_path)

    def test_supplied_layout_matches_golden_reference_geometry_and_scales(self) -> None:
        package = self._complete_supplied_package()
        renderer = RotplRenderer(package)
        context = renderer.sample_context()
        actual = np.asarray(renderer.render(context), dtype=np.uint8)
        golden = np.asarray(
            package.read_image("preview/reference-overlay-1920x1080.png"),
            dtype=np.uint8,
        )
        actual_alpha = actual[..., 3]
        golden_alpha = golden[..., 3]
        intersection = np.logical_and(actual_alpha > 64, golden_alpha > 64).sum()
        union = np.logical_or(actual_alpha > 64, golden_alpha > 64).sum()
        self.assertGreater(intersection / union, 0.94)
        self.assertLess(
            np.mean(np.abs(actual_alpha.astype(np.int16) - golden_alpha.astype(np.int16))),
            9.0,
        )
        scaled = renderer.render(context, output_size=(960, 540))
        self.assertEqual(scaled.size, (960, 540))

    def test_960_overlay_is_master_lanczos_and_tracks_golden(self) -> None:
        package = self._complete_supplied_package()
        renderer = RotplRenderer(package)
        context = renderer.sample_context()
        master = renderer.render(context, output_size=(1920, 1080))
        expected = (
            master.convert("RGBa")
            .resize((960, 540), Image.Resampling.LANCZOS)
            .convert("RGBA")
        )
        actual_bgra = renderer.render_bgra(context, output_size=(960, 540))
        actual_rgba = actual_bgra[..., [2, 1, 0, 3]]
        np.testing.assert_array_equal(actual_rgba, np.asarray(expected))

        golden_master = package.read_image("preview/reference-overlay-1920x1080.png")
        golden_small = np.asarray(
            golden_master.convert("RGBa")
            .resize((960, 540), Image.Resampling.LANCZOS)
            .convert("RGBA"),
            dtype=np.uint8,
        )
        self.assertLess(
            np.mean(
                np.abs(
                    actual_rgba[..., 3].astype(np.int16)
                    - golden_small[..., 3].astype(np.int16)
                )
            ),
            9.0,
        )

    def test_overlay_alpha_is_clear_away_from_declared_scrims_and_panels(self) -> None:
        package = self._complete_supplied_package()
        renderer = RotplRenderer(package)
        overlay = np.asarray(renderer.render(renderer.sample_context()), dtype=np.uint8)
        self.assertEqual(int(overlay[500, 500, 3]), 0)
        self.assertGreater(int(overlay[0, 500, 3]), 150)
        self.assertGreater(int(overlay[900, 500, 3]), 100)

    def test_missing_thrust_renders_na_and_hides_unit_and_gauge(self) -> None:
        package = self._complete_supplied_package()
        renderer = RotplRenderer(package)
        context = renderer.sample_context()
        context.values.update(
            {
                "telemetry.thrust.available": False,
                "telemetry.thrust.value": None,
                "telemetry.thrust.formatted": "N/A",
                "telemetry.thrust.unit": "SHOULD-NOT-RENDER",
                "telemetry.thrust.normalized": 1.0,
                "metrics.thrust.peak": 895.4,
            }
        )
        self.assertFalse(context.available("telemetry.thrust.unit"))
        self.assertFalse(context.available("telemetry.thrust.normalized"))
        self.assertFalse(context.available("metrics.thrust.peak"))
        missing = np.asarray(renderer.render(context), dtype=np.uint8)

        present_context = renderer.sample_context()
        present_context.values["telemetry.thrust.normalized"] = 1.0
        present = np.asarray(renderer.render(present_context), dtype=np.uint8)
        # A live channel draws a white bar; a missing channel leaves only the
        # translucent console panel at the same coordinate.
        self.assertGreater(int(present[950, 900, :3].max()), 240)
        self.assertLess(int(missing[950, 900, :3].max()), 40)
        # The N/A glyphs are still visible in the dynamic-value region.
        na_region = missing[895:943, 810:930, :3]
        self.assertGreater(int(na_region.max()), 220)

    def test_runtime_adapter_never_converts_absent_thrust_to_zero(self) -> None:
        cfg = SimpleNamespace(
            title="RNX-TEST",
            subtitle="STATIC FIRE TEST",
            pressure_unit="bar",
            thrust_unit="N",
            accent="#F59E0B",
            organization_name="STELLAR KINETICS",
            test_site="DUQM, OMAN",
            propellant="LOX / PROPANE",
            camera_label="CAM 01",
            capture_fps="120 FPS",
            pressure_limit=None,
            logo=None,
        )
        assets = SimpleNamespace(
            p_peak=70.0,
            f_peak=0.0,
            t_end=6.5,
            has_thrust=False,
            logo=None,
        )
        metrics = SimpleNamespace(
            peak_pressure=66.82,
            peak_pressure_time=1.42,
            peak_thrust=None,
            peak_thrust_time=None,
            total_impulse=None,
            burn_duration=6.5,
        )
        context = TemplateContext.from_runtime(
            cfg, assets, -4.52, 0.0, 0.0, metrics=metrics
        )
        self.assertFalse(context.resolve("telemetry.thrust.available"))
        self.assertIsNone(context.resolve("telemetry.thrust.value"))
        self.assertEqual(context.resolve("telemetry.thrust.formatted"), "N/A")
        self.assertIsNone(context.resolve("telemetry.thrust.normalized"))
        self.assertEqual(context.resolve("frame.mission_clock"), "T- 00:04.52")
        self.assertEqual(context.resolve("test.oxidizer"), "LOX")
        self.assertEqual(context.resolve("test.fuel"), "PROPANE")

    def test_config_requires_resolved_path_for_selected_template(self) -> None:
        with self.assertRaisesRegex(RocketOverlayError, "Template path is required"):
            validate_config_values(
                {
                    "video": Path("v"),
                    "data": Path("d"),
                    "output": Path("o"),
                    "template_id": "test.fixture",
                    "template_version": "1.0.0",
                    "template_sha256": "a" * 64,
                }
            )

    def test_job_loader_checks_exact_identity_and_archive_sha(self) -> None:
        directory = self._minimal_package([])
        archive_path = self.temp / "fixture.rotpl"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(directory / "manifest.json", "manifest.json")
            archive.write(directory / "layout.json", "layout.json")
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        cfg = Config(
            video=Path("v"),
            data=Path("d"),
            output=Path("o"),
            template_id="test.fixture",
            template_version="1.0.0",
            template_sha256=digest,
            template_path=archive_path,
        )
        package, renderer = load_rotpl_template(cfg)
        self.assertIsNotNone(package)
        self.assertIsInstance(renderer, RotplRenderer)
        package.close()

        cfg.template_version = "2.0.0"
        with self.assertRaisesRegex(RocketOverlayError, "version does not match"):
            load_rotpl_template(cfg)
        cfg.template_version = "1.0.0"
        cfg.template_sha256 = "0" * 64
        with self.assertRaisesRegex(RocketOverlayError, "SHA-256 does not match"):
            load_rotpl_template(cfg)
        archive_path.unlink()

    def test_delivery_render_uses_selected_rotpl_compositor(self) -> None:
        template_path = self._minimal_package(
            [
                {
                    "id": "delivery_marker",
                    "type": "rect",
                    "z": 50,
                    "x": 0,
                    "y": 0,
                    "w": 20,
                    "h": 10,
                    "fill": "#FF0000",
                    "opacity": 1,
                }
            ]
        )
        source = self.temp / "source.mp4"
        writer = cv2.VideoWriter(
            str(source), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (160, 90)
        )
        self.assertTrue(writer.isOpened())
        for _ in range(2):
            writer.write(np.full((90, 160, 3), (15, 25, 35), dtype=np.uint8))
        writer.release()
        data = self.temp / "telemetry.csv"
        pd.DataFrame(
            {"time": [0.0, 0.4], "pressure": [1.0, 2.0]}
        ).to_csv(data, index=False)
        output = self.temp / "delivery.mp4"
        render_video(
            Config(
                video=source,
                data=data,
                output=output,
                thrust_column="__none__",
                width=960,
                height=540,
                intro_duration_s=0,
                outro_duration_s=0,
                keep_audio=False,
                thumbnail=False,
                template_id="test.fixture",
                template_version="1.0.0",
                template_path=template_path,
            )
        )
        capture = cv2.VideoCapture(str(output))
        ok, frame = capture.read()
        capture.release()
        self.assertTrue(ok)
        # H.264 chroma subsampling can shift a few code values, hence ranges.
        self.assertGreater(int(frame[20, 20, 2]), 220)
        self.assertLess(int(frame[20, 20, 1]), 35)
        self.assertLess(int(frame[20, 20, 0]), 35)
        self.assertGreater(int(frame[300, 400, 0]), 5)
        summary = json.loads(output.with_suffix(".json").read_text("utf-8"))
        self.assertEqual(summary["template"]["id"], "test.fixture")
        self.assertEqual(summary["template"]["version"], "1.0.0")
        self.assertEqual(
            summary["template"]["renderer"], "opencv-declarative-v1"
        )

    def test_sdr_composition_uses_the_exact_hdr_bgra_overlay(self) -> None:
        package = RotplPackage.open(
            self._minimal_package(
                [
                    {
                        "id": "alpha_marker",
                        "type": "rect",
                        "z": 50,
                        "x": 21,
                        "y": 17,
                        "w": 51,
                        "h": 33,
                        "fill": "#F04020",
                        "opacity": 0.63,
                    }
                ]
            )
        )
        renderer = RotplRenderer(package)
        hdr_bgra = renderer.render_bgra({}, output_size=(100, 50))
        overlay_rgba = Image.fromarray(hdr_bgra[..., [2, 1, 0, 3]], "RGBA")
        background_bgr = np.full((50, 100, 3), (20, 40, 80), dtype=np.uint8)
        background_rgba = Image.fromarray(background_bgr[..., ::-1], "RGB").convert(
            "RGBA"
        )
        expected_bgr = np.asarray(
            Image.alpha_composite(background_rgba, overlay_rgba).convert("RGB"),
            dtype=np.uint8,
        )[..., ::-1]
        actual_bgr = renderer.compose_bgr(background_bgr, {})
        np.testing.assert_array_equal(actual_bgr, expected_bgr)
        # Premultiplied resizing leaves no coloured RGB behind zero alpha.
        self.assertTrue(np.all(hdr_bgra[hdr_bgra[..., 3] == 0, :3] == 0))


if __name__ == "__main__":
    unittest.main()
