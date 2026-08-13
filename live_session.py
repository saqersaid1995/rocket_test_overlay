"""Live Studio session state: camera ingestion, telemetry ingestion, the
mission readiness/countdown state machine, and the live compositor.

This module is deliberately separate from rocket_overlay.py's batch-export
``Config``: batch rendering derives everything from a scrub position into a
recorded CSV, while a live session derives everything from wall-clock time
plus operator-controlled hold state. They are different domains that happen
to share the same ROTPL rendering engine (rotpl_renderer.py), which this
module reuses untouched.
"""

from __future__ import annotations

import hmac
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal, Optional

import cv2
import numpy as np

from app_state import template_registry
from rotpl_registry import RotplError, RotplNotFoundError
from rotpl_renderer import (
    RotplPackage,
    RotplRenderer,
    TemplateContext,
    _format_mission_clock,
)

MissionType = Literal["static_fire", "launch"]

TELEMETRY_STALE_S = 5.0
CAMERA_TICK_S = 1.0 / 30.0
COMPOSITOR_FPS = 12.0
SESSION_IDLE_TTL_S = 6 * 60 * 60


# --------------------------------------------------------------------------
# Mission configuration
# --------------------------------------------------------------------------


@dataclass
class MissionConfig:
    mission_name: str
    mission_type: MissionType = "static_fire"
    run_number: str = ""
    site: str = ""
    team: str = ""
    t0_epoch: float = 0.0
    organization_name: str = "STELLAR KINETICS"
    accent: str = "#F59E0B"
    camera_label: str = "CAM 1"
    pressure_unit: str = "bar"
    thrust_unit: str = "N"
    max_wind_speed_mps: float = 12.0
    temp_min_c: float = -10.0
    temp_max_c: float = 45.0
    hold_points_s: list[float] = field(default_factory=lambda: [-120.0])


@dataclass
class StaticFireDetails:
    engine_type: str = ""
    oxidizer: str = ""
    fuel: str = ""
    ablative_material: str = ""
    expected_burn_duration_s: float = 10.0
    pressure_limit: Optional[float] = None


@dataclass
class LaunchDetails:
    vehicle_type: str = ""
    stage_count: int = 1
    target_altitude_km: float = 0.0
    target_orbit: str = ""
    evacuation_radius_m: float = 0.0
    launch_window_s: float = 1800.0
    ascent_duration_s: float = 480.0
    max_velocity_mps: float = 8000.0


def default_checklist(mission_type: MissionType) -> list[dict[str, Any]]:
    """Fresh (mutable) default checklist rows for a new session."""
    shared = [
        {"id": "cameras", "label": "Cameras connected", "required": True,
         "hold_point": False, "source": "manual"},
        {"id": "telemetry", "label": "Telemetry live", "required": True,
         "hold_point": False, "source": "manual"},
    ]
    if mission_type == "launch":
        rows = shared + [
            {"id": "range_safety", "label": "Range safety clear", "required": True,
             "hold_point": True, "source": "manual"},
            {"id": "flight_termination", "label": "Flight termination system armed",
             "required": True, "hold_point": False, "source": "manual"},
            {"id": "recovery_team", "label": "Recovery team ready", "required": True,
             "hold_point": False, "source": "manual"},
        ]
    else:
        rows = shared + [
            {"id": "safety_officer", "label": "Safety officer ready", "required": True,
             "hold_point": True, "source": "manual"},
            {"id": "propulsion_team", "label": "Propulsion team ready", "required": True,
             "hold_point": False, "source": "manual"},
        ]
    rows.append({"id": "broadcast", "label": "Broadcast ready", "required": True,
                 "hold_point": False, "source": "manual"})
    rows.append({"id": "weather_hold", "label": "Weather within limits", "required": True,
                 "hold_point": True, "source": "auto"})
    for row in rows:
        row["state"] = "pending"
        row["note"] = None
    return rows


