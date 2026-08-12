
#!/usr/bin/env python3
"""
Rocket Test Video Overlay
Combines a motor-test video with synchronized pressure/thrust telemetry.

Features
--------
- Reads Excel (.xlsx/.xls) or CSV telemetry.
- Supports explicit or automatic column selection.
- Synchronizes telemetry with video using an ignition offset.
- Professional 1080p split-screen layout.
- Static high-quality chart + real-time cursor, markers and numeric readouts.
- Peak pressure / peak thrust summary.
- Keeps original audio when FFmpeg is available.
- Configurable from CLI or YAML.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "rocket_overlay_matplotlib")
)

import cv2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from PIL import Image, ImageDraw, ImageFont
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib import font_manager

import rocket_overlay_broadcast as broadcast_overlay
from rotpl_renderer import (
    RotplError,
    RotplPackage,
    RotplRenderer,
    TemplateContext,
)


# ----------------------------- Configuration ----------------------------- #

@dataclass
class Config:
    video: Path
    data: Path
    output: Path
    video_2: Optional[Path] = None
    video_3: Optional[Path] = None
    camera_2_ignition_s: float = 0.0
    camera_3_ignition_s: float = 0.0
    camera_mode: str = "single"
    camera_3_switch_delay_s: float = 2.0
    camera_2_crop_zoom: float = 1.0
    camera_2_crop_x: float = 0.5
    camera_2_crop_y: float = 0.5
    camera_3_crop_zoom: float = 1.0
    camera_3_crop_x: float = 0.5
    camera_3_crop_y: float = 0.5

    sheet: str | int = 0
    time_column: Optional[str] = None
    pressure_column: Optional[str] = None
    thrust_column: Optional[str] = None

    ignition_video_s: float = 0.0
    telemetry_zero_s: Optional[float] = None
    time_scale: float = 1.0

    title: str = "ROCKET MOTOR STATIC TEST"
    subtitle: str = "Pressure & Derived Thrust"
    pressure_unit: str = "bar"
    thrust_unit: str = "N"

    width: int = 1920
    height: int = 1080
    video_fraction: float = 0.70
    chart_fraction: float = 0.38

    chart_window_before_s: float = 1.0
    chart_window_after_s: float = 1.0

    logo: Optional[Path] = None
    test_date: str = ""
    motor_type: str = ""
    propellant: str = ""
    run_number: str = ""
    organization_name: str = "STELLAR KINETICS"
    test_site: str = "ETLAQ SPACEPORT - DUQM, OMAN"
    coordinates_text: str = ""
    oxidizer: str = ""
    fuel: str = ""
    ablative_material: str = ""
    footer_tagline: str = "STELLAR KINETICS - ENGINEERING THE VOID"
    camera_label: str = "CAM 01"
    capture_fps: str = "120 FPS"
    output_style: str = "engineering"
    broadcast_theme: str = "launch"
    accent: str = "#38BDF8"
    show_chart: bool = True
    show_phases: bool = True
    intro_duration_s: float = 0.0
    outro_duration_s: float = 2.5
    reveal_chart: bool = True
    enhance_video: bool = False
    denoise_video: bool = False
    stabilize_video: bool = False
    crop_zoom: float = 1.0
    crop_x: float = 0.5
    crop_y: float = 0.5
    pressure_limit: Optional[float] = None
    thumbnail: bool = True
    delivery_codec: str = "h264"
    keep_audio: bool = True
    # Normal delivery follows the browser preview: source-size SDR Rec.709.
    # Main10 HLG preservation is an explicit archival mode because it produces
    # very large files and is meaningful only for compatible HDR sources.
    preserve_source_quality: bool = False
    codec: str = "mp4v"
    crf: int = 15
    scene_config: Optional[dict[str, Any]] = None
    # Ordered kept [start_s, end_s) ranges in the primary camera's own clock.
    # None/empty means no cuts (identical to today's behavior). Applying a cut
    # removes that time range from every camera, the audio track, and the
    # telemetry lookup at once, so telemetry readings never lose their real
    # physical timestamp. SDR delivery only - see preserve_hlg_source gate.
    cut_list: Optional[list[tuple[float, float]]] = None
    # A registry-resolved declarative package.  The identity fields are kept
    # with each job so the selected immutable template can be audited later.
    template_id: Optional[str] = None
    template_version: Optional[str] = None
    template_sha256: Optional[str] = None
    template_path: Optional[Path] = None


class RocketOverlayError(RuntimeError):
    """A user-facing validation or rendering error."""


COLUMN_ALIASES = {
    "time": (
        "time", "time_s", "time (s)", "seconds", "sec", "t", "timestamp",
        "elapsed time", "elapsed_time", "sample time"
    ),
    "pressure": (
        "pressure", "pressure_bar", "pressure (bar)", "chamber pressure",
        "pc", "p", "bar"
    ),
    "thrust": (
        "thrust", "thrust_n", "thrust (n)", "force", "force_n", "force (n)",
        "derived thrust", "f"
    ),
}


# ------------------------------- Utilities ------------------------------- #

def fail(message: str) -> "None":
    raise RocketOverlayError(message)


def safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def parse_sheet(value: str) -> str | int:
    try:
        return int(value)
    except ValueError:
        return value


def load_yaml(path: Path) -> dict:
    if not path.exists():
        fail(f"Config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        fail(f"Could not read YAML config: {exc}")
    if not isinstance(data, dict):
        fail("YAML config must contain a mapping/object at the top level.")
    return data


def validate_config_values(values: dict) -> dict:
    allowed = {field.name for field in dataclasses.fields(Config)}
    unknown = sorted(set(values) - allowed)
    if unknown:
        fail("Unknown config option(s): " + ", ".join(unknown))

    numeric_positive = ("width", "height", "time_scale")
    for name in numeric_positive:
        try:
            if float(values.get(name, getattr(Config, name, 0))) <= 0:
                fail(f"{name.replace('_', ' ').title()} must be greater than zero.")
        except (TypeError, ValueError):
            fail(f"{name.replace('_', ' ').title()} must be a number.")

    fraction = float(values.get("video_fraction", 0.70))
    if not 0.45 <= fraction <= 0.80:
        fail("Video fraction must be between 0.45 and 0.80.")
    if int(values.get("width", 1920)) < 960 or int(values.get("height", 1080)) < 540:
        fail("Output resolution is too small. Use at least 960x540.")
    if len(str(values.get("codec", "mp4v"))) != 4:
        fail("Codec must contain exactly four characters.")
    if values.get("output_style", "engineering") not in {
        "engineering", "presentation", "archive", "social"
    }:
        fail("Output style must be engineering, presentation, archive, or social.")
    if values.get("broadcast_theme", "launch") not in {
        "launch", "mission_control", "stellar_console", "range_line"
    }:
        fail(
            "Broadcast theme must be launch, mission_control, or stellar_console."
        )
    if values.get("delivery_codec", "h264") not in {"h264", "h265"}:
        fail("Delivery codec must be h264 or h265.")
    try:
        crf = int(values.get("crf", 15))
    except (TypeError, ValueError):
        fail("CRF must be an integer between 0 and 51.")
    if not 0 <= crf <= 51:
        fail("CRF must be between 0 and 51.")
    zoom = float(values.get("crop_zoom", 1.0))
    if not 1.0 <= zoom <= 4.0:
        fail("Crop zoom must be between 1.0 and 4.0.")
    for name in ("crop_x", "crop_y"):
        value = float(values.get(name, 0.5))
        if not 0.0 <= value <= 1.0:
            fail(f"{name.replace('_', ' ').title()} must be between 0 and 1.")
    for name in ("intro_duration_s", "outro_duration_s"):
        if float(values.get(name, 0.0)) < 0:
            fail(f"{name.replace('_', ' ').title()} cannot be negative.")
    scene = values.get("scene_config")
    if scene is not None:
        validate_scene_config(scene)
    cut_list = values.get("cut_list")
    if cut_list:
        validate_cut_list(cut_list)
        if values.get("preserve_source_quality"):
            fail(
                "Cut editing is not available with source-quality (HDR) "
                "preservation; disable it or clear the cut list."
            )
    template_path = values.get("template_path")
    identity = {
        name: values.get(name)
        for name in ("template_id", "template_version", "template_sha256")
    }
    if template_path in (None, "") and any(identity.values()):
        fail("Template path is required when a ROTPL template identity is selected.")
    for name in ("template_id", "template_version"):
        value = identity[name]
        if value is not None and not str(value).strip():
            fail(f"{name.replace('_', ' ').title()} cannot be blank.")
    template_sha = identity["template_sha256"]
    if template_sha is not None and not re.fullmatch(
        r"[0-9a-fA-F]{64}", str(template_sha).strip()
    ):
        fail("Template SHA-256 must contain exactly 64 hexadecimal characters.")
    return values


def validate_cut_list(cut_list: object) -> list[tuple[float, float]]:
    """Validate a shared cut list: ascending, non-overlapping kept ranges.

    Only validates shape here (the video's real duration isn't known yet at
    this point) - render() clamps ranges against the actual duration once the
    source video is open.
    """
    if not isinstance(cut_list, (list, tuple)):
        fail("Cut list must be a list of [start, end] ranges.")
    if len(cut_list) > 200:
        fail("Cut list cannot contain more than 200 ranges.")
    ranges: list[tuple[float, float]] = []
    for entry in cut_list:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            fail("Each cut list entry must be a [start, end] pair.")
        try:
            start, end = float(entry[0]), float(entry[1])
        except (TypeError, ValueError):
            fail("Cut list start/end values must be numbers.")
        if start < 0 or end <= start:
            fail("Each cut list range must satisfy 0 <= start < end.")
        ranges.append((start, end))
    for previous, current in zip(ranges, ranges[1:]):
        if current[0] < previous[1]:
            fail("Cut list ranges must be sorted and non-overlapping.")
    return ranges


SCENE_ELEMENT_TYPES = {
    "video", "chart", "title", "timecode", "pressure_card",
    "thrust_card", "status", "identity", "logo",
}


def default_scene_config() -> dict[str, Any]:
    """Default full-video editor scene. Geometry is normalized to 0..1."""
    return {
        "version": 1,
        "designRevision": 8,
        "background": "#05080C",
        "elements": [
            {"id": "video", "type": "video", "x": 0, "y": 0, "width": 1,
             "height": 1, "opacity": 1, "visible": True, "locked": True, "z": 0},
            {"id": "title", "type": "title", "x": .04, "y": .05, "width": .46,
             "height": .09, "opacity": 1, "visible": True, "z": 20,
             "color": "#FFFFFF", "background": "#050B11", "backgroundOpacity": 0,
             "fontSize": 1.0},
            {"id": "timecode", "type": "timecode", "x": .745, "y": .055, "width": .115,
             "height": .062, "opacity": 1, "visible": True, "z": 21,
             "color": "#FFFFFF", "background": "#050B11", "backgroundOpacity": .58},
            {"id": "chart", "type": "chart", "x": .02, "y": .805, "width": .96,
             "height": .165, "opacity": 1, "visible": True, "z": 10,
             "background": "#040A10", "backgroundOpacity": .93,
             "pressureColor": "#5EDCFF", "thrustColor": "#FFCB66",
             "gridColor": "#718491", "cursorColor": "#FFFFFF",
             "fillOpacity": .10, "lineWidth": 4, "borderRadius": 5,
             "chartType": "area", "hudMode": True,
             "showAxes": True, "showGrid": True, "showTitle": True},
            {"id": "pressure", "type": "pressure_card", "x": .71, "y": .825,
             "width": .125, "height": .125, "opacity": 1, "visible": True, "z": 12,
             "color": "#5EDCFF", "background": "#040A10", "backgroundOpacity": 0},
            {"id": "thrust", "type": "thrust_card", "x": .845, "y": .825,
             "width": .115, "height": .125, "opacity": 1, "visible": True, "z": 12,
             "color": "#FFCB66", "background": "#040A10", "backgroundOpacity": 0},
            {"id": "status", "type": "status", "x": .875, "y": .055, "width": .105,
             "height": .062, "opacity": 1, "visible": True, "z": 22,
             "color": "#B9F3CC", "background": "#050B11", "backgroundOpacity": .58},
            {"id": "identity", "type": "identity", "x": .04, "y": .142, "width": .46,
             "height": .035, "opacity": 1, "visible": True, "z": 20,
             "color": "#EDF3F6", "background": "#050B11", "backgroundOpacity": 0},
            {"id": "logo", "type": "logo", "x": .91, "y": .14, "width": .07,
             "height": .055, "opacity": .95, "visible": True, "z": 30},
        ],
    }


def validate_scene_config(scene: object) -> dict[str, Any]:
    if not isinstance(scene, dict):
        fail("Scene config must be an object.")
    if scene.get("version", 1) != 1:
        fail("Unsupported scene config version.")
    elements = scene.get("elements")
    if not isinstance(elements, list) or not elements:
        fail("Scene config must contain elements.")
    seen: set[str] = set()
    for element in elements:
        if not isinstance(element, dict):
            fail("Every scene element must be an object.")
        element_id = str(element.get("id", "")).strip()
        kind = element.get("type")
        if not element_id or element_id in seen:
            fail("Scene element IDs must be present and unique.")
        if kind not in SCENE_ELEMENT_TYPES:
            fail(f"Unsupported scene element type: {kind}")
        seen.add(element_id)
        for name in ("x", "y", "width", "height"):
            try:
                value = float(element.get(name, 0))
            except (TypeError, ValueError):
                fail(f"Invalid {name} for scene element {element_id}.")
            if name in {"width", "height"} and value <= 0:
                fail(f"{name.title()} must be positive for scene element {element_id}.")
            if value < 0 or value > 1:
                fail(f"{name.title()} must be between 0 and 1 for scene element {element_id}.")
        opacity = float(element.get("opacity", 1))
        background_opacity = float(element.get("backgroundOpacity", 1))
        if not 0 <= opacity <= 1 or not 0 <= background_opacity <= 1:
            fail(f"Opacity must be between 0 and 1 for scene element {element_id}.")
        font_size = float(element.get("fontSize", 1))
        if not 0.5 <= font_size <= 2.5:
            fail(f"Font size must be between 0.5 and 2.5 for scene element {element_id}.")
        if kind == "chart" and element.get("chartType", "line") not in {
            "line", "area", "step", "points"
        }:
            fail(f"Unsupported chart type for scene element {element_id}.")
        for name in ("startTime", "endTime", "fadeIn", "fadeOut"):
            value = element.get(name)
            if value not in (None, "") and float(value) < 0:
                fail(f"{name} cannot be negative for scene element {element_id}.")
        start = element.get("startTime")
        end = element.get("endTime")
        if start not in (None, "") and end not in (None, "") and float(end) < float(start):
            # Editing numeric fields can temporarily place the end before the
            # start. Normalize the interval instead of failing after large
            # source videos have already uploaded.
            element["startTime"], element["endTime"] = float(end), float(start)
    return scene


def resolve_column(df: pd.DataFrame, explicit: Optional[str], kind: str) -> str:
    columns = list(df.columns)
    lower_map = {str(c).strip().lower(): str(c) for c in columns}

    if explicit:
        if explicit in columns:
            return explicit
        candidate = lower_map.get(explicit.strip().lower())
        if candidate:
            return candidate
        fail(
            f'Column "{explicit}" was not found. Available columns:\n'
            + ", ".join(map(str, columns))
        )

    for alias in COLUMN_ALIASES[kind]:
        if alias.lower() in lower_map:
            return lower_map[alias.lower()]

    # Conservative contains-match fallback.
    tokens = {
        "time": ("time", "sec"),
        "pressure": ("pressure", "bar", "chamber"),
        "thrust": ("thrust", "force"),
    }[kind]
    for col in columns:
        name = str(col).strip().lower()
        if any(token in name for token in tokens):
            return str(col)

    fail(
        f"Could not auto-detect the {kind} column. "
        f"Use --{kind}-column. Available columns:\n"
        + ", ".join(map(str, columns))
    )


def suggest_telemetry_zero_s(
    times: Sequence[float],
    pressure: Sequence[float],
    thrust: Optional[Sequence[float]] = None,
) -> tuple[float, dict[str, Any]]:
    """Locate the first sustained motor response in a raw telemetry trace.

    Recording systems often start tens of seconds before ignition.  Treating
    the first timestamp as T+0 then leaves every preview frame inside that
    idle lead-in.  This detector uses chamber pressure as the authoritative
    signal (and thrust only as a fallback), requires a sustained excursion to
    reject isolated noise, then walks back to the beginning of the rise.

    The returned timestamp is expressed in the source time units, before
    ``time_scale`` is applied.  Callers must always prefer an explicitly
    supplied ``telemetry_zero_s`` over this suggestion.
    """

    raw_times = np.asarray(times, dtype=float).reshape(-1)
    if raw_times.size == 0:
        fail("Telemetry contains no valid time values.")

    finite_time = np.isfinite(raw_times)
    if not finite_time.any():
        fail("Telemetry contains no finite time values.")

    signal_inputs: list[tuple[str, Sequence[float]]] = [
        ("pressure", pressure),
    ]
    if thrust is not None:
        signal_inputs.append(("thrust", thrust))

    # Build one common, stable ordering so diagnostics are deterministic even
    # when this helper is called directly with unsorted source rows.
    valid_indices = np.flatnonzero(finite_time)
    order = valid_indices[np.argsort(raw_times[valid_indices], kind="stable")]
    sorted_times = raw_times[order]
    first_time = float(sorted_times[0])
    last_time = float(sorted_times[-1])
    diagnostics: dict[str, Any] = {
        "method": "first_sample",
        "signal": None,
        "confidence": 0.0,
        "first_timestamp_s": first_time,
        "last_timestamp_s": last_time,
        "duration_s": max(0.0, last_time - first_time),
        "lead_in_s": 0.0,
        "candidates": [],
    }

    # Very short traces cannot distinguish a real rise from a single sample.
    if sorted_times.size < 8 or last_time <= first_time:
        diagnostics["reason"] = "insufficient_samples_for_onset_detection"
        return first_time, diagnostics

    candidates: list[dict[str, Any]] = []
    for signal_name, raw_signal in signal_inputs:
        values_all = np.asarray(raw_signal, dtype=float).reshape(-1)
        if values_all.size != raw_times.size:
            continue
        values = values_all[order]
        finite = np.isfinite(values)
        if finite.sum() < 8:
            continue
        if not finite.all():
            values = pd.Series(values).interpolate(
                limit_direction="both"
            ).to_numpy(dtype=float)

        # The leading one percent is the best available idle baseline for a
        # recorder that starts before ignition.  Capping it prevents a long
        # recording from letting later drift contaminate that baseline.
        head_count = max(3, min(200, int(math.ceil(values.size * 0.01))))
        baseline_slice = values[:head_count]
        baseline = float(np.median(baseline_slice))
        baseline_mad = float(
            np.median(np.abs(baseline_slice - np.median(baseline_slice)))
        )
        noise_sigma = 1.4826 * baseline_mad

        positive_excursion = float(np.max(values) - baseline)
        negative_excursion = float(baseline - np.min(values))
        polarity = 1.0 if positive_excursion >= negative_excursion else -1.0
        response = (values - baseline) * polarity
        amplitude = float(np.max(response))
        if not np.isfinite(amplitude) or amplitude <= max(1e-9, noise_sigma * 8):
            continue

        high_threshold = max(amplitude * 0.05, noise_sigma * 8, 1e-9)
        low_threshold = max(amplitude * 0.003, noise_sigma * 3, 1e-10)
        above_high = response >= high_threshold
        run_length = 3 if values.size >= 12 else 2
        high_index: int | None = None
        for index in range(0, values.size - run_length + 1):
            if bool(np.all(above_high[index:index + run_length])):
                high_index = index
                break
        if high_index is None:
            continue

        onset_index = high_index
        while onset_index > 0 and response[onset_index - 1] > low_threshold:
            onset_index -= 1

        onset_time = float(sorted_times[onset_index])
        lead_in = max(0.0, onset_time - first_time)
        signal_to_noise = amplitude / max(noise_sigma, amplitude * 1e-6)
        confidence = float(
            np.clip(0.55 + 0.10 * math.log10(max(signal_to_noise, 1.0)), 0, 0.99)
        )
        if onset_index == 0:
            confidence = min(confidence, 0.65)
        candidates.append({
            "signal": signal_name,
            "timestamp_s": onset_time,
            "lead_in_s": lead_in,
            "baseline": baseline,
            "peak": float(values[np.argmax(response)]),
            "polarity": "positive" if polarity > 0 else "negative",
            "high_threshold": high_threshold,
            "low_threshold": low_threshold,
            "noise_sigma": noise_sigma,
            "confidence": confidence,
        })

    diagnostics["candidates"] = candidates
    if not candidates:
        diagnostics["reason"] = "no_sustained_signal_excursion"
        return first_time, diagnostics

    # Chamber pressure is the physical source of truth for these templates.
    # A thrust channel is useful only when pressure cannot provide an onset.
    selected = next(
        (candidate for candidate in candidates if candidate["signal"] == "pressure"),
        candidates[0],
    )
    suggested = float(selected["timestamp_s"])
    diagnostics.update({
        "method": "sustained_signal_onset",
        "signal": selected["signal"],
        "confidence": selected["confidence"],
        "lead_in_s": selected["lead_in_s"],
        "reason": "detected_first_sustained_motor_response",
    })
    return suggested, diagnostics


def read_telemetry(cfg: Config) -> pd.DataFrame:
    suffix = cfg.data.suffix.lower()

    if suffix in (".xlsx", ".xls", ".xlsm"):
        try:
            df = pd.read_excel(cfg.data, sheet_name=cfg.sheet)
        except Exception as exc:
            fail(f"Could not read Excel file: {exc}")
    elif suffix in (".csv", ".txt"):
        try:
            df = pd.read_csv(cfg.data)
        except Exception as exc:
            fail(f"Could not read CSV file: {exc}")
    else:
        fail("Telemetry file must be .xlsx, .xls, .xlsm, .csv, or .txt")

    if df.empty:
        fail("Telemetry file contains no rows.")

    t_col = resolve_column(df, cfg.time_column, "time")
    p_col = resolve_column(df, cfg.pressure_column, "pressure")
    f_col = None
    if cfg.thrust_column != "__none__":
        try:
            f_col = resolve_column(df, cfg.thrust_column, "thrust")
        except RocketOverlayError:
            if cfg.thrust_column is not None:
                raise

    clean = pd.DataFrame({
        "time": pd.to_numeric(df[t_col], errors="coerce"),
        "pressure": pd.to_numeric(df[p_col], errors="coerce"),
        "thrust": (
            pd.to_numeric(df[f_col], errors="coerce")
            if f_col is not None
            else np.zeros(len(df), dtype=float)
        ),
    }).dropna(subset=["time"])

    clean = clean.replace([np.inf, -np.inf], np.nan)
    clean["pressure"] = clean["pressure"].interpolate(limit_direction="both")
    clean["thrust"] = clean["thrust"].interpolate(limit_direction="both")
    clean = clean.dropna().sort_values("time")

    # Remove duplicate timestamps by averaging values.
    clean = clean.groupby("time", as_index=False).mean(numeric_only=True)

    if len(clean) < 2:
        fail("Telemetry must contain at least two valid rows.")

    zero = cfg.telemetry_zero_s
    if zero is None:
        zero, zero_diagnostics = suggest_telemetry_zero_s(
            clean["time"].to_numpy(dtype=float),
            clean["pressure"].to_numpy(dtype=float),
            clean["thrust"].to_numpy(dtype=float) if f_col is not None else None,
        )
        zero_source = "automatic"
    else:
        zero = float(zero)
        zero_source = "explicit"
        zero_diagnostics = {
            "method": "explicit",
            "signal": None,
            "confidence": 1.0,
            "first_timestamp_s": float(clean["time"].iloc[0]),
            "last_timestamp_s": float(clean["time"].iloc[-1]),
            "duration_s": float(clean["time"].iloc[-1] - clean["time"].iloc[0]),
            "lead_in_s": float(zero - clean["time"].iloc[0]),
            "candidates": [],
            "reason": "user_supplied_telemetry_zero",
        }

    clean["time"] = (clean["time"] - zero) * cfg.time_scale

    if not np.all(np.diff(clean["time"].to_numpy()) > 0):
        fail("Telemetry time values must be strictly increasing after cleanup.")

    clean = clean.reset_index(drop=True)
    clean.attrs["has_thrust"] = f_col is not None
    clean.attrs["time_column"] = t_col
    clean.attrs["pressure_column"] = p_col
    clean.attrs["thrust_column"] = f_col
    clean.attrs["telemetry_zero_s"] = float(zero)
    clean.attrs["telemetry_zero_source"] = zero_source
    clean.attrs["telemetry_zero_diagnostics"] = zero_diagnostics
    return clean


@dataclass(frozen=True)
class TestMetrics:
    peak_pressure: float
    peak_pressure_time: float
    average_pressure: float
    burn_duration: float
    max_pressure_rise: float
    peak_thrust: Optional[float] = None
    peak_thrust_time: Optional[float] = None
    total_impulse: Optional[float] = None


def telemetry_has_thrust(telemetry: pd.DataFrame) -> bool:
    return bool(telemetry.attrs.get("has_thrust", True))


def calculate_metrics(telemetry: pd.DataFrame) -> TestMetrics:
    times = telemetry["time"].to_numpy(dtype=float)
    pressure = telemetry["pressure"].to_numpy(dtype=float)
    peak_idx = int(np.nanargmax(pressure))
    peak = float(pressure[peak_idx])
    threshold = max(peak * 0.05, 1e-9)
    active = np.flatnonzero(pressure >= threshold)
    burn_duration = (
        float(times[active[-1]] - times[active[0]]) if active.size > 1 else 0.0
    )
    rise = np.gradient(pressure, times)
    has_thrust = telemetry_has_thrust(telemetry)
    thrust = telemetry["thrust"].to_numpy(dtype=float)
    thrust_idx = int(np.nanargmax(thrust)) if has_thrust else 0
    return TestMetrics(
        peak_pressure=peak,
        peak_pressure_time=float(times[peak_idx]),
        average_pressure=float(np.nanmean(pressure)),
        burn_duration=burn_duration,
        max_pressure_rise=float(np.nanmax(rise)),
        peak_thrust=float(thrust[thrust_idx]) if has_thrust else None,
        peak_thrust_time=float(times[thrust_idx]) if has_thrust else None,
        total_impulse=float(np.trapezoid(np.maximum(thrust, 0), times))
        if has_thrust else None,
    )


def determine_test_phase(
    telemetry_time: float,
    metrics: TestMetrics,
    pressure: float,
    pressure_limit: Optional[float] = None,
) -> tuple[str, tuple[int, int, int]]:
    if pressure_limit is not None and pressure > pressure_limit:
        return "ABORT", (71, 71, 255)
    if telemetry_time < -0.12:
        return "STANDBY", (148, 163, 184)
    if telemetry_time < 0.18:
        return "IGNITION", (56, 189, 248)
    if telemetry_time <= metrics.burn_duration:
        return "TEST ACTIVE", (128, 222, 74)
    if telemetry_time <= metrics.burn_duration + 0.8:
        return "TAIL-OFF", (94, 234, 251)
    return "TEST COMPLETE", (184, 211, 148)


def finite_range(values: np.ndarray, padding_ratio: float = 0.08) -> tuple[float, float]:
    values = values[np.isfinite(values)]
    if values.size == 0:
        return 0.0, 1.0
    lo, hi = float(values.min()), float(values.max())
    if math.isclose(lo, hi):
        span = abs(lo) if lo else 1.0
        return lo - 0.5 * span, hi + 0.5 * span
    pad = (hi - lo) * padding_ratio
    return min(0.0, lo - pad), hi + pad


def cover_resize(frame: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    return cover_resize_position(frame, target_w, target_h, 0.5, 0.5, 1.0)


def cover_resize_position(
    frame: np.ndarray,
    target_w: int,
    target_h: int,
    focus_x: float,
    focus_y: float,
    zoom: float,
) -> np.ndarray:
    h, w = frame.shape[:2]
    scale = max(target_w / w, target_h / h) * max(1.0, float(zoom))
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
    x0 = int(round(np.clip(focus_x, 0, 1) * max(0, nw - target_w)))
    y0 = int(round(np.clip(focus_y, 0, 1) * max(0, nh - target_h)))
    return resized[y0:y0 + target_h, x0:x0 + target_w]


def prepare_video_frame(frame: np.ndarray, cfg: Config) -> np.ndarray:
    """Apply restrained crop and low-light enhancement without clipping the flame."""
    h, w = frame.shape[:2]
    zoom = max(1.0, cfg.crop_zoom)
    crop_w, crop_h = max(2, int(w / zoom)), max(2, int(h / zoom))
    cx = int(cfg.crop_x * w)
    cy = int(cfg.crop_y * h)
    x0 = int(np.clip(cx - crop_w // 2, 0, w - crop_w))
    y0 = int(np.clip(cy - crop_h // 2, 0, h - crop_h))
    result = frame[y0:y0 + crop_h, x0:x0 + crop_w]

    if cfg.denoise_video:
        result = cv2.fastNlMeansDenoisingColored(result, None, 3, 3, 7, 21)
    if cfg.enhance_video:
        lab = cv2.cvtColor(result, cv2.COLOR_BGR2LAB)
        light, a, b = cv2.split(lab)
        # A conservative CLAHE raises shadow detail while limiting flame highlights.
        light = cv2.createCLAHE(clipLimit=1.45, tileGridSize=(8, 8)).apply(light)
        result = cv2.cvtColor(cv2.merge((light, a, b)), cv2.COLOR_LAB2BGR)
    return result


def alpha_blend(dst: np.ndarray, overlay: np.ndarray, x: int, y: int, alpha: float = 1.0) -> None:
    h, w = overlay.shape[:2]
    if x >= dst.shape[1] or y >= dst.shape[0]:
        return
    x2, y2 = min(dst.shape[1], x + w), min(dst.shape[0], y + h)
    crop = overlay[:y2 - y, :x2 - x]

    if crop.shape[2] == 4:
        a = crop[:, :, 3:4].astype(np.float32) / 255.0
        a *= alpha
        rgb = crop[:, :, :3].astype(np.float32)
    else:
        a = np.full((*crop.shape[:2], 1), alpha, dtype=np.float32)
        rgb = crop.astype(np.float32)

    base = dst[y:y2, x:x2].astype(np.float32)
    dst[y:y2, x:x2] = np.clip(rgb * a + base * (1.0 - a), 0, 255).astype(np.uint8)


def draw_text(
    image: np.ndarray,
    text: str,
    xy: tuple[int, int],
    scale: float,
    color: tuple[int, int, int] = (255, 255, 255),
    thickness: int = 2,
    font: int = cv2.FONT_HERSHEY_DUPLEX,
) -> None:
    cv2.putText(image, text, xy, font, scale, color, thickness, cv2.LINE_AA)


def text_size(text: str, scale: float, thickness: int = 2) -> tuple[int, int]:
    (w, h), _ = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_DUPLEX, scale, thickness
    )
    return w, h


UI_FONT_REGULAR = font_manager.findfont(
    font_manager.FontProperties(family="DejaVu Sans", weight="normal")
)
UI_FONT_BOLD = font_manager.findfont(
    font_manager.FontProperties(family="DejaVu Sans", weight="bold")
)


@lru_cache(maxsize=48)
def ui_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Cache production-quality fonts used by the broadcast overlay."""
    return ImageFont.truetype(
        UI_FONT_BOLD if bold else UI_FONT_REGULAR,
        max(8, int(size)),
    )


