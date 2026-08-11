import argparse
import dataclasses
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
import pandas as pd
import cv2
import rocket_overlay_broadcast as broadcast_overlay

from app import app, normalize_preview_telemetry
from rocket_overlay import (
    Config,
    RocketOverlayError,
    build_parser,
    cover_resize_position,
    default_scene_config,
    config_from_args,
    calculate_metrics,
    camera_layout_frame,
    compose_scene_frame,
    finite_range,
    interpolate_telemetry,
    ffmpeg_executable,
    hlg_overlay_filter_graph,
    probe_nominal_fps,
    read_telemetry,
    render,
    suggest_telemetry_zero_s,
    telemetry_has_thrust,
    determine_test_phase,
    validate_config_values,
    validate_scene_config,
)


class TelemetryTests(unittest.TestCase):
    def test_automatic_zero_finds_ignition_after_long_recorder_lead_in(self):
        times = np.arange(0.0, 100.0, 0.05)
        pressure = np.zeros_like(times)
        rising = (times >= 82.0) & (times < 83.0)
        pressure[rising] = (times[rising] - 82.0) * 60.0
        falling = (times >= 83.0) & (times < 87.0)
        pressure[falling] = np.maximum(0.0, 60.0 - (times[falling] - 83.0) * 15.0)

        zero, diagnostics = suggest_telemetry_zero_s(times, pressure)

        self.assertGreaterEqual(zero, 81.95)
        self.assertLess(zero, 82.15)
        self.assertEqual(diagnostics["method"], "sustained_signal_onset")
        self.assertEqual(diagnostics["signal"], "pressure")
        self.assertGreater(diagnostics["lead_in_s"], 80.0)

    def test_preview_normalization_auto_aligns_data_and_preserves_manual_zero(self):
        frame = pd.DataFrame({
            "Time(s)": np.arange(0.0, 90.0, 0.05),
            "Pressure(bar)": 0.0,
        })
        active = frame["Time(s)"] >= 82.0
        frame.loc[active, "Pressure(bar)"] = np.minimum(
            (frame.loc[active, "Time(s)"] - 82.0) * 40.0, 60.0
        )
        payload = {
            "time_column": "Time(s)",
            "pressure_column": "Pressure(bar)",
            "thrust_column": "__none__",
        }

        automatic = normalize_preview_telemetry(frame, payload)
        self.assertEqual(automatic.attrs["telemetry_zero_source"], "automatic")
        self.assertGreater(automatic.attrs["telemetry_zero_s"], 81.9)
        self.assertGreater(
            interpolate_telemetry(automatic, 1.0)[0],
            0.0,
        )
        self.assertFalse(automatic.attrs["has_thrust"])

        explicit = normalize_preview_telemetry(
            frame, {**payload, "telemetry_zero_s": "80.0"}
        )
        self.assertEqual(explicit.attrs["telemetry_zero_s"], 80.0)
        self.assertEqual(explicit.attrs["telemetry_zero_source"], "explicit")
        self.assertEqual(explicit.attrs["telemetry_zero_diagnostics"]["method"], "explicit")

    def test_cleanup_scaling_duplicates_and_interpolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.csv"
            pd.DataFrame({
                "Time (s)": [1000, 1000, 2000, 3000],
                "Pressure (bar)": [10, 14, np.nan, 30],
                "Thrust (N)": [100, 140, 220, 300],
            }).to_csv(path, index=False)
            cfg = Config(
                video=Path("unused.mp4"),
                data=path,
                output=Path("unused-output.mp4"),
                time_scale=0.001,
            )
            clean = read_telemetry(cfg)
            self.assertEqual(clean["time"].tolist(), [0.0, 1.0, 2.0])
            self.assertEqual(clean["pressure"].tolist(), [12.0, 22.0, 30.0])
            self.assertEqual(interpolate_telemetry(clean, 0.5), (17.0, 170.0))

    def test_values_are_zero_outside_recorded_range(self):
        frame = pd.DataFrame({
            "time": [0.0, 1.0],
            "pressure": [3.0, 7.0],
            "thrust": [30.0, 70.0],
        })
        self.assertEqual(interpolate_telemetry(frame, -0.01), (0.0, 0.0))
        self.assertEqual(interpolate_telemetry(frame, 1.01), (0.0, 0.0))
        self.assertEqual(interpolate_telemetry(frame, 0.0), (3.0, 30.0))

    def test_pressure_only_file_uses_zero_thrust(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pressure.csv"
            pd.DataFrame({
                "Time(s)": [44.162, 44.167],
                "Pressure(bar)": [0.174, 0.250],
            }).to_csv(path, index=False)
            cfg = Config(
                video=Path("unused.mp4"),
                data=path,
                output=Path("unused-output.mp4"),
                thrust_column="__none__",
                telemetry_zero_s=44.162,
            )
            clean = read_telemetry(cfg)
            self.assertEqual(clean["thrust"].tolist(), [0.0, 0.0])
            self.assertFalse(telemetry_has_thrust(clean))
            self.assertAlmostEqual(clean["time"].iloc[0], 0.0)
            self.assertAlmostEqual(clean["time"].iloc[1], 0.005)

    def test_flat_finite_range_never_collapses(self):
        lo, hi = finite_range(np.array([0.0, 0.0]))
        self.assertLess(lo, hi)

    def test_engineering_metrics_and_phases(self):
        frame = pd.DataFrame({
            "time": [0.0, 1.0, 2.0, 3.0],
            "pressure": [0.0, 10.0, 5.0, 0.0],
            "thrust": [0.0, 100.0, 50.0, 0.0],
        })
        frame.attrs["has_thrust"] = True
        metrics = calculate_metrics(frame)
        self.assertEqual(metrics.peak_pressure, 10.0)
        self.assertEqual(metrics.peak_pressure_time, 1.0)
        self.assertAlmostEqual(metrics.total_impulse, 150.0)
        self.assertEqual(determine_test_phase(-1.0, metrics, 0.0)[0], "STANDBY")
        self.assertEqual(determine_test_phase(0.0, metrics, 0.0)[0], "IGNITION")
        self.assertEqual(determine_test_phase(1.0, metrics, 11.0, 10.0)[0], "ABORT")


class ConfigTests(unittest.TestCase):
    def test_cli_defaults_to_sdr_and_archive_hdr_is_explicit(self):
        parser = build_parser()
        required = [
            "--video", "source.mp4",
            "--data", "telemetry.csv",
            "--output", "result.mp4",
        ]
        default_cfg = config_from_args(parser.parse_args(required))
        archive_cfg = config_from_args(
            parser.parse_args([*required, "--archive-hdr"])
        )

        self.assertFalse(default_cfg.preserve_source_quality)
        self.assertEqual(default_cfg.crf, 15)
        self.assertTrue(archive_cfg.preserve_source_quality)

    def test_broadcast_themes_remain_pixel_exact_to_supplied_templates(self):
        self.assertIs(
            broadcast_overlay.compose_frame_range_line,
            broadcast_overlay.compose_frame_stellar_console,
        )
        reference_path = (
            Path(__file__).resolve().parents[1]
            / "rocket_overlay_broadcast-3).py"
        )
        spec = importlib.util.spec_from_file_location(
            "rocket_overlay_broadcast_reference", reference_path
        )
        reference = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = reference
        try:
            spec.loader.exec_module(reference)
        finally:
            sys.modules.pop(spec.name, None)

        telemetry = pd.DataFrame(
            {
                "time": [0.0, 0.4, 1.2, 2.1, 3.0],
                "pressure": [0.0, 43.0, 67.0, 31.0, 0.0],
                "thrust": [0.0, 720.0, 1280.0, 610.0, 0.0],
            }
        )
        telemetry.attrs["has_thrust"] = True
        height, width = 360, 640
        yy, xx = np.indices((height, width))
        frame = np.stack(
            ((xx + yy) % 256, (2 * xx + yy) % 256, (xx + 2 * yy) % 256),
            axis=2,
        ).astype(np.uint8)
        mappings = (
            ("launch", "a", reference.compose_frame, broadcast_overlay.compose_frame_launch),
            ("mission_control", "b", reference.compose_frame_b, broadcast_overlay.compose_frame_mission_control),
            (
                "stellar_console",
                "d",
                reference.compose_frame_d,
                broadcast_overlay.compose_frame_stellar_console,
            ),
        )
        for theme, template, reference_compose, current_compose in mappings:
            with self.subTest(theme=theme):
                shared = dict(
                    video=Path("video.mov"),
                    data=Path("telemetry.csv"),
                    output=Path("output.mp4"),
                    width=width,
                    height=height,
                    title="RNX-TEST-L819-V2",
                    subtitle="STATIC FIRE TEST",
                    accent="#38BDF8",
                    show_chart=True,
                    show_phases=True,
                )
                reference_cfg = reference.Config(**shared, template=template)
                current_cfg = broadcast_overlay.Config(**shared, broadcast_theme=theme)
                reference_assets = reference.build_assets(reference_cfg, telemetry)
                current_assets = broadcast_overlay.build_assets(current_cfg, telemetry)
                for sample_time in (-3.5, 1.7, 3.8):
                    expected = reference_compose(
                        frame,
                        reference_cfg,
                        reference_assets,
                        sample_time,
                        38.2,
                        1240.0,
                    )
                    actual = current_compose(
                        frame,
                        current_cfg,
                        current_assets,
                        sample_time,
                        38.2,
                        1240.0,
                    )
                    self.assertTrue(
                        np.array_equal(actual, expected),
                        (
                            f"{theme} drifted from the supplied "
                            f"{template.upper()} template at t={sample_time}"
                        ),
                    )

    def test_image_enhancement_is_opt_in_by_default(self):
        cfg = Config(video=Path("v"), data=Path("d"), output=Path("o"))
        self.assertFalse(cfg.enhance_video)
        self.assertEqual(cfg.broadcast_theme, "launch")

    def test_broadcast_theme_validation(self):
        for theme in ("launch", "mission_control", "stellar_console"):
            values = {
                "video": Path("v"), "data": Path("d"), "output": Path("o"),
                "broadcast_theme": theme,
            }
            self.assertEqual(validate_config_values(values)["broadcast_theme"], theme)
        with self.assertRaisesRegex(RocketOverlayError, "Broadcast theme"):
            validate_config_values({
                "video": Path("v"), "data": Path("d"), "output": Path("o"),
                "broadcast_theme": "unknown",
            })

    def test_nominal_fps_probe_uses_ffmpeg_tbr_for_vfr_sources(self):
        probe_nominal_fps.cache_clear()
        probe_result = mock.Mock(
            stderr=(
                "Stream #0:0: Video: hevc, yuv420p10le, 1280x720, "
                "29.32 fps, 29.97 tbr, 600 tbn\n"
            )
        )
        with (
            mock.patch("rocket_overlay.ffmpeg_executable", return_value="ffmpeg"),
            mock.patch("rocket_overlay.subprocess.run", return_value=probe_result),
        ):
            self.assertEqual(probe_nominal_fps(Path("iphone.mov")), 29.97)
        probe_nominal_fps.cache_clear()

    def test_hlg_overlay_is_composited_in_linear_light(self):
        executable = ffmpeg_executable()
        if executable is None:
            self.skipTest("FFmpeg is unavailable")

        width, height = 16, 8
        base_y = 810
        alpha_u8 = 98
        # One neutral 10-bit HLG frame.  Neutral chroma makes the measured Y
        # values a direct luminance proxy after conversion to linear BT.2020.
        base = np.concatenate((
            np.full(width * height, base_y, dtype="<u2"),
            np.full(width * height // 4, 512, dtype="<u2"),
            np.full(width * height // 4, 512, dtype="<u2"),
        ))
        overlay = np.zeros((height, width, 4), dtype=np.uint8)
        overlay[:, :8, 3] = alpha_u8
        overlay[2:6, 2:6, :3] = 255
        overlay[2:6, 2:6, 3] = 255

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_path = root / "base.yuv"
            overlay_path = root / "overlay.bgra"
            base_path.write_bytes(base.tobytes())
            overlay_path.write_bytes(overlay.tobytes())
            rendered = subprocess.run(
                [
                    executable,
                    "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "rawvideo", "-pixel_format", "yuv420p10le",
                    "-video_size", f"{width}x{height}", "-framerate", "1",
                    "-i", str(base_path),
                    "-f", "rawvideo", "-pixel_format", "bgra",
                    "-video_size", f"{width}x{height}", "-framerate", "1",
                    "-i", str(overlay_path),
                    "-filter_complex", hlg_overlay_filter_graph(),
                    "-map", "[outv]", "-frames:v", "1",
                    "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout

        output = np.frombuffer(rendered, dtype="<u2")
        output_y = output[:width * height].reshape(height, width)
        # Fully transparent pixels survive the float round trip exactly, while
        # graphics white reaches nominal HLG peak instead of 203-nit Y=720.
        self.assertTrue(np.all(output_y[:, 8:] == base_y))
        self.assertGreaterEqual(int(output_y[2:6, 2:6].min()), 930)

        def to_linear_y(raw_frame: bytes) -> np.ndarray:
            converted = subprocess.run(
                [
                    executable,
                    "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-f", "rawvideo", "-pixel_format", "yuv420p10le",
                    "-video_size", f"{width}x{height}", "-framerate", "1",
                    "-i", "pipe:0",
                    "-vf",
                    "setparams=range=limited:color_primaries=bt2020:"
                    "color_trc=arib-std-b67:colorspace=bt2020nc,"
                    "zscale=pin=bt2020:tin=arib-std-b67:min=bt2020nc:"
                    "rin=limited:p=bt2020:t=linear:m=bt2020nc:r=full:"
                    "npl=1000,format=yuv444p16le",
                    "-frames:v", "1", "-f", "rawvideo",
                    "-pix_fmt", "yuv444p16le", "pipe:1",
                ],
                input=raw_frame,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout
            return np.frombuffer(converted, dtype="<u2")[:width * height].reshape(
                height, width
            )

        linear = to_linear_y(rendered)
        untouched = float(np.median(linear[:2, 8:]))
        shaded = float(np.median(linear[:2, :8]))
        expected_transmission = 1.0 - alpha_u8 / 255.0
        self.assertAlmostEqual(
            shaded / untouched,
            expected_transmission,
            delta=0.01,
        )

    def test_transparent_broadcast_layer_reconstructs_existing_composite(self):
        telemetry = pd.DataFrame({
            "time": [0.0, 1.0, 2.0],
            "pressure": [0.0, 10.0, 0.0],
            "thrust": [0.0, 0.0, 0.0],
        })
        telemetry.attrs["has_thrust"] = False
        base = np.full((180, 320, 3), (36, 92, 148), dtype=np.uint8)
        rendered = []
        for theme in ("launch", "mission_control", "stellar_console"):
            cfg = Config(
                video=Path("v"), data=Path("d"), output=Path("o"),
                width=320, height=180, title="TEST", subtitle="STATIC FIRE",
                broadcast_theme=theme,
            )
            assets = broadcast_overlay.build_assets(cfg, telemetry)
            expected = broadcast_overlay.compose_frame(
                base, cfg, assets, 1.0, 10.0, 0.0,
            )
            overlay = broadcast_overlay.compose_overlay_bgra(
                cfg, assets, 1.0, 10.0, 0.0,
            )
            alpha = overlay[:, :, 3:4].astype(np.float32) / 255.0
            reconstructed = np.clip(
                np.rint(
                    overlay[:, :, :3].astype(np.float32) * alpha
                    + base.astype(np.float32) * (1.0 - alpha)
                ),
                0,
                255,
            ).astype(np.uint8)
            error = np.abs(
                expected.astype(np.int16) - reconstructed.astype(np.int16)
            )
            self.assertLess(float(error.mean()), 0.5, theme)
            self.assertLess(
                float(np.mean(np.any(error > 2, axis=2))), 0.002, theme
            )
            rendered.append(expected)
        self.assertTrue(
            all(
                not np.array_equal(rendered[left], rendered[right])
                for left in range(len(rendered))
                for right in range(left + 1, len(rendered))
            )
        )

    def test_all_broadcast_themes_handle_zero_pressure_without_thrust(self):
        telemetry = pd.DataFrame({
            "time": [0.0, 1.0],
            "pressure": [0.0, 0.0],
            "thrust": [0.0, 0.0],
        })
        telemetry.attrs["has_thrust"] = False
        for theme in ("launch", "mission_control", "stellar_console"):
            cfg = Config(
                video=Path("v"), data=Path("d"), output=Path("o"),
                width=320, height=180, broadcast_theme=theme,
            )
            assets = broadcast_overlay.build_assets(cfg, telemetry)
            self.assertTrue(np.isfinite(assets.chart_pts).all(), theme)
            frame = broadcast_overlay.compose_frame(
                np.zeros((180, 320, 3), dtype=np.uint8),
                cfg, assets, 0.5, 0.0, 0.0,
            )
            self.assertEqual(frame.shape, (180, 320, 3), theme)

    def test_missing_thrust_is_never_formatted_as_a_measured_zero(self):
        telemetry = pd.DataFrame({
            "time": [0.0, 1.0],
            "pressure": [0.0, 5.0],
            "thrust": [0.0, 0.0],
        })
        telemetry.attrs["has_thrust"] = False
        cfg = Config(
            video=Path("v"), data=Path("d"), output=Path("o"),
            width=640, height=360, broadcast_theme="stellar_console",
        )
        assets = broadcast_overlay.build_assets(cfg, telemetry)
        self.assertEqual(
            broadcast_overlay.thrust_display(assets, 0.0, "N"),
            ("N/A", ""),
        )
        default_frame = broadcast_overlay.compose_frame(
            np.zeros((360, 640, 3), dtype=np.uint8),
            cfg, assets, 1.0, 5.0, 0.0,
        )
        custom_cfg = dataclasses.replace(
            cfg,
            test_site="CUSTOM TEST RANGE",
            run_number="RUN-042",
            motor_type="MOTOR-X",
            propellant="CUSTOM PROPELLANT",
        )
        custom_frame = broadcast_overlay.compose_frame(
            np.zeros((360, 640, 3), dtype=np.uint8),
            custom_cfg, assets, 1.0, 5.0, 0.0,
        )
        self.assertFalse(np.array_equal(default_frame, custom_frame))

    def test_default_scene_has_normalized_unique_elements(self):
        scene = validate_scene_config(default_scene_config())
        ids = [element["id"] for element in scene["elements"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("chart", ids)
        cfg = Config(video=Path("v"), data=Path("d"), output=Path("o"))
        self.assertEqual(cfg.intro_duration_s, 0.0)

    def test_scene_rejects_invalid_geometry_and_type(self):
        scene = default_scene_config()
        scene["elements"][0]["width"] = 1.2
        with self.assertRaisesRegex(RocketOverlayError, "between 0 and 1"):
            validate_scene_config(scene)
        scene = default_scene_config()
        scene["elements"][0]["type"] = "unknown"
        with self.assertRaisesRegex(RocketOverlayError, "Unsupported"):
            validate_scene_config(scene)

    def test_scene_accepts_font_size_and_supported_chart_types(self):
        for chart_type in ("line", "area", "step", "points"):
            scene = default_scene_config()
            scene["elements"][1]["fontSize"] = 1.75
            next(item for item in scene["elements"] if item["type"] == "chart")[
                "chartType"
            ] = chart_type
            self.assertEqual(
                validate_scene_config(scene)["elements"][1]["fontSize"], 1.75
            )

    def test_scene_normalizes_reversed_timeline_interval(self):
        scene = default_scene_config()
        scene["elements"][1]["startTime"] = 0.26
        scene["elements"][1]["endTime"] = 0.1324
        validated = validate_scene_config(scene)
        self.assertEqual(validated["elements"][1]["startTime"], 0.1324)
        self.assertEqual(validated["elements"][1]["endTime"], 0.26)

    def test_unknown_yaml_option_has_clear_error(self):
        values = {
            "video": Path("v.mp4"),
            "data": Path("d.csv"),
            "output": Path("o.mp4"),
            "surprise": True,
        }
        with self.assertRaisesRegex(RocketOverlayError, "Unknown config"):
            validate_config_values(values)

    def test_yaml_paths_are_relative_to_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "video: input/v.mp4\n"
                "data: input/d.csv\n"
                "output: output/o.mp4\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(**{
                "config": config,
                "video": None, "data": None, "output": None,
                "sheet": 0, "time_column": None, "pressure_column": None,
                "thrust_column": None, "ignition_video_s": 0.0,
                "telemetry_zero_s": None, "time_scale": 1.0,
                "title": "ROCKET MOTOR STATIC TEST",
                "subtitle": "Pressure & Derived Thrust",
                "pressure_unit": "bar", "thrust_unit": "N", "logo": None,
                "width": 1920, "height": 1080, "video_fraction": 0.62,
                "chart_window_before_s": 1.0, "chart_window_after_s": 1.0,
                "no_audio": False, "codec": "mp4v", "crf": 18,
            })
            cfg = config_from_args(args)
            self.assertEqual(cfg.video, root / "input/v.mp4")
            self.assertEqual(cfg.output, root / "output/o.mp4")

    def test_multicamera_layouts(self):
        first = np.full((100, 160, 3), (10, 20, 30), dtype=np.uint8)
        second = np.full((100, 160, 3), (40, 50, 60), dtype=np.uint8)
        third = np.full((100, 160, 3), (70, 80, 90), dtype=np.uint8)
        before = camera_layout_frame(
            [first, second, third], "switch", 0.5, 1.0, 2.0
        )
        after = camera_layout_frame(
            [first, second, third], "switch", 1.1, 1.0, 2.0
        )
        late = camera_layout_frame(
            [first, second, third], "switch", 3.1, 1.0, 2.0
        )
        self.assertTrue(np.array_equal(before, first))
        self.assertTrue(np.array_equal(after, second))
        self.assertTrue(np.array_equal(late, third))
        split = camera_layout_frame(
            [first, second], "split", 0.0, 0.0, 2.0
        )
        self.assertEqual(split.shape, first.shape)
        self.assertLess(float(split[:, :80].mean()), float(split[:, 80:].mean()))

    def test_camera_focus_changes_visible_crop(self):
        frame = np.zeros((100, 300, 3), dtype=np.uint8)
        frame[:, :100] = (10, 20, 30)
        frame[:, 200:] = (200, 210, 220)
        left = cover_resize_position(frame, 100, 100, 0.0, 0.5, 1.0)
        right = cover_resize_position(frame, 100, 100, 1.0, 0.5, 1.0)
        self.assertLess(float(left.mean()), float(right.mean()))

    def test_scene_text_is_clipped_to_element_bounds(self):
        telemetry = pd.DataFrame({
            "time": [0.0, 1.0], "pressure": [0.0, 1.0], "thrust": [0.0, 0.0],
        })
        telemetry.attrs["has_thrust"] = False
        scene = {
            "version": 1, "background": "#000000", "elements": [
                {"id": "video", "type": "video", "x": 0, "y": 0,
                 "width": 1, "height": 1, "visible": True, "z": 0},
                {"id": "title", "type": "title", "x": 0, "y": 0,
                 "width": .1, "height": .1, "visible": True, "z": 1,
                 "color": "#ffffff", "backgroundOpacity": 0, "fontSize": 2.5},
            ],
        }
        cfg = Config(
            video=Path("v"), data=Path("d"), output=Path("o"),
            width=960, height=540, title="A VERY LONG TITLE THAT MUST BE CLIPPED",
            enhance_video=False, scene_config=scene,
        )
        frame = compose_scene_frame(
            np.zeros((540, 960, 3), dtype=np.uint8),
            np.zeros((200, 300, 3), dtype=np.uint8),
            cfg, 0.0, 0.0, 0.0, 0.0, None,
            calculate_metrics(telemetry), False,
        )
        self.assertEqual(int(frame[:54, 96:].max()), 0)


class WebPreviewTests(unittest.TestCase):
    def test_index_exposes_all_broadcast_theme_choices(self):
        with app.test_client() as client:
            response = client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for theme in ("launch", "mission_control", "stellar_console"):
            self.assertIn(
                f'name="broadcast_theme" value="{theme}"', html
            )
            self.assertIn(f'data-theme-layout="{theme}"', html)
        self.assertIn('id="broadcastRasterOverlay"', html)
        for filename in (
            "rocket_overlay_broadcast.py",
            "rocket_overlay_broadcast (1).py",
            "rocket_overlay_broadcast-3).py",
        ):
            self.assertIn(filename, html)
        for field in (
            "organization_name", "test_site", "footer_tagline",
            "camera_label", "capture_fps",
        ):
            self.assertIn(f'name="{field}"', html)
        self.assertIn('id="telemetryDiagnostics"', html)
        self.assertIn(
            '<input type="checkbox" id="preserveSourceQuality">', html
        )
        self.assertIn("SDR Rec.709 عالي الجودة بدقة المصدر", html)

    def test_same_aspect_upscale_is_capped_to_source_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upload_dir = root / "uploads"
            output_dir = root / "outputs"
            upload_dir.mkdir()
            output_dir.mkdir()

            source = root / "source.mp4"
            writer = cv2.VideoWriter(
                str(source), cv2.VideoWriter_fourcc(*"mp4v"), 7.0, (426, 240)
            )
            self.assertTrue(writer.isOpened())
            writer.write(np.full((240, 426, 3), 96, dtype=np.uint8))
            writer.release()
            telemetry = root / "telemetry.csv"
            telemetry.write_text("time,pressure\n0,0\n1,1\n", encoding="utf-8")

            with (
                mock.patch("app.UPLOAD_DIR", upload_dir),
                mock.patch("app.OUTPUT_DIR", output_dir),
                mock.patch("app.threading.Thread") as thread_class,
                app.test_client() as client,
                source.open("rb") as video_stream,
                telemetry.open("rb") as telemetry_stream,
            ):
                response = client.post(
                    "/api/jobs",
                    data={
                        "video": (video_stream, "source.mp4"),
                        "data": (telemetry_stream, "telemetry.csv"),
                        "resolution": "3840x2160",
                        "thrust_column": "__none__",
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 202)
            payload = response.get_json()
            cfg = thread_class.call_args.kwargs["args"][1]
            self.assertEqual((cfg.width, cfg.height), (426, 240))
            self.assertEqual(payload["output_resolution"], "426x240")
            self.assertIn("تكبير", payload["notice"])

    def test_source_resolution_preserves_uploaded_video_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upload_dir = root / "uploads"
            output_dir = root / "outputs"
            upload_dir.mkdir()
            output_dir.mkdir()

            source = root / "source.mp4"
            source_width, source_height = 426, 240
            writer = cv2.VideoWriter(
                str(source),
                cv2.VideoWriter_fourcc(*"mp4v"),
                7.0,
                (source_width, source_height),
            )
            self.assertTrue(writer.isOpened())
            writer.write(
                np.full((source_height, source_width, 3), 96, dtype=np.uint8)
            )
            writer.release()

            telemetry = root / "telemetry.csv"
            telemetry.write_text(
                "time,pressure\n0,0\n1,1\n",
                encoding="utf-8",
            )

            with (
                mock.patch("app.UPLOAD_DIR", upload_dir),
                mock.patch("app.OUTPUT_DIR", output_dir),
                mock.patch("app.threading.Thread") as thread_class,
                app.test_client() as client,
                source.open("rb") as video_stream,
                telemetry.open("rb") as telemetry_stream,
            ):
                response = client.post(
                    "/api/jobs",
                    data={
                        "video": (video_stream, "source.mp4"),
                        "data": (telemetry_stream, "telemetry.csv"),
                        "resolution": "source",
                        "thrust_column": "__none__",
                        "broadcast_theme": "stellar_console",
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 202)
            cfg = thread_class.call_args.kwargs["args"][1]
            self.assertEqual((cfg.width, cfg.height), (source_width, source_height))
            self.assertFalse(cfg.enhance_video)
            self.assertFalse(cfg.preserve_source_quality)
            self.assertEqual(cfg.broadcast_theme, "stellar_console")

    def test_web_archive_hdr_checkbox_is_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upload_dir = root / "uploads"
            output_dir = root / "outputs"
            upload_dir.mkdir()
            output_dir.mkdir()

            source = root / "source.mp4"
            writer = cv2.VideoWriter(
                str(source), cv2.VideoWriter_fourcc(*"mp4v"), 7.0, (426, 240)
            )
            self.assertTrue(writer.isOpened())
            writer.write(np.full((240, 426, 3), 96, dtype=np.uint8))
            writer.release()
            telemetry = root / "telemetry.csv"
            telemetry.write_text("time,pressure\n0,0\n1,1\n", encoding="utf-8")

            with (
                mock.patch("app.UPLOAD_DIR", upload_dir),
                mock.patch("app.OUTPUT_DIR", output_dir),
                mock.patch("app.threading.Thread") as thread_class,
                app.test_client() as client,
                source.open("rb") as video_stream,
                telemetry.open("rb") as telemetry_stream,
            ):
                response = client.post(
                    "/api/jobs",
                    data={
                        "video": (video_stream, "source.mp4"),
                        "data": (telemetry_stream, "telemetry.csv"),
                        "resolution": "source",
                        "thrust_column": "__none__",
                        "preserve_source_quality": "true",
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 202)
            cfg = thread_class.call_args.kwargs["args"][1]
            self.assertTrue(cfg.preserve_source_quality)
            self.assertEqual((cfg.width, cfg.height), (426, 240))

    def test_chunked_upload_bypasses_single_request_size(self):
        video_bytes = b"v" * (5 * 1024 * 1024)
        data_bytes = b"time,pressure\n0,0\n1,1\n"
        with app.test_client() as client:
            tokens = {}
            for field, filename, content in (
                ("video", "large.mp4", video_bytes),
                ("data", "data.csv", data_bytes),
            ):
                response = client.post(
                    "/api/uploads/init",
                    json={"field": field, "filename": filename},
                )
                self.assertEqual(response.status_code, 200)
                token = response.get_json()["upload_id"]
                for offset in range(0, len(content), 2 * 1024 * 1024):
                    chunk_response = client.post(
                        f"/api/uploads/{token}",
                        data=content[offset:offset + 2 * 1024 * 1024],
                        content_type="application/octet-stream",
                    )
                    self.assertEqual(chunk_response.status_code, 200)
                tokens[field] = token
            capture = mock.Mock()
            capture.get.side_effect = lambda prop: {
                cv2.CAP_PROP_FRAME_WIDTH: 960,
                cv2.CAP_PROP_FRAME_HEIGHT: 540,
            }.get(prop, 0)
            with (
                mock.patch("app.render"),
                mock.patch("app.cv2.VideoCapture", return_value=capture),
            ):
                response = client.post("/api/jobs", data={
                    "video_token": tokens["video"],
                    "data_token": tokens["data"],
                    "resolution": "960x540",
                    "thrust_column": "__none__",
                })
            self.assertEqual(response.status_code, 202)
            reuse = client.post("/api/uploads/init", json={
                "field": "video", "filename": "large.mp4",
                "size": len(video_bytes),
            })
            self.assertEqual(reuse.status_code, 200)
            self.assertTrue(reuse.get_json()["reused"])

    def test_inspect_preview_spans_entire_large_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.csv"
            pd.DataFrame({
                "time": np.linspace(0, 8, 1400),
                "pressure": np.linspace(0, 70, 1400),
            }).to_csv(path, index=False)
            with app.test_client() as client, path.open("rb") as stream:
                response = client.post(
                    "/api/inspect",
                    data={"data": (stream, "telemetry.csv"), "sheet": "0"},
                    content_type="multipart/form-data",
                )
            self.assertEqual(response.status_code, 200)
            series = response.get_json()["series"]
            self.assertLessEqual(len(series), 700)
            self.assertEqual(float(series[0]["time"]), 0.0)
            self.assertEqual(float(series[-1]["time"]), 8.0)

    def test_inspect_reports_auto_zero_and_missing_thrust_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pressure_log.csv"
            times = np.arange(0.0, 90.0, 0.05)
            pressure = np.zeros_like(times)
            active = times >= 82.0
            pressure[active] = np.minimum((times[active] - 82.0) * 50.0, 65.0)
            pd.DataFrame({
                "Time(s)": times,
                "Pressure(bar)": pressure,
            }).to_csv(path, index=False)

            with app.test_client() as client, path.open("rb") as stream:
                response = client.post(
                    "/api/inspect",
                    data={"data": (stream, "pressure_log.csv"), "sheet": "0"},
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertGreater(payload["suggested_telemetry_zero_s"], 81.9)
            diagnostics = payload["telemetry_diagnostics"]
            self.assertEqual(diagnostics["status"], "ready")
            self.assertEqual(diagnostics["method"], "sustained_signal_onset")
            self.assertEqual(diagnostics["pressure_column"], "Pressure(bar)")
            self.assertIsNone(diagnostics["thrust_column"])
            self.assertFalse(diagnostics["has_thrust"])
            self.assertAlmostEqual(diagnostics["peak_pressure"], 65.0)
            self.assertIsNone(diagnostics["peak_thrust"])

    def test_raster_preview_reuses_and_invalidates_session_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_path = root / "telemetry.csv"
            pd.DataFrame({
                "time": [0.0, 0.5, 1.0, 1.5],
                "pressure": [0.0, 30.0, 60.0, 0.0],
            }).to_csv(telemetry_path, index=False)
            logo_path = root / "logo.png"
            self.assertTrue(cv2.imwrite(
                str(logo_path),
                np.full((12, 20, 4), (255, 180, 40, 255), dtype=np.uint8),
            ))

            with app.test_client() as client, telemetry_path.open("rb") as stream:
                inspected = client.post(
                    "/api/inspect",
                    data={"data": (stream, "telemetry.csv"), "sheet": "0"},
                    content_type="multipart/form-data",
                )
                self.assertEqual(inspected.status_code, 200)
                preview_id = inspected.get_json()["preview_id"]

                base_payload = {
                    "broadcast_theme": "launch",
                    "time": 0.5,
                    "time_column": "time",
                    "pressure_column": "pressure",
                    "thrust_column": "__none__",
                    "width": 320,
                    "height": 180,
                }
                original_normalize = normalize_preview_telemetry
                original_build_assets = broadcast_overlay.build_assets
                transparent_overlay = np.zeros((180, 320, 4), dtype=np.uint8)
                with (
                    mock.patch(
                        "app.normalize_preview_telemetry",
                        wraps=original_normalize,
                    ) as normalize_mock,
                    mock.patch(
                        "app.broadcast_overlay.build_assets",
                        wraps=original_build_assets,
                    ) as assets_mock,
                    mock.patch(
                        "app.broadcast_overlay.compose_overlay_bgra",
                        return_value=transparent_overlay,
                    ),
                ):
                    first = client.post(
                        f"/api/previews/{preview_id}/render", json=base_payload
                    )
                    second = client.post(
                        f"/api/previews/{preview_id}/render",
                        json={**base_payload, "time": 1.0},
                    )
                    self.assertEqual(first.headers["X-Preview-Telemetry-Cache"], "miss")
                    self.assertEqual(first.headers["X-Preview-Assets-Cache"], "miss")
                    self.assertEqual(second.headers["X-Preview-Telemetry-Cache"], "hit")
                    self.assertEqual(second.headers["X-Preview-Assets-Cache"], "hit")
                    self.assertEqual(normalize_mock.call_count, 1)
                    self.assertEqual(assets_mock.call_count, 1)

                    config_changed = client.post(
                        f"/api/previews/{preview_id}/render",
                        json={**base_payload, "accent": "#22D3EE"},
                    )
                    self.assertEqual(
                        config_changed.headers["X-Preview-Telemetry-Cache"], "hit"
                    )
                    self.assertEqual(
                        config_changed.headers["X-Preview-Assets-Cache"], "miss"
                    )

                    telemetry_changed = client.post(
                        f"/api/previews/{preview_id}/render",
                        json={**base_payload, "time_scale": 2.0},
                    )
                    self.assertEqual(
                        telemetry_changed.headers["X-Preview-Telemetry-Cache"], "miss"
                    )
                    self.assertEqual(
                        telemetry_changed.headers["X-Preview-Assets-Cache"], "miss"
                    )
                    self.assertEqual(normalize_mock.call_count, 2)
                    self.assertEqual(assets_mock.call_count, 3)

                    with logo_path.open("rb") as logo_stream:
                        uploaded = client.post(
                            f"/api/previews/{preview_id}/logo",
                            data={"logo": (logo_stream, "logo.png")},
                            content_type="multipart/form-data",
                        )
                    self.assertEqual(uploaded.status_code, 200)
                    after_upload = client.post(
                        f"/api/previews/{preview_id}/render", json=base_payload
                    )
                    upload_reuse = client.post(
                        f"/api/previews/{preview_id}/render",
                        json={**base_payload, "time": 1.0},
                    )
                    self.assertEqual(
                        after_upload.headers["X-Preview-Telemetry-Cache"], "hit"
                    )
                    self.assertEqual(
                        after_upload.headers["X-Preview-Assets-Cache"], "miss"
                    )
                    self.assertEqual(
                        upload_reuse.headers["X-Preview-Assets-Cache"], "hit"
                    )

                    deleted = client.delete(f"/api/previews/{preview_id}/logo")
                    self.assertEqual(deleted.status_code, 200)
                    after_delete = client.post(
                        f"/api/previews/{preview_id}/render", json=base_payload
                    )
                    self.assertEqual(
                        after_delete.headers["X-Preview-Telemetry-Cache"], "hit"
                    )
                    self.assertEqual(
                        after_delete.headers["X-Preview-Assets-Cache"], "miss"
                    )
                    self.assertEqual(normalize_mock.call_count, 2)
                    self.assertEqual(assets_mock.call_count, 5)

    def test_exact_raster_preview_uses_all_three_supplied_themes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.csv"
            pd.DataFrame({
                "time": [0.0, 0.4, 1.0, 1.8, 2.6],
                "pressure": [0.0, 38.0, 67.0, 29.0, 0.0],
            }).to_csv(path, index=False)
            with app.test_client() as client, path.open("rb") as stream:
                inspected = client.post(
                    "/api/inspect",
                    data={"data": (stream, "telemetry.csv"), "sheet": "0"},
                    content_type="multipart/form-data",
                )
                self.assertEqual(inspected.status_code, 200)
                preview_id = inspected.get_json()["preview_id"]
                for theme in ("launch", "mission_control", "stellar_console"):
                    with self.subTest(theme=theme):
                        rendered = client.post(
                            f"/api/previews/{preview_id}/render",
                            json={
                                "broadcast_theme": theme,
                                "time": 1.0,
                                "time_column": "time",
                                "pressure_column": "pressure",
                                "thrust_column": "__none__",
                                "width": 640,
                                "height": 360,
                            },
                        )
                        self.assertEqual(rendered.status_code, 200)
                        self.assertEqual(rendered.mimetype, "image/png")
                        self.assertEqual(
                            rendered.headers["X-Telemetry-Has-Thrust"], "false"
                        )
                        self.assertEqual(
                            rendered.headers["X-Telemetry-Thrust"], "N/A"
                        )
                        self.assertAlmostEqual(
                            float(rendered.headers["X-Telemetry-Pressure"]),
                            67.0,
                        )
                        self.assertAlmostEqual(
                            float(rendered.headers["X-Telemetry-Peak-Pressure"]),
                            67.0,
                        )
                        overlay = cv2.imdecode(
                            np.frombuffer(rendered.data, dtype=np.uint8),
                            cv2.IMREAD_UNCHANGED,
                        )
                        self.assertEqual(overlay.shape, (360, 640, 4))
                        self.assertEqual(int(overlay[:, :, 3].min()), 0)
                        self.assertGreater(int(overlay[:, :, 3].max()), 0)


class RenderSmokeTests(unittest.TestCase):
    def test_split_camera_render_creates_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "camera1.mp4", root / "camera2.mp4"]
            for source, color in zip(sources, ((20, 30, 180), (180, 30, 20))):
                writer = cv2.VideoWriter(
                    str(source), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (160, 90)
                )
                self.assertTrue(writer.isOpened())
                for _ in range(4):
                    writer.write(np.full((90, 160, 3), color, dtype=np.uint8))
                writer.release()
            data = root / "data.csv"
            pd.DataFrame({
                "time": [0.0, 0.4, 0.8],
                "pressure": [0.0, 5.0, 0.0],
            }).to_csv(data, index=False)
            output = root / "multi.mp4"
            render(Config(
                video=sources[0], video_2=sources[1],
                camera_mode="split", camera_2_ignition_s=0.0,
                data=data, output=output, thrust_column="__none__",
                width=960, height=540, intro_duration_s=0.0,
                outro_duration_s=0.0, keep_audio=False, thumbnail=False,
                scene_config=default_scene_config(),
            ))
            capture = cv2.VideoCapture(str(output))
            ok, frame = capture.read()
            capture.release()
            self.assertTrue(ok)
            self.assertEqual(frame.shape[:2], (540, 960))

    def test_pressure_only_render_creates_delivery_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            data = root / "data.csv"
            output = root / "result.mp4"
            writer = cv2.VideoWriter(
                str(source), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (320, 180)
            )
            self.assertTrue(writer.isOpened())
            for index in range(6):
                frame = np.full((180, 320, 3), (8, 12, 18), dtype=np.uint8)
                cv2.circle(frame, (130 + index * 5, 100), 12, (20, 140, 255), -1)
                writer.write(frame)
            writer.release()
            pd.DataFrame({
                "time": [0.0, 0.3, 0.6, 0.9],
                "pressure": [0.0, 8.0, 4.0, 0.0],
            }).to_csv(data, index=False)
            cfg = Config(
                video=source, data=data, output=output,
                thrust_column="__none__", width=960, height=540,
                broadcast_theme="stellar_console",
                intro_duration_s=0.0, outro_duration_s=0.0,
                keep_audio=False, thumbnail=True,
                scene_config=default_scene_config(),
            )
            render(cfg)
            self.assertTrue(output.exists())
            self.assertTrue(output.with_suffix(".json").exists())
            self.assertTrue(output.with_name("result_thumbnail.jpg").exists())


if __name__ == "__main__":
    unittest.main()