# --------------------------------------------------------------------------
# Camera ingestion
# --------------------------------------------------------------------------


class CameraSource:
    """Continuously reads frames from an RTSP URL or a local video path.

    A local file is treated identically to a flaky RTSP camera: on read
    failure it retries with backoff, and end-of-stream loops back to the
    start rather than stopping, so a looped local test video exercises the
    same reconnect code path a real camera would.
    """

    def __init__(self, uri: str, loop: bool = True):
        self.uri = uri
        self.loop = loop
        self._lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_ts: float = 0.0
        self._connected = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def latest(self) -> tuple[Optional[np.ndarray], float, bool]:
        with self._lock:
            return self._latest_frame, self._latest_ts, self._connected

    def _set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            cap = cv2.VideoCapture(self.uri)
            if not cap.isOpened():
                self._set_connected(False)
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2.0, 10.0)
                continue
            backoff = 1.0
            self._set_connected(True)
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    if self.loop:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok, frame = cap.read()
                    if not ok:
                        break
                with self._lock:
                    self._latest_frame = frame
                    self._latest_ts = time.time()
                if self._stop_event.wait(CAMERA_TICK_S):
                    break
            cap.release()
            self._set_connected(False)
            if self._stop_event.is_set():
                break
            self._stop_event.wait(backoff)


# --------------------------------------------------------------------------
# Telemetry ingestion
# --------------------------------------------------------------------------


class TelemetryStore:
    def __init__(self, history_len: int = 600):
        self._lock = threading.Lock()
        self.latest: dict[str, float] = {}
        self.peaks: dict[str, float] = {}
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=history_len))
        self._latest_monotonic: float = 0.0

    def ingest(self, channels: dict[str, float], ts: Optional[float]) -> None:
        wall_ts = ts if ts is not None else time.time()
        with self._lock:
            for name, value in channels.items():
                self.latest[name] = value
                self.history[name].append((wall_ts, value))
                self.peaks[name] = max(self.peaks.get(name, value), value)
            self._latest_monotonic = time.monotonic()

    def _connected_locked(self, stale_s: float = TELEMETRY_STALE_S) -> bool:
        if not self.latest:
            return False
        return (time.monotonic() - self._latest_monotonic) < stale_s

    def connected(self, stale_s: float = TELEMETRY_STALE_S) -> bool:
        with self._lock:
            return self._connected_locked(stale_s)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "latest": dict(self.latest),
                "peaks": dict(self.peaks),
                "connected": self._connected_locked(),
            }


# --------------------------------------------------------------------------
# Mission readiness / countdown state machine
# --------------------------------------------------------------------------


class MissionStateMachine(threading.Thread):
    """1Hz ticker: countdown, weather-hold, hold-point gating."""

    def __init__(self, session: "LiveSession", tick_interval: float = 1.0):
        super().__init__(daemon=True)
        self.session = session
        self.tick_interval = tick_interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # defensive: never let the ticker die silently
                self.session.log(f"State machine error: {exc}")
            if self._stop_event.wait(self.tick_interval):
                break

    def _tick(self) -> None:
        session = self.session
        self._update_weather_item()
        mission_t = session.mission_t()
        holding = False
        if mission_t < 0:
            holding = self._should_hold(mission_t)
            if holding:
                session.held_seconds += self.tick_interval
        was_holding = session.currently_holding
        session.currently_holding = holding
        if holding and not was_holding:
            session.log(f"Hold engaged: {_hold_reason(session) or 'unspecified'}")
        elif was_holding and not holding:
            session.log("Hold released, countdown resumed")
        if mission_t >= 0 and not session.liftoff_logged:
            session.liftoff_logged = True
            session.log("T-0 reached")

    def _update_weather_item(self) -> None:
        session = self.session
        item = session.checklist_item("weather_hold")
        if item is None:
            return
        wind = session.telemetry.latest.get("wind_speed")
        temp = session.telemetry.latest.get("temp_c")
        if wind is None and temp is None:
            return
        problems = []
        if wind is not None and wind > session.mission.max_wind_speed_mps:
            problems.append(
                f"wind {wind:.1f} m/s exceeds limit {session.mission.max_wind_speed_mps:.1f} m/s"
            )
        if temp is not None and not (
            session.mission.temp_min_c <= temp <= session.mission.temp_max_c
        ):
            problems.append(
                f"temperature {temp:.1f}°C outside "
                f"{session.mission.temp_min_c:.0f}–{session.mission.temp_max_c:.0f}°C"
            )
        new_state = "no_go" if problems else "go"
        if item["state"] != new_state:
            item["state"] = new_state
            item["note"] = "; ".join(problems) if problems else None
            session.log(
                "Weather hold engaged: " + item["note"]
                if problems
                else "Weather hold cleared"
            )

    def _should_hold(self, mission_t: float) -> bool:
        session = self.session
        if session.manual_hold:
            return True
        weather_item = session.checklist_item("weather_hold")
        if weather_item is not None and weather_item["state"] == "no_go":
            return True
        for hold_point in session.mission.hold_points_s:
            if hold_point - self.tick_interval <= mission_t <= hold_point:
                if not session.checklist_all_required_go():
                    return True
        return False