def draw_ui_text(
    image: np.ndarray,
    text: str,
    xy: tuple[int, int],
    size: int,
    color: tuple[int, int, int],
    bold: bool = False,
    stroke_width: int = 0,
) -> None:
    """Draw crisp TrueType UI text into a BGR OpenCV image."""
    if not text or image.size == 0:
        return
    rgb_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    painter = ImageDraw.Draw(rgb_image)
    rgb_color = (int(color[2]), int(color[1]), int(color[0]))
    painter.text(
        xy, text, font=ui_font(size, bold), fill=rgb_color,
        stroke_width=max(0, int(stroke_width)), stroke_fill=(0, 0, 0),
    )
    image[:] = cv2.cvtColor(np.asarray(rgb_image), cv2.COLOR_RGB2BGR)


# ----------------------------- Chart Rendering ---------------------------- #

@dataclass
class ChartAsset:
    image: np.ndarray
    plot_x0: int
    plot_x1: int
    plot_y0: int
    plot_y1: int
    time_min: float
    time_max: float
    pressure_min: float
    pressure_max: float
    thrust_min: float
    thrust_max: float
    times: np.ndarray
    pressure: np.ndarray
    thrust: np.ndarray
    has_thrust: bool
    reveal_chart: bool
    pressure_color: tuple[int, int, int]
    thrust_color: tuple[int, int, int]
    cursor_color: tuple[int, int, int]
    fill_opacity: float
    line_width: int
    chart_type: str
    hud_mode: bool


