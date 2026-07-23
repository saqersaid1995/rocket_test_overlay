
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
import math
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import cv2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from matplotlib.backends.backend_agg import FigureCanvasAgg


# ----------------------------- Configuration ----------------------------- #

@dataclass
class Config:
    video: Path
    data: Path
    output: Path

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
    video_fraction: float = 0.62
    chart_fraction: float = 0.38

    chart_window_before_s: float = 1.0
    chart_window_after_s: float = 1.0

    logo: Optional[Path] = None
    keep_audio: bool = True
    codec: str = "mp4v"
    crf: int = 18


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

def fail(message: str, code: int = 2) -> "None":
    print(f"\nERROR: {message}\n", file=sys.stderr)
    raise SystemExit(code)


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
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        fail("YAML config must contain a mapping/object at the top level.")
    return data


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
    f_col = resolve_column(df, cfg.thrust_column, "thrust")

    clean = pd.DataFrame({
        "time": pd.to_numeric(df[t_col], errors="coerce"),
        "pressure": pd.to_numeric(df[p_col], errors="coerce"),
        "thrust": pd.to_numeric(df[f_col], errors="coerce"),
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
        zero = float(clean["time"].iloc[0])

    clean["time"] = (clean["time"] - zero) * cfg.time_scale

    if not np.all(np.diff(clean["time"].to_numpy()) > 0):
        fail("Telemetry time values must be strictly increasing after cleanup.")

    return clean.reset_index(drop=True)


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
    h, w = frame.shape[:2]
    scale = max(target_w / w, target_h / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
    x0 = max(0, (nw - target_w) // 2)
    y0 = max(0, (nh - target_h) // 2)
    return resized[y0:y0 + target_h, x0:x0 + target_w]


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


def render_chart_asset(
    telemetry: pd.DataFrame,
    width: int,
    height: int,
    cfg: Config,
) -> ChartAsset:
    times = telemetry["time"].to_numpy(dtype=float)
    pressure = telemetry["pressure"].to_numpy(dtype=float)
    thrust = telemetry["thrust"].to_numpy(dtype=float)

    t_min = float(times.min()) - cfg.chart_window_before_s
    t_max = float(times.max()) + cfg.chart_window_after_s
    p_min, p_max = finite_range(pressure)
    f_min, f_max = finite_range(thrust)

    dpi = 120
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    fig.patch.set_facecolor("#0B1017")

    ax = fig.add_axes([0.12, 0.20, 0.75, 0.65])
    ax2 = ax.twinx()

    ax.set_facecolor("#0B1017")
    ax.plot(times, pressure, linewidth=2.4, color="#38BDF8", label="Pressure")
    ax2.plot(times, thrust, linewidth=2.4, color="#FB7185", label="Thrust")

    ax.set_xlim(t_min, t_max)
    ax.set_ylim(p_min, p_max)
    ax2.set_ylim(f_min, f_max)

    ax.set_xlabel("Time from ignition (s)", color="#CBD5E1", fontsize=10)
    ax.set_ylabel(f"Pressure ({cfg.pressure_unit})", color="#38BDF8", fontsize=10)
    ax2.set_ylabel(f"Thrust ({cfg.thrust_unit})", color="#FB7185", fontsize=10)

    ax.tick_params(colors="#94A3B8", labelsize=8)
    ax2.tick_params(colors="#94A3B8", labelsize=8)
    ax.grid(True, alpha=0.18, color="#64748B", linewidth=0.8)

    for spine in ax.spines.values():
        spine.set_color("#334155")
    for spine in ax2.spines.values():
        spine.set_color("#334155")

    p_idx = int(np.nanargmax(pressure))
    f_idx = int(np.nanargmax(thrust))

    ax.scatter(
        [times[p_idx]], [pressure[p_idx]], s=36, color="#38BDF8",
        edgecolors="white", linewidths=0.8, zorder=5
    )
    ax2.scatter(
        [times[f_idx]], [thrust[f_idx]], s=36, color="#FB7185",
        edgecolors="white", linewidths=0.8, zorder=5
    )

    fig.text(
        0.04, 0.94, cfg.subtitle,
        color="white", fontsize=14, fontweight="bold", ha="left", va="top"
    )
    fig.text(
        0.04, 0.885,
        f"Peak pressure  {pressure[p_idx]:.2f} {cfg.pressure_unit}"
        f"    |    Peak thrust  {thrust[f_idx]:.1f} {cfg.thrust_unit}",
        color="#94A3B8", fontsize=8.5, ha="left", va="top"
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

    if t <= times[0]:
        return float(p[0]), float(f[0])
    if t >= times[-1]:
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

    # Current-time cursor.
    cv2.line(
        img, (x, asset.plot_y0), (x, asset.plot_y1),
        (245, 245, 245), 2, cv2.LINE_AA
    )

    # Current points.
    cv2.circle(img, (x, py), 7, (248, 189, 56), -1, cv2.LINE_AA)
    cv2.circle(img, (x, py), 10, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.circle(img, (x, fy), 7, (133, 113, 251), -1, cv2.LINE_AA)
    cv2.circle(img, (x, fy), 10, (255, 255, 255), 1, cv2.LINE_AA)

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
    draw_text(canvas, label.upper(), (x + 24, y + 31), 0.55, (148, 163, 184), 1)
    draw_text(canvas, value, (x + 24, y + 78), 1.05, (255, 255, 255), 2)


def compose_frame(
    video_frame: np.ndarray,
    chart: np.ndarray,
    cfg: Config,
    telemetry_time: float,
    pressure: float,
    thrust: float,
    video_time: float,
    logo: Optional[np.ndarray],
) -> np.ndarray:
    W, H = cfg.width, cfg.height
    left_w = int(round(W * cfg.video_fraction))
    right_w = W - left_w

    canvas = np.zeros((H, W, 3), dtype=np.uint8)
    canvas[:] = (11, 16, 23)

    # Video side.
    video_area = cover_resize(video_frame, left_w, H)
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

    chart_h = int(H * 0.70)
    chart_resized = cv2.resize(chart, (right_w, chart_h), interpolation=cv2.INTER_AREA)
    canvas[105:105 + chart_h, left_w:W] = chart_resized

    # Header.
    draw_text(canvas, cfg.title, (42, 62), 1.05, (255, 255, 255), 2)
    draw_text(
        canvas,
        f"VIDEO  {video_time:06.2f}s    |    TELEMETRY  {telemetry_time:+06.2f}s",
        (44, 103), 0.55, (203, 213, 225), 1
    )

    # Live metric cards.
    card_y = 850
    gap = 18
    card_w = (right_w - 64 - gap) // 2
    card_h = 112
    draw_metric_card(
        canvas, left_w + 28, card_y, card_w, card_h,
        "Pressure", f"{pressure:,.2f} {cfg.pressure_unit}", (248, 189, 56)
    )
    draw_metric_card(
        canvas, left_w + 28 + card_w + gap, card_y, card_w, card_h,
        "Thrust", f"{thrust:,.1f} {cfg.thrust_unit}", (133, 113, 251)
    )

    # Ignition status.
    status = "PRE-IGNITION" if telemetry_time < 0 else "TEST ACTIVE"
    status_color = (100, 116, 139) if telemetry_time < 0 else (74, 222, 128)
    sw, _ = text_size(status, 0.58, 2)
    cv2.rectangle(
        canvas, (44, H - 74), (44 + sw + 32, H - 28), (15, 23, 42), -1
    )
    draw_text(canvas, status, (60, H - 42), 0.58, status_color, 2)

    if logo is not None:
        max_w, max_h = 220, 90
        lh, lw = logo.shape[:2]
        scale = min(max_w / lw, max_h / lh, 1.0)
        resized = cv2.resize(
            logo, (int(lw * scale), int(lh * scale)),
            interpolation=cv2.INTER_AREA
        )
        alpha_blend(canvas, resized, left_w - resized.shape[1] - 28, 26, 0.92)

    return canvas


# ------------------------------- FFmpeg --------------------------------- #

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def mux_original_audio(
    silent_video: Path,
    source_video: Path,
    output: Path,
    crf: int,
) -> bool:
    if not ffmpeg_available():
        return False

    cmd = [
        "ffmpeg", "-y",
        "-i", str(silent_video),
        "-i", str(source_video),
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
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

def render(cfg: Config) -> None:
    if not cfg.video.exists():
        fail(f"Video not found: {cfg.video}")
    if not cfg.data.exists():
        fail(f"Telemetry file not found: {cfg.data}")

    cfg.output.parent.mkdir(parents=True, exist_ok=True)

    telemetry = read_telemetry(cfg)
    print(
        f"Telemetry: {len(telemetry):,} rows | "
        f"{telemetry['time'].min():.3f}s to {telemetry['time'].max():.3f}s"
    )

    cap = cv2.VideoCapture(str(cfg.video))
    if not cap.isOpened():
        fail(f"Could not open video: {cfg.video}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if not fps or fps <= 0:
        fail("Video FPS could not be determined.")
    duration = frame_count / fps if frame_count > 0 else 0.0

    left_w = int(round(cfg.width * cfg.video_fraction))
    right_w = cfg.width - left_w
    chart_asset = render_chart_asset(
        telemetry=telemetry,
        width=right_w,
        height=int(cfg.height * 0.70),
        cfg=cfg,
    )

    logo = None
    if cfg.logo:
        if not cfg.logo.exists():
            fail(f"Logo file not found: {cfg.logo}")
        logo = cv2.imread(str(cfg.logo), cv2.IMREAD_UNCHANGED)
        if logo is None:
            fail(f"Could not read logo image: {cfg.logo}")

    with tempfile.TemporaryDirectory(prefix="rocket_overlay_") as tmpdir:
        silent_path = Path(tmpdir) / "silent_output.mp4"

        fourcc = cv2.VideoWriter_fourcc(*cfg.codec)
        writer = cv2.VideoWriter(
            str(silent_path), fourcc, fps, (cfg.width, cfg.height)
        )
        if not writer.isOpened():
            fail(
                "Could not create output video. Try codec 'mp4v' "
                "or install a compatible OpenCV build."
            )

        print(
            f"Rendering {frame_count:,} frames at {fps:.3f} FPS "
            f"({duration:.2f}s)..."
        )

        idx = 0
        last_percent = -1

        while True:
            ok, frame = cap.read()
            if not ok:
                break

            video_t = idx / fps
            telemetry_t = video_t - cfg.ignition_video_s
            p, f = interpolate_telemetry(telemetry, telemetry_t)

            dynamic_chart = chart_frame(
                chart_asset, telemetry_t, p, f
            )
            output_frame = compose_frame(
                frame, dynamic_chart, cfg,
                telemetry_time=telemetry_t,
                pressure=p,
                thrust=f,
                video_time=video_t,
                logo=logo,
            )
            writer.write(output_frame)

            idx += 1
            if frame_count > 0:
                percent = int(idx * 100 / frame_count)
                if percent != last_percent and percent % 5 == 0:
                    print(f"  {percent:3d}%")
                    last_percent = percent

        writer.release()
        cap.release()

        if idx == 0:
            fail("No video frames were decoded.")

        if cfg.keep_audio and mux_original_audio(
            silent_path, cfg.video, cfg.output, cfg.crf
        ):
            print(f"Done with original audio: {cfg.output}")
        else:
            shutil.copy2(silent_path, cfg.output)
            print(f"Done: {cfg.output}")
            if cfg.keep_audio and not ffmpeg_available():
                print(
                    "Note: FFmpeg was not found, so the output has no audio. "
                    "Install FFmpeg and run again to preserve audio."
                )


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
    p.add_argument("--video-fraction", type=float, default=0.62)
    p.add_argument("--chart-window-before-s", type=float, default=1.0)
    p.add_argument("--chart-window-after-s", type=float, default=1.0)

    p.add_argument(
        "--no-audio", action="store_true",
        help="Do not attempt to keep the original audio."
    )
    p.add_argument("--codec", default="mp4v")
    p.add_argument("--crf", type=int, default=18)

    return p


def config_from_args(args: argparse.Namespace) -> Config:
    values = vars(args).copy()
    config_path = values.pop("config", None)

    base: dict = {}
    if config_path:
        base = load_yaml(config_path)

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

    path_fields = ("video", "data", "output", "logo")
    for field in path_fields:
        if base.get(field) is not None:
            base[field] = Path(base[field])

    if "sheet" in base and isinstance(base["sheet"], str):
        base["sheet"] = parse_sheet(base["sheet"])

    if not 0.45 <= float(base.get("video_fraction", 0.62)) <= 0.80:
        fail("--video-fraction must be between 0.45 and 0.80.")

    if int(base.get("width", 1920)) < 960 or int(base.get("height", 1080)) < 540:
        fail("Output resolution is too small. Use at least 960x540.")

    base["chart_fraction"] = 1.0 - float(base.get("video_fraction", 0.62))
    return Config(**base)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    cfg = config_from_args(args)
    render(cfg)


if __name__ == "__main__":
    main()