def _hold_reason(session: "LiveSession") -> Optional[str]:
    reasons: list[str] = []
    if session.manual_hold:
        reasons.append("manual hold")
    for item in session.checklist:
        if item["state"] != "go" and item.get("required"):
            if item["id"] == "weather_hold":
                reasons.append(item.get("note") or "weather")
            else:
                reasons.append(item["label"])
    return "; ".join(reasons) if reasons else None


# --------------------------------------------------------------------------
# Template context builders
# --------------------------------------------------------------------------


def _apply_hold_and_abort(session: "LiveSession", context: TemplateContext, mission_t: float) -> None:
    holding = bool(session.currently_holding and mission_t < 0)
    context.values["mission.hold_active"] = holding
    context.values["mission.hold_reason"] = _hold_reason(session) if holding else None
    if holding:
        context.values["status.code"] = "hold"
        context.values["status.label"] = "HOLD"
        context.values["status.color"] = "#F59E0B"
    if session.aborted:
        context.values["status.code"] = "abort"
        context.values["status.label"] = "ABORT"
        context.values["status.color"] = "#FF4747"


def build_static_fire_template_context(session: "LiveSession") -> TemplateContext:
    mission = session.mission
    details = session.details
    mission_t = session.mission_t()
    pressure = session.telemetry.latest.get("pressure", 0.0)
    thrust = session.telemetry.latest.get("thrust")
    has_thrust = thrust is not None

    cfg = SimpleNamespace(
        title=mission.mission_name,
        subtitle="STATIC FIRE TEST",
        organization_name=mission.organization_name,
        footer_tagline="",
        accent=mission.accent,
        run_number=mission.run_number,
        motor_type=details.engine_type,
        test_date=time.strftime("%Y-%m-%d", time.localtime(mission.t0_epoch)),
        site=mission.site,
        coordinates_text="",
        oxidizer=details.oxidizer,
        fuel=details.fuel,
        ablative_material=details.ablative_material,
        camera_label=mission.camera_label,
        capture_fps="",
        pressure_unit=mission.pressure_unit,
        thrust_unit=mission.thrust_unit,
        pressure_limit=details.pressure_limit,
        logo=None,
    )
    assets = SimpleNamespace(
        p_peak=session.telemetry.peaks.get("pressure", max(pressure, 1.0)),
        f_peak=session.telemetry.peaks.get("thrust") if has_thrust else None,
        t_end=details.expected_burn_duration_s,
        has_thrust=has_thrust,
        logo=None,
    )
    context = TemplateContext.from_runtime(cfg, assets, mission_t, pressure, thrust)
    _apply_hold_and_abort(session, context, mission_t)
    return context


