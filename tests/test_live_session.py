"""Unit tests for live_session.py: CameraSource against a synthetic local
video, and MissionStateMachine's countdown/weather-hold/checklist gating.

No Flask app or real hardware involved — CameraSource opens a local test
video through the exact same cv2.VideoCapture code path a real RTSP camera
would use, and the state machine is driven with a very short tick interval
against real wall-clock time so tests stay fast without needing a fake clock.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from live_session import (
    CameraSource,
    LaunchDetails,
    MissionConfig,
    MissionStateMachine,
    StaticFireDetails,
    LiveSession,
    build_launch_template_context,
    build_static_fire_template_context,
    default_checklist,
)


def _write_test_video(path: Path, frames: int = 6, size: tuple[int, int] = (64, 48)) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, size)
    assert writer.isOpened()
    for index in range(frames):
        frame = np.full((size[1], size[0], 3), (10 + index, 20, 30), dtype=np.uint8)
        writer.write(frame)
    writer.release()


class CameraSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)

    def test_reads_frames_from_local_video_and_reports_connected(self):
        video_path = self.temp / "cam.mp4"
        _write_test_video(video_path)
        source = CameraSource(str(video_path))
        source.start()
        try:
            deadline = time.monotonic() + 5.0
            frame = None
            connected = False
            while time.monotonic() < deadline:
                frame, _ts, connected = source.latest()
                if frame is not None and connected:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(frame)
            self.assertTrue(connected)
            self.assertEqual(frame.shape[:2], (48, 64))
        finally:
            source.stop()

    def test_loops_past_end_of_file_instead_of_stopping(self):
        video_path = self.temp / "cam.mp4"
        _write_test_video(video_path, frames=2)
        source = CameraSource(str(video_path))
        source.start()
        try:
            # Let it play well past the 2-frame file's natural end; it must
            # still be reporting a connected, fresh frame instead of giving up.
            time.sleep(1.0)
            frame, ts, connected = source.latest()
            self.assertIsNotNone(frame)
            self.assertTrue(connected)
            self.assertGreater(ts, 0.0)
        finally:
            source.stop()

    def test_unopenable_uri_reports_disconnected_without_crashing(self):
        source = CameraSource(str(self.temp / "does-not-exist.mp4"), loop=False)
        source.start()
        try:
            time.sleep(0.3)
            frame, _ts, connected = source.latest()
            self.assertIsNone(frame)
            self.assertFalse(connected)
        finally:
            source.stop()


class ChecklistTests(unittest.TestCase):
    def test_static_fire_checklist_has_weather_hold_point(self):
        checklist = default_checklist("static_fire")
        weather = next(item for item in checklist if item["id"] == "weather_hold")
        self.assertTrue(weather["hold_point"])
        self.assertEqual(weather["source"], "auto")
        self.assertEqual(weather["state"], "pending")

    def test_launch_checklist_includes_range_safety_hold_point(self):
        checklist = default_checklist("launch")
        ids = {item["id"] for item in checklist}
        self.assertIn("range_safety", ids)
        self.assertIn("weather_hold", ids)


def _mark_all_go(session: LiveSession) -> None:
    for item in session.checklist:
        if item["source"] != "auto":
            session.set_checklist_state(item["id"], "go")


class MissionStateMachineTests(unittest.TestCase):
    def _session(self, mission_type="static_fire", **mission_overrides):
        mission = MissionConfig(
            mission_name="Unit Test Mission",
            mission_type=mission_type,
            t0_epoch=time.time() + mission_overrides.pop("t0_offset", 2.0),
            hold_points_s=mission_overrides.pop("hold_points_s", [-1000.0]),
            **mission_overrides,
        )
        details = LaunchDetails() if mission_type == "launch" else StaticFireDetails()
        return LiveSession("test", mission, details)

    def test_countdown_advances_toward_zero_without_holds(self):
        session = self._session(hold_points_s=[-1000.0])  # unreachable hold point
        _mark_all_go(session)
        machine = MissionStateMachine(session, tick_interval=0.05)
        machine.start()
        try:
            time.sleep(0.3)
            self.assertFalse(session.currently_holding)
            self.assertAlmostEqual(session.held_seconds, 0.0, delta=0.01)
        finally:
            machine.stop()
            machine.join(timeout=1.0)

    def test_manual_hold_freezes_mission_clock(self):
        session = self._session(t0_offset=1.0)
        session.hold()
        machine = MissionStateMachine(session, tick_interval=0.05)
        machine.start()
        try:
            time.sleep(0.3)
            t1 = session.mission_t()
            time.sleep(0.3)
            t2 = session.mission_t()
            self.assertTrue(session.currently_holding)
            # Frozen: elapsed wall time must not leak into mission_t while held.
            self.assertAlmostEqual(t1, t2, delta=0.1)
        finally:
            machine.stop()
            machine.join(timeout=1.0)

    def test_resume_lets_clock_advance_again(self):
        session = self._session(t0_offset=5.0)
        session.hold()
        machine = MissionStateMachine(session, tick_interval=0.05)
        machine.start()
        try:
            time.sleep(0.2)
            frozen_t = session.mission_t()
            session.resume()
            time.sleep(0.3)
            self.assertFalse(session.currently_holding)
            self.assertGreater(session.mission_t(), frozen_t)
        finally:
            machine.stop()
            machine.join(timeout=1.0)

    def test_hold_point_blocks_until_checklist_clear(self):
        # t0 is 0.3s out; hold point sits right at -0.2s so the ticker should
        # catch it mid-countdown and refuse to let mission_t cross it.
        session = self._session(t0_offset=0.3, hold_points_s=[-0.2])
        # A weather reading within limits is required too: with no reading at
        # all, weather_hold stays "pending" (unknown weather fails safe, same
        # as "no_go") rather than "go", by design.
        session.ingest_telemetry({"wind_speed": 3.0, "temp_c": 20.0}, None)
        machine = MissionStateMachine(session, tick_interval=0.05)
        machine.start()
        try:
            time.sleep(0.5)
            self.assertTrue(session.currently_holding)
            self.assertLess(session.mission_t(), 0.0)
            _mark_all_go(session)
            time.sleep(0.4)
            self.assertFalse(session.currently_holding)
        finally:
            machine.stop()
            machine.join(timeout=1.0)

    def test_weather_out_of_range_engages_auto_hold(self):
        session = self._session(t0_offset=1000.0, max_wind_speed_mps=10.0)
        _mark_all_go(session)
        session.ingest_telemetry({"wind_speed": 15.0}, None)
        machine = MissionStateMachine(session, tick_interval=0.05)
        machine.start()
        try:
            time.sleep(0.3)
            weather = session.checklist_item("weather_hold")
            self.assertEqual(weather["state"], "no_go")
            self.assertTrue(session.currently_holding)
            session.ingest_telemetry({"wind_speed": 4.0}, None)
            time.sleep(0.3)
            self.assertEqual(session.checklist_item("weather_hold")["state"], "go")
            self.assertFalse(session.currently_holding)
        finally:
            machine.stop()
            machine.join(timeout=1.0)


class TemplateContextTests(unittest.TestCase):
    def test_static_fire_context_reflects_live_telemetry(self):
        mission = MissionConfig(mission_name="Static Test", t0_epoch=time.time() - 2.0)
        details = StaticFireDetails(expected_burn_duration_s=8.0)
        session = LiveSession("s1", mission, details)
        session.ingest_telemetry({"pressure": 42.5, "thrust": 900.0}, None)
        context = build_static_fire_template_context(session)
        self.assertEqual(context.values["telemetry.pressure.value"], 42.5)
        self.assertEqual(context.values["telemetry.thrust.value"], 900.0)
        self.assertTrue(context.values["telemetry.thrust.available"])
        self.assertIn(context.values["status.code"], ("hot_fire", "pre_ignition", "test_complete"))

    def test_static_fire_context_shows_hold_when_holding(self):
        mission = MissionConfig(mission_name="Static Test", t0_epoch=time.time() + 100.0)
        details = StaticFireDetails()
        session = LiveSession("s2", mission, details)
        session.currently_holding = True
        session.manual_hold = True
        context = build_static_fire_template_context(session)
        self.assertEqual(context.values["status.code"], "hold")
        self.assertTrue(context.values["mission.hold_active"])
        self.assertIn("manual hold", context.values["mission.hold_reason"])

    def test_launch_context_uses_altitude_velocity_in_pressure_thrust_slots(self):
        mission = MissionConfig(
            mission_name="Launch Test", mission_type="launch", t0_epoch=time.time() - 10.0
        )
        details = LaunchDetails(target_altitude_km=100.0, max_velocity_mps=7800.0)
        session = LiveSession("s3", mission, details)
        session.ingest_telemetry({"altitude_m": 5000.0, "velocity_mps": 1200.0}, None)
        context = build_launch_template_context(session)
        self.assertEqual(context.values["telemetry.pressure.value"], 5000.0)
        self.assertEqual(context.values["telemetry.thrust.value"], 1200.0)
        self.assertEqual(len(context.values["phases.items"]), 4)
        self.assertEqual(context.values["phases.items"][0]["id"], "countdown")
        self.assertEqual(context.values["phases.items"][-1]["id"], "mission_complete")

    def test_aborted_session_forces_abort_status_in_both_context_builders(self):
        for mission_type, details in (
            ("static_fire", StaticFireDetails()),
            ("launch", LaunchDetails()),
        ):
            mission = MissionConfig(
                mission_name="Abort Test", mission_type=mission_type,
                t0_epoch=time.time() + 50.0,
            )
            session = LiveSession("s4", mission, details)
            session.abort()
            context = (
                build_launch_template_context(session)
                if mission_type == "launch"
                else build_static_fire_template_context(session)
            )
            self.assertEqual(context.values["status.code"], "abort")
            self.assertEqual(context.values["status.label"], "ABORT")


if __name__ == "__main__":
    unittest.main()
