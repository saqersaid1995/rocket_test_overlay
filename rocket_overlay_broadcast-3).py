#!/usr/bin/env python3
"""
Rocket Test Video Overlay — Broadcast Edition
Full-bleed test video with a launch-broadcast style telemetry overlay:
top mission identity + status, bottom band with arc gauges, T+ clock,
phase bar and a translucent live pressure chart.

Usage is identical to the previous version:
    python rocket_overlay.py --config config.yaml
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
from typing import Optional

import cv2
import numpy as np
import pandas as pd
import yaml


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
    subtitle: str = "STATIC FIRE TEST"
    pressure_unit: str = "bar"
    thrust_unit: str = "N"

    width: int = 1920
    height: int = 1080

    accent: str = "#38BDF8"
    template: str = "a"
    show_chart: bool = True
    show_phases: bool = True

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

WHITE = (255, 255, 255)
GRAY = (184, 163, 148)          # BGR of #94A3B8
GREEN = (128, 222, 74)          # BGR of #4ADE80
YELLOW = (21, 204, 250)         # BGR of #FACC15
FONT = cv2.FONT_HERSHEY_DUPLEX
FONT_MONO = cv2.FONT_HERSHEY_SIMPLEX


def hex_to_bgr(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) != 6:
        fail(f"Invalid hex color: {value}")
    r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    return (b, g, r)


# ------------------------------- Utilities ------------------------------- #

def fail(message: str, code: int = 2) -> None:
    print(f"\nERROR: {message}\n", file=sys.stderr)
    raise SystemExit(code)


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
    if x >= dst.shape[1] or y >= dst.shape[0] or x + w <= 0 or y + h <= 0:
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


def draw_text(img, text, xy, scale, color=WHITE, thickness=1, font=FONT):
    cv2.putText(img, text, xy, font, scale, color, thickness, cv2.LINE_AA)


def text_size(text, scale, thickness=1, font=FONT):
    (w, h), _ = cv2.getTextSize(text, font, scale, thickness)
    return w, h


def draw_spaced_text(img, text, xy, scale, color, thickness=1, spacing=6, font=FONT):
    """Letter-spaced uppercase label, broadcast style. Returns total width."""
    x, y = xy
    for ch in text:
        draw_text(img, ch, (x, y), scale, color, thickness, font)
        cw, _ = text_size(ch, scale, thickness, font)
        x += cw + spacing
    return x - xy[0] - spacing


def interpolate_telemetry(telemetry: pd.DataFrame, t: float) -> tuple[float, float]:
    times = telemetry["time"].to_numpy(dtype=float)
    p = telemetry["pressure"].to_numpy(dtype=float)
    f = telemetry["thrust"].to_numpy(dtype=float)
    if t <= times[0]:
        return float(p[0]), float(f[0])
    if t >= times[-1]:
        return float(p[-1]), float(f[-1])
    return float(np.interp(t, times, p)), float(np.interp(t, times, f))


# --------------------------- Overlay components --------------------------- #

@dataclass
class OverlayAssets:
    scrim_mult: np.ndarray          # per-pixel brightness multiplier (H,1) column
    chart_pts: np.ndarray           # polyline points (N,2) int32, chart-local
    chart_x: int
    chart_y: int
    chart_w: int
    chart_h: int
    t_end: float                    # burnout time (s)
    t_chart_max: float
    p_peak: float
    f_peak: float
    logo: Optional[np.ndarray]
    accent: tuple[int, int, int]
    s: float                        # global scale = height / 1080


def sc(assets: OverlayAssets, v: float) -> int:
    return int(round(v * assets.s))


def build_assets(cfg: Config, telemetry: pd.DataFrame) -> OverlayAssets:
    W, H = cfg.width, cfg.height
    s = H / 1080.0

    # Gradient scrims as a brightness multiplier column (black scrims).
    mult = np.ones((H, 1, 1), dtype=np.float32)
    if cfg.template == "b":
        top_h = int(240 * s)
        for y in range(top_h):
            mult[y] *= 1 - 0.72 * (1 - y / top_h)
        bot_h = int(300 * s)
        for i in range(bot_h):
            y = H - bot_h + i
            r = i / bot_h
            a = 0.35 * min(1.0, r / 0.4) if r < 0.4 else 0.35 + 0.47 * ((r - 0.4) / 0.6)
            mult[y] *= (1 - a)
    elif cfg.template == "c":
        top_h = int(200 * s)
        for y in range(top_h):
            mult[y] *= 1 - 0.62 * (1 - y / top_h)
        bot_h = int(360 * s)
        for i in range(bot_h):
            y = H - bot_h + i
            r = i / bot_h
            a = 0.40 * min(1.0, r / 0.45) if r < 0.45 else 0.40 + 0.40 * ((r - 0.45) / 0.55)
            mult[y] *= (1 - a)
    elif cfg.template == "d":
        top_h = int(190 * s)
        for y in range(top_h):
            mult[y] *= 1 - 0.72 * (1 - y / top_h)
        bot_h = int(330 * s)
        for i in range(bot_h):
            y = H - bot_h + i
            r = i / bot_h
            a = 0.55 * min(1.0, r / 0.5) if r < 0.5 else 0.55 + 0.37 * ((r - 0.5) / 0.5)
            mult[y] *= (1 - a)
    else:
        top_h = int(220 * s)
        for y in range(top_h):
            a = 0.66 * (1 - y / top_h)
            mult[y] *= (1 - a)
        bot_h = int(400 * s)
        for i in range(bot_h):
            y = H - bot_h + i
            r = i / bot_h                   # 0 at top of scrim -> 1 at bottom
            a = 0.45 * min(1.0, r / 0.45) if r < 0.45 else 0.45 + 0.33 * ((r - 0.45) / 0.55)
            mult[y] *= (1 - a)

    times = telemetry["time"].to_numpy(dtype=float)
    pressure = telemetry["pressure"].to_numpy(dtype=float)
    thrust = telemetry["thrust"].to_numpy(dtype=float)
    p_peak = float(np.nanmax(pressure))
    f_peak = float(np.nanmax(thrust))

    # Burnout: last time pressure is above 5% of peak.
    above = times[pressure >= 0.05 * p_peak]
    t_end = float(above[-1]) if above.size else float(times[-1])
    t_chart_max = max(t_end + 0.5, float(times[-1]) * 0.0 + t_end + 0.5)

    # Chart geometry.
    if cfg.template == "b":
        # Full-width strip chart along the bottom.
        chart_w, chart_h = W - 2 * int(64 * s), int(96 * s)
        chart_x = int(64 * s)
        chart_y = H - int(36 * s) - chart_h
    elif cfg.template == "c":
        # Full-width shared timeline above the readout row.
        chart_w, chart_h = W - 2 * int(96 * s), int(112 * s)
        chart_x = int(96 * s)
        chart_y = H - int(44 * s) - int(150 * s) - chart_h
    else:
        chart_w, chart_h = int(430 * s), int(128 * s)
        chart_x = W - int(64 * s) - chart_w
        chart_y = H - int(44 * s) - int(58 * s) - chart_h  # leaves room for peak row

    mask = (times >= 0.0) & (times <= t_chart_max)
    ct = times[mask]
    cp = pressure[mask]
    if ct.size < 2:
        ct, cp = times, pressure
    xs = (ct / t_chart_max) * (chart_w - 1)
    ys = (chart_h - 2) - (np.clip(cp, 0, None) / (p_peak * 1.05)) * (chart_h - 12)
    pts = np.stack([xs, ys], axis=1).astype(np.int32)

    logo = None
    if cfg.logo:
        if not cfg.logo.exists():
            fail(f"Logo file not found: {cfg.logo}")
        logo = cv2.imread(str(cfg.logo), cv2.IMREAD_UNCHANGED)
        if logo is None:
            fail(f"Could not read logo image: {cfg.logo}")
        max_w, max_h = int(180 * s), int(72 * s)
        lh, lw = logo.shape[:2]
        k = min(max_w / lw, max_h / lh, 1.0)
        logo = cv2.resize(logo, (int(lw * k), int(lh * k)), interpolation=cv2.INTER_AREA)

    return OverlayAssets(
        scrim_mult=mult, chart_pts=pts,
        chart_x=chart_x, chart_y=chart_y, chart_w=chart_w, chart_h=chart_h,
        t_end=t_end, t_chart_max=t_chart_max,
        p_peak=p_peak, f_peak=f_peak,
        logo=logo, accent=hex_to_bgr(cfg.accent), s=s,
    )


def draw_gauge(img, assets, cx, cy, frac, value_text, unit, label, color):
    r = sc(assets, 62)
    th = max(2, sc(assets, 7))
    frac = float(np.clip(frac, 0.0, 1.0))
    cv2.ellipse(img, (cx, cy), (r, r), 0, 135, 405, (70, 62, 55), th, cv2.LINE_AA)
    if frac > 0.003:
        cv2.ellipse(img, (cx, cy), (r, r), 0, 135, 135 + 270 * frac, color, th, cv2.LINE_AA)
    vw, vh = text_size(value_text, 0.95 * assets.s, 2)
    draw_text(img, value_text, (cx - vw // 2, cy + vh // 2 - sc(assets, 6)), 0.95 * assets.s, WHITE, 2)
    uw, _ = text_size(unit, 0.45 * assets.s, 1)
    draw_text(img, unit, (cx - uw // 2, cy + sc(assets, 28)), 0.45 * assets.s, GRAY, 1)
    lw_, _ = text_size(label, 0.42 * assets.s, 1)
    draw_text(img, label, (cx - lw_ // 2, cy + r + sc(assets, 28)), 0.42 * assets.s, (200, 200, 200), 1)


def draw_bar_meter(img, assets, x, y, w, label, unit, value_text, frac, peak_text, color):
    A = assets
    draw_spaced_text(img, label, (x, y), 0.42 * A.s, (200, 200, 200), 1, sc(A, 3))
    uw, _ = text_size(unit, 0.42 * A.s, 1)
    draw_text(img, unit, (x + w - uw, y), 0.42 * A.s, GRAY, 1)
    draw_text(img, value_text, (x - sc(A, 4), y + sc(A, 62)), 1.8 * A.s, WHITE, 3)
    by = y + sc(A, 84)
    bh = max(2, sc(A, 6))
    cv2.rectangle(img, (x, by), (x + w, by + bh), (70, 62, 55), -1)
    fw = int(w * float(np.clip(frac, 0, 1)))
    if fw > 0:
        cv2.rectangle(img, (x, by), (x + fw, by + bh), color, -1)
    tick = x + int(w * 0.95)
    cv2.line(img, (tick, by - sc(A, 4)), (tick, by + bh + sc(A, 4)), (160, 160, 160), max(1, sc(A, 2)), cv2.LINE_AA)
    draw_text(img, "0", (x, by + sc(A, 26)), 0.38 * A.s, (115, 108, 100), 1)
    pw, _ = text_size(peak_text, 0.38 * A.s, 1)
    draw_text(img, peak_text, (x + w - pw, by + sc(A, 26)), 0.38 * A.s, (115, 108, 100), 1)


def draw_strip_chart(frame, cfg, assets, t):
    A = assets
    cxr, cyr, cwd, chg = A.chart_x, A.chart_y, A.chart_w, A.chart_h
    roi = frame[cyr:cyr + chg, cxr:cxr + cwd]
    faint = roi.copy()
    cv2.line(faint, (0, chg - 1), (cwd, chg - 1), WHITE, 1, cv2.LINE_AA)
    cv2.line(faint, (0, chg // 2), (cwd, chg // 2), WHITE, 1, cv2.LINE_AA)
    cv2.polylines(faint, [A.chart_pts], False, WHITE, max(1, sc(A, 2)), cv2.LINE_AA)
    frame[cyr:cyr + chg, cxr:cxr + cwd] = cv2.addWeighted(faint, 0.30, roi, 0.70, 0)

    cur_x = int(np.clip(t / A.t_chart_max, 0, 1) * (cwd - 1))
    revealed = A.chart_pts[A.chart_pts[:, 0] <= cur_x]
    if revealed.shape[0] >= 2:
        cv2.polylines(frame[cyr:cyr + chg, cxr:cxr + cwd], [revealed], False, A.accent, max(2, sc(A, 3)), cv2.LINE_AA)
    cv2.line(frame, (cxr + cur_x, cyr), (cxr + cur_x, cyr + chg), (230, 230, 230), 1, cv2.LINE_AA)
    cur_y = int(np.interp(cur_x, A.chart_pts[:, 0], A.chart_pts[:, 1])) if A.chart_pts.shape[0] else chg
    cv2.circle(frame, (cxr + cur_x, cyr + cur_y), sc(A, 5), A.accent, -1, cv2.LINE_AA)
    cv2.circle(frame, (cxr + cur_x, cyr + cur_y), sc(A, 5), WHITE, 1, cv2.LINE_AA)
    draw_spaced_text(frame, f"CHAMBER PRESSURE - {cfg.pressure_unit}", (cxr, cyr - sc(A, 14)), 0.42 * A.s, (200, 200, 200), 1, sc(A, 3))
    tl = f"T 0-{A.t_chart_max:.0f} s"
    tw, _ = text_size(tl, 0.42 * A.s, 1)
    draw_text(frame, tl, (cxr + cwd - tw, cyr - sc(A, 14)), 0.42 * A.s, GRAY, 1)


def compose_frame_b(
    video_frame: np.ndarray,
    cfg: Config,
    assets: OverlayAssets,
    t: float,
    pressure: float,
    thrust: float,
) -> np.ndarray:
    W, H = cfg.width, cfg.height
    A = assets
    frame = cover_resize(video_frame, W, H).astype(np.float32)
    frame *= A.scrim_mult
    frame = frame.astype(np.uint8)
    margin = sc(A, 64)

    # --- Top-center: clock + mission + status ---
    clock = format_clock(t)
    c_scale = 1.9 * A.s
    cw, _ = text_size(clock, c_scale, 3)
    ccx = W // 2
    draw_text(frame, clock, (ccx - cw // 2, sc(A, 96)), c_scale, WHITE, 3)
    mw = draw_spaced_text(frame, cfg.title, (-10000, -10000), 0.55 * A.s, WHITE, 1, sc(A, 6))
    mx = ccx - mw // 2
    my = sc(A, 138)
    draw_spaced_text(frame, cfg.title, (mx, my), 0.55 * A.s, (218, 218, 218), 1, sc(A, 6))
    cv2.line(frame, (mx - sc(A, 74), my - sc(A, 6)), (mx - sc(A, 18), my - sc(A, 6)), (110, 105, 100), 1, cv2.LINE_AA)
    cv2.line(frame, (mx + mw + sc(A, 18), my - sc(A, 6)), (mx + mw + sc(A, 74), my - sc(A, 6)), (110, 105, 100), 1, cv2.LINE_AA)

    if t < 0:
        status, s_color = "PRE-IGNITION", GRAY
    elif t <= A.t_end + 0.1:
        status, s_color = "TEST ACTIVE", GREEN
    else:
        status, s_color = "TEST COMPLETE", YELLOW
    sw = draw_spaced_text(frame, status, (-10000, -10000), 0.5 * A.s, s_color, 1, sc(A, 4))
    sx = ccx - sw // 2
    sy = sc(A, 176)
    draw_spaced_text(frame, status, (sx, sy), 0.5 * A.s, s_color, 1, sc(A, 4))
    pulse = 0.55 + 0.45 * math.sin(t * 4.0)
    cv2.circle(frame, (sx - sc(A, 18), sy - sc(A, 6)), sc(A, 4), tuple(int(c * pulse) for c in s_color), -1, cv2.LINE_AA)

    # --- Top-left: logo + label ---
    x = margin
    if A.logo is not None:
        alpha_blend(frame, A.logo, x, sc(A, 52), 0.95)
        x += A.logo.shape[1] + sc(A, 20)
    else:
        box = sc(A, 52)
        cv2.rectangle(frame, (x, sc(A, 56)), (x + box, sc(A, 56) + box), (200, 200, 200), 1, cv2.LINE_AA)
        tw, thh = text_size("LOGO", 0.35 * A.s, 1)
        draw_text(frame, "LOGO", (x + (box - tw) // 2, sc(A, 56) + (box + thh) // 2), 0.35 * A.s, GRAY, 1)
        x += box + sc(A, 20)
    draw_spaced_text(frame, cfg.subtitle.upper(), (x, sc(A, 88)), 0.46 * A.s, A.accent, 1, sc(A, 5))

    # --- Left column: bar meters ---
    meter_w = sc(A, 330)
    m_y = H // 2 - sc(A, 190)
    draw_bar_meter(
        frame, A, margin, m_y, meter_w,
        "CHAMBER PRESSURE", cfg.pressure_unit, f"{pressure:.1f}",
        pressure / (A.p_peak * 1.05) if A.p_peak else 0,
        f"PEAK {A.p_peak:.2f}", A.accent,
    )
    draw_bar_meter(
        frame, A, margin, m_y + sc(A, 168), meter_w,
        "THRUST", cfg.thrust_unit, f"{thrust:.0f}",
        thrust / (A.f_peak * 1.05) if A.f_peak else 0,
        f"PEAK {A.f_peak:.1f}", WHITE,
    )

    # --- Right column: phase checklist ---
    if cfg.show_phases:
        phases = [("T-COUNT", None), ("IGNITION", 0.0),
                  ("BURN", min(0.6, A.t_end * 0.1)), ("BURNOUT", A.t_end)]
        active = max(i for i, ph in enumerate(phases) if ph[1] is None or t >= ph[1])
        row_h = sc(A, 48)
        list_w = sc(A, 300)
        lx = W - margin - list_w
        ly = H // 2 - sc(A, 170)
        for i, (label, pt) in enumerate(phases):
            ry = ly + i * row_h
            dot_c = A.accent if i <= active else (48, 44, 40)
            cv2.circle(frame, (lx + sc(A, 5), ry - sc(A, 6)), sc(A, 5), dot_c, -1, cv2.LINE_AA)
            t_color = WHITE if i == active else ((155, 150, 145) if i < active else (95, 90, 85))
            draw_spaced_text(frame, label, (lx + sc(A, 26), ry), 0.46 * A.s, t_color, 1, sc(A, 3))
            time_txt = "HOLD" if pt is None else (f"T- {abs(pt):.1f}s" if pt < 0 else f"T+ {pt:.1f}s")
            tw, _ = text_size(time_txt, 0.42 * A.s, 1)
            time_c = (180, 175, 170) if i <= active else (90, 85, 80)
            draw_text(frame, time_txt, (lx + list_w - tw, ry), 0.42 * A.s, time_c, 1)
            cv2.line(frame, (lx, ry + sc(A, 12)), (lx + list_w, ry + sc(A, 12)), (58, 54, 50), 1, cv2.LINE_AA)

    # --- Bottom: full-width strip chart ---
    if cfg.show_chart:
        draw_strip_chart(frame, cfg, A, t)

    return frame


def compose_frame_c(
    video_frame: np.ndarray,
    cfg: Config,
    assets: OverlayAssets,
    t: float,
    pressure: float,
    thrust: float,
) -> np.ndarray:
    W, H = cfg.width, cfg.height
    A = assets
    frame = cover_resize(video_frame, W, H).astype(np.float32)
    frame *= A.scrim_mult
    frame = frame.astype(np.uint8)
    margin = sc(A, 96)

    # --- Corner brackets (range-camera framing) ---
    ins, ln, th = sc(A, 36), sc(A, 42), max(1, sc(A, 2))
    bc = (160, 160, 160)
    for (cx0, cy0, dx, dy) in ((ins, ins, 1, 1), (W - ins, ins, -1, 1),
                               (ins, H - ins, 1, -1), (W - ins, H - ins, -1, -1)):
        cv2.line(frame, (cx0, cy0), (cx0 + dx * ln, cy0), bc, th, cv2.LINE_AA)
        cv2.line(frame, (cx0, cy0), (cx0, cy0 + dy * ln), bc, th, cv2.LINE_AA)

    # --- Top-left: identity ---
    x = margin
    if A.logo is not None:
        alpha_blend(frame, A.logo, x, sc(A, 58), 0.95)
        x += A.logo.shape[1] + sc(A, 22)
    else:
        box = sc(A, 52)
        cv2.rectangle(frame, (x, sc(A, 64)), (x + box, sc(A, 64) + box), (200, 200, 200), 1, cv2.LINE_AA)
        tw, thh = text_size("LOGO", 0.35 * A.s, 1)
        draw_text(frame, "LOGO", (x + (box - tw) // 2, sc(A, 64) + (box + thh) // 2), 0.35 * A.s, GRAY, 1)
        x += box + sc(A, 22)
    draw_spaced_text(frame, cfg.subtitle.upper(), (x, sc(A, 84)), 0.46 * A.s, A.accent, 1, sc(A, 5))
    draw_text(frame, cfg.title, (x, sc(A, 120)), 0.9 * A.s, WHITE, 2)

    # --- Top-right: camera meta + status ---
    meta = "CAM 01 - 120 FPS"
    mw, _ = text_size(meta, 0.46 * A.s, 1, FONT_MONO)
    draw_text(frame, meta, (W - margin - mw, sc(A, 80)), 0.46 * A.s, GRAY, 1, FONT_MONO)
    if t < 0:
        status, s_color = "PRE-IGNITION", GRAY
    elif t <= A.t_end + 0.1:
        status, s_color = "TEST ACTIVE", GREEN
    else:
        status, s_color = "TEST COMPLETE", YELLOW
    sw = draw_spaced_text(frame, status, (-10000, -10000), 0.55 * A.s, s_color, 2, sc(A, 4))
    sx = W - margin - sw
    sy = sc(A, 118)
    draw_spaced_text(frame, status, (sx, sy), 0.55 * A.s, s_color, 2, sc(A, 4))
    pulse = 0.55 + 0.45 * math.sin(t * 4.0)
    cv2.circle(frame, (sx - sc(A, 20), sy - sc(A, 7)), sc(A, 5), tuple(int(c * pulse) for c in s_color), -1, cv2.LINE_AA)

    # --- Shared timeline: area chart + ticks + phase diamonds + cursor ---
    cxr, cyr, cwd, chg = A.chart_x, A.chart_y, A.chart_w, A.chart_h
    base_y = cyr + chg
    roi = frame[cyr:base_y + sc(A, 10), cxr:cxr + cwd]
    faint = roi.copy()
    area = np.vstack([A.chart_pts, [[cwd - 1, chg - 1], [0, chg - 1]]]).astype(np.int32)
    cv2.fillPoly(faint, [area], (255, 255, 255))
    frame[cyr:base_y + sc(A, 10), cxr:cxr + cwd] = cv2.addWeighted(faint, 0.12, roi, 0.88, 0)

    cv2.line(frame, (cxr, base_y), (cxr + cwd, base_y), (140, 140, 140), 1, cv2.LINE_AA)
    cur_x = int(np.clip(t / A.t_chart_max, 0, 1) * (cwd - 1))
    cv2.line(frame, (cxr, base_y), (cxr + cur_x, base_y), A.accent, max(2, sc(A, 3)), cv2.LINE_AA)

    # second ticks
    for sec in range(int(A.t_chart_max) + 1):
        tx = cxr + int((sec / A.t_chart_max) * (cwd - 1))
        cv2.line(frame, (tx, base_y), (tx, base_y + sc(A, 8)), (130, 130, 130), 1, cv2.LINE_AA)
        lbl = f"{sec}s"
        lw_, _ = text_size(lbl, 0.36 * A.s, 1, FONT_MONO)
        draw_text(frame, lbl, (tx - lw_ // 2, base_y + sc(A, 26)), 0.36 * A.s, (115, 110, 105), 1, FONT_MONO)

    revealed = A.chart_pts[A.chart_pts[:, 0] <= cur_x]
    if revealed.shape[0] >= 2:
        cv2.polylines(frame[cyr:base_y, cxr:cxr + cwd], [revealed], False, A.accent, max(2, sc(A, 3)), cv2.LINE_AA)

    # phase diamonds
    phases = [("IGNITION", 0.0), ("BURN", min(0.6, A.t_end * 0.1)), ("BURNOUT", A.t_end)]
    for label, pt in phases:
        px = cxr + int((pt / A.t_chart_max) * (cwd - 1))
        reached = t >= pt
        d = sc(A, 7)
        pts_d = np.array([[px, base_y - d], [px + d, base_y], [px, base_y + d], [px - d, base_y]], dtype=np.int32)
        cv2.fillPoly(frame, [pts_d], A.accent if reached else (80, 74, 68), cv2.LINE_AA)
        t_color = WHITE if reached else (110, 104, 98)
        lw_ = draw_spaced_text(frame, label, (-10000, -10000), 0.4 * A.s, t_color, 1, sc(A, 3))
        lx = int(np.clip(px - lw_ // 2, cxr, cxr + cwd - lw_))
        draw_spaced_text(frame, label, (lx, cyr - sc(A, 6)), 0.4 * A.s, t_color, 1, sc(A, 3))

    # cursor
    cv2.line(frame, (cxr + cur_x, cyr + sc(A, 14)), (cxr + cur_x, base_y), (230, 230, 230), 1, cv2.LINE_AA)
    cur_y = int(np.interp(cur_x, A.chart_pts[:, 0], A.chart_pts[:, 1])) if A.chart_pts.shape[0] else chg
    cv2.circle(frame, (cxr + cur_x, cyr + cur_y), sc(A, 5), A.accent, -1, cv2.LINE_AA)
    cv2.circle(frame, (cxr + cur_x, cyr + cur_y), sc(A, 5), WHITE, 1, cv2.LINE_AA)

    # --- Bottom row: clock | live readouts | peaks ---
    row_y = H - sc(A, 52)
    draw_spaced_text(frame, "MISSION TIME", (margin, row_y - sc(A, 66)), 0.4 * A.s, (140, 133, 125), 1, sc(A, 3))
    draw_text(frame, format_clock(t), (margin - sc(A, 4), row_y), 1.9 * A.s, WHITE, 3)

    def readout(x0, label, value_text, unit, frac, color):
        bar_h, bar_w = sc(A, 64), max(3, sc(A, 8))
        cv2.rectangle(frame, (x0, row_y - bar_h), (x0 + bar_w, row_y), (70, 62, 55), -1)
        fh = int(bar_h * float(np.clip(frac, 0, 1)))
        if fh > 0:
            cv2.rectangle(frame, (x0, row_y - fh), (x0 + bar_w, row_y), color, -1)
        tx0 = x0 + bar_w + sc(A, 16)
        draw_spaced_text(frame, label, (tx0, row_y - sc(A, 44)), 0.4 * A.s, (140, 133, 125), 1, sc(A, 3))
        draw_text(frame, value_text, (tx0, row_y), 1.25 * A.s, WHITE, 2)
        vw, _ = text_size(value_text, 1.25 * A.s, 2)
        draw_text(frame, unit, (tx0 + vw + sc(A, 10), row_y), 0.55 * A.s, GRAY, 1)

    r_x = W // 2 - sc(A, 260)
    readout(r_x, "CHAMBER PRESSURE", f"{pressure:.1f}", cfg.pressure_unit,
            pressure / (A.p_peak * 1.05) if A.p_peak else 0, A.accent)
    readout(r_x + sc(A, 330), "THRUST", f"{thrust:.0f}", cfg.thrust_unit,
            thrust / (A.f_peak * 1.05) if A.f_peak else 0, WHITE)

    peaks = [("PEAK PRESSURE", f"{A.p_peak:.2f} {cfg.pressure_unit}"),
             ("PEAK THRUST", f"{A.f_peak:.1f} {cfg.thrust_unit}")]
    for i, (lbl, val) in enumerate(peaks):
        py = row_y - sc(A, 34) + i * sc(A, 34)
        vw, _ = text_size(val, 0.6 * A.s, 1, FONT_MONO)
        draw_text(frame, val, (W - margin - vw, py), 0.6 * A.s, WHITE, 1, FONT_MONO)
        lw_ = draw_spaced_text(frame, lbl, (-10000, -10000), 0.36 * A.s, (140, 133, 125), 1, sc(A, 3))
        draw_spaced_text(frame, lbl, (W - margin - vw - lw_ - sc(A, 16), py), 0.36 * A.s, (140, 133, 125), 1, sc(A, 3))

    return frame


def compose_frame_d(
    video_frame: np.ndarray,
    cfg: Config,
    assets: OverlayAssets,
    t: float,
    pressure: float,
    thrust: float,
) -> np.ndarray:
    """Stellar Kinetics broadcast console: glassy bottom bar with clock,
    live values, peaks and a numbered phase sequencer; vertical pressure
    tape on the right; brand strip on top."""
    W, H = cfg.width, cfg.height
    A = assets
    frame = cover_resize(video_frame, W, H).astype(np.float32)
    frame *= A.scrim_mult
    frame = frame.astype(np.uint8)
    margin = sc(A, 88)

    # --- Top: brand + mission strip ---
    x = margin
    if A.logo is not None:
        alpha_blend(frame, A.logo, x, sc(A, 44), 0.97)
        x += A.logo.shape[1] + sc(A, 26)
    else:
        draw_text(frame, "STELLAR KINETICS", (x, sc(A, 86)), 0.85 * A.s, WHITE, 2)
        x += text_size("STELLAR KINETICS", 0.85 * A.s, 2)[0] + sc(A, 26)
    cv2.line(frame, (x, sc(A, 50)), (x, sc(A, 94)), (95, 90, 85), 1, cv2.LINE_AA)
    x += sc(A, 26)
    draw_spaced_text(frame, cfg.subtitle.upper(), (x, sc(A, 64)), 0.42 * A.s, A.accent, 1, sc(A, 5))
    draw_text(frame, cfg.title, (x, sc(A, 100)), 0.8 * A.s, WHITE, 2)

    if t < 0:
        status, s_color = "PRE-IGNITION", GRAY
    elif t <= A.t_end + 0.1:
        status, s_color = "HOT FIRE", GREEN
    else:
        status, s_color = "TEST COMPLETE", YELLOW
    sw = draw_spaced_text(frame, status, (-10000, -10000), 0.55 * A.s, s_color, 2, sc(A, 5))
    sx = W - margin - sw
    draw_spaced_text(frame, status, (sx, sc(A, 72)), 0.55 * A.s, s_color, 2, sc(A, 5))
    pulse = 0.55 + 0.45 * math.sin(t * 4.2)
    cv2.circle(frame, (sx - sc(A, 22), sc(A, 64)), sc(A, 5), tuple(int(c * pulse) for c in s_color), -1, cv2.LINE_AA)
    site = "ETLAQ SPACEPORT - DUQM, OMAN"
    stw, _ = text_size(site, 0.42 * A.s, 1, FONT_MONO)
    draw_text(frame, site, (W - margin - stw, sc(A, 102)), 0.42 * A.s, GRAY, 1, FONT_MONO)

    # --- Right: vertical pressure tape ---
    tape_h, tape_w = sc(A, 340), sc(A, 14)
    tape_x = W - margin - tape_w - sc(A, 44)
    tape_y = H // 2 - tape_h // 2 - sc(A, 60)
    cv2.rectangle(frame, (tape_x, tape_y), (tape_x + tape_w, tape_y + tape_h), (58, 54, 50), -1)
    cv2.rectangle(frame, (tape_x, tape_y), (tape_x + tape_w, tape_y + tape_h), (110, 105, 100), 1, cv2.LINE_AA)
    frac = float(np.clip(pressure / (A.p_peak * 1.05) if A.p_peak else 0, 0, 1))
    fh = int(tape_h * frac)
    if fh > 0:
        cv2.rectangle(frame, (tape_x, tape_y + tape_h - fh), (tape_x + tape_w, tape_y + tape_h), A.accent, -1)
    peak_y = tape_y + tape_h - int(tape_h * (1.0 / 1.05))
    cv2.line(frame, (tape_x - sc(A, 6), peak_y), (tape_x + tape_w + sc(A, 6), peak_y), WHITE, max(1, sc(A, 2)), cv2.LINE_AA)
    for i in range(5):
        vy = tape_y + int(tape_h * i / 4)
        val = A.p_peak * 1.05 * (1 - i / 4)
        draw_text(frame, f"{val:.0f}", (tape_x + tape_w + sc(A, 12), vy + sc(A, 5)), 0.38 * A.s, (125, 118, 112), 1, FONT_MONO)
    lbl = f"CHAMBER PRESSURE - {cfg.pressure_unit}"
    lw_ = draw_spaced_text(frame, lbl, (-10000, -10000), 0.36 * A.s, (140, 133, 125), 1, sc(A, 3))
    draw_spaced_text(frame, lbl, (tape_x + tape_w // 2 - lw_ // 2, tape_y - sc(A, 16)), 0.36 * A.s, (140, 133, 125), 1, sc(A, 3))

    # --- Bottom broadcast console ---
    con_h = sc(A, 150)
    con_y = H - sc(A, 44) - sc(A, 30) - con_h
    con_x0, con_x1 = margin, W - margin
    roi = frame[con_y:con_y + con_h, con_x0:con_x1]
    panel = roi.copy()
    panel[:] = (16, 11, 8)
    frame[con_y:con_y + con_h, con_x0:con_x1] = cv2.addWeighted(panel, 0.58, roi, 0.42, 0)
    cv2.rectangle(frame, (con_x0, con_y), (con_x1, con_y + con_h), (78, 72, 66), 1, cv2.LINE_AA)
    # sweep light on the top edge
    sweep_w = int((con_x1 - con_x0) * 0.4)
    sweep_x = con_x0 + int(((t * 0.3) % 1.4 - 0.2) * (con_x1 - con_x0))
    for i in range(0, sweep_w, 2):
        a = math.sin(math.pi * i / sweep_w)
        px = sweep_x + i
        if con_x0 < px < con_x1 - 1:
            c = tuple(int(ch * a + 78 * (1 - a)) for ch in A.accent)
            cv2.line(frame, (px, con_y), (px + 1, con_y), c, max(1, sc(A, 2)))

    # clock cell
    cx0 = con_x0 + sc(A, 48)
    draw_spaced_text(frame, "MISSION TIME", (cx0, con_y + sc(A, 44)), 0.4 * A.s, (140, 133, 125), 1, sc(A, 3))
    clock_color = A.accent if t < 0 else WHITE
    draw_text(frame, format_clock(t), (cx0 - sc(A, 4), con_y + sc(A, 112)), 1.75 * A.s, clock_color, 3)
    div1 = con_x0 + sc(A, 420)
    cv2.line(frame, (div1, con_y), (div1, con_y + con_h), (78, 72, 66), 1, cv2.LINE_AA)

    # live values cell
    def value_block(x0, label, value_text, unit, frac_v, color):
        draw_spaced_text(frame, label, (x0, con_y + sc(A, 44)), 0.4 * A.s, (140, 133, 125), 1, sc(A, 3))
        draw_text(frame, value_text, (x0, con_y + sc(A, 98)), 1.2 * A.s, WHITE, 2)
        vw, _ = text_size(value_text, 1.2 * A.s, 2)
        draw_text(frame, unit, (x0 + vw + sc(A, 8), con_y + sc(A, 98)), 0.5 * A.s, GRAY, 1)
        by = con_y + sc(A, 116)
        bw = sc(A, 170)
        cv2.rectangle(frame, (x0, by), (x0 + bw, by + max(2, sc(A, 4))), (70, 62, 55), -1)
        fw = int(bw * float(np.clip(frac_v, 0, 1)))
        if fw > 0:
            cv2.rectangle(frame, (x0, by), (x0 + fw, by + max(2, sc(A, 4))), color, -1)

    v_x = div1 + sc(A, 56)
    value_block(v_x, "PRESSURE", f"{pressure:.1f}", cfg.pressure_unit,
                pressure / (A.p_peak * 1.05) if A.p_peak else 0, A.accent)
    value_block(v_x + sc(A, 250), "THRUST", f"{thrust:.0f}", cfg.thrust_unit,
                thrust / (A.f_peak * 1.05) if A.f_peak else 0, WHITE)
    pk_x = v_x + sc(A, 500)
    for i, (lbl2, val2) in enumerate((("PEAK P", f"{A.p_peak:.2f} {cfg.pressure_unit}"),
                                      ("PEAK T", f"{A.f_peak:.1f} {cfg.thrust_unit}"))):
        py = con_y + sc(A, 64) + i * sc(A, 34)
        draw_spaced_text(frame, lbl2, (pk_x, py), 0.36 * A.s, (125, 118, 112), 1, sc(A, 3))
        draw_text(frame, val2, (pk_x + sc(A, 110), py), 0.55 * A.s, WHITE, 1, FONT_MONO)
    div2 = con_x1 - sc(A, 380)
    cv2.line(frame, (div2, con_y), (div2, con_y + con_h), (78, 72, 66), 1, cv2.LINE_AA)

    # phase sequencer cell
    if cfg.show_phases:
        phases = [("AUTO SEQUENCE", -4.0), ("IGNITION", 0.0),
                  ("FULL THRUST", min(0.6, A.t_end * 0.1)), ("BURNOUT", A.t_end)]
        active = max(i for i, ph in enumerate(phases) if t >= ph[1])
        seq_x = div2 + sc(A, 42)
        row_h = sc(A, 32)
        y0 = con_y + sc(A, 32)
        for i, (label, pt) in enumerate(phases):
            ry = y0 + i * row_h
            num_c = A.accent if i <= active else (85, 80, 75)
            draw_text(frame, f"{i + 1:02d}", (seq_x, ry), 0.4 * A.s, num_c, 1, FONT_MONO)
            d = sc(A, 5)
            dcx, dcy = seq_x + sc(A, 44), ry - sc(A, 5)
            pts_d = np.array([[dcx, dcy - d], [dcx + d, dcy], [dcx, dcy + d], [dcx - d, dcy]], dtype=np.int32)
            dot_c = A.accent if i < active else (WHITE if i == active else (58, 54, 50))
            cv2.fillPoly(frame, [pts_d], dot_c, cv2.LINE_AA)
            t_color = WHITE if i == active else ((150, 145, 140) if i < active else (85, 80, 75))
            draw_spaced_text(frame, label, (seq_x + sc(A, 64), ry), 0.42 * A.s, t_color, 1, sc(A, 3))
            time_txt = f"T-{abs(pt):.0f}s" if pt < 0 else f"T+{pt:.1f}s"
            tw2, _ = text_size(time_txt, 0.38 * A.s, 1, FONT_MONO)
            time_c = (160, 155, 150) if i <= active else (75, 70, 66)
            draw_text(frame, time_txt, (con_x1 - sc(A, 40) - tw2, ry), 0.38 * A.s, time_c, 1, FONT_MONO)

    # footer line
    fy = H - sc(A, 46)
    draw_text(frame, "STELLAR KINETICS - ENGINEERING THE VOID", (con_x0, fy), 0.42 * A.s, (120, 114, 108), 1, FONT_MONO)
    right_txt = "LOX / PROPANE - CAM 01 - 120 FPS"
    rtw, _ = text_size(right_txt, 0.42 * A.s, 1, FONT_MONO)
    draw_text(frame, right_txt, (con_x1 - rtw, fy), 0.42 * A.s, (120, 114, 108), 1, FONT_MONO)

    return frame


def format_clock(t: float) -> str:
    sign = "T-" if t < 0 else "T+"
    at = abs(t)
    return f"{sign} {int(at // 60):02d}:{at % 60:05.2f}"


def compose_frame(
    video_frame: np.ndarray,
    cfg: Config,
    assets: OverlayAssets,
    t: float,
    pressure: float,
    thrust: float,
) -> np.ndarray:
    W, H = cfg.width, cfg.height
    A = assets
    frame = cover_resize(video_frame, W, H).astype(np.float32)
    frame *= A.scrim_mult
    frame = frame.astype(np.uint8)

    margin = sc(A, 64)

    # --- Top-left: logo + mission identity ---
    x = margin
    if A.logo is not None:
        alpha_blend(frame, A.logo, x, sc(A, 52), 0.95)
        x += A.logo.shape[1] + sc(A, 24)
    else:
        box = sc(A, 56)
        cv2.rectangle(frame, (x, sc(A, 56)), (x + box, sc(A, 56) + box), (200, 200, 200), 1, cv2.LINE_AA)
        tw, thh = text_size("LOGO", 0.38 * A.s, 1)
        draw_text(frame, "LOGO", (x + (box - tw) // 2, sc(A, 56) + (box + thh) // 2), 0.38 * A.s, GRAY, 1)
        x += box + sc(A, 24)

    draw_spaced_text(frame, cfg.subtitle.upper(), (x, sc(A, 76)), 0.5 * A.s, A.accent, 1, sc(A, 5))
    draw_text(frame, cfg.title, (x, sc(A, 118)), 1.1 * A.s, WHITE, 2)

    # --- Top-right: status ---
    if t < 0:
        status, s_color = "PRE-IGNITION", GRAY
    elif t <= A.t_end + 0.1:
        status, s_color = "TEST ACTIVE", GREEN
    else:
        status, s_color = "TEST COMPLETE", YELLOW
    sw = draw_spaced_text(frame, status, (-10000, -10000), 0.62 * A.s, s_color, 2, sc(A, 4))
    sx = W - margin - sw
    draw_spaced_text(frame, status, (sx, sc(A, 84)), 0.62 * A.s, s_color, 2, sc(A, 4))
    pulse = 0.55 + 0.45 * math.sin(t * 4.0)
    cv2.circle(frame, (sx - sc(A, 24), sc(A, 76)), sc(A, 6), tuple(int(c * pulse) for c in s_color), -1, cv2.LINE_AA)

    # --- Bottom-left: gauges ---
    g_cy = H - sc(A, 44) - sc(A, 40) - sc(A, 74)
    g1_cx = margin + sc(A, 82)
    g2_cx = g1_cx + sc(A, 192)
    draw_gauge(
        frame, A, g1_cx, g_cy,
        pressure / (A.p_peak * 1.05) if A.p_peak else 0,
        f"{pressure:.1f}", cfg.pressure_unit, "CHAMBER PRESSURE", A.accent,
    )
    draw_gauge(
        frame, A, g2_cx, g_cy,
        thrust / (A.f_peak * 1.05) if A.f_peak else 0,
        f"{thrust:.0f}", cfg.thrust_unit, "THRUST", WHITE,
    )

    # --- Bottom-center: T+ clock + phase bar ---
    clock = format_clock(t)
    c_scale = 2.2 * A.s
    cw, chh = text_size(clock, c_scale, 3)
    ccx = W // 2
    draw_text(frame, clock, (ccx - cw // 2, H - sc(A, 44) - sc(A, 64)), c_scale, WHITE, 3)

    if cfg.show_phases:
        bar_w = sc(A, 560)
        bar_x = ccx - bar_w // 2
        bar_y = H - sc(A, 44) - sc(A, 36)
        phases = [("T-COUNT", -math.inf), ("IGNITION", 0.0),
                  ("BURN", min(0.6, A.t_end * 0.1)), ("BURNOUT", A.t_end)]
        active = max(i for i, ph in enumerate(phases) if t >= ph[1])
        # progress fraction across bar
        if active < 3:
            t0 = phases[active][1] if active > 0 else min(t, 0.0) - 4.0
            t1 = phases[active + 1][1]
            span = max(1e-6, t1 - t0)
            frac = (active + float(np.clip((t - t0) / span, 0, 1))) / 3.0
        else:
            frac = 1.0
        cv2.line(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y), (70, 62, 55), max(1, sc(A, 2)), cv2.LINE_AA)
        cv2.line(frame, (bar_x, bar_y), (bar_x + int(bar_w * frac), bar_y), A.accent, max(1, sc(A, 2)), cv2.LINE_AA)
        for i, (label, _) in enumerate(phases):
            px = bar_x + int(bar_w * i / 3)
            color = A.accent if i <= active else (90, 82, 75)
            cv2.circle(frame, (px, bar_y), sc(A, 5), color, -1, cv2.LINE_AA)
            t_color = WHITE if i == active else (120, 112, 105)
            lw_, _ = text_size(label, 0.42 * A.s, 1)
            lx = int(np.clip(px - lw_ // 2, bar_x - sc(A, 30), bar_x + bar_w - lw_ + sc(A, 30)))
            draw_text(frame, label, (lx, bar_y + sc(A, 26)), 0.42 * A.s, t_color, 1)

    # --- Bottom-right: chart + peaks ---
    if cfg.show_chart:
        cxr, cyr, cwd, chg = A.chart_x, A.chart_y, A.chart_w, A.chart_h
        roi = frame[cyr:cyr + chg, cxr:cxr + cwd]
        faint = roi.copy()
        cv2.line(faint, (0, chg - 1), (cwd, chg - 1), WHITE, 1, cv2.LINE_AA)
        cv2.line(faint, (0, chg // 2), (cwd, chg // 2), WHITE, 1, cv2.LINE_AA)
        cv2.polylines(faint, [A.chart_pts], False, WHITE, max(1, sc(A, 2)), cv2.LINE_AA)
        frame[cyr:cyr + chg, cxr:cxr + cwd] = cv2.addWeighted(faint, 0.30, roi, 0.70, 0)

        cur_x = int(np.clip(t / A.t_chart_max, 0, 1) * (cwd - 1))
        revealed = A.chart_pts[A.chart_pts[:, 0] <= cur_x]
        if revealed.shape[0] >= 2:
            cv2.polylines(
                frame[cyr:cyr + chg, cxr:cxr + cwd],
                [revealed], False, A.accent, max(2, sc(A, 3)), cv2.LINE_AA,
            )
        cv2.line(frame, (cxr + cur_x, cyr), (cxr + cur_x, cyr + chg), (230, 230, 230), 1, cv2.LINE_AA)
        cur_y = int(np.interp(cur_x, A.chart_pts[:, 0], A.chart_pts[:, 1])) if A.chart_pts.shape[0] else chg
        cv2.circle(frame, (cxr + cur_x, cyr + cur_y), sc(A, 5), A.accent, -1, cv2.LINE_AA)
        cv2.circle(frame, (cxr + cur_x, cyr + cur_y), sc(A, 5), WHITE, 1, cv2.LINE_AA)

        # chart header
        draw_spaced_text(
            frame, f"CHAMBER PRESSURE - {cfg.pressure_unit}",
            (cxr, cyr - sc(A, 14)), 0.42 * A.s, (200, 200, 200), 1, sc(A, 3),
        )
        # peaks row
        py = cyr + chg + sc(A, 30)
        draw_spaced_text(frame, "PEAK PRESSURE", (cxr, py), 0.38 * A.s, GRAY, 1, sc(A, 3))
        draw_text(frame, f"{A.p_peak:.2f} {cfg.pressure_unit}", (cxr, py + sc(A, 26)), 0.6 * A.s, WHITE, 1)
        px2 = cxr + sc(A, 220)
        draw_spaced_text(frame, "PEAK THRUST", (px2, py), 0.38 * A.s, GRAY, 1, sc(A, 3))
        draw_text(frame, f"{A.f_peak:.1f} {cfg.thrust_unit}", (px2, py + sc(A, 26)), 0.6 * A.s, WHITE, 1)

    return frame


# ------------------------------- FFmpeg --------------------------------- #

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def mux_original_audio(silent_video: Path, source_video: Path, output: Path, crf: int) -> bool:
    if not ffmpeg_available():
        return False
    cmd = [
        "ffmpeg", "-y",
        "-i", str(silent_video),
        "-i", str(source_video),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        str(output),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
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

    assets = build_assets(cfg, telemetry)

    with tempfile.TemporaryDirectory(prefix="rocket_overlay_") as tmpdir:
        silent_path = Path(tmpdir) / "silent_output.mp4"
        fourcc = cv2.VideoWriter_fourcc(*cfg.codec)
        writer = cv2.VideoWriter(str(silent_path), fourcc, fps, (cfg.width, cfg.height))
        if not writer.isOpened():
            fail("Could not create output video. Try codec 'mp4v'.")

        print(f"Rendering {frame_count:,} frames at {fps:.3f} FPS ({duration:.2f}s)...")

        idx = 0
        last_percent = -1
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            video_t = idx / fps
            telemetry_t = video_t - cfg.ignition_video_s
            p, f = interpolate_telemetry(telemetry, telemetry_t)
            if telemetry_t < 0 or telemetry_t > assets.t_end + 0.5:
                p, f = 0.0, 0.0
            compose = {"b": compose_frame_b, "c": compose_frame_c, "d": compose_frame_d}.get(cfg.template, compose_frame)
            writer.write(compose(frame, cfg, assets, telemetry_t, p, f))
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

        if cfg.keep_audio and mux_original_audio(silent_path, cfg.video, cfg.output, cfg.crf):
            print(f"Done with original audio: {cfg.output}")
        else:
            shutil.copy2(silent_path, cfg.output)
            print(f"Done: {cfg.output}")
            if cfg.keep_audio and not ffmpeg_available():
                print("Note: FFmpeg was not found, so the output has no audio.")


# ---------------------------------- CLI ---------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Broadcast-style rocket test overlay video.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", type=Path, help="Optional YAML config file.")
    p.add_argument("--video", type=Path)
    p.add_argument("--data", type=Path)
    p.add_argument("--output", type=Path)

    p.add_argument("--sheet", type=parse_sheet, default=0)
    p.add_argument("--time-column")
    p.add_argument("--pressure-column")
    p.add_argument("--thrust-column")

    p.add_argument("--ignition-video-s", type=float, default=0.0)
    p.add_argument("--telemetry-zero-s", type=float)
    p.add_argument("--time-scale", type=float, default=1.0)

    p.add_argument("--title", default="ROCKET MOTOR STATIC TEST")
    p.add_argument("--subtitle", default="STATIC FIRE TEST")
    p.add_argument("--pressure-unit", default="bar")
    p.add_argument("--thrust-unit", default="N")
    p.add_argument("--logo", type=Path)

    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)

    p.add_argument("--accent", default="#38BDF8", help="Accent color (hex).")
    p.add_argument("--template", default="a", choices=("a", "b", "c", "d"),
                   help="a = gauges, b = bar meters, c = Rangeline, d = Stellar Kinetics broadcast console.")
    p.add_argument("--no-chart", action="store_true", help="Hide the mini chart.")
    p.add_argument("--no-phases", action="store_true", help="Hide the phase bar.")

    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--codec", default="mp4v")
    p.add_argument("--crf", type=int, default=18)
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    values = vars(args).copy()
    config_path = values.pop("config", None)

    base: dict = {}
    if config_path:
        base = load_yaml(config_path)

    defaults = vars(build_parser().parse_args([]))
    defaults.pop("config", None)

    flag_map = {"no_audio": "keep_audio", "no_chart": "show_chart", "no_phases": "show_phases"}
    for key, value in values.items():
        if key in flag_map:
            target = flag_map[key]
            if value:
                base[target] = False
            elif target not in base:
                base[target] = True
            continue
        if value is not None and (
            key not in defaults or value != defaults[key] or key not in base
        ):
            base[key] = value

    required = ("video", "data", "output")
    missing = [name for name in required if not base.get(name)]
    if missing:
        fail("Missing required options: " + ", ".join(f"--{n.replace('_', '-')}" for n in missing))

    for field in ("video", "data", "output", "logo"):
        if base.get(field) is not None:
            base[field] = Path(base[field])

    if "sheet" in base and isinstance(base["sheet"], str):
        base["sheet"] = parse_sheet(base["sheet"])

    if int(base.get("width", 1920)) < 960 or int(base.get("height", 1080)) < 540:
        fail("Output resolution is too small. Use at least 960x540.")

    base.pop("video_fraction", None)
    base.pop("chart_fraction", None)
    base.pop("chart_window_before_s", None)
    base.pop("chart_window_after_s", None)

    return Config(**base)


def main() -> None:
    cfg = config_from_args(build_parser().parse_args())
    render(cfg)


if __name__ == "__main__":
    main()