def build_launch_template_context(session: "LiveSession") -> TemplateContext:
    """Launch missions get their own phase set (countdown/liftoff/ascent).

    No launch-flavored ROTPL template vocabulary exists yet, so this reuses
    the existing pressure/thrust binding slots to carry altitude/velocity
    (documented limitation, not an oversight) until a real launch template
    is designed.
    """
    mission = session.mission
    details = session.details
    mission_t = session.mission_t()
    altitude = session.telemetry.latest.get("altitude_m", 0.0)
    velocity = session.telemetry.latest.get("velocity_mps")
    has_velocity = velocity is not None
    altitude_peak = session.telemetry.peaks.get("altitude_m", max(altitude, 1.0))
    velocity_peak = session.telemetry.peaks.get("velocity_mps") if has_velocity else None
    target_altitude_m = max(details.target_altitude_km * 1000.0, 1.0)
    max_velocity = max(details.max_velocity_mps, 1.0)
    ascent_duration = max(details.ascent_duration_s, 5.0)

    phase_specs = [
        ("countdown", "COUNTDOWN", min(mission.hold_points_s, default=-120.0)),
        ("liftoff", "LIFTOFF", 0.0),
        ("ascent", "ASCENT", 5.0),
        ("mission_complete", "MISSION COMPLETE", ascent_duration),
    ]
    reached = [index for index, spec in enumerate(phase_specs) if mission_t >= spec[2]]
    active_index = reached[-1] if reached else 0
    phase_items = []
    for index, (phase_id, label, phase_t) in enumerate(phase_specs):
        is_reached = mission_t >= phase_t
        is_active = index == active_index
        phase_items.append({
            "id": phase_id,
            "label": label,
            "time_s": phase_t,
            "time_text": f"T-{abs(phase_t):g}s" if phase_t < 0 else f"T+{phase_t:.1f}s",
            "state": "active" if is_active else ("complete" if is_reached else "pending"),
            "reached": is_reached,
            "active": is_active,
            "available": True,
            "progress": 0.0,
        })

    holding = bool(session.currently_holding and mission_t < 0)
    if session.aborted:
        status_code, status_label, status_color = "abort", "ABORT", "#FF4747"
    elif holding:
        status_code, status_label, status_color = "hold", "HOLD", "#F59E0B"
    elif mission_t < 0:
        status_code, status_label, status_color = "countdown", "COUNTDOWN", "#94A3B8"
    elif mission_t < 5.0:
        status_code, status_label, status_color = "liftoff", "LIFTOFF", "#4ADE80"
    elif mission_t < ascent_duration:
        status_code, status_label, status_color = "ascent", "ASCENT", mission.accent
    else:
        status_code, status_label, status_color = "mission_complete", "MISSION COMPLETE", "#FACC15"

    values: dict[str, Any] = {
        "brand.logo": None,
        "brand.organization_name": mission.organization_name,
        "brand.footer_tagline": "",
        "brand.accent_color": mission.accent,
        "test.title": mission.mission_name,
        "test.subtitle": details.vehicle_type,
        "test.run_number": mission.run_number,
        "test.motor_type": details.vehicle_type,
        "test.date": time.strftime("%Y-%m-%d", time.localtime(mission.t0_epoch)),
        "test.site": mission.site,
        "test.coordinates_text": "",
        "test.oxidizer": "",
        "test.fuel": "",
        "test.ablative_material": "",
        "camera.label": mission.camera_label,
        "camera.capture_fps_label": "",
        "units.pressure": "m",
        "units.thrust": "m/s",
        "frame.mission_time_s": mission_t,
        "frame.mission_clock": _format_mission_clock(mission_t),
        "status.code": status_code,
        "status.label": status_label,
        "status.color": status_color,
        "status.pulse": min(1.0, max(0.0, 0.55 + 0.45 * math.sin(mission_t * 4.2))),
        "telemetry.pressure.available": True,
        "telemetry.pressure.value": altitude,
        "telemetry.pressure.formatted": f"{altitude:.0f}",
        "telemetry.pressure.unit": "m",
        "telemetry.pressure.normalized": min(1.0, max(0.0, altitude / target_altitude_m)),
        "telemetry.pressure.scale_max": target_altitude_m,
        "telemetry.thrust.available": has_velocity,
        "telemetry.thrust.value": velocity,
        "telemetry.thrust.formatted": f"{velocity:.0f}" if has_velocity else "N/A",
        "telemetry.thrust.unit": "m/s" if has_velocity else None,
        "telemetry.thrust.normalized": (
            min(1.0, max(0.0, velocity / max_velocity)) if has_velocity else None
        ),
        "telemetry.thrust.scale_max": max_velocity if has_velocity else None,
        "metrics.pressure.peak": altitude_peak,
        "metrics.pressure.peak_time_s": None,
        "metrics.thrust.peak": velocity_peak,
        "metrics.thrust.peak_time_s": None,
        "metrics.thrust.total_impulse": None,
        "metrics.burn.duration_s": ascent_duration,
        "phases.active_id": phase_specs[active_index][0],
        "phases.active_index": active_index,
        "phases.progress": 0.0,
        "phases.items": phase_items,
        "mission.hold_active": holding,
        "mission.hold_reason": _hold_reason(session) if holding else None,
    }
    return TemplateContext(values)


