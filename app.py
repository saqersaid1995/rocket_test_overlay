#!/usr/bin/env python3
"""Local web interface for Rocket Test Video Overlay."""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import cv2
from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

import rocket_overlay_broadcast as broadcast_overlay
from rocket_overlay import (
    Config, RocketOverlayError, default_scene_config, render, resolve_column,
    suggest_telemetry_zero_s, validate_scene_config,
)
from rotpl_registry import (
    MAX_ARCHIVE_BYTES,
    RotplActivationError,
    RotplConflictError,
    RotplError,
    RotplNotFoundError,
    RotplRegistry,
    ResolvedTemplate,
    RotplValidationError,
)
from rotpl_renderer import (
    RotplError as RotplRenderError,
    RotplPackage,
    RotplRenderer,
    TemplateContext,
)


BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "workspace"
UPLOAD_DIR = WORK_DIR / "uploads"
OUTPUT_DIR = WORK_DIR / "outputs"
PREVIEW_DIR = WORK_DIR / "previews"
TEMPLATE_PACKAGE_DIR = WORK_DIR / "template_packages"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
DATA_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv", ".txt"}
LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
PREVIEW_SESSION_TTL_S = 60 * 60
PREVIEW_MAX_SESSIONS = 12
PREVIEW_MAX_WIDTH = 1920
PREVIEW_MAX_HEIGHT = 1080
PREVIEW_MAX_PIXELS = PREVIEW_MAX_WIDTH * PREVIEW_MAX_HEIGHT
PREVIEW_LOGO_MAX_BYTES = 10 * 1024 * 1024
PREVIEW_TELEMETRY_CACHE_SIZE = 8
PREVIEW_ASSETS_CACHE_SIZE = 24
PREVIEW_TEMPLATE_CACHE_SIZE = 4