def color_bgr(value: object, fallback: str = "#FFFFFF") -> tuple[int, int, int]:
    text = str(value or fallback).lstrip("#")
    if len(text) != 6:
        text = fallback.lstrip("#")
    try:
        red, green, blue = (int(text[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        red, green, blue = 255, 255, 255
    return blue, green, red


def scene_element(cfg: Config, kind: str) -> dict[str, Any]:
    scene = cfg.scene_config or default_scene_config()
    return next(
        (element for element in scene["elements"] if element.get("type") == kind),
        {},
    )


def render_chart_asset(
    telemetry: pd.DataFrame,
    width: int,
    height: int,
    cfg: Config,
) -> ChartAsset:
    times = telemetry["time"].to_numpy(dtype=float)
    pressure = telemetry["pressure"].to_numpy(dtype=float)
    thrust = telemetry["thrust"].to_numpy(dtype=float)
    has_thrust = telemetry_has_thrust(telemetry)

    t_min = float(times.min()) - cfg.chart_window_before_s
    t_max = float(times.max()) + cfg.chart_window_after_s
    p_min, p_max = finite_range(pressure)
    f_min, f_max = finite_range(thrust)

    style = scene_element(cfg, "chart")
    pressure_hex = style.get("pressureColor", "#38BDF8")
    thrust_hex = style.get("thrustColor", "#FB7185")
    grid_hex = style.get("gridColor", "#64748B")
    background_hex = style.get("background", "#0B1017")
    show_axes = bool(style.get("showAxes", True))
    show_grid = bool(style.get("showGrid", True))
    show_title = bool(style.get("showTitle", True))
    hud_mode = bool(style.get("hudMode", False))

    dpi = 120
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    fig.patch.set_facecolor(background_hex)

    compact = width < 500
    ax = fig.add_axes(
        [0.20, 0.22, 0.47, 0.60]
        if hud_mode else [0.13, 0.16, 0.74 if compact else 0.79, 0.66]
    )
    ax2 = ax.twinx() if has_thrust and not hud_mode else None

    ax.set_facecolor(background_hex)
    # The complete trace is deliberately faint; the live portion is drawn per frame.
    ax.plot(times, pressure, linewidth=1.8, color=pressure_hex, alpha=.18, label="Pressure")
    if ax2 is not None:
        ax2.plot(times, thrust, linewidth=1.4, color=thrust_hex, alpha=.22, label="Thrust")

    ax.set_xlim(t_min, t_max)
    ax.set_ylim(p_min, p_max)
    if ax2 is not None:
        ax2.set_ylim(f_min, f_max)

    axis_font = 8 if compact or hud_mode else 12
    tick_font = 7.5 if compact or hud_mode else 10.5
    ax.set_xlabel("" if hud_mode else "TIME FROM IGNITION  (s)", color="#B8C7D4", fontsize=axis_font,
                  fontweight="bold", labelpad=8)
    ax.set_ylabel("" if hud_mode else f"PRESSURE  ({cfg.pressure_unit})", color=pressure_hex,
                  fontsize=axis_font, fontweight="bold", labelpad=8)
    if ax2 is not None:
        ax2.set_ylabel(f"Thrust ({cfg.thrust_unit})", color=thrust_hex, fontsize=axis_font)

    ax.tick_params(colors="#D3DEE7", labelsize=tick_font, width=0, pad=6)
    if ax2 is not None:
        ax2.tick_params(colors="#94A3B8", labelsize=tick_font)
    ax.grid(show_grid, alpha=0.20 if hud_mode else 0.28, color=grid_hex, linewidth=0.8)
    if not show_axes:
        ax.set_xlabel("")
        ax.set_ylabel("")
        ax.tick_params(labelbottom=False, labelleft=False)
        if ax2 is not None:
            ax2.set_ylabel("")
            ax2.tick_params(labelright=False)
    if cfg.pressure_limit is not None:
        ax.axhline(
            cfg.pressure_limit, color="#EF4444", linewidth=1.4,
            linestyle="--", alpha=0.9,
        )

    for spine in ax.spines.values():
        spine.set_color("#587083")
    if ax2 is not None:
        for spine in ax2.spines.values():
            spine.set_color("#334155")

    p_idx = int(np.nanargmax(pressure))

    chart_title = cfg.subtitle
    if not has_thrust and chart_title == "Pressure & Derived Thrust":
        chart_title = "Chamber Pressure"
    if show_title:
        fig.text(
            0.025 if hud_mode else 0.04, 0.64 if hud_mode else 0.94, chart_title,
            color="#F5F9FC", fontsize=12 if hud_mode else (13 if compact else 17),
            fontweight="bold", ha="left", va="top"
        )
        fig.text(
            0.025 if hud_mode else 0.04, 0.40 if hud_mode else 0.885,
            f"Peak pressure  {pressure[p_idx]:.2f} {cfg.pressure_unit}"
            + (
                f"    |    Peak thrust  {thrust[int(np.nanargmax(thrust))]:.1f} {cfg.thrust_unit}"
                if has_thrust and not hud_mode else ""
            ),
            color="#B8C7D4", fontsize=7.5 if hud_mode else (8 if compact else 10.5),
            fontweight="bold", ha="left", va="top"
        )

    canvas = FigureCanvasAgg(fig)
    canvas.draw()

    rgba = np.asarray(canvas.buffer_rgba()).copy()
    rgb = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)

    # Pixel coordinates of axes inside rendered image.
    bbox = ax.get_window_extent()
    plot_x0 = int(round(bbox.x0))
    plot_x1 = int(round(bbox.x1))
    plot_y0 = height - int(round(bbox.y1))
    plot_y1 = height - int(round(bbox.y0))

    plt.close(fig)

    return ChartAsset(
        image=rgb,
        plot_x0=plot_x0,
        plot_x1=plot_x1,
        plot_y0=plot_y0,
        plot_y1=plot_y1,
        time_min=t_min,
        time_max=t_max,
        pressure_min=p_min,
        pressure_max=p_max,
        thrust_min=f_min,
        thrust_max=f_max,
        times=times,
        pressure=pressure,
        thrust=thrust,
        has_thrust=has_thrust,
        reveal_chart=cfg.reveal_chart,
        pressure_color=color_bgr(pressure_hex, "#38BDF8"),
        thrust_color=color_bgr(thrust_hex, "#FB7185"),
        cursor_color=color_bgr(style.get("cursorColor"), "#F8FAFC"),
        fill_opacity=float(style.get("fillOpacity", .2)),
        line_width=max(1, int(style.get("lineWidth", 3))),
        chart_type=str(style.get("chartType", "line")),
        hud_mode=hud_mode,
    )


def map_x(value: float, asset: ChartAsset) -> int:
    ratio = (value - asset.time_min) / (asset.time_max - asset.time_min)
    ratio = float(np.clip(ratio, 0.0, 1.0))
    return int(round(asset.plot_x0 + ratio * (asset.plot_x1 - asset.plot_x0)))


def map_y(value: float, lo: float, hi: float, asset: ChartAsset) -> int:
    ratio = (value - lo) / (hi - lo)
    ratio = float(np.clip(ratio, 0.0, 1.0))
    return int(round(asset.plot_y1 - ratio * (asset.plot_y1 - asset.plot_y0)))


def interpolate_telemetry(
    telemetry: pd.DataFrame,
    t: float,
) -> tuple[float, float]:
    times = telemetry["time"].to_numpy(dtype=float)
    p = telemetry["pressure"].to_numpy(dtype=float)
    f = telemetry["thrust"].to_numpy(dtype=float)

    # Do not present stale measurements outside the logger's data range.
    if t < times[0] or t > times[-1]:
        return 0.0, 0.0
    if t == times[0]:
        return float(p[0]), float(f[0])
    if t == times[-1]:
        return float(p[-1]), float(f[-1])

    return (
        float(np.interp(t, times, p)),
        float(np.interp(t, times, f)),
    )


def chart_frame(
    asset: ChartAsset,
    telemetry_time: float,
    pressure: float,
    thrust: float,
) -> np.ndarray:
    img = asset.image.copy()

    x = map_x(telemetry_time, asset)
    py = map_y(pressure, asset.pressure_min, asset.pressure_max, asset)
    fy = map_y(thrust, asset.thrust_min, asset.thrust_max, asset)

    visible = np.flatnonzero(
        asset.times <= telemetry_time if asset.reveal_chart
        else np.ones_like(asset.times, dtype=bool)
    )
    if visible.size:
        end = int(visible[-1]) + 1
        p_points = np.array([
            [map_x(t, asset), map_y(p, asset.pressure_min, asset.pressure_max, asset)]
            for t, p in zip(asset.times[:end], asset.pressure[:end])
        ], dtype=np.int32)
        if len(p_points) > 1:
            if asset.chart_type == "step":
                stepped = []
                for index, point in enumerate(p_points):
                    if index:
                        stepped.append([point[0], p_points[index - 1, 1]])
                    stepped.append(point.tolist())
                p_points = np.asarray(stepped, dtype=np.int32)
            fill_points = np.vstack((
                p_points,
                [p_points[-1, 0], asset.plot_y1],
                [p_points[0, 0], asset.plot_y1],
            ))
            shade = img.copy()
            cv2.fillPoly(shade, [fill_points], asset.pressure_color)
            opacity = float(np.clip(
                asset.fill_opacity if asset.chart_type == "area" else 0,
                0, 1,
            ))
            img = cv2.addWeighted(shade, opacity, img, 1 - opacity, 0)
            if asset.chart_type == "points":
                for point in p_points:
                    cv2.circle(img, tuple(point), max(2, asset.line_width + 1),
                               asset.pressure_color, -1, cv2.LINE_AA)
            else:
                cv2.polylines(img, [p_points], False, asset.pressure_color,
                              asset.line_width, cv2.LINE_AA)
        if asset.has_thrust and not asset.hud_mode:
            f_points = np.array([
                [map_x(t, asset), map_y(f, asset.thrust_min, asset.thrust_max, asset)]
                for t, f in zip(asset.times[:end], asset.thrust[:end])
            ], dtype=np.int32)
            if len(f_points) > 1:
                if asset.chart_type == "step":
                    stepped = []
                    for index, point in enumerate(f_points):
                        if index:
                            stepped.append([point[0], f_points[index - 1, 1]])
                        stepped.append(point.tolist())
                    f_points = np.asarray(stepped, dtype=np.int32)
                if asset.chart_type == "points":
                    for point in f_points:
                        cv2.circle(img, tuple(point), max(2, asset.line_width + 1),
                                   asset.thrust_color, -1, cv2.LINE_AA)
                else:
                    cv2.polylines(img, [f_points], False, asset.thrust_color,
                                  asset.line_width, cv2.LINE_AA)

    # Current-time cursor.
    cv2.line(
        img, (x, asset.plot_y0), (x, asset.plot_y1),
        asset.cursor_color, 2, cv2.LINE_AA
    )

    # Current points.
    cv2.circle(img, (x, py), 7, asset.pressure_color, -1, cv2.LINE_AA)
    cv2.circle(img, (x, py), 10, (255, 255, 255), 1, cv2.LINE_AA)
    if asset.has_thrust and not asset.hud_mode:
        cv2.circle(img, (x, fy), 7, asset.thrust_color, -1, cv2.LINE_AA)
        cv2.circle(img, (x, fy), 10, (255, 255, 255), 1, cv2.LINE_AA)

    p_idx = int(np.nanargmax(asset.pressure))
    if telemetry_time >= asset.times[p_idx]:
        peak_point = (
            map_x(asset.times[p_idx], asset),
            map_y(asset.pressure[p_idx], asset.pressure_min, asset.pressure_max, asset),
        )
        cv2.circle(img, peak_point, 6, asset.pressure_color, -1, cv2.LINE_AA)
        cv2.circle(img, peak_point, 9, (255, 255, 255), 1, cv2.LINE_AA)
    if asset.has_thrust and not asset.hud_mode:
        f_idx = int(np.nanargmax(asset.thrust))
        if telemetry_time >= asset.times[f_idx]:
            peak_point = (
                map_x(asset.times[f_idx], asset),
                map_y(asset.thrust[f_idx], asset.thrust_min, asset.thrust_max, asset),
            )
            cv2.circle(img, peak_point, 6, asset.thrust_color, -1, cv2.LINE_AA)
            cv2.circle(img, peak_point, 9, (255, 255, 255), 1, cv2.LINE_AA)

    return img


# ------------------------------ Composition ------------------------------ #

def draw_metric_card(
    canvas: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    label: str,
    value: str,
    accent: tuple[int, int, int],
) -> None:
    cv2.rectangle(canvas, (x, y), (x + w, y + h), (18, 26, 38), -1)
    cv2.rectangle(canvas, (x, y), (x + 6, y + h), accent, -1)
    label_scale = 0.55 if h >= 100 else 0.42
    value_scale = 1.05 if h >= 100 else 0.68
    draw_text(canvas, label.upper(), (x + 18, y + int(h * 0.29)),
              label_scale, (148, 163, 184), 1)
    draw_text(canvas, value, (x + 18, y + int(h * 0.73)),
              value_scale, (255, 255, 255), 2)


def element_rect(element: dict[str, Any], width: int, height: int) -> tuple[int, int, int, int]:
    x = int(round(float(element.get("x", 0)) * width))
    y = int(round(float(element.get("y", 0)) * height))
    w = max(1, int(round(float(element.get("width", 1)) * width)))
    h = max(1, int(round(float(element.get("height", 1)) * height)))
    return x, y, min(w, width - x), min(h, height - y)


def translucent_panel(
    canvas: np.ndarray,
    rect: tuple[int, int, int, int],
    color: object,
    opacity: float,
    radius: int = 0,
) -> None:
    x, y, w, h = rect
    if w <= 0 or h <= 0 or opacity <= 0:
        return
    panel = np.full((h, w, 4), 0, dtype=np.uint8)
    panel[:, :, :3] = color_bgr(color, "#0B1017")
    alpha = int(round(float(np.clip(opacity, 0, 1)) * 255))
    radius = max(0, min(int(radius), min(w, h) // 2))
    if radius:
        cv2.rectangle(panel, (radius, 0), (w - radius, h), (*color_bgr(color), alpha), -1)
        cv2.rectangle(panel, (0, radius), (w, h - radius), (*color_bgr(color), alpha), -1)
        for center in ((radius, radius), (w - radius, radius),
                       (radius, h - radius), (w - radius, h - radius)):
            cv2.circle(panel, center, radius, (*color_bgr(color), alpha), -1, cv2.LINE_AA)
    else:
        panel[:, :, 3] = alpha
    alpha_blend(canvas, panel, x, y)


def compose_scene_frame(
    video_frame: np.ndarray,
    chart: np.ndarray,
    cfg: Config,
    telemetry_time: float,
    pressure: float,
    thrust: float,
    video_time: float,
    logo: Optional[np.ndarray],
    metrics: TestMetrics,
    has_thrust: bool,
) -> np.ndarray:
    """Compose the normalized editor scene used by both preview and delivery."""
    scene = validate_scene_config(cfg.scene_config or default_scene_config())
    canvas = np.full(
        (cfg.height, cfg.width, 3),
        color_bgr(scene.get("background"), "#0B1017"),
        dtype=np.uint8,
    )
    elements = sorted(scene["elements"], key=lambda item: int(item.get("z", 0)))
    identity = "  •  ".join(filter(None, (
        cfg.run_number, cfg.motor_type, cfg.propellant, cfg.test_date
    )))
    status, status_color = determine_test_phase(
        telemetry_time, metrics, pressure, cfg.pressure_limit
    )

    for element in elements:
        if not element.get("visible", True):
            continue
        kind = element["type"]
        if kind == "thrust_card" and not has_thrust:
            continue
        if kind == "identity" and not identity:
            continue
        if kind == "logo" and logo is None:
            continue
        rect = element_rect(element, cfg.width, cfg.height)
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            continue
        start_time = float(element.get("startTime", 0) or 0)
        end_value = element.get("endTime")
        end_time = float(end_value) if end_value not in (None, "") else float("inf")
        if video_time < start_time or video_time > end_time:
            continue
        time_opacity = 1.0
        fade_in = float(element.get("fadeIn", 0) or 0)
        fade_out = float(element.get("fadeOut", 0) or 0)
        if fade_in > 0:
            time_opacity = min(time_opacity, (video_time - start_time) / fade_in)
        if fade_out > 0 and math.isfinite(end_time):
            time_opacity = min(time_opacity, (end_time - video_time) / fade_out)
        opacity = float(element.get("opacity", 1)) * float(np.clip(time_opacity, 0, 1))

        if kind == "video":
            prepared = prepare_video_frame(video_frame, cfg)
            alpha_blend(canvas, cover_resize(prepared, w, h), x, y, opacity)
            continue
        if kind == "chart":
            chart_background = element.get("background", "#0B1017")
            translucent_panel(
                canvas, rect, chart_background,
                float(element.get("backgroundOpacity", 1)) * opacity,
                int(element.get("borderRadius", 0)),
            )
            resized_chart = cv2.resize(
                chart, (w, h), interpolation=cv2.INTER_AREA
            )
            background_pixel = np.asarray(
                color_bgr(chart_background, "#0B1017"), dtype=np.int16
            )
            distance = np.max(
                np.abs(resized_chart.astype(np.int16) - background_pixel),
                axis=2,
            )
            chart_alpha = np.clip(distance * 5, 0, 255).astype(np.uint8)
            chart_rgba = np.dstack((resized_chart, chart_alpha))
            alpha_blend(
                canvas, chart_rgba, x, y, opacity,
            )
            continue
        if kind == "logo":
            lh, lw = logo.shape[:2]
            scale = min(w / lw, h / lh)
            resized = cv2.resize(
                logo, (max(1, int(lw * scale)), max(1, int(lh * scale))),
                interpolation=cv2.INTER_AREA,
            )
            alpha_blend(
                canvas, resized,
                x + (w - resized.shape[1]) // 2,
                y + (h - resized.shape[0]) // 2,
                opacity,
            )
            continue

        default_radius = h // 2 if kind == "status" else max(0, int(min(w, h) * .08))
        translucent_panel(
            canvas, rect, element.get("background", "#0F172A"),
            float(element.get("backgroundOpacity", 0)) * opacity,
            int(element.get("borderRadius", default_radius)),
        )
        color = color_bgr(element.get("color"), "#FFFFFF")
        font_multiplier = float(element.get("fontSize", 1))
        padding = max(5, int(min(w, h) * .12))
        # Draw text into the element's own ROI. The browser preview clips
        # overflowing content to the element box; drawing on the full canvas
        # let long titles and timecodes spill across unrelated elements.
        roi = canvas[y:y + h, x:x + w]
        if kind == "title":
            cv2.rectangle(roi, (0, 0), (max(2, int(w * .003)), h),
                          color_bgr("#5EDCFF"), -1)
            draw_ui_text(
                roi, "STATIC FIRE TEST", (padding, int(h * .10)),
                int(h * .12 * font_multiplier), (180, 199, 211), True, 1,
            )
            draw_ui_text(
                roi, cfg.title[:70], (padding, int(h * .35)),
                int(h * .30 * font_multiplier), color, True, 1,
            )
        elif kind == "timecode":
            sign = "+" if telemetry_time >= 0 else "-"
            absolute_time = abs(telemetry_time)
            minutes = int(absolute_time // 60)
            seconds = absolute_time - minutes * 60
            draw_ui_text(
                roi, "MISSION TIME", (padding, int(h * .10)),
                int(h * .13 * font_multiplier), (180, 199, 211), True,
            )
            draw_ui_text(
                roi, f"T{sign} {minutes:02d}:{seconds:05.2f}",
                (padding, int(h * .39)), int(h * .28 * font_multiplier),
                color, True,
            )
        elif kind == "identity":
            draw_ui_text(
                roi, identity[:90], (padding, int(h * .16)),
                int(h * .40 * font_multiplier), color, True, 1,
            )
        elif kind in {"pressure_card", "thrust_card"}:
            is_pressure = kind == "pressure_card"
            label = "CHAMBER PRESSURE" if is_pressure else "THRUST"
            value = (
                f"{pressure:,.2f} {cfg.pressure_unit}" if is_pressure
                else f"{thrust:,.1f} {cfg.thrust_unit}"
            )
            accent = color_bgr(
                element.get("color"),
                "#38BDF8" if is_pressure else "#FB7185",
            )
            cv2.rectangle(roi, (0, 0), (max(3, int(w * .018)), h),
                          accent, -1)
            draw_ui_text(
                roi, label, (padding, int(h * .16)),
                int(h * .10 * font_multiplier), (175, 193, 205), True,
            )
            draw_ui_text(
                roi, value, (padding, int(h * .43)),
                int(h * .24 * font_multiplier), (255, 255, 255), True,
            )
        elif kind == "status":
            radius = max(3, int(h * .07))
            cv2.circle(roi, (padding, h // 2), radius, status_color, -1, cv2.LINE_AA)
            draw_ui_text(
                roi, status, (padding + radius * 3, int(h * .30)),
                int(h * .20 * font_multiplier), status_color, True,
            )
    return canvas


def compose_frame(
    video_frame: np.ndarray,
    chart: np.ndarray,
    cfg: Config,
    telemetry_time: float,
    pressure: float,
    thrust: float,
    video_time: float,
    logo: Optional[np.ndarray],
    metrics: TestMetrics,
    has_thrust: bool,
) -> np.ndarray:
    W, H = cfg.width, cfg.height
    if cfg.scene_config is not None:
        return compose_scene_frame(
            video_frame, chart, cfg, telemetry_time, pressure, thrust,
            video_time, logo, metrics, has_thrust,
        )
    if H > W:
        return compose_portrait_frame(
            video_frame, chart, cfg, telemetry_time, pressure, thrust,
            video_time, logo, metrics, has_thrust,
        )
    left_w = int(round(W * cfg.video_fraction))
    right_w = W - left_w

    canvas = np.zeros((H, W, 3), dtype=np.uint8)
    canvas[:] = (11, 16, 23)

    # Video side.
    video_area = cover_resize(prepare_video_frame(video_frame, cfg), left_w, H)
    canvas[:, :left_w] = video_area

    # Subtle dark gradient at top/bottom for readability.
    overlay = canvas.copy()
    cv2.rectangle(overlay, (0, 0), (left_w, 150), (0, 0, 0), -1)
    cv2.rectangle(overlay, (0, H - 180), (left_w, H), (0, 0, 0), -1)
    canvas[:, :left_w] = cv2.addWeighted(
        overlay[:, :left_w], 0.38, canvas[:, :left_w], 0.62, 0
    )

    # Right panel.
    cv2.rectangle(canvas, (left_w, 0), (W, H), (11, 16, 23), -1)
    cv2.line(canvas, (left_w, 0), (left_w, H), (51, 65, 85), 2)

    chart_top = max(56, int(H * 0.075))
    chart_h = int(H * (0.72 if has_thrust else 0.76))
    footer_top = chart_top + chart_h
    chart_resized = cv2.resize(chart, (right_w, chart_h), interpolation=cv2.INTER_AREA)
    canvas[chart_top:chart_top + chart_h, left_w:W] = chart_resized

    # Header.
    header_scale = max(0.58, min(1.05, H / 1028))
    draw_text(canvas, cfg.title, (int(W * 0.022), int(H * 0.052)), header_scale, (255, 255, 255), 2)
    draw_text(
        canvas,
        f"VIDEO  {video_time:06.2f}s    |    TELEMETRY  {telemetry_time:+06.2f}s",
        (int(W * 0.023), int(H * 0.088)), max(0.38, H / 1964),
        (203, 213, 225), 1
    )

    # Live metric cards.
    panel_pad = max(14, int(right_w * 0.04))
    gap = max(10, int(right_w * 0.025))
    card_y = footer_top + max(10, int(H * 0.018))
    card_w = (
        (right_w - panel_pad * 2 - gap) // 2
        if has_thrust else right_w - panel_pad * 2
    )
    card_h = min(112, max(72, H - card_y - max(42, int(H * 0.09))))
    draw_metric_card(
        canvas, left_w + panel_pad, card_y, card_w, card_h,
        "Pressure", f"{pressure:,.2f} {cfg.pressure_unit}", (248, 189, 56)
    )
    if has_thrust:
        draw_metric_card(
            canvas, left_w + panel_pad + card_w + gap, card_y, card_w, card_h,
            "Thrust", f"{thrust:,.1f} {cfg.thrust_unit}", (133, 113, 251)
        )

    # Test state determined from ignition, burn duration, tail-off and safety limit.
    status, status_color = determine_test_phase(
        telemetry_time, metrics, pressure, cfg.pressure_limit
    )
    sw, _ = text_size(status, 0.58, 2)
    status_x = int(W * 0.023)
    status_bottom = H - max(14, int(H * 0.026))
    cv2.rectangle(canvas, (status_x, status_bottom - 46),
                  (status_x + sw + 32, status_bottom), (15, 23, 42), -1)
    draw_text(canvas, status, (status_x + 16, status_bottom - 14), 0.58, status_color, 2)

    if logo is not None:
        max_w, max_h = 220, 90
        lh, lw = logo.shape[:2]
        scale = min(max_w / lw, max_h / lh, 1.0)
        resized = cv2.resize(
            logo, (int(lw * scale), int(lh * scale)),
            interpolation=cv2.INTER_AREA
        )
        alpha_blend(canvas, resized, left_w - resized.shape[1] - 28, 26, 0.92)

    # Compact engineering identity line in the panel header.
    identity = "  •  ".join(filter(None, (
        cfg.run_number, cfg.motor_type, cfg.propellant, cfg.test_date
    )))
    if identity:
        draw_text(
            canvas, identity[:58], (left_w + panel_pad, int(H * 0.045)),
            max(0.34, H / 2600), (148, 163, 184), 1
        )

    return canvas


def compose_portrait_frame(
    video_frame: np.ndarray,
    chart: np.ndarray,
    cfg: Config,
    telemetry_time: float,
    pressure: float,
    thrust: float,
    video_time: float,
    logo: Optional[np.ndarray],
    metrics: TestMetrics,
    has_thrust: bool,
) -> np.ndarray:
    W, H = cfg.width, cfg.height
    canvas = np.full((H, W, 3), (11, 16, 23), dtype=np.uint8)
    header_h = int(H * 0.075)
    video_h = int(H * 0.50)
    chart_h = int(H * 0.31)
    canvas[header_h:header_h + video_h] = cover_resize(
        prepare_video_frame(video_frame, cfg), W, video_h
    )
    chart_top = header_h + video_h
    canvas[chart_top:chart_top + chart_h] = cv2.resize(
        chart, (W, chart_h), interpolation=cv2.INTER_AREA
    )
    draw_text(canvas, cfg.title, (int(W * 0.045), int(header_h * 0.58)),
              max(0.65, W / 1250), (255, 255, 255), 2)
    draw_text(
        canvas, f"VIDEO {video_time:06.2f}s  |  TELEMETRY {telemetry_time:+06.2f}s",
        (int(W * 0.045), int(header_h * 0.86)), max(0.36, W / 2500),
        (148, 163, 184), 1,
    )
    footer_y = chart_top + chart_h + int(H * 0.012)
    pad, gap = int(W * 0.04), int(W * 0.025)
    card_h = max(86, int(H * 0.06))
    card_w = (W - pad * 2 - gap) // 2 if has_thrust else W - pad * 2
    draw_metric_card(canvas, pad, footer_y, card_w, card_h, "Pressure",
                     f"{pressure:,.2f} {cfg.pressure_unit}", (248, 189, 56))
    if has_thrust:
        draw_metric_card(canvas, pad + card_w + gap, footer_y, card_w, card_h,
                         "Thrust", f"{thrust:,.1f} {cfg.thrust_unit}",
                         (133, 113, 251))
    status, color = determine_test_phase(
        telemetry_time, metrics, pressure, cfg.pressure_limit
    )
    draw_text(canvas, status, (pad, min(H - 24, footer_y + card_h + 46)),
              0.58, color, 2)
    if logo is not None:
        lh, lw = logo.shape[:2]
        scale = min(W * 0.15 / lw, header_h * 0.55 / lh, 1.0)
        resized = cv2.resize(logo, (int(lw * scale), int(lh * scale)),
                             interpolation=cv2.INTER_AREA)
        alpha_blend(canvas, resized, W - resized.shape[1] - pad,
                    int(header_h * 0.18), 0.92)
    return canvas


def make_title_frame(
    cfg: Config,
    logo: Optional[np.ndarray],
    metrics: Optional[TestMetrics] = None,
) -> np.ndarray:
    canvas = np.full((cfg.height, cfg.width, 3), (12, 16, 21), dtype=np.uint8)
    accent_x = int(cfg.width * 0.09)
    cv2.rectangle(
        canvas, (accent_x, int(cfg.height * 0.28)),
        (accent_x + 7, int(cfg.height * 0.68)), (213, 199, 49), -1
    )
    draw_text(canvas, cfg.title, (accent_x + 40, int(cfg.height * 0.42)),
              max(0.9, cfg.height / 680), (248, 250, 252), 2)
    subtitle = "STATIC FIRE TEST • TELEMETRY REPORT" if metrics is None else "TEST SUMMARY"
    draw_text(canvas, subtitle, (accent_x + 43, int(cfg.height * 0.49)),
              max(0.46, cfg.height / 1600), (213, 199, 49), 1)
    info = "  •  ".join(filter(None, (cfg.run_number, cfg.motor_type, cfg.test_date)))
    if info:
        draw_text(canvas, info, (accent_x + 43, int(cfg.height * 0.55)),
                  max(0.38, cfg.height / 2100), (148, 163, 184), 1)
    if metrics is not None:
        summary = (
            f"PEAK  {metrics.peak_pressure:.2f} {cfg.pressure_unit}"
            f"    |    BURN  {metrics.burn_duration:.2f} s"
            f"    |    PEAK TIME  {metrics.peak_pressure_time:.2f} s"
        )
        draw_text(canvas, summary, (accent_x + 43, int(cfg.height * 0.61)),
                  max(0.4, cfg.height / 1900), (226, 232, 240), 1)
        detail = (
            f"AVERAGE  {metrics.average_pressure:.2f} {cfg.pressure_unit}"
            f"    |    MAX dP/dt  {metrics.max_pressure_rise:.2f} {cfg.pressure_unit}/s"
        )
        if metrics.total_impulse is not None:
            detail += f"    |    IMPULSE  {metrics.total_impulse:.1f} N·s"
        draw_text(canvas, detail, (accent_x + 43, int(cfg.height * 0.66)),
                  max(0.34, cfg.height / 2200), (148, 163, 184), 1)
    if logo is not None:
        max_w, max_h = int(cfg.width * 0.16), int(cfg.height * 0.13)
        lh, lw = logo.shape[:2]
        scale = min(max_w / lw, max_h / lh, 1.0)
        resized = cv2.resize(logo, (int(lw * scale), int(lh * scale)),
                             interpolation=cv2.INTER_AREA)
        alpha_blend(canvas, resized, cfg.width - resized.shape[1] - accent_x,
                    int(cfg.height * 0.1), 0.95)
    return canvas


# ------------------------------- FFmpeg --------------------------------- #

@lru_cache(maxsize=1)
def ffmpeg_executable() -> Optional[str]:
    """Return a usable FFmpeg executable, including imageio's bundled build."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        return None

    if bundled_ffmpeg and Path(bundled_ffmpeg).is_file():
        return str(bundled_ffmpeg)
    return None


def ffmpeg_available() -> bool:
    return ffmpeg_executable() is not None


@lru_cache(maxsize=32)
def probe_nominal_fps(source_video: Path) -> Optional[float]:
    """Read FFmpeg's nominal frame rate without decoding the whole video.

    OpenCV reports the average rate for some iPhone variable-frame-rate MOV
    files. Feeding decoded frames back at that average rate can lengthen the
    export and drift its audio. FFmpeg's ``tbr`` value reflects the intended
    presentation cadence (29.97 for the affected source).
    """
    executable = ffmpeg_executable()
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-i", str(source_video)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    for line in result.stderr.splitlines():
        if "Video:" not in line:
            continue
        match = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s+tbr\b", line)
        if not match:
            continue
        fps = float(match.group(1))
        if 1.0 <= fps <= 240.0:
            return fps
    return None


@lru_cache(maxsize=32)
def source_is_hlg_main10(source_video: Path) -> bool:
    """Return whether the source is a 10-bit HLG stream suitable for HDR mode."""
    executable = ffmpeg_executable()
    if executable is None:
        return False
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-i", str(source_video)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    video_line = next(
        (line for line in result.stderr.splitlines() if "Video:" in line),
        "",
    )
    return (
        "10le" in video_line
        and "bt2020" in video_line
        and "arib-std-b67" in video_line
    )


class FFmpegRawVideoWriter:
    """Stream composed BGR frames directly into the final delivery encoder."""

    def __init__(
        self,
        output: Path,
        source_video: Path,
        width: int,
        height: int,
        fps: float,
        crf: int,
        intro_duration_s: float = 0.0,
        keep_audio: bool = True,
        delivery_codec: str = "h264",
        cut_list: Optional[list[tuple[float, float]]] = None,
    ) -> None:
        executable = ffmpeg_executable()
        if executable is None:
            fail(
                "FFmpeg is required for high-quality export. Install FFmpeg "
                "or install the imageio-ffmpeg Python package."
            )

        self.output = output
        self._working_output = output.with_name(
            f".{output.stem}.encoding{output.suffix or '.mp4'}"
        )
        self._working_output.unlink(missing_ok=True)
        self.width = int(width)
        self.height = int(height)
        self._closed = False
        self._stderr = tempfile.TemporaryFile(mode="w+b")

        encoder = "libx265" if delivery_codec == "h265" else "libx264"
        is_large_frame = self.width * self.height > 1920 * 1080
        # 4K with x264's slow preset creates a large look-ahead buffer and can
        # exhaust constrained web workers. Medium retains the same CRF quality
        # target with substantially lower memory/CPU use for genuine 4K input.
        preset = "medium" if is_large_frame else "slow"
        # OpenCV has already decoded the source into 8-bit BGR. The raw frames
        # are therefore an SDR delivery surface; copying source BT.2020/HLG or
        # Dolby Vision tags would describe pixels that no longer carry HDR.
        cmd = [
            executable,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-thread_queue_size", "64",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s:v", f"{self.width}x{self.height}",
            "-r", f"{float(fps):.12g}",
            "-i", "pipe:0",
        ]
        # A cut list needs its kept ranges trimmed out of the *original*
        # audio timeline and re-joined, which is incompatible with the
        # simple constant -itsoffset shift used below - so it gets its own
        # -filter_complex atrim+concat input path instead.
        use_cut_audio = bool(keep_audio and cut_list)
        intro_s = max(0.0, float(intro_duration_s))
        if keep_audio:
            if use_cut_audio:
                cmd += [
                    "-thread_queue_size", "512",
                    "-i", str(source_video),
                ]
                if intro_s > 0:
                    # A silent lead-in is spliced into the concat chain below
                    # rather than mixed with -itsoffset on the same input.
                    cmd += [
                        "-f", "lavfi", "-t", f"{intro_s:.9f}",
                        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    ]
            else:
                cmd += [
                    "-thread_queue_size", "512",
                    "-itsoffset", f"{intro_s:.9f}",
                    "-i", str(source_video),
                ]

        cmd += [
            "-map", "0:v:0",
        ]
        if keep_audio:
            if use_cut_audio:
                segment_labels = []
                filter_parts = []
                if intro_s > 0:
                    filter_parts.append("[2:a]asetpts=PTS-STARTPTS[cut_a_intro]")
                    segment_labels.append("[cut_a_intro]")
                for index, (start, end) in enumerate(cut_list):
                    label = f"cut_a{index}"
                    filter_parts.append(
                        f"[1:a]atrim=start={start:.9f}:end={end:.9f},"
                        f"asetpts=PTS-STARTPTS[{label}]"
                    )
                    segment_labels.append(f"[{label}]")
                filter_parts.append(
                    "".join(segment_labels)
                    + f"concat=n={len(segment_labels)}:v=0:a=1[cut_aout]"
                )
                cmd += [
                    "-filter_complex", ";".join(filter_parts),
                    "-map", "[cut_aout]",
                ]
            else:
                # Optional mapping keeps silent source files exportable while
                # preserving the source audio whenever a track is present.
                cmd += ["-map", "1:a:0?"]
        cmd += [
            "-c:v", encoder,
            "-preset", preset,
            "-crf", str(int(crf)),
            "-vf",
            "scale=in_range=full:out_range=tv:out_color_matrix=bt709,"
            "format=yuv420p",
            "-color_range", "tv",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-fps_mode", "cfr",
        ]
        if is_large_frame:
            cmd += ["-threads", "4"]
        if delivery_codec == "h265":
            # hvc1 is recognized by Apple/Windows players more consistently.
            cmd += ["-tag:v", "hvc1"]
        if keep_audio:
            cmd += [
                "-c:a", "aac",
                "-b:a", "256k",
            ]
        else:
            cmd += ["-an"]
        cmd += [
            "-max_muxing_queue_size", "1024",
            "-movflags", "+faststart",
            str(self._working_output),
        ]

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr,
                bufsize=1024 * 1024,
            )
        except OSError as exc:
            self._stderr.close()
            fail(f"Could not start FFmpeg: {exc}")

        if self._process.stdin is None:
            self.abort()
            fail("Could not open the FFmpeg video input pipe.")

    def _stderr_tail(self, limit: int = 2500) -> str:
        try:
            self._stderr.flush()
            self._stderr.seek(0, os.SEEK_END)
            size = self._stderr.tell()
            self._stderr.seek(max(0, size - limit))
            return self._stderr.read().decode("utf-8", errors="replace").strip()
        except (OSError, ValueError):
            return ""

    def _failure(self, return_code: Optional[int] = None) -> RocketOverlayError:
        details = self._stderr_tail()
        status = "" if return_code is None else f" (exit code {return_code})"
        if details:
            print(
                f"FFmpeg delivery encoding failed{status}:\n{details}",
                file=sys.stderr,
            )
        if "Immediate exit requested" in details:
            return RocketOverlayError(
                "The export encoder stopped before the file finished. The partial "
                "file was removed; use the source resolution and try again."
            )
        return RocketOverlayError(
            f"Could not finish encoding the high-quality video{status}."
        )

    def write(self, frame: np.ndarray) -> bool:
        if self._closed:
            raise RocketOverlayError("Cannot write to a closed FFmpeg encoder.")
        if frame.shape != (self.height, self.width, 3):
            raise RocketOverlayError(
                "Rendered frame size does not match the configured output "
                f"resolution ({frame.shape[1]}x{frame.shape[0]} instead of "
                f"{self.width}x{self.height})."
            )
        if frame.dtype != np.uint8:
            raise RocketOverlayError("Rendered frames must use 8-bit BGR pixels.")

        return_code = self._process.poll()
        if return_code is not None:
            if return_code == 0:
                return False
            raise self._failure(return_code)

        payload = memoryview(np.ascontiguousarray(frame)).cast("B")
        try:
            while payload:
                written = self._process.stdin.write(payload)
                if written is None or written <= 0:
                    raise BrokenPipeError
                payload = payload[written:]
        except (BrokenPipeError, OSError) as exc:
            return_code = self._process.wait()
            if return_code == 0:
                return False
            raise self._failure(return_code) from exc
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        return_code = self._process.wait()
        if return_code != 0:
            error = self._failure(return_code)
            self._stderr.close()
            self._working_output.unlink(missing_ok=True)
            raise error
        self._stderr.close()
        if (
            not self._working_output.exists()
            or self._working_output.stat().st_size == 0
        ):
            raise RocketOverlayError("FFmpeg completed without creating a video.")
        os.replace(self._working_output, self.output)

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._stderr.close()
        self._working_output.unlink(missing_ok=True)


HLG_NOMINAL_PEAK_NITS = 1000


def _ordinal_cadence_filter(fps: float) -> str:
    """Return an exact, frame-ordinal time base for an FFmpeg filter input.

    The HDR source and the raw graphics pipe contain corresponding frames, but
    their demuxers can describe those frames with slightly different PTS (for
    example, 29.97-VFR source timestamps versus a 30-CFR raw-video clock).
    Multi-input filters then see both timestamp sets and can emit each pair as
    two output frames.  Giving both inputs the same rational time base and PTS
    by ordinal keeps frame ``N`` paired with graphics frame ``N``.
    """
    if not math.isfinite(fps) or fps <= 0:
        raise RocketOverlayError("HDR overlay FPS must be a positive number.")
    rate = Fraction(str(float(fps))).limit_denominator(1_000_000)
    return f"settb=expr={rate.denominator}/{rate.numerator},setpts=N"


def hlg_overlay_filter_graph(fps: Optional[float] = None) -> str:
    """Build the source-preserving, linear-light HLG compositor graph.

    The broadcast graphics are authored in SDR/sRGB.  Blending their black
    scrims directly into gamma-encoded HLG makes the intended transmission
    far too dark, while 203-nit SDR white can sit below a bright HDR sky.
    Convert both inputs to linear BT.2020, merge them with the untouched
    full-range alpha plane, then encode the result back to HLG.  ``maskedmerge``
    is used instead of ``overlay`` here because it retains float precision;
    FFmpeg's overlay filter otherwise negotiates a 10-bit *linear* surface.
    """
    npl = HLG_NOMINAL_PEAK_NITS
    # Direct callers that only need the colour transform retain the input
    # timestamps.  The streaming writer supplies ``fps`` so both corresponding
    # streams are explicitly locked to one cadence before ``maskedmerge``.
    cadence = (
        _ordinal_cadence_filter(fps)
        if fps is not None
        else "setpts=PTS-STARTPTS"
    )
    return (
        f"[0:v:0]{cadence},format=yuv420p10le,"
        "zscale=pin=bt2020:tin=arib-std-b67:min=bt2020nc:rin=limited:"
        f"p=bt2020:t=linear:m=gbr:r=full:npl={npl},"
        "format=gbrpf32le[base_linear];"
        f"[1:v:0]{cadence},split[overlay_rgb][overlay_alpha];"
        "[overlay_alpha]alphaextract,format=grayf32le,"
        "split=3[alpha_g][alpha_b][alpha_r];"
        "[alpha_g][alpha_b][alpha_r]"
        "mergeplanes=map0s=0:map0p=0:map1s=1:map1p=0:"
        "map2s=2:map2p=0:format=gbrpf32le[alpha_rgb];"
        "[overlay_rgb]format=gbrp10le,"
        "zscale=pin=bt709:tin=iec61966-2-1:min=gbr:rin=full:"
        f"p=bt2020:t=linear:m=gbr:r=full:npl={npl},"
        "format=gbrpf32le[graphics_linear];"
        "[base_linear][graphics_linear][alpha_rgb]"
        "maskedmerge=planes=7[composite_linear];"
        "[composite_linear]"
        "zscale=pin=bt2020:tin=linear:min=gbr:rin=full:"
        f"p=bt2020:t=arib-std-b67:m=bt2020nc:r=limited:npl={npl},"
        "format=yuv420p10le,"
        "setparams=range=limited:color_primaries=bt2020:"
        "color_trc=arib-std-b67:colorspace=bt2020nc[outv]"
    )


class FFmpegHLGOverlayWriter:
    """Composite BGRA graphics over the untouched Main10 HLG source in FFmpeg."""

    def __init__(
        self,
        output: Path,
        source_video: Path,
        width: int,
        height: int,
        fps: float,
        keep_audio: bool = True,
    ) -> None:
        executable = ffmpeg_executable()
        if executable is None:
            fail("FFmpeg is required for source-quality HDR export.")

        self.output = output
        self._working_output = output.with_name(
            f".{output.stem}.encoding{output.suffix or '.mp4'}"
        )
        self._working_output.unlink(missing_ok=True)
        self.width = int(width)
        self.height = int(height)
        self._closed = False
        self._stderr = tempfile.TemporaryFile(mode="w+b")

        # Keep alpha full-range and composite in linear light.  Converting the
        # alpha plane as video-range luma, or blending on the HLG signal, would
        # respectively create a frame-wide veil or excessively deep scrims.
        filter_graph = hlg_overlay_filter_graph(fps)
        x265_params = (
            "lossless=1:colorprim=9:transfer=18:colormatrix=9:"
            "range=limited:repeat-headers=1"
        )
        cmd = [
            executable,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-thread_queue_size", "512",
            "-i", str(source_video),
            "-thread_queue_size", "64",
            "-f", "rawvideo",
            "-pixel_format", "bgra",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", f"{float(fps):.12g}",
            "-i", "pipe:0",
            "-filter_complex", filter_graph,
            "-map", "[outv]",
        ]
        if keep_audio:
            cmd += ["-map", "0:a:0?"]
        cmd += [
            "-fps_mode:v", "passthrough",
            "-c:v", "libx265",
            "-profile:v", "main10",
            "-pix_fmt", "yuv420p10le",
            # Lossless is intentional in source-quality mode. It preserves
            # decoded source samples outside the pixels covered by graphics.
            "-preset", "ultrafast",
            "-x265-params", x265_params,
            "-tag:v", "hvc1",
            "-color_range", "tv",
            "-color_primaries", "bt2020",
            "-color_trc", "arib-std-b67",
            "-colorspace", "bt2020nc",
        ]
        if keep_audio:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd += [
            "-map_metadata", "-1",
            "-max_muxing_queue_size", "1024",
            "-movflags", "+faststart",
            str(self._working_output),
        ]

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr,
                bufsize=1024 * 1024,
            )
        except OSError as exc:
            self._stderr.close()
            fail(f"Could not start FFmpeg HDR encoder: {exc}")
        if self._process.stdin is None:
            self.abort()
            fail("Could not open the FFmpeg HDR overlay pipe.")

    def _stderr_tail(self, limit: int = 3500) -> str:
        try:
            self._stderr.flush()
            self._stderr.seek(0, os.SEEK_END)
            size = self._stderr.tell()
            self._stderr.seek(max(0, size - limit))
            return self._stderr.read().decode(
                "utf-8", errors="replace"
            ).strip()
        except (OSError, ValueError):
            return ""

    def _failure(self, return_code: Optional[int] = None) -> RocketOverlayError:
        details = self._stderr_tail()
        if details:
            print(f"FFmpeg HDR encoding failed:\n{details}", file=sys.stderr)
        status = "" if return_code is None else f" (exit code {return_code})"
        return RocketOverlayError(
            f"Could not finish the source-matching HDR export{status}."
        )

    def write(self, frame: np.ndarray) -> bool:
        if self._closed:
            raise RocketOverlayError("Cannot write to a closed HDR encoder.")
        if frame.shape != (self.height, self.width, 4):
            raise RocketOverlayError(
                "HDR overlay size does not match the source resolution."
            )
        if frame.dtype != np.uint8:
            raise RocketOverlayError("HDR overlays must use 8-bit BGRA pixels.")
        return_code = self._process.poll()
        if return_code is not None:
            if return_code == 0:
                return False
            raise self._failure(return_code)
        payload = memoryview(np.ascontiguousarray(frame)).cast("B")
        try:
            while payload:
                written = self._process.stdin.write(payload)
                if written is None or written <= 0:
                    raise BrokenPipeError
                payload = payload[written:]
        except (BrokenPipeError, OSError) as exc:
            return_code = self._process.wait()
            if return_code == 0:
                return False
            raise self._failure(return_code) from exc
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        return_code = self._process.wait()
        if return_code != 0:
            error = self._failure(return_code)
            self._stderr.close()
            self._working_output.unlink(missing_ok=True)
            raise error
        self._stderr.close()
        if (
            not self._working_output.exists()
            or self._working_output.stat().st_size == 0
        ):
            self._working_output.unlink(missing_ok=True)
            raise RocketOverlayError(
                "FFmpeg completed without creating an HDR video."
            )
        os.replace(self._working_output, self.output)

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._stderr.close()
        self._working_output.unlink(missing_ok=True)


def mux_original_audio(
    silent_video: Path,
    source_video: Path,
    output: Path,
    crf: int,
    intro_duration_s: float = 0.0,
    keep_audio: bool = True,
    delivery_codec: str = "h264",
) -> bool:
    executable = ffmpeg_executable()
    if executable is None:
        return False

    cmd = [
        executable, "-y",
        "-i", str(silent_video),
    ]
    if keep_audio:
        cmd += [
        "-itsoffset", str(intro_duration_s),
        "-i", str(source_video),
        ]
    cmd += [
        "-map", "0:v:0",
    ]
    if keep_audio:
        cmd += ["-map", "1:a:0?"]
    cmd += [
        # This compatibility helper must never re-encode its video input.
        "-c:v", "copy",
    ]
    if keep_audio:
        cmd += [
        "-c:a", "aac",
        "-b:a", "192k",
        ]
    cmd += [
        "-movflags", "+faststart",
        str(output),
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        print("FFmpeg audio mux failed; keeping silent video.", file=sys.stderr)
        print(result.stderr[-1500:], file=sys.stderr)
        return False
    return True


# ------------------------------- Pipeline -------------------------------- #

def synchronized_camera_frame(
    captures: list[cv2.VideoCapture],
    ignition_times: list[float],
    video_time: float,
    primary_ignition_s: float,
) -> list[Optional[np.ndarray]]:
    """Read camera frames aligned to the primary camera's ignition."""
    frames: list[Optional[np.ndarray]] = []
    for capture, ignition_s in zip(captures, ignition_times):
        source_time = video_time - primary_ignition_s + ignition_s
        if source_time < 0:
            frames.append(None)
            continue
        capture.set(cv2.CAP_PROP_POS_MSEC, source_time * 1000.0)
        ok, frame = capture.read()
        frames.append(frame if ok else None)
    return frames


def camera_layout_frame(
    frames: list[Optional[np.ndarray]],
    mode: str,
    video_time: float,
    ignition_s: float,
    third_switch_delay_s: float,
    camera_crops: Optional[list[tuple[float, float, float]]] = None,
) -> np.ndarray:
    """Combine synchronized sources into one frame for the existing compositor."""
    available_with_index = [
        (index, frame) for index, frame in enumerate(frames) if frame is not None
    ]
    available = [frame for _, frame in available_with_index]
    if not available:
        fail("No synchronized camera frame could be decoded.")
    primary = frames[0] if frames[0] is not None else available[0]
    target_h, target_w = primary.shape[:2]
    crops = camera_crops or [(0.5, 0.5, 1.0)] * len(frames)

    def fitted(frame: np.ndarray, width: int, height: int, index: int) -> np.ndarray:
        x, y, zoom = crops[min(index, len(crops) - 1)]
        return cover_resize_position(frame, width, height, x, y, zoom)

    if mode == "switch":
        if video_time < ignition_s or len(frames) == 1:
            chosen = frames[0]
        elif len(frames) >= 3 and video_time >= ignition_s + third_switch_delay_s:
            chosen = frames[2]
        else:
            chosen = frames[1] if len(frames) >= 2 else frames[0]
        if chosen is None:
            chosen, chosen_index = primary, 0
        else:
            chosen_index = (
                0 if chosen is frames[0] else
                (2 if len(frames) >= 3 and chosen is frames[2] else 1)
            )
        return fitted(chosen, target_w, target_h, chosen_index)
    if mode == "split":
        active = available_with_index[:3]
        if len(active) == 1:
            index, frame = active[0]
            return fitted(frame, target_w, target_h, index)
        cell_w = target_w // len(active)
        canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        for cell_index, (camera_index, frame) in enumerate(active):
            x = cell_index * cell_w
            width = target_w - x if cell_index == len(active) - 1 else cell_w
            canvas[:, x:x + width] = fitted(
                frame, width, target_h, camera_index
            )
        return canvas
    if mode == "pip":
        canvas = fitted(primary, target_w, target_h, 0)
        inset_w, inset_h = int(target_w * .30), int(target_h * .30)
        margin = max(8, int(min(target_w, target_h) * .025))
        for index, frame in enumerate(frames[1:3]):
            if frame is None:
                continue
            inset = fitted(frame, inset_w, inset_h, index + 1)
            x = target_w - inset_w - margin
            y = margin + index * (inset_h + margin)
            cv2.rectangle(
                canvas, (x - 3, y - 3), (x + inset_w + 3, y + inset_h + 3),
                (240, 245, 250), 3,
            )
            canvas[y:y + inset_h, x:x + inset_w] = inset
        return canvas
    return fitted(primary, target_w, target_h, 0)


def load_rotpl_template(
    cfg: Config,
) -> tuple[Optional[RotplPackage], Optional[RotplRenderer]]:
    """Load and identity-check the registry-resolved template for a job.

    The web layer must resolve ``template_id`` + ``template_version`` and
    compare the selected registry SHA before placing the immutable
    ``files_path`` in ``cfg.template_path``.  This function repeats the
    identity check against the package manifest.  When a direct ``.rotpl``
    archive is supplied it also verifies its bytes against
    ``cfg.template_sha256``.
    """
    if cfg.template_path is None or not str(cfg.template_path).strip():
        return None, None
    template_path = Path(cfg.template_path).resolve()
    if not template_path.exists():
        fail(f"Template package was not found: {template_path}")
    expected_sha = (cfg.template_sha256 or "").strip().lower()
    if expected_sha and template_path.is_file():
        digest = hashlib.sha256()
        try:
            with template_path.open("rb") as package_file:
                for chunk in iter(lambda: package_file.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            fail(f"Could not verify template package: {exc}")
        if digest.hexdigest() != expected_sha:
            fail("Template package SHA-256 does not match the selected version.")
    try:
        package = RotplPackage.open(template_path)
    except RotplError as exc:
        fail(f"Could not load ROTPL template: {exc}")
    assert package is not None
    if cfg.template_id and package.template_id != cfg.template_id:
        package.close()
        fail(
            "Template ID does not match the selected package "
            f"({cfg.template_id} != {package.template_id})."
        )
    if cfg.template_version and package.template_version != cfg.template_version:
        package.close()
        fail(
            "Template version does not match the selected package "
            f"({cfg.template_version} != {package.template_version})."
        )
    return package, RotplRenderer(package)


def iter_kept_video_times(
    cut_list: Optional[list[tuple[float, float]]], fps: float
) -> Iterator[float]:
    """Yield the source video_t for each output frame, in output order.

    ``cut_list`` must already be clamped/validated kept ranges (see
    ``render()``). With no cut list this reproduces the render loop's
    long-standing unbounded ``idx / fps`` sequence - the caller stops
    consuming once decoding/seeking fails. With a cut list, only source
    times inside the kept ranges are yielded; the caller still derives
    telemetry_t from each yielded (uncut) video_t, so pressure/thrust
    readings never lose their real timestamp across a cut.
    """
    if not cut_list:
        index = 0
        while True:
            yield index / fps
            index += 1
    for start, end in cut_list:
        frame_total = int(round((end - start) * fps))
        for i in range(frame_total):
            yield start + i / fps


def kept_duration_frames(
    cut_list: Optional[list[tuple[float, float]]], fps: float, frame_count: int
) -> int:
    """Total output frames once ``cut_list`` is applied (``frame_count`` otherwise)."""
    if not cut_list:
        return frame_count
    return sum(int(round((end - start) * fps)) for start, end in cut_list)


def render(
    cfg: Config,
    progress: Optional[Callable[[int, str], None]] = None,
) -> Path:
    validate_config_values(dataclasses.asdict(cfg))

    def report(percent: int, message: str) -> None:
        if progress:
            progress(percent, message)

    if not cfg.video.exists():
        fail(f"Video not found: {cfg.video}")
    for camera_path in (cfg.video_2, cfg.video_3):
        if camera_path is not None and not camera_path.exists():
            fail(f"Camera video not found: {camera_path}")
    if cfg.camera_mode not in {"single", "switch", "split", "pip"}:
        fail(f"Unsupported camera mode: {cfg.camera_mode}")
    if not cfg.data.exists():
        fail(f"Telemetry file not found: {cfg.data}")

    cfg.output.parent.mkdir(parents=True, exist_ok=True)

    report(1, "Reading telemetry")
    telemetry = read_telemetry(cfg)
    metrics = calculate_metrics(telemetry)
    has_thrust = telemetry_has_thrust(telemetry)
    print(
        f"Telemetry: {len(telemetry):,} rows | "
        f"{telemetry['time'].min():.3f}s to {telemetry['time'].max():.3f}s"
    )

    report(3, "Opening video")
    cap = cv2.VideoCapture(str(cfg.video))
    if not cap.isOpened():
        fail(f"Could not open video: {cfg.video}")
    camera_paths = [cfg.video] + [
        path for path in (cfg.video_2, cfg.video_3) if path is not None
    ]
    captures = [cap]
    for path in camera_paths[1:]:
        camera_capture = cv2.VideoCapture(str(path))
        if not camera_capture.isOpened():
            for opened_capture in captures:
                opened_capture.release()
            fail(f"Could not open camera video: {path}")
        captures.append(camera_capture)
    ignition_times = [cfg.ignition_video_s]
    if cfg.video_2 is not None:
        ignition_times.append(cfg.camera_2_ignition_s)
    if cfg.video_3 is not None:
        ignition_times.append(cfg.camera_3_ignition_s)
    multi_camera = len(captures) > 1 and cfg.camera_mode != "single"
    camera_crops = [
        (cfg.crop_x, cfg.crop_y, cfg.crop_zoom),
    ]
    if cfg.video_2 is not None:
        camera_crops.append((
            cfg.camera_2_crop_x, cfg.camera_2_crop_y, cfg.camera_2_crop_zoom
        ))
    if cfg.video_3 is not None:
        camera_crops.append((
            cfg.camera_3_crop_x, cfg.camera_3_crop_y, cfg.camera_3_crop_zoom
        ))
    compose_cfg = (
        dataclasses.replace(cfg, crop_x=0.5, crop_y=0.5, crop_zoom=1.0)
        if multi_camera else cfg
    )

    capture_fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if not capture_fps or capture_fps <= 0:
        fail("Video FPS could not be determined.")
    nominal_fps = probe_nominal_fps(cfg.video)
    # Some containers expose a field/display rate (for example 50 tbr for
    # 25 fps interlaced video). Only accept the probe when it is close enough
    # to OpenCV's decoded cadence to be the same rate rather than a multiple.
    if nominal_fps and abs(nominal_fps - capture_fps) / capture_fps <= 0.10:
        fps = nominal_fps
    else:
        fps = capture_fps
    duration = frame_count / fps if frame_count > 0 else 0.0

    # A cut list is SDR-only (like intro/outro), so it's resolved against the
    # real duration up front - clamped to bounds, empty/invalid ranges
    # dropped - and threaded through as the single source of truth for which
    # frames get rendered, for every camera, the audio, and telemetry alike.
    kept_cut_list: Optional[list[tuple[float, float]]] = None
    if cfg.cut_list:
        kept_cut_list = []
        for start, end in cfg.cut_list:
            clamped_start = max(0.0, min(start, duration))
            clamped_end = max(0.0, min(end, duration))
            if clamped_end > clamped_start:
                kept_cut_list.append((clamped_start, clamped_end))
        if not kept_cut_list:
            for opened_capture in captures:
                opened_capture.release()
            fail("Cut list leaves no video to render.")

    source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    hlg_main10_source = source_is_hlg_main10(cfg.video)
    preserve_hlg_source = (
        cfg.preserve_source_quality
        and hlg_main10_source
        and not multi_camera
        and cfg.camera_mode == "single"
        and cfg.width == source_width
        and cfg.height == source_height
        and not cfg.enhance_video
        and not cfg.denoise_video
        and not cfg.stabilize_video
        and abs(cfg.crop_zoom - 1.0) <= 1e-6
        and cfg.intro_duration_s <= 0
        and cfg.outro_duration_s <= 0
        and not kept_cut_list
    )
    if cfg.preserve_source_quality and hlg_main10_source and not preserve_hlg_source:
        for opened_capture in captures:
            opened_capture.release()
        fail(
            "HDR source-quality matching requires source resolution, a single "
            "camera, and no crop, zoom, enhancement, stabilization, intro/outro, "
            "or cut list."
        )

    # The Broadcast Edition compositor is now the delivery source of truth.
    # It accepts the richer web Config by duck typing while the surrounding
    # pipeline continues to provide multi-camera sync, enhancement and audio.
    broadcast_assets = broadcast_overlay.build_assets(cfg, telemetry)

    logo = None
    if cfg.logo:
        if not cfg.logo.exists():
            fail(f"Logo file not found: {cfg.logo}")
        logo = cv2.imread(str(cfg.logo), cv2.IMREAD_UNCHANGED)
        if logo is None:
            fail(f"Could not read logo image: {cfg.logo}")

    rotpl_package, template_renderer = load_rotpl_template(cfg)

    delivery_description = (
        "lossless HEVC Main10 HLG source-preserving delivery"
        if preserve_hlg_source
        else f"one-pass {cfg.delivery_codec.upper()} SDR delivery"
    )
    print(
        f"Rendering {frame_count:,} frames at {fps:.3f} FPS "
        f"({duration:.2f}s) with {delivery_description}..."
    )

    idx = 0
    last_percent = -1
    total_output_frames = kept_duration_frames(kept_cut_list, fps, frame_count)
    frame_times = iter_kept_video_times(kept_cut_list, fps)
    intro_frames = int(round(cfg.intro_duration_s * fps))
    outro_frames = int(round(cfg.outro_duration_s * fps))
    previous_gray: Optional[np.ndarray] = None
    thumbnail_frame: Optional[np.ndarray] = None
    thumbnail_pressure = float("-inf")
    writer: Optional[FFmpegRawVideoWriter | FFmpegHLGOverlayWriter] = None

    try:
        if preserve_hlg_source:
            writer = FFmpegHLGOverlayWriter(
                output=cfg.output,
                source_video=cfg.video,
                width=cfg.width,
                height=cfg.height,
                fps=fps,
                keep_audio=cfg.keep_audio,
            )
        else:
            writer = FFmpegRawVideoWriter(
                output=cfg.output,
                source_video=cfg.video,
                width=cfg.width,
                height=cfg.height,
                fps=fps,
                crf=cfg.crf,
                intro_duration_s=cfg.intro_duration_s,
                keep_audio=cfg.keep_audio,
                delivery_codec=cfg.delivery_codec,
                cut_list=kept_cut_list,
            )
        if intro_frames:
            intro = make_title_frame(cfg, logo)
            for _ in range(intro_frames):
                writer.write(intro)
        while True:
            try:
                video_t = next(frame_times)
            except StopIteration:
                break
            if preserve_hlg_source:
                ok = cap.grab()
                if not ok:
                    break
                frame = None
            elif multi_camera:
                if total_output_frames > 0 and idx >= total_output_frames:
                    break
                camera_frames = synchronized_camera_frame(
                    captures, ignition_times, video_t, cfg.ignition_video_s
                )
                if camera_frames[0] is None:
                    break
                frame = camera_layout_frame(
                    camera_frames, cfg.camera_mode, video_t,
                    cfg.ignition_video_s, cfg.camera_3_switch_delay_s,
                    camera_crops,
                )
            else:
                if kept_cut_list is not None:
                    cap.set(cv2.CAP_PROP_POS_MSEC, video_t * 1000.0)
                ok, frame = cap.read()
                if not ok:
                    break
            if not preserve_hlg_source and cfg.stabilize_video:
                assert frame is not None
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
                if previous_gray is not None:
                    shift, response = cv2.phaseCorrelate(previous_gray, gray)
                    if response > 0.15 and abs(shift[0]) < frame.shape[1] * 0.08 \
                            and abs(shift[1]) < frame.shape[0] * 0.08:
                        matrix = np.float32([
                            [1, 0, -shift[0]],
                            [0, 1, -shift[1]],
                        ])
                        frame = cv2.warpAffine(
                            frame, matrix, (frame.shape[1], frame.shape[0]),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REFLECT,
                        )
                previous_gray = gray

            telemetry_t = video_t - cfg.ignition_video_s
            p, f = interpolate_telemetry(telemetry, telemetry_t)
            template_context = (
                TemplateContext.from_runtime(
                    cfg,
                    broadcast_assets,
                    telemetry_t,
                    p,
                    f,
                    telemetry=telemetry,
                    metrics=metrics,
                )
                if template_renderer is not None
                else None
            )

            if preserve_hlg_source:
                try:
                    overlay_frame = (
                        template_renderer.render_bgra(
                            template_context,
                            output_size=(cfg.width, cfg.height),
                        )
                        if template_renderer is not None
                        else broadcast_overlay.compose_overlay_bgra(
                            cfg, broadcast_assets, telemetry_t, p, f,
                        )
                    )
                except RotplError as exc:
                    fail(f"Could not render ROTPL template: {exc}")
                if writer.write(overlay_frame) is False:
                    break
                if cfg.thumbnail and p > thumbnail_pressure:
                    retrieved, thumbnail_source = cap.retrieve()
                    if retrieved:
                        thumbnail_pressure = p
                        if template_renderer is not None:
                            try:
                                thumbnail_frame = template_renderer.compose_bgr(
                                    cover_resize(
                                        prepare_video_frame(
                                            thumbnail_source, compose_cfg
                                        ),
                                        cfg.width,
                                        cfg.height,
                                    ),
                                    template_context,
                                )
                            except RotplError as exc:
                                fail(f"Could not render ROTPL thumbnail: {exc}")
                        else:
                            thumbnail_frame = broadcast_overlay.compose_frame(
                                thumbnail_source, cfg, broadcast_assets,
                                telemetry_t, p, f,
                            )
            else:
                assert frame is not None
                prepared_frame = prepare_video_frame(frame, compose_cfg)
                try:
                    output_frame = (
                        template_renderer.compose_bgr(
                            cover_resize(prepared_frame, cfg.width, cfg.height),
                            template_context,
                        )
                        if template_renderer is not None
                        else broadcast_overlay.compose_frame(
                            prepared_frame, cfg, broadcast_assets,
                            telemetry_t, p, f,
                        )
                    )
                except RotplError as exc:
                    fail(f"Could not render ROTPL template: {exc}")
                writer.write(output_frame)
                if p > thumbnail_pressure:
                    thumbnail_pressure = p
                    thumbnail_frame = output_frame.copy()

            idx += 1
            if total_output_frames > 0:
                percent = int(idx * 100 / total_output_frames)
                if percent != last_percent and percent % 5 == 0:
                    print(f"  {percent:3d}%")
                    report(
                        5 + int(percent * 0.88),
                        f"Rendering frame {idx:,} of {total_output_frames:,}",
                    )
                    last_percent = percent

        if idx == 0:
            fail("No video frames were decoded.")
        if outro_frames:
            outro = make_title_frame(cfg, logo, metrics)
            for _ in range(outro_frames):
                writer.write(outro)

        report(
            94,
            "Finalizing lossless Main10 HLG video and original audio"
            if preserve_hlg_source
            else "Finalizing high-quality video and audio",
        )
        writer.close()
    except BaseException:
        if writer is not None:
            writer.abort()
        raise
    finally:
        for opened_capture in captures:
            opened_capture.release()
        if rotpl_package is not None:
            rotpl_package.close()

    print(
        "Done with lossless source-quality HLG encoding: "
        if preserve_hlg_source
        else "Done with high-quality delivery encoding: ",
        cfg.output,
        sep="",
    )
    summary_path = cfg.output.with_suffix(".json")
    summary: dict[str, Any] = dataclasses.asdict(metrics)
    summary["template"] = (
        {
            "id": cfg.template_id,
            "version": cfg.template_version,
            "sha256": cfg.template_sha256,
            "renderer": "opencv-declarative-v1",
        }
        if template_renderer is not None
        else {
            "id": cfg.broadcast_theme,
            "version": "built-in",
            "renderer": "broadcast-overlay",
        }
    )
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if cfg.thumbnail:
        thumb = (
            thumbnail_frame
            if thumbnail_frame is not None
            else make_title_frame(cfg, logo, metrics)
        )
        cv2.imwrite(
            str(cfg.output.with_name(cfg.output.stem + "_thumbnail.jpg")),
            thumb,
        )
    report(100, "Complete")
    return cfg.output


# ---------------------------------- CLI ---------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Create a professional synchronized rocket-test video "
            "with pressure and thrust telemetry."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument("--config", type=Path, help="Optional YAML config file.")
    p.add_argument("--video", type=Path, help="Input test video.")
    p.add_argument("--data", type=Path, help="Excel or CSV telemetry.")
    p.add_argument("--output", type=Path, help="Output MP4 path.")

    p.add_argument("--sheet", type=parse_sheet, default=0)
    p.add_argument("--time-column")
    p.add_argument("--pressure-column")
    p.add_argument("--thrust-column")

    p.add_argument(
        "--ignition-video-s", type=float, default=0.0,
        help="Video timestamp at which ignition occurs."
    )
    p.add_argument(
        "--telemetry-zero-s", type=float,
        help="Telemetry timestamp to treat as t=0. Defaults to first row."
    )
    p.add_argument(
        "--time-scale", type=float, default=1.0,
        help="Multiply telemetry time by this value (e.g. 0.001 for ms to s)."
    )

    p.add_argument("--title", default="ROCKET MOTOR STATIC TEST")
    p.add_argument("--subtitle", default="Pressure & Derived Thrust")
    p.add_argument("--pressure-unit", default="bar")
    p.add_argument("--thrust-unit", default="N")
    p.add_argument("--logo", type=Path)

    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--video-fraction", type=float, default=0.70)
    p.add_argument("--chart-window-before-s", type=float, default=1.0)
    p.add_argument("--chart-window-after-s", type=float, default=1.0)
    p.add_argument("--test-date", default="")
    p.add_argument("--motor-type", default="")
    p.add_argument("--propellant", default="")
    p.add_argument("--run-number", default="")
    p.add_argument("--organization-name", default="STELLAR KINETICS")
    p.add_argument("--test-site", default="ETLAQ SPACEPORT - DUQM, OMAN")
    p.add_argument("--coordinates-text", default="")
    p.add_argument("--oxidizer", default="")
    p.add_argument("--fuel", default="")
    p.add_argument("--ablative-material", default="")
    p.add_argument(
        "--footer-tagline",
        default="STELLAR KINETICS - ENGINEERING THE VOID",
    )
    p.add_argument("--camera-label", default="CAM 01")
    p.add_argument("--capture-fps", default="120 FPS")
    p.add_argument(
        "--output-style",
        choices=("engineering", "presentation", "archive", "social"),
        default="engineering",
    )
    p.add_argument("--intro-duration-s", type=float, default=0.0)
    p.add_argument("--outro-duration-s", type=float, default=2.5)
    p.add_argument("--crop-zoom", type=float, default=1.0)
    p.add_argument("--crop-x", type=float, default=0.5)
    p.add_argument("--crop-y", type=float, default=0.5)
    p.add_argument("--pressure-limit", type=float)
    p.add_argument("--accent", default="#38BDF8")
    p.add_argument(
        "--broadcast-theme",
        choices=("launch", "mission_control", "stellar_console", "range_line"),
        default="launch",
    )
    p.add_argument("--no-chart", action="store_true")
    p.add_argument("--no-phases", action="store_true")
    p.add_argument("--no-chart-reveal", action="store_true")
    enhancement = p.add_mutually_exclusive_group()
    enhancement.add_argument(
        "--enhance-video", dest="enhance_video", action="store_true",
        default=False,
        help="Apply optional local-contrast enhancement to decoded frames.",
    )
    enhancement.add_argument(
        "--no-enhance-video", dest="no_enhance_video", action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    p.add_argument("--denoise-video", action="store_true")
    p.add_argument("--stabilize-video", action="store_true")
    p.add_argument("--no-thumbnail", action="store_true")

    p.add_argument(
        "--no-audio", action="store_true",
        help="Do not attempt to keep the original audio."
    )
    p.add_argument("--codec", default="mp4v")
    p.add_argument("--crf", type=int, default=15)
    p.add_argument("--delivery-codec", choices=("h264", "h265"), default="h264")
    delivery_mode = p.add_mutually_exclusive_group()
    delivery_mode.add_argument(
        "--archive-hdr", action="store_true",
        help=(
            "Archive a compatible 10-bit HLG source at its original size "
            "using lossless HEVC Main10. Creates a very large file."
        ),
    )
    delivery_mode.add_argument(
        "--compatibility-sdr", action="store_true",
        help=(
            "Explicitly select the default high-quality SDR Rec.709 "
            "delivery mode (kept for backward compatibility)."
        ),
    )
    p.add_argument("--template-id", help="Exact installed ROTPL template ID.")
    p.add_argument("--template-version", help="Exact installed ROTPL version.")
    p.add_argument(
        "--template-sha256",
        help="Registry SHA-256 for the exact selected ROTPL package.",
    )
    p.add_argument(
        "--template-path",
        type=Path,
        help="Registry-resolved ROTPL archive or immutable files directory.",
    )

    return p


def config_from_args(args: argparse.Namespace) -> Config:
    values = vars(args).copy()
    config_path = values.pop("config", None)

    base: dict = {}
    if config_path:
        base = load_yaml(config_path)
        config_dir = config_path.resolve().parent
        for field in ("video", "data", "output", "logo", "template_path"):
            value = base.get(field)
            if value and not Path(value).is_absolute():
                base[field] = config_dir / value

    # CLI values override YAML only when explicitly provided.
    defaults = vars(build_parser().parse_args([]))
    defaults.pop("config", None)

    for key, value in values.items():
        if key == "no_audio":
            if value:
                base["keep_audio"] = False
            elif "keep_audio" not in base:
                base["keep_audio"] = True
            continue
        if key == "archive_hdr":
            if value:
                base["preserve_source_quality"] = True
            elif "preserve_source_quality" not in base:
                base["preserve_source_quality"] = False
            continue
        boolean_inversions = {
            "compatibility_sdr": "preserve_source_quality",
            "no_chart_reveal": "reveal_chart",
            "no_enhance_video": "enhance_video",
            "no_thumbnail": "thumbnail",
            "no_chart": "show_chart",
            "no_phases": "show_phases",
        }
        if key in boolean_inversions:
            target = boolean_inversions[key]
            if value:
                base[target] = False
            elif target not in base:
                # Keep command-line defaults aligned with Config. In
                # particular, SDR Rec.709 is now the normal delivery mode;
                # source-preserving HDR remains opt-in through configuration.
                base[target] = getattr(Config, target)
            continue

        if value is not None and (
            key not in defaults or value != defaults[key] or key not in base
        ):
            base[key] = value

    required = ("video", "data", "output")
    missing = [name for name in required if not base.get(name)]
    if missing:
        fail(
            "Missing required options: "
            + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        )

    path_fields = ("video", "data", "output", "logo", "template_path")
    for field in path_fields:
        if base.get(field) is not None:
            base[field] = Path(base[field])

    if "sheet" in base and isinstance(base["sheet"], str):
        base["sheet"] = parse_sheet(base["sheet"])

    base["chart_fraction"] = 1.0 - float(base.get("video_fraction", 0.70))
    validate_config_values(base)
    return Config(**base)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        cfg = config_from_args(args)
        render(cfg)
    except RocketOverlayError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