# --------------------------------------------------------------------------
# Live compositor
# --------------------------------------------------------------------------


def _placeholder_frame(text: str, size: tuple[int, int] = (1280, 720)) -> np.ndarray:
    width, height = size
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = (18, 18, 22)
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 2.0
    thickness = 3
    (text_w, text_h), _ = cv2.getTextSize(text, font, scale, thickness)
    x = max(0, (width - text_w) // 2)
    y = (height + text_h) // 2
    cv2.putText(frame, text, (x, y), font, scale, (150, 150, 160), thickness, cv2.LINE_AA)
    return frame


class CompositorWorker(threading.Thread):
    """Renders the active Program camera through the active ROTPL template
    once per tick and publishes a single shared JPEG buffer — every viewer
    and the control room's Program panel tail this same buffer instead of
    triggering their own render.
    """

    def __init__(self, session: "LiveSession", fps: float = COMPOSITOR_FPS):
        super().__init__(daemon=True)
        self.session = session
        self.interval = 1.0 / fps
        self._stop_event = threading.Event()
        self.condition = threading.Condition()
        self.latest_jpeg: Optional[bytes] = None
        self.generation = 0

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def stop(self) -> None:
        self._stop_event.set()
        with self.condition:
            self.condition.notify_all()

    def run(self) -> None:
        while not self._stop_event.is_set():
            start = time.time()
            try:
                self._render_once()
            except Exception as exc:  # defensive: never let the compositor die silently
                self.session.log(f"Compositor error: {exc}")
            elapsed = time.time() - start
            if self._stop_event.wait(max(0.0, self.interval - elapsed)):
                break

    def _render_once(self) -> None:
        session = self.session
        frame = None
        cam = None
        if session.program_camera_index is not None:
            cam = session.cameras.get(session.program_camera_index)
        if cam is not None:
            frame, _ts, _connected = cam.latest()
        if frame is None:
            frame = _placeholder_frame("NO SIGNAL")

        if not session.broadcast_live:
            composited = _placeholder_frame("STANDBY")
        else:
            renderer = session.resolve_active_template()
            if renderer is None:
                composited = frame
            else:
                context = session.build_template_context()
                composited = renderer.compose_bgr(frame, context)

        ok, encoded = cv2.imencode(".jpg", composited, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            return
        with self.condition:
            self.latest_jpeg = encoded.tobytes()
            self.generation += 1
            self.condition.notify_all()


# --------------------------------------------------------------------------
# LiveSession + registry
# --------------------------------------------------------------------------


class LiveSession:
    def __init__(self, session_id: str, mission: MissionConfig, details: Any):
        self.id = session_id
        self.mission = mission
        self.details = details
        self.created_monotonic = time.monotonic()
        self.last_active_monotonic = time.monotonic()

        self.cameras: dict[int, CameraSource] = {}
        self.program_camera_index: Optional[int] = None
        self.telemetry = TelemetryStore()

        self.checklist = default_checklist(mission.mission_type)
        self.manual_hold = False
        self.currently_holding = False
        self.held_seconds = 0.0
        self.aborted = False
        self.liftoff_logged = False
        self.broadcast_live = False

        self.event_log: deque = deque(maxlen=200)
        self.template_selection: Optional[tuple[str, str, str]] = None
        self.template_cache: dict[tuple[str, str, str], RotplRenderer] = {}

        self._lock = threading.RLock()
        self._armed = False
        self._ended = False
        self.state_machine: Optional[MissionStateMachine] = None
        self.compositor: Optional[CompositorWorker] = None

        self.log("Session created")

    # -- bookkeeping ------------------------------------------------------

    def touch(self) -> None:
        self.last_active_monotonic = time.monotonic()

    def log(self, message: str) -> None:
        self.event_log.append({"ts": time.time(), "message": message})

    # -- checklist ----------------------------------------------------------

    def checklist_item(self, item_id: str) -> Optional[dict[str, Any]]:
        for item in self.checklist:
            if item["id"] == item_id:
                return item
        return None

    def checklist_all_required_go(self) -> bool:
        return all(item["state"] == "go" for item in self.checklist if item.get("required"))

    def set_checklist_state(self, item_id: str, state: str) -> dict[str, Any]:
        item = self.checklist_item(item_id)
        if item is None:
            raise KeyError(f"Unknown checklist item: {item_id}")
        if item.get("source") == "auto":
            raise PermissionError(f"{item_id} is set automatically and cannot be toggled manually.")
        if state not in ("pending", "go", "no_go"):
            raise ValueError("state must be pending, go, or no_go")
        item["state"] = state
        self.log(f"Checklist '{item['label']}' set to {state}")
        return item

    # -- countdown ----------------------------------------------------------

    def mission_t(self) -> float:
        return time.time() - self.mission.t0_epoch - self.held_seconds

    def hold(self) -> None:
        self.manual_hold = True
        self.log("Manual hold engaged")

    def resume(self) -> None:
        self.manual_hold = False
        self.log("Manual hold released")

    def abort(self) -> None:
        self.aborted = True
        self.log("MISSION ABORTED")

    # -- cameras ----------------------------------------------------------

    def attach_camera(self, index: int, uri: str) -> None:
        with self._lock:
            existing = self.cameras.get(index)
            if existing is not None:
                existing.stop()
            source = CameraSource(uri)
            self.cameras[index] = source
            if self.program_camera_index is None:
                self.program_camera_index = index
        source.start()
        self.log(f"Camera {index} attached ({uri})")

    def set_program_camera(self, index: int) -> None:
        if index not in self.cameras:
            raise KeyError(f"Camera {index} is not attached")
        self.program_camera_index = index
        self.log(f"Program camera set to {index}")

    # -- telemetry ----------------------------------------------------------

    def ingest_telemetry(self, channels: dict[str, float], ts: Optional[float]) -> None:
        self.telemetry.ingest(channels, ts)

    # -- template selection --------------------------------------------------

    def select_template(self, template_id: str, version: str, sha256: str) -> None:
        with self._lock:
            self.template_selection = (template_id, version, sha256)
        self.log(f"Active template set to {template_id} {version}")

    def resolve_active_template(self) -> Optional[RotplRenderer]:
        with self._lock:
            selection = self.template_selection
        try:
            if selection:
                template_id, version, sha256 = selection
                resolved = template_registry.resolve(template_id, version)
                if sha256 and not hmac.compare_digest(resolved.sha256.lower(), sha256.lower()):
                    return None
            else:
                resolved = template_registry.resolve()
        except (RotplError, RotplNotFoundError):
            return None
        cache_key = (resolved.id, resolved.version, resolved.sha256)
        with self._lock:
            renderer = self.template_cache.get(cache_key)
            if renderer is None:
                try:
                    package = RotplPackage.open(resolved.files_path)
                    renderer = RotplRenderer(package)
                except Exception:
                    return None
                self.template_cache[cache_key] = renderer
            return renderer

    # -- template context ---------------------------------------------------

    def build_template_context(self) -> TemplateContext:
        if self.mission.mission_type == "launch":
            return build_launch_template_context(self)
        return build_static_fire_template_context(self)

    # -- lifecycle ----------------------------------------------------------

    def arm(self) -> None:
        with self._lock:
            if self._armed or self._ended:
                return
            self._armed = True
        self.state_machine = MissionStateMachine(self)
        self.state_machine.start()
        self.compositor = CompositorWorker(self)
        self.compositor.start()
        self.log("Session armed: state machine and compositor running")

    def set_broadcast_live(self, live: bool) -> None:
        self.broadcast_live = live
        self.log("Broadcast started" if live else "Broadcast stopped")

    def end(self) -> None:
        with self._lock:
            if self._ended:
                return
            self._ended = True
        for camera in self.cameras.values():
            camera.stop()
        if self.state_machine is not None:
            self.state_machine.stop()
        if self.compositor is not None:
            self.compositor.stop()
        for renderer in self.template_cache.values():
            try:
                renderer.package.close()
            except Exception:
                pass
        self.log("Session ended")

    def state_snapshot(self) -> dict[str, Any]:
        mission_t = self.mission_t()
        telemetry_snapshot = self.telemetry.snapshot()
        cameras = {
            str(index): {
                "uri": source.uri,
                "connected": source.latest()[2],
                "program": index == self.program_camera_index,
            }
            for index, source in self.cameras.items()
        }
        return {
            "id": self.id,
            "mission_type": self.mission.mission_type,
            "mission_name": self.mission.mission_name,
            "t0_epoch": self.mission.t0_epoch,
            "mission_time_s": mission_t,
            "mission_clock": _format_mission_clock(mission_t),
            "armed": self._armed,
            "ended": self._ended,
            "manual_hold": self.manual_hold,
            "currently_holding": self.currently_holding,
            "aborted": self.aborted,
            "broadcast_live": self.broadcast_live,
            "checklist": [dict(item) for item in self.checklist],
            "checklist_ready": self.checklist_all_required_go(),
            "cameras": cameras,
            "program_camera_index": self.program_camera_index,
            "telemetry": telemetry_snapshot,
            "environment_limits": {
                "max_wind_speed_mps": self.mission.max_wind_speed_mps,
                "temp_min_c": self.mission.temp_min_c,
                "temp_max_c": self.mission.temp_max_c,
            },
            "active_template": (
                {
                    "template_id": self.template_selection[0],
                    "template_version": self.template_selection[1],
                    "sha256": self.template_selection[2],
                }
                if self.template_selection
                else None
            ),
            "event_log": list(self.event_log)[-50:],
        }


class LiveSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    def create(self, mission: MissionConfig, details: Any) -> LiveSession:
        session_id = uuid.uuid4().hex[:12]
        session = LiveSession(session_id, mission, details)
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Optional[LiveSession]:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            session.touch()
        return session

    def end(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.end()

    def sweep_idle(self, ttl_s: float = SESSION_IDLE_TTL_S) -> None:
        now = time.monotonic()
        with self._lock:
            stale_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if now - session.last_active_monotonic > ttl_s
            ]
            stale_sessions = [self._sessions.pop(session_id) for session_id in stale_ids]
        for session in stale_sessions:
            session.end()


live_sessions = LiveSessionRegistry()