for directory in (UPLOAD_DIR, OUTPUT_DIR, PREVIEW_DIR, TEMPLATE_PACKAGE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
chunk_uploads: dict[str, dict[str, Any]] = {}
chunk_uploads_lock = threading.Lock()
preview_sessions: dict[str, dict[str, Any]] = {}
preview_sessions_lock = threading.Lock()
template_registry = RotplRegistry(TEMPLATE_PACKAGE_DIR)


@app.after_request
def prevent_stale_studio(response):
    """The studio shell and assets must update together during local editing."""
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


def error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def remove_preview_artifacts(preview_id: str, session: dict[str, Any]) -> None:
    """Remove files owned by one expired in-memory preview session."""
    session_lock = session.get("cache_lock")
    if session_lock is None:
        logo_path = session.get("logo_path")
        for renderer in session.get("template_cache", {}).values():
            renderer.package.close()
        session.get("template_cache", {}).clear()
    else:
        # A capacity cleanup can race a request that already holds a reference
        # to this session. Do not remove its logo while build_assets is reading
        # it; the session lock makes cleanup wait for that short critical path.
        with session_lock:
            logo_path = session.get("logo_path")
            session.get("telemetry_cache", {}).clear()
            session.get("assets_cache", {}).clear()
            for renderer in session.get("template_cache", {}).values():
                renderer.package.close()
            session.get("template_cache", {}).clear()
    if logo_path:
        Path(logo_path).unlink(missing_ok=True)
    session_dir = PREVIEW_DIR / preview_id
    if session_dir.is_dir():
        for child in session_dir.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        try:
            session_dir.rmdir()
        except OSError:
            pass


def cleanup_preview_sessions(reserve: int = 0) -> None:
    """Expire idle sessions and bound the raw DataFrames retained in memory."""
    now = time.monotonic()
    removed: list[tuple[str, dict[str, Any]]] = []
    with preview_sessions_lock:
        for preview_id, session in list(preview_sessions.items()):
            if now - float(session["touched_at"]) > PREVIEW_SESSION_TTL_S:
                removed.append((preview_id, preview_sessions.pop(preview_id)))

        capacity = max(0, PREVIEW_MAX_SESSIONS - reserve)
        overflow = max(0, len(preview_sessions) - capacity)
        if overflow:
            oldest = sorted(
                preview_sessions.items(), key=lambda item: item[1]["touched_at"]
            )[:overflow]
            for preview_id, _ in oldest:
                removed.append((preview_id, preview_sessions.pop(preview_id)))

    for preview_id, session in removed:
        remove_preview_artifacts(preview_id, session)


def get_preview_session(preview_id: str) -> dict[str, Any] | None:
    cleanup_preview_sessions()
    with preview_sessions_lock:
        session = preview_sessions.get(preview_id)
        if session is not None:
            session["touched_at"] = time.monotonic()
        return session


def preview_payload() -> dict[str, Any]:
    if request.is_json:
        value = request.get_json(silent=True)
        if value is None:
            raise RocketOverlayError("تعذر قراءة بيانات المعاينة المرسلة.")
        if not isinstance(value, dict):
            raise RocketOverlayError("بيانات المعاينة يجب أن تكون كائناً.")
        return value
    return request.form.to_dict()


def payload_text(
    payload: dict[str, Any], name: str, default: str = "", *aliases: str
) -> str:
    for key in (name, *aliases):
        if key in payload and payload[key] is not None:
            return str(payload[key]).strip()
    return default


def payload_float(
    payload: dict[str, Any], name: str, default: float, *aliases: str
) -> float:
    raw = payload_text(payload, name, "", *aliases)
    if raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RocketOverlayError(f"القيمة الرقمية غير صحيحة للحقل: {name}") from exc
    if not np.isfinite(value):
        raise RocketOverlayError(f"القيمة الرقمية غير محدودة للحقل: {name}")
    return value


def payload_int(
    payload: dict[str, Any], name: str, default: int, *aliases: str
) -> int:
    raw = payload_text(payload, name, "", *aliases)
    if raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RocketOverlayError(f"القيمة الصحيحة غير صالحة للحقل: {name}") from exc


def payload_bool(payload: dict[str, Any], name: str, default: bool) -> bool:
    if name not in payload:
        return default
    value = payload[name]
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RocketOverlayError(f"قيمة التشغيل غير صحيحة للحقل: {name}")


def resolve_preview_column(
    frame: pd.DataFrame,
    explicit: str,
    kind: str,
    arabic_name: str,
    optional: bool = False,
) -> str | None:
    if explicit.lower() in {"__none__", "none", "null"}:
        return None
    try:
        return resolve_column(frame, explicit or None, kind)
    except RocketOverlayError as exc:
        if optional and not explicit:
            return None
        columns = "، ".join(str(column) for column in frame.columns)
        raise RocketOverlayError(
            f"تعذر تحديد عمود {arabic_name}. الأعمدة المتاحة: {columns}"
        ) from exc


def normalize_preview_telemetry(
    raw_frame: pd.DataFrame, payload: dict[str, Any]
) -> pd.DataFrame:
    if raw_frame.empty:
        raise RocketOverlayError("ملف القياسات لا يحتوي على صفوف.")

    time_column = resolve_preview_column(
        raw_frame, payload_text(payload, "time_column"), "time", "الزمن"
    )
    pressure_column = resolve_preview_column(
        raw_frame,
        payload_text(payload, "pressure_column"),
        "pressure",
        "الضغط",
    )
    thrust_column = resolve_preview_column(
        raw_frame,
        payload_text(payload, "thrust_column"),
        "thrust",
        "الدفع",
        optional=True,
    )

    clean = pd.DataFrame({
        "time": pd.to_numeric(raw_frame[time_column], errors="coerce"),
        "pressure": pd.to_numeric(raw_frame[pressure_column], errors="coerce"),
        "thrust": (
            pd.to_numeric(raw_frame[thrust_column], errors="coerce")
            if thrust_column is not None
            else np.zeros(len(raw_frame), dtype=float)
        ),
    }).dropna(subset=["time"])
    clean = clean.replace([np.inf, -np.inf], np.nan)
    clean["pressure"] = clean["pressure"].interpolate(limit_direction="both")
    clean["thrust"] = clean["thrust"].interpolate(limit_direction="both")
    clean = clean.dropna().sort_values("time")
    clean = clean.groupby("time", as_index=False).mean(numeric_only=True)
    if len(clean) < 2:
        raise RocketOverlayError("يلزم صفّان صالحان على الأقل لإنشاء المعاينة.")

    first_time = float(clean["time"].iloc[0])
    last_time = float(clean["time"].iloc[-1])
    telemetry_zero_text = payload_text(
        payload, "telemetry_zero_s", "", "telemetry_zero"
    )
    if telemetry_zero_text:
        telemetry_zero = payload_float(
            payload, "telemetry_zero_s", first_time, "telemetry_zero"
        )
        zero_source = "explicit"
        zero_diagnostics = {
            "method": "explicit",
            "signal": None,
            "confidence": 1.0,
            "first_timestamp_s": first_time,
            "last_timestamp_s": last_time,
            "duration_s": max(0.0, last_time - first_time),
            "lead_in_s": telemetry_zero - first_time,
            "candidates": [],
            "reason": "user_supplied_telemetry_zero",
        }
    else:
        telemetry_zero, zero_diagnostics = suggest_telemetry_zero_s(
            clean["time"].to_numpy(dtype=float),
            clean["pressure"].to_numpy(dtype=float),
            clean["thrust"].to_numpy(dtype=float)
            if thrust_column is not None
            else None,
        )
        zero_source = "automatic"
    time_scale = payload_float(payload, "time_scale", 1.0)
    if time_scale <= 0:
        raise RocketOverlayError("مقياس الزمن يجب أن يكون أكبر من صفر.")
    clean["time"] = (clean["time"] - telemetry_zero) * time_scale
    if not np.all(np.diff(clean["time"].to_numpy(dtype=float)) > 0):
        raise RocketOverlayError("قيم الزمن يجب أن تكون متزايدة بعد التنظيف.")
    clean = clean.reset_index(drop=True)
    clean.attrs["has_thrust"] = thrust_column is not None
    clean.attrs["time_column"] = time_column
    clean.attrs["pressure_column"] = pressure_column
    clean.attrs["thrust_column"] = thrust_column
    clean.attrs["telemetry_zero_s"] = float(telemetry_zero)
    clean.attrs["telemetry_zero_source"] = zero_source
    clean.attrs["telemetry_zero_diagnostics"] = zero_diagnostics
    return clean


def preview_telemetry_cache_key(payload: dict[str, Any]) -> tuple[Any, ...]:
    """Return every input that can change normalized preview telemetry."""
    telemetry_zero_text = payload_text(
        payload, "telemetry_zero_s", "", "telemetry_zero"
    )
    telemetry_zero_key: tuple[str, float | None]
    if telemetry_zero_text:
        telemetry_zero_key = (
            "explicit",
            payload_float(payload, "telemetry_zero_s", 0.0, "telemetry_zero"),
        )
    else:
        telemetry_zero_key = ("automatic", None)
    return (
        payload_text(payload, "time_column"),
        payload_text(payload, "pressure_column"),
        payload_text(payload, "thrust_column"),
        telemetry_zero_key,
        payload_float(payload, "time_scale", 1.0),
    )


def preview_assets_cache_key(
    cfg: broadcast_overlay.Config,
    telemetry_key: tuple[Any, ...],
    logo_revision: int,
) -> tuple[Any, ...]:
    """Key assets by a complete immutable snapshot of their dependencies.

    Including every Config field is intentionally conservative: it prevents a
    future template change from silently reusing assets after a newly-relevant
    field is added to ``build_assets``. Paths and the logo revision are both
    retained so replacing bytes can never reuse the previous decoded logo.
    """
    config_key = tuple(
        (
            field.name,
            str(value) if isinstance(value := getattr(cfg, field.name), Path) else value,
        )
        for field in dataclass_fields(cfg)
    )
    return (telemetry_key, int(logo_revision), config_key)


def preview_cache_store(
    cache: dict[tuple[Any, ...], Any],
    key: tuple[Any, ...],
    value: Any,
    max_size: int,
) -> None:
    """Store a bounded per-session cache entry using insertion-order LRU."""
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > max_size:
        cache.pop(next(iter(cache)))


def resolve_preview_template(
    payload: dict[str, Any], session: dict[str, Any]
) -> tuple[RotplRenderer | None, ResolvedTemplate | None, bool]:
    """Resolve an immutable ROTPL selection and return its session renderer.

    The registry integrity-check is intentionally performed on every request.
    Only the already-validated renderer is cached, and the immutable package
    digest is part of its key.  This prevents an old renderer from being reused
    after a version is replaced or the browser changes its selected package.
    """
    template_id = payload_text(payload, "template_id")
    template_version = payload_text(payload, "template_version")
    requested_sha256 = payload_text(payload, "sha256", "").lower()
    if not template_id:
        if template_version or requested_sha256:
            raise RocketOverlayError(
                "معرّف القالب مطلوب عند إرسال الإصدار أو البصمة الرقمية."
            )
        return None, None, False
    if not template_version or not requested_sha256:
        raise RocketOverlayError(
            "يجب إرسال معرّف القالب وإصداره وبصمته الرقمية معاً."
        )
    if len(requested_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in requested_sha256
    ):
        raise RocketOverlayError("بصمة حزمة القالب SHA-256 غير صالحة.")

    registry_get = getattr(template_registry, "get", None)
    if callable(registry_get):
        record = registry_get(template_id, template_version)
        validation = record.get("validation", {})
        if not validation.get("activatable", False):
            reasons = (
                validation.get("blocked_reasons")
                or validation.get("errors")
                or []
            )
            details = "; ".join(str(reason) for reason in reasons)
            raise RocketOverlayError(
                "لا يمكن استخدام قالب محظور أو غير مكتمل"
                + (f": {details}" if details else ".")
            )
    resolved = template_registry.resolve(template_id, template_version)
    if not hmac.compare_digest(resolved.sha256.lower(), requested_sha256):
        raise RocketOverlayError(
            "بصمة القالب المحدد لا تطابق النسخة المثبتة؛ أعد اختيار القالب."
        )

    cache_key = (resolved.id, resolved.version, resolved.sha256)
    template_cache = session.setdefault("template_cache", {})
    renderer = template_cache.get(cache_key)
    cache_hit = renderer is not None
    if renderer is None:
        package = RotplPackage.open(resolved.files_path)
        if package.template_id != resolved.id or package.template_version != resolved.version:
            package.close()
            raise RocketOverlayError(
                "هوية ملفات القالب لا تطابق السجل المثبت."
            )
        try:
            renderer = RotplRenderer(package)
        except Exception:
            package.close()
            raise
        template_cache[cache_key] = renderer
        while len(template_cache) > PREVIEW_TEMPLATE_CACHE_SIZE:
            oldest_key = next(iter(template_cache))
            oldest_renderer = template_cache.pop(oldest_key)
            oldest_renderer.package.close()
    else:
        # Maintain insertion-order LRU while still verifying the registry hash
        # above on every request.
        template_cache.pop(cache_key)
        template_cache[cache_key] = renderer
    return renderer, resolved, cache_hit


def preview_template_metrics(
    telemetry: pd.DataFrame, assets: Any
) -> dict[str, float | None]:
    """Calculate the stable metrics exposed to declarative preview bindings."""
    cached = telemetry.attrs.get("_rotpl_preview_metrics")
    if isinstance(cached, dict):
        return cached
    times = telemetry["time"].to_numpy(dtype=float)
    pressure = telemetry["pressure"].to_numpy(dtype=float)
    peak_pressure_index = int(np.argmax(pressure))
    duration = max(float(times[-1] - times[0]), 0.0)
    average_pressure = (
        float(np.trapezoid(pressure, times) / duration)
        if duration > 0 else float(pressure[-1])
    )
    pressure_rise = (
        np.gradient(pressure, times)
        if len(times) > 1 else np.zeros_like(pressure)
    )
    result: dict[str, float | None] = {
        "peak_pressure": float(pressure[peak_pressure_index]),
        "peak_pressure_time": float(times[peak_pressure_index]),
        "average_pressure": average_pressure,
        "max_pressure_rise": float(np.max(pressure_rise)),
        "burn_duration": float(getattr(assets, "t_end", duration)),
        "peak_thrust": None,
        "peak_thrust_time": None,
        "total_impulse": None,
    }
    if bool(telemetry.attrs.get("has_thrust", False)):
        thrust = telemetry["thrust"].to_numpy(dtype=float)
        peak_thrust_index = int(np.argmax(thrust))
        result.update({
            "peak_thrust": float(thrust[peak_thrust_index]),
            "peak_thrust_time": float(times[peak_thrust_index]),
            "total_impulse": float(np.trapezoid(np.maximum(thrust, 0.0), times)),
        })
    telemetry.attrs["_rotpl_preview_metrics"] = result
    return result


def build_preview_template_context(
    payload: dict[str, Any],
    cfg: broadcast_overlay.Config,
    assets: Any,
    telemetry: pd.DataFrame,
    telemetry_time: float,
    pressure: float,
    thrust: float,
) -> TemplateContext:
    metrics = preview_template_metrics(telemetry, assets)
    context = TemplateContext.from_runtime(
        cfg,
        assets,
        telemetry_time,
        pressure,
        thrust,
        telemetry=telemetry,
        metrics=metrics,
    )

    # These fields belong to the ROTPL contract but are not part of the legacy
    # broadcast Config.  Keep them in the canonical context instead of changing
    # a hard-coded theme's public API.
    for binding, field_name in (
        ("test.coordinates_text", "coordinates_text"),
        ("test.oxidizer", "oxidizer"),
        ("test.fuel", "fuel"),
        ("test.ablative_material", "ablative_material"),
    ):
        value = payload_text(payload, field_name)
        if value:
            context.values[binding] = value

    times = telemetry["time"].to_numpy(dtype=float)
    progress_denominator = float(times[-1] - times[0])
    progress = (
        (telemetry_time - float(times[0])) / progress_denominator
        if progress_denominator > 0 else 0.0
    )
    frame_index = int(np.searchsorted(times, telemetry_time, side="right") - 1)
    context.values.update({
        "frame.index": max(0, min(frame_index, len(times) - 1)),
        "frame.progress": max(0.0, min(1.0, progress)),
        "metrics.pressure.average": metrics["average_pressure"],
        "metrics.pressure.max_rise_rate": metrics["max_pressure_rise"],
    })
    return context


def save_upload(field: str, job_dir: Path, extensions: set[str], required: bool = True) -> Path | None:
    uploaded = request.files.get(field)
    if not uploaded or not uploaded.filename:
        if required:
            raise RocketOverlayError(f"الملف المطلوب غير موجود: {field}")
        return None
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in extensions:
        raise RocketOverlayError(f"صيغة الملف غير مدعومة: {uploaded.filename}")
    name = secure_filename(uploaded.filename) or f"{field}{suffix}"
    path = job_dir / name
    uploaded.save(path)
    return path


def consume_upload(
    field: str,
    job_dir: Path,
    extensions: set[str],
    required: bool = True,
) -> Path | None:
    token = request.form.get(f"{field}_token", "").strip()
    if not token:
        return save_upload(field, job_dir, extensions, required)
    with chunk_uploads_lock:
        item = chunk_uploads.pop(token, None)
    if not item or item["field"] != field:
        raise RocketOverlayError(f"رمز رفع الملف غير صالح: {field}")
    source = Path(item["path"])
    if not source.exists() or source.stat().st_size != item["received"]:
        raise RocketOverlayError(f"لم يكتمل رفع الملف: {field}")
    destination = job_dir / item["filename"]
    source.replace(destination)
    fingerprint = item.get("fingerprint") or {}
    if fingerprint.get("size") and fingerprint.get("last_modified") and fingerprint.get("partial_sha256"):
        _write_fingerprint(destination, fingerprint)
    source.parent.rmdir()
    return destination


def read_columns(path: Path, sheet: str | int = 0) -> list[str]:
    try:
        if path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
            frame = pd.read_excel(path, sheet_name=sheet, nrows=12)
        else:
            frame = pd.read_csv(path, nrows=12)
    except Exception as exc:
        raise RocketOverlayError(f"تعذر قراءة ملف القياسات: {exc}") from exc
    if frame.empty and not len(frame.columns):
        raise RocketOverlayError("ملف القياسات لا يحتوي على أعمدة.")
    return [str(column) for column in frame.columns]


def form_float(name: str, default: float) -> float:
    value = request.form.get(name, "").strip()
    try:
        return default if value == "" else float(value)
    except ValueError as exc:
        raise RocketOverlayError(f"القيمة غير صحيحة للحقل: {name}") from exc


def form_int(name: str, default: int) -> int:
    value = request.form.get(name, "").strip()
    try:
        return default if value == "" else int(value)
    except ValueError as exc:
        raise RocketOverlayError(f"القيمة غير صحيحة للحقل: {name}") from exc


def run_job(job_id: str, cfg: Config) -> None:
    def progress(percent: int, message: str) -> None:
        with jobs_lock:
            jobs[job_id].update(progress=percent, message=message)

    try:
        render(cfg, progress)
        thumbnail = cfg.output.with_name(cfg.output.stem + "_thumbnail.jpg")
        summary = cfg.output.with_suffix(".json")
        with jobs_lock:
            jobs[job_id].update(
                status="complete",
                progress=100,
                message="اكتمل إنشاء الفيديو",
                result_url=f"/outputs/{job_id}/{cfg.output.name}",
                thumbnail_url=f"/outputs/{job_id}/{thumbnail.name}" if thumbnail.exists() else None,
                summary_url=f"/outputs/{job_id}/{summary.name}" if summary.exists() else None,
            )
    except Exception as exc:
        with jobs_lock:
            jobs[job_id].update(
                status="error",
                message=str(exc),
            )


@app.get("/")
def index():
    return render_template("index.html")


def _fingerprint_path(path: Path) -> Path:
    return path.with_name(path.name + ".fingerprint.json")


def _read_fingerprint(path: Path) -> dict | None:
    try:
        with _fingerprint_path(path).open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def _write_fingerprint(path: Path, fingerprint: dict) -> None:
    with _fingerprint_path(path).open("w", encoding="utf-8") as stream:
        json.dump(fingerprint, stream)


@app.post("/api/uploads/init")
def initialize_upload():
    payload = request.get_json(silent=True) or {}
    field = str(payload.get("field", ""))
    filename = secure_filename(str(payload.get("filename", "")))
    expected_size = int(payload.get("size", 0) or 0)
    last_modified = int(payload.get("last_modified", 0) or 0)
    partial_sha256 = str(payload.get("partial_sha256", "")).strip().lower()
    allowed = {
        "video": VIDEO_EXTENSIONS, "video_2": VIDEO_EXTENSIONS,
        "video_3": VIDEO_EXTENSIONS, "data": DATA_EXTENSIONS,
        "logo": LOGO_EXTENSIONS,
    }
    if field not in allowed or not filename:
        return error("بيانات الملف غير صحيحة.")
    if Path(filename).suffix.lower() not in allowed[field]:
        return error(f"صيغة الملف غير مدعومة: {filename}")
    upload_id = uuid.uuid4().hex
    upload_dir = UPLOAD_DIR / f"chunk-{upload_id}"
    upload_dir.mkdir(parents=True)
    path = upload_dir / filename
    reused = False
    # Reuse a previously uploaded file only when the client can prove it is
    # byte-identical (size + browser last-modified stamp + a hash of a
    # leading sample), never on filename/size alone — two different clips
    # can share both by coincidence.
    fingerprint = {
        "size": expected_size,
        "last_modified": last_modified,
        "partial_sha256": partial_sha256,
    }
    if expected_size > 0 and last_modified > 0 and len(partial_sha256) == 64:
        candidates = sorted(
            (
                candidate for candidate in UPLOAD_DIR.glob(f"*/{filename}")
                if not candidate.parent.name.startswith("chunk-")
                and candidate.is_file()
                and candidate.stat().st_size == expected_size
                and _read_fingerprint(candidate) == fingerprint
            ),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            path.hardlink_to(candidates[0])
            reused = True
    if not reused:
        path.touch()
    with chunk_uploads_lock:
        chunk_uploads[upload_id] = {
            "field": field, "filename": filename, "path": str(path),
            "received": expected_size if reused else 0,
            "reused": reused,
            "fingerprint": fingerprint,
        }
    return jsonify({"upload_id": upload_id, "reused": reused})


@app.post("/api/uploads/<upload_id>")
def append_upload(upload_id: str):
    chunk = request.get_data(cache=False)
    if not chunk:
        return error("جزء الملف فارغ.")
    with chunk_uploads_lock:
        item = chunk_uploads.get(upload_id)
        if not item:
            return error("جلسة رفع الملف غير موجودة.", 404)
        if item.get("reused"):
            return jsonify({"upload_id": upload_id, "received": item["received"]})
        path = Path(item["path"])
        with path.open("ab") as stream:
            stream.write(chunk)
        item["received"] += len(chunk)
        received = item["received"]
    return jsonify({"upload_id": upload_id, "received": received})


@app.post("/api/inspect")
def inspect_data():
    temp_dir = UPLOAD_DIR / f"inspect-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True)
    try:
        path = save_upload("data", temp_dir, DATA_EXTENSIONS)
        sheet_value = request.form.get("sheet", "0").strip()
        sheet: str | int = int(sheet_value) if sheet_value.isdigit() else sheet_value
        try:
            frame = (
                pd.read_excel(path, sheet_name=sheet)
                if path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}
                else pd.read_csv(path)
            )
        except Exception as exc:
            raise RocketOverlayError(
                f"تعذر قراءة ملف القياسات كاملاً: {exc}"
            ) from exc
        if frame.empty and not len(frame.columns):
            raise RocketOverlayError("ملف القياسات لا يحتوي على أعمدة.")
        columns = [str(column) for column in frame.columns]
        preview = frame.head(5)
        detected = {}
        for kind in ("time", "pressure", "thrust"):
            try:
                detected[kind] = resolve_column(preview, None, kind)
            except RocketOverlayError:
                detected[kind] = ""

        suggested_telemetry_zero_s: float | None = None
        telemetry_diagnostics: dict[str, Any] = {
            "status": "unavailable",
            "row_count": int(len(frame)),
            "time_column": detected["time"] or None,
            "pressure_column": detected["pressure"] or None,
            "thrust_column": detected["thrust"] or None,
            "has_thrust": bool(detected["thrust"]),
        }
        if detected["time"] and detected["pressure"]:
            try:
                probe = normalize_preview_telemetry(frame, {
                    "time_column": detected["time"],
                    "pressure_column": detected["pressure"],
                    "thrust_column": detected["thrust"] or "__none__",
                    "time_scale": 1.0,
                })
                suggested_telemetry_zero_s = float(
                    probe.attrs["telemetry_zero_s"]
                )
                telemetry_diagnostics = dict(
                    probe.attrs["telemetry_zero_diagnostics"]
                )
                telemetry_diagnostics.update({
                    "status": "ready",
                    "row_count": int(len(probe)),
                    "time_column": detected["time"],
                    "pressure_column": detected["pressure"],
                    "thrust_column": detected["thrust"] or None,
                    "has_thrust": bool(detected["thrust"]),
                    "peak_pressure": float(probe["pressure"].max()),
                    "peak_thrust": (
                        float(probe["thrust"].max())
                        if detected["thrust"]
                        else None
                    ),
                })
            except RocketOverlayError as exc:
                telemetry_diagnostics["reason"] = str(exc)
        sample = frame
        if len(sample) > 700:
            # Keep the preview lightweight without truncating the test trace.
            # Evenly distributed indices preserve ignition, tail-off, and the
            # final timestamp instead of returning only the first 700 rows.
            indices = np.linspace(0, len(sample) - 1, num=700, dtype=int)
            sample = sample.iloc[np.unique(indices)]

        cleanup_preview_sessions(reserve=1)
        preview_id = uuid.uuid4().hex
        now = time.monotonic()
        with preview_sessions_lock:
            preview_sessions[preview_id] = {
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
        return jsonify({
            "preview_id": preview_id,
            "columns": columns,
            "detected": detected,
            "rows": preview.fillna("").astype(str).to_dict(orient="records"),
            "series": sample.fillna("").astype(str).to_dict(orient="records"),
            "suggested_telemetry_zero_s": suggested_telemetry_zero_s,
            "telemetry_diagnostics": telemetry_diagnostics,
            "default_scene": default_scene_config(),
        })
    except RocketOverlayError as exc:
        return error(str(exc))
    finally:
        for child in temp_dir.glob("*"):
            child.unlink(missing_ok=True)
        temp_dir.rmdir()


@app.route(
    "/api/previews/<preview_id>/logo", methods=["POST", "PUT", "DELETE"]
)
@app.route(
    "/api/preview/<preview_id>/logo", methods=["POST", "PUT", "DELETE"]
)
def preview_logo(preview_id: str):
    session = get_preview_session(preview_id)
    if session is None:
        return error("جلسة المعاينة غير موجودة أو انتهت صلاحيتها.", 404)

    if request.method == "DELETE":
        old_path: Path | None = None
        with session["cache_lock"]:
            with preview_sessions_lock:
                current = preview_sessions.get(preview_id)
                if current is not session:
                    return error("جلسة المعاينة غير موجودة أو انتهت صلاحيتها.", 404)
                if current.get("logo_path"):
                    old_path = Path(current["logo_path"])
                current["logo_path"] = None
                current["logo_filename"] = None
                current["logo_revision"] += 1
                current["assets_cache"].clear()
                current["touched_at"] = time.monotonic()
            if old_path is not None:
                old_path.unlink(missing_ok=True)
        return jsonify({"preview_id": preview_id, "logo": None})

    uploaded = request.files.get("logo")
    if not uploaded or not uploaded.filename:
        return error("اختر ملف شعار لرفعه.")
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in LOGO_EXTENSIONS:
        return error("صيغة الشعار غير مدعومة. استخدم PNG أو JPG أو WEBP.")

    raw = uploaded.read(PREVIEW_LOGO_MAX_BYTES + 1)
    if len(raw) > PREVIEW_LOGO_MAX_BYTES:
        return error("حجم الشعار يتجاوز الحد المسموح وهو 10 ميجابايت.", 413)
    decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None or decoded.ndim not in {2, 3}:
        return error("تعذر قراءة صورة الشعار أو أن الملف تالف.")
    if decoded.shape[0] > 8192 or decoded.shape[1] > 8192:
        return error("أبعاد الشعار كبيرة جداً؛ الحد الأقصى 8192 بكسل.")

    safe_name = secure_filename(uploaded.filename) or f"logo{suffix}"
    session_dir = PREVIEW_DIR / preview_id
    session_dir.mkdir(parents=True, exist_ok=True)
    new_path = session_dir / f"logo-{uuid.uuid4().hex}{suffix}"
    try:
        new_path.write_bytes(raw)
    except OSError as exc:
        return error(f"تعذر حفظ الشعار المؤقت: {exc}", 500)

    old_path = None
    with session["cache_lock"]:
        with preview_sessions_lock:
            current = preview_sessions.get(preview_id)
            if current is not session:
                new_path.unlink(missing_ok=True)
                return error("جلسة المعاينة غير موجودة أو انتهت صلاحيتها.", 404)
            if current.get("logo_path"):
                old_path = Path(current["logo_path"])
            current["logo_path"] = new_path
            current["logo_filename"] = safe_name
            current["logo_revision"] += 1
            current["assets_cache"].clear()
            current["touched_at"] = time.monotonic()
        if old_path is not None and old_path != new_path:
            old_path.unlink(missing_ok=True)
    return jsonify({
        "preview_id": preview_id,
        "logo": {"filename": safe_name, "uploaded": True},
    })


@app.post("/api/previews/<preview_id>/render")
@app.post("/api/preview/<preview_id>/render")
def render_preview(preview_id: str):
    session = get_preview_session(preview_id)
    if session is None:
        return error("جلسة المعاينة غير موجودة أو انتهت صلاحيتها.", 404)

    try:
        payload = preview_payload()
        width = payload_int(payload, "width", 1280)
        height = payload_int(payload, "height", 720)
        if width < 320 or height < 180:
            raise RocketOverlayError("أبعاد المعاينة يجب ألا تقل عن 320×180.")
        if (
            width > PREVIEW_MAX_WIDTH
            or height > PREVIEW_MAX_HEIGHT
            or width * height > PREVIEW_MAX_PIXELS
        ):
            raise RocketOverlayError(
                "أبعاد المعاينة تتجاوز الحد الآمن 1920×1080."
            )
        custom_template_requested = bool(payload_text(payload, "template_id"))
        if custom_template_requested:
            # ROTPL previews are authored and rasterized at the 1920x1080
            # master.  The browser scales this PNG for display; it must not ask
            # the server to throw away typography/detail while video is playing.
            width, height = 1920, 1080

        theme = payload_text(
            payload, "broadcast_theme", "launch", "theme"
        ).lower()
        theme = {
            "a": "launch",
            "b": "mission_control",
            "d": "stellar_console",
            "rocket_overlay_broadcast": "launch",
            "rocket_overlay_broadcast_1": "mission_control",
            "rocket_overlay_broadcast_3": "stellar_console",
            "range_line": "stellar_console",
        }.get(theme, theme)
        if theme not in broadcast_overlay.BROADCAST_THEMES:
            raise RocketOverlayError("ثيم المعاينة المحدد غير معروف.")

        accent = payload_text(payload, "accent", "#38BDF8")
        if (
            len(accent) != 7
            or not accent.startswith("#")
            or any(char not in "0123456789abcdefABCDEF" for char in accent[1:])
        ):
            raise RocketOverlayError("لون الهوية يجب أن يكون بصيغة #RRGGBB.")

        telemetry_key = preview_telemetry_cache_key(payload)
        pressure_limit_text = payload_text(payload, "pressure_limit")
        with session["cache_lock"]:
            template_renderer, resolved_template, template_cache_hit = (
                resolve_preview_template(payload, session)
            )
            telemetry_cache = session["telemetry_cache"]
            telemetry = telemetry_cache.get(telemetry_key)
            telemetry_cache_hit = telemetry is not None
            if telemetry is None:
                telemetry = normalize_preview_telemetry(session["frame"], payload)
                preview_cache_store(
                    telemetry_cache,
                    telemetry_key,
                    telemetry,
                    PREVIEW_TELEMETRY_CACHE_SIZE,
                )
            else:
                # Refresh insertion order so rapidly toggled column mappings
                # retain their most recently used normalized frame.
                telemetry_cache.pop(telemetry_key)
                telemetry_cache[telemetry_key] = telemetry

            logo_path = session.get("logo_path")
            logo_revision = int(session.get("logo_revision", 0))
            cfg = broadcast_overlay.Config(
                video=Path("__preview_video__"),
                data=Path("__preview_telemetry__"),
                output=Path("__preview_output__.png"),
                title=payload_text(
                    payload, "title", "ROCKET MOTOR STATIC TEST"
                ) or "ROCKET MOTOR STATIC TEST",
                subtitle=payload_text(payload, "subtitle", "STATIC FIRE TEST")
                or "STATIC FIRE TEST",
                pressure_unit=payload_text(payload, "pressure_unit", "bar") or "bar",
                thrust_unit=payload_text(payload, "thrust_unit", "N") or "N",
                test_date=payload_text(payload, "test_date"),
                motor_type=payload_text(payload, "motor_type"),
                propellant=payload_text(payload, "propellant"),
                run_number=payload_text(payload, "run_number"),
                organization_name=payload_text(
                    payload, "organization_name", "STELLAR KINETICS"
                ) or "STELLAR KINETICS",
                test_site=payload_text(
                    payload, "test_site", "ETLAQ SPACEPORT - DUQM, OMAN"
                ) or "ETLAQ SPACEPORT - DUQM, OMAN",
                footer_tagline=payload_text(
                    payload,
                    "footer_tagline",
                    "STELLAR KINETICS - ENGINEERING THE VOID",
                ) or "STELLAR KINETICS - ENGINEERING THE VOID",
                camera_label=payload_text(payload, "camera_label", "CAM 01") or "CAM 01",
                capture_fps=payload_text(payload, "capture_fps", "120 FPS") or "120 FPS",
                pressure_limit=(
                    payload_float(payload, "pressure_limit", 0.0)
                    if pressure_limit_text else None
                ),
                width=width,
                height=height,
                accent=accent,
                broadcast_theme=theme,
                show_chart=payload_bool(payload, "show_chart", True),
                show_phases=payload_bool(payload, "show_phases", True),
                logo=Path(logo_path) if logo_path else None,
            )
            assets_key = preview_assets_cache_key(
                cfg, telemetry_key, logo_revision
            )
            assets_cache = session["assets_cache"]
            assets = assets_cache.get(assets_key)
            assets_cache_hit = assets is not None
            if assets is None:
                assets = broadcast_overlay.build_assets(cfg, telemetry)
                preview_cache_store(
                    assets_cache,
                    assets_key,
                    assets,
                    PREVIEW_ASSETS_CACHE_SIZE,
                )
            else:
                assets_cache.pop(assets_key)
                assets_cache[assets_key] = assets
        telemetry_time = payload_float(
            payload, "time", 0.0, "telemetry_time", "time_s"
        )
        pressure, thrust = broadcast_overlay.interpolate_telemetry(
            telemetry, telemetry_time
        )
        if template_renderer is None:
            overlay = broadcast_overlay.compose_overlay_bgra(
                cfg, assets, telemetry_time, pressure, thrust
            )
        else:
            context = build_preview_template_context(
                payload,
                cfg,
                assets,
                telemetry,
                telemetry_time,
                pressure,
                thrust,
            )
            overlay = template_renderer.render_bgra(
                context, output_size=(1920, 1080)
            )
        encoded_ok, encoded = cv2.imencode(
            ".png", overlay, [cv2.IMWRITE_PNG_COMPRESSION, 3]
        )
        if not encoded_ok:
            raise RocketOverlayError("تعذر ترميز صورة المعاينة بصيغة PNG.")
    except RotplNotFoundError as exc:
        return error(f"تعذر إنشاء المعاينة: {exc}", 404)
    except (
        RocketOverlayError,
        broadcast_overlay.BroadcastOverlayError,
        RotplError,
        RotplRenderError,
    ) as exc:
        return error(f"تعذر إنشاء المعاينة: {exc}")
    except (KeyError, TypeError, ValueError, cv2.error) as exc:
        return error(f"تعذر إنشاء المعاينة بسبب بيانات غير صالحة: {exc}")
    except Exception as exc:
        return error(f"حدث خطأ داخلي أثناء إنشاء المعاينة: {exc}", 500)

    response = Response(encoded.tobytes(), mimetype="image/png")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Preview-Id"] = preview_id
    response.headers["X-Preview-Width"] = str(width)
    response.headers["X-Preview-Height"] = str(height)
    response.headers["X-Preview-Telemetry-Cache"] = (
        "hit" if telemetry_cache_hit else "miss"
    )
    response.headers["X-Preview-Assets-Cache"] = (
        "hit" if assets_cache_hit else "miss"
    )
    response.headers["X-Telemetry-Time"] = f"{telemetry_time:.9g}"
    response.headers["X-Telemetry-Pressure"] = f"{pressure:.9g}"
    response.headers["X-Telemetry-Has-Thrust"] = (
        "true" if telemetry.attrs.get("has_thrust", False) else "false"
    )
    response.headers["X-Telemetry-Thrust"] = (
        f"{thrust:.9g}" if telemetry.attrs.get("has_thrust", False) else "N/A"
    )
    response.headers["X-Telemetry-Peak-Pressure"] = f"{assets.p_peak:.9g}"
    response.headers["X-Telemetry-Peak-Thrust"] = (
        f"{assets.f_peak:.9g}" if assets.has_thrust else "N/A"
    )
    response.headers["X-Telemetry-Zero"] = f"{telemetry.attrs['telemetry_zero_s']:.9g}"
    response.headers["X-Telemetry-Zero-Source"] = str(
        telemetry.attrs["telemetry_zero_source"]
    )
    if resolved_template is None:
        response.headers["X-Preview-Template-Renderer"] = "hardcoded"
    else:
        response.headers["X-Preview-Template-Renderer"] = "rotpl-declarative-v1"
        response.headers["X-Preview-Template-Id"] = resolved_template.id
        response.headers["X-Preview-Template-Version"] = resolved_template.version
        response.headers["X-Preview-Template-Sha256"] = resolved_template.sha256
        response.headers["X-Preview-Template-Cache"] = (
            "hit" if template_cache_hit else "miss"
        )
    return response


@app.post("/api/jobs")
def create_job():
    job_id = uuid.uuid4().hex
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True)
    try:
        video = consume_upload("video", job_dir, VIDEO_EXTENSIONS)
        video_2 = consume_upload("video_2", job_dir, VIDEO_EXTENSIONS, required=False)
        video_3 = consume_upload("video_3", job_dir, VIDEO_EXTENSIONS, required=False)
        data = consume_upload("data", job_dir, DATA_EXTENSIONS)
        logo = consume_upload("logo", job_dir, LOGO_EXTENSIONS, required=False)
        source_capture = cv2.VideoCapture(str(video))
        source_width = int(source_capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        source_height = int(source_capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        source_fps = float(source_capture.get(cv2.CAP_PROP_FPS))
        source_capture.release()
        if source_width <= 0 or source_height <= 0:
            raise RocketOverlayError("تعذر تحديد دقة الفيديو الأصلية.")
        # Delivery codecs require even dimensions; normal camera files already
        # satisfy this, while unusual sources are rounded safely.
        source_width -= source_width % 2
        source_height -= source_height % 2

        template_id = request.form.get("template_id", "").strip()
        template_version = request.form.get("template_version", "").strip()
        requested_template_sha = request.form.get("sha256", "").strip().lower()
        selected_template: ResolvedTemplate | None = None
        if any((template_id, template_version, requested_template_sha)):
            if not all((template_id, template_version, requested_template_sha)):
                raise RocketOverlayError(
                    "يجب إرسال معرّف القالب وإصداره وبصمته الرقمية معاً."
                )
            if len(requested_template_sha) != 64 or any(
                character not in "0123456789abcdef"
                for character in requested_template_sha
            ):
                raise RocketOverlayError("بصمة حزمة القالب SHA-256 غير صالحة.")
            template_record = template_registry.get(template_id, template_version)
            validation = template_record.get("validation", {})
            if not validation.get("activatable", False):
                reasons = (
                    validation.get("blocked_reasons")
                    or validation.get("errors")
                    or []
                )
                details = "; ".join(str(reason) for reason in reasons)
                raise RocketOverlayError(
                    "لا يمكن تصدير قالب محظور أو غير مكتمل"
                    + (f": {details}" if details else ".")
                )
            selected_template = template_registry.resolve(
                template_id, template_version
            )
            if not hmac.compare_digest(
                selected_template.sha256.lower(), requested_template_sha
            ):
                raise RocketOverlayError(
                    "بصمة القالب المحدد لا تطابق النسخة المثبتة؛ أعد اختياره."
                )

        resolution_value = request.form.get("resolution", "source")
        resolution_notice = ""
        if resolution_value == "source":
            output_width = source_width - source_width % 2
            output_height = source_height - source_height % 2
        else:
            resolution = resolution_value.split("x")
            if len(resolution) != 2:
                raise RocketOverlayError("دقة الإخراج غير صحيحة.")
            output_width, output_height = map(int, resolution)
            source_ratio = source_width / source_height
            output_ratio = output_width / output_height
            same_shape = abs(source_ratio - output_ratio) <= 0.01
            requests_upscale = (
                output_width > source_width or output_height > source_height
            )
            if same_shape and requests_upscale and selected_template is None:
                requested_label = f"{output_width}×{output_height}"
                output_width, output_height = source_width, source_height
                resolution_notice = (
                    f"تم تجاهل التكبير إلى {requested_label} واستخدام دقة المصدر "
                    f"{source_width}×{source_height}؛ التكبير لا يضيف تفاصيل للصورة."
                )

        sheet_value = request.form.get("sheet", "0").strip()
        sheet: str | int = int(sheet_value) if sheet_value.lstrip("-").isdigit() else sheet_value
        telemetry_zero = request.form.get("telemetry_zero_s", "").strip()
        output_style = request.form.get("output_style", "engineering")
        style_video_fraction = {
            "engineering": 0.70,
            "presentation": 0.76,
            "archive": 0.66,
            "social": 0.62,
        }.get(output_style, 0.70)
        broadcast_theme = request.form.get("broadcast_theme", "launch")
        if broadcast_theme == "range_line":
            broadcast_theme = "stellar_console"
        if broadcast_theme not in {
            "launch", "mission_control", "stellar_console"
        }:
            raise RocketOverlayError("ثيم البث المحدد غير معروف.")
        raw_scene = request.form.get("scene_config", "").strip()
        scene_config = validate_scene_config(json.loads(raw_scene)) if raw_scene else None
        cfg = Config(
            video=video,
            video_2=video_2,
            video_3=video_3,
            camera_2_ignition_s=form_float("camera_2_ignition_s", 0.0),
            camera_3_ignition_s=form_float("camera_3_ignition_s", 0.0),
            camera_mode=request.form.get("camera_mode", "single"),
            camera_3_switch_delay_s=form_float("camera_3_switch_delay_s", 2.0),
            camera_2_crop_zoom=form_float("camera_2_crop_zoom", 1.0),
            camera_2_crop_x=form_float("camera_2_crop_x", 0.5),
            camera_2_crop_y=form_float("camera_2_crop_y", 0.5),
            camera_3_crop_zoom=form_float("camera_3_crop_zoom", 1.0),
            camera_3_crop_x=form_float("camera_3_crop_x", 0.5),
            camera_3_crop_y=form_float("camera_3_crop_y", 0.5),
            data=data,
            output=OUTPUT_DIR / job_id / "rocket_test_overlay.mp4",
            sheet=sheet,
            time_column=request.form.get("time_column") or None,
            pressure_column=request.form.get("pressure_column") or None,
            thrust_column=request.form.get("thrust_column") or None,
            ignition_video_s=form_float("ignition_video_s", 0.0),
            telemetry_zero_s=float(telemetry_zero) if telemetry_zero else None,
            time_scale=form_float("time_scale", 1.0),
            title=request.form.get("title", "").strip() or "ROCKET MOTOR STATIC TEST",
            subtitle=request.form.get("subtitle", "").strip() or "STATIC FIRE TEST",
            pressure_unit=request.form.get("pressure_unit", "").strip() or "bar",
            thrust_unit=request.form.get("thrust_unit", "").strip() or "N",
            width=output_width,
            height=output_height,
            video_fraction=form_float("video_fraction", style_video_fraction),
            logo=logo,
            test_date=request.form.get("test_date", "").strip(),
            motor_type=request.form.get("motor_type", "").strip(),
            propellant=request.form.get("propellant", "").strip(),
            run_number=request.form.get("run_number", "").strip(),
            organization_name=(
                request.form.get("organization_name", "").strip()
                or "STELLAR KINETICS"
            ),
            test_site=(
                request.form.get("test_site", "").strip()
                or "ETLAQ SPACEPORT - DUQM, OMAN"
            ),
            coordinates_text=request.form.get("coordinates_text", "").strip(),
            oxidizer=request.form.get("oxidizer", "").strip(),
            fuel=request.form.get("fuel", "").strip(),
            ablative_material=request.form.get(
                "ablative_material", ""
            ).strip(),
            footer_tagline=(
                request.form.get("footer_tagline", "").strip()
                or "STELLAR KINETICS - ENGINEERING THE VOID"
            ),
            camera_label=request.form.get("camera_label", "").strip() or "CAM 01",
            capture_fps=(
                request.form.get("capture_fps", "").strip()
                or (f"{source_fps:g} FPS" if source_fps > 0 else "120 FPS")
            ),
            output_style=output_style,
            broadcast_theme=broadcast_theme,
            accent=request.form.get("accent", "#38BDF8"),
            show_chart=request.form.get("show_chart", "true") == "true",
            show_phases=request.form.get("show_phases", "true") == "true",
            intro_duration_s=form_float("intro_duration_s", 0.0),
            outro_duration_s=form_float("outro_duration_s", 2.5),
            reveal_chart=request.form.get("reveal_chart") == "true",
            enhance_video=request.form.get("enhance_video") == "true",
            denoise_video=request.form.get("denoise_video") == "true",
            stabilize_video=request.form.get("stabilize_video") == "true",
            crop_zoom=form_float("crop_zoom", 1.0),
            crop_x=form_float("crop_x", 0.5),
            crop_y=form_float("crop_y", 0.5),
            pressure_limit=(
                form_float("pressure_limit", 0.0)
                if request.form.get("pressure_limit", "").strip() else None
            ),
            thumbnail=request.form.get("thumbnail") == "true",
            delivery_codec=request.form.get("delivery_codec", "h264"),
            keep_audio=request.form.get("keep_audio") == "true",
            preserve_source_quality=(
                request.form.get("preserve_source_quality", "false") == "true"
            ),
            scene_config=scene_config,
            template_id=selected_template.id if selected_template else None,
            template_version=(
                selected_template.version if selected_template else None
            ),
            template_sha256=(
                selected_template.sha256 if selected_template else None
            ),
            template_path=(
                selected_template.files_path if selected_template else None
            ),
        )
        cfg.output.parent.mkdir(parents=True, exist_ok=True)
        with jobs_lock:
            jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0,
                "message": "بانتظار بدء المعالجة",
                "notice": resolution_notice or None,
                "output_resolution": f"{output_width}x{output_height}",
                "template": (
                    {
                        "id": selected_template.id,
                        "version": selected_template.version,
                        "sha256": selected_template.sha256,
                    }
                    if selected_template else None
                ),
            }
        thread = threading.Thread(target=run_job, args=(job_id, cfg), daemon=True)
        thread.start()
        return jsonify(jobs[job_id]), 202
    except (
        RocketOverlayError,
        RotplError,
        RotplRenderError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return error(str(exc))


@app.get("/api/default-scene")
def default_scene():
    return jsonify(default_scene_config())


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        return jsonify(job) if job else error("المهمة غير موجودة.", 404)


def template_api_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return registry metadata with stable, browser-facing resource URLs."""
    result = dict(record)
    if result.get("has_thumbnail"):
        result["thumbnail_url"] = (
            f"/api/templates/{result['id']}/thumbnail?version={result['version']}"
        )
    else:
        result["thumbnail_url"] = None
    return result


@app.get("/api/templates")
def list_templates():
    try:
        listing = template_registry.list()
        listing["templates"] = [
            template_api_record(record) for record in listing["templates"]
        ]
        return jsonify(listing)
    except RotplError as exc:
        return error(f"تعذر قراءة سجل القوالب: {exc}", 500)


@app.post("/api/templates")
@app.post("/api/templates/upload")
def upload_template():
    # Reject normal browser uploads before Werkzeug parses/spools a huge
    # multipart body.  The stream installer enforces the exact archive limit
    # again, including for requests without Content-Length.
    if (
        request.content_length is not None
        and request.content_length > MAX_ARCHIVE_BYTES + 1024 * 1024
    ):
        return error("حجم حزمة القالب يتجاوز الحد المسموح وهو 64 ميجابايت.", 413)
    uploaded = request.files.get("template")
    if not uploaded or not uploaded.filename:
        return error("اختر حزمة قالب بصيغة .rotpl.")
    original_suffix = Path(uploaded.filename).suffix.lower()
    filename = secure_filename(uploaded.filename)
    if original_suffix == ".rotpl" and Path(filename).suffix.lower() != ".rotpl":
        filename = "template.rotpl"
    filename = filename or "template.rotpl"
    try:
        record = template_registry.install_stream(uploaded.stream, filename)
        return jsonify({"template": template_api_record(record)}), 201
    except RotplValidationError as exc:
        return jsonify({"error": str(exc), "validation": exc.report}), 400
    except RotplConflictError as exc:
        return error(str(exc), 409)
    except RotplError as exc:
        return error(f"تعذر تثبيت القالب: {exc}", 500)


@app.get("/api/templates/<template_id>")
def template_details(template_id: str):
    version = request.args.get("version", "").strip() or None
    try:
        record = template_registry.get(template_id, version)
        manifest, layout = template_registry.manifest_and_layout(
            record["id"], record["version"]
        )
        return jsonify({
            "template": template_api_record(record),
            "manifest": manifest,
            "layout": layout,
        })
    except RotplNotFoundError as exc:
        return error(str(exc), 404)
    except RotplValidationError as exc:
        return error(str(exc), 400)
    except RotplError as exc:
        return error(f"تعذر قراءة القالب: {exc}", 500)


@app.post("/api/templates/<template_id>/activate")
def activate_template(template_id: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not str(payload.get("version", "")).strip():
        return error("رقم إصدار القالب مطلوب.")
    version = str(payload["version"]).strip()
    try:
        active = template_registry.activate(template_id, version)
        record = template_registry.get(template_id, version)
        return jsonify({
            "active": active,
            "template": template_api_record(record),
        })
    except RotplNotFoundError as exc:
        return error(str(exc), 404)
    except RotplValidationError as exc:
        return error(str(exc), 400)
    except RotplActivationError as exc:
        return error(str(exc), 409)
    except RotplError as exc:
        return error(f"تعذر تفعيل القالب: {exc}", 500)


@app.post("/api/templates/rollback")
def rollback_template():
    try:
        active = template_registry.rollback()
        record = template_registry.get(active["id"], active["version"])
        return jsonify({
            "active": active,
            "template": template_api_record(record),
        })
    except RotplNotFoundError as exc:
        return error(str(exc), 404)
    except RotplActivationError as exc:
        return error(str(exc), 409)
    except RotplError as exc:
        return error(f"تعذر استرجاع القالب السابق: {exc}", 500)


@app.get("/api/templates/<template_id>/thumbnail")
def template_thumbnail(template_id: str):
    version = request.args.get("version", "").strip()
    if not version:
        return error("رقم إصدار القالب مطلوب.")
    try:
        thumbnail = template_registry.asset_path(
            template_id, version, "preview/thumbnail.png"
        )
        response = send_from_directory(
            thumbnail.parent, thumbnail.name, conditional=True
        )
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response
    except RotplNotFoundError as exc:
        return error(str(exc), 404)
    except RotplValidationError as exc:
        return error(str(exc), 400)
    except RotplError as exc:
        return error(f"تعذر قراءة صورة القالب: {exc}", 500)


@app.get("/outputs/<job_id>/<path:filename>")
def output_file(job_id: str, filename: str):
    return send_from_directory(OUTPUT_DIR / job_id, filename, conditional=True)


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
