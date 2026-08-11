"""Safe declarative renderer for Rocket Overlay Template packages (``.rotpl``).

The module deliberately does not execute code from a template.  A package is
read as a ZIP (or an unpacked directory), validated, and rendered with Pillow
from the declarations in ``manifest.json`` and ``layout.json``.  Coordinates
are authored on the template canvas (normally 1920x1080) and are scaled to the
requested output size at render time.

The public surface is intentionally small so the preview and delivery paths
can use the exact same compositor::

    package = RotplPackage.open("stellar-kinetics.rotpl")
    renderer = RotplRenderer(package)
    context = TemplateContext.from_runtime(cfg, assets, t, pressure, thrust,
                                           telemetry, metrics)
    rgba = renderer.render(context, output_size=(1920, 1080))
    bgr = renderer.compose_bgr(video_frame, context)
    bgra = renderer.render_bgra(context, output_size=(1920, 1080))

Fonts are always package-local.  In particular, ``font_fallback=none`` is
honoured strictly: a missing or invalid TTF prevents the package from loading.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont


class RotplError(RuntimeError):
    """Base class for user-facing template errors."""


class RotplValidationError(RotplError):
    """The package is malformed, unsafe, or declares unsupported features."""


class RotplBindingError(RotplError):
    """A required runtime binding is absent."""


SUPPORTED_ELEMENT_TYPES = {
    "gradient_scrim",
    "rect",
    "line",
    "text",
    "logo",
    "image",
    "vertical_gauge",
    "bar_gauge",
    "phase_list",
}


_MISSING = object()


def _finite(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, (float, np.floating)):
        return math.isfinite(float(value))
    return True


def _safe_member_name(name: object) -> str:
    """Return a normalized, package-relative POSIX member name."""
    text = str(name or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise RotplValidationError(f"Unsafe template member path: {text!r}")
    return path.as_posix()


def _json_object(data: bytes, member: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RotplValidationError(f"Invalid JSON in {member}: {exc}") from exc
    if not isinstance(value, dict):
        raise RotplValidationError(f"{member} must contain a JSON object.")
    return value


class RotplPackage:
    """Validated access to a ``.rotpl`` ZIP or unpacked template directory."""

    MAX_MEMBER_BYTES = 64 * 1024 * 1024
    MAX_TOTAL_BYTES = 256 * 1024 * 1024

    def __init__(self, source: Path):
        self.source = source
        self._zip: Optional[zipfile.ZipFile] = None
        self._members: set[str] = set()
        self._font_bytes: dict[str, bytes] = {}

        if source.is_dir():
            self._members = {
                path.relative_to(source).as_posix()
                for path in source.rglob("*")
                if path.is_file()
            }
        elif source.is_file():
            if not zipfile.is_zipfile(source):
                raise RotplValidationError(
                    f"Template package is not a valid ZIP/ROTPL file: {source}"
                )
            try:
                self._zip = zipfile.ZipFile(source, "r")
            except (OSError, zipfile.BadZipFile) as exc:
                raise RotplValidationError(f"Could not open template: {exc}") from exc
            total = 0
            for info in self._zip.infolist():
                name = _safe_member_name(info.filename.rstrip("/"))
                if info.is_dir():
                    continue
                if info.file_size > self.MAX_MEMBER_BYTES:
                    raise RotplValidationError(
                        f"Template member is too large: {name}"
                    )
                total += info.file_size
                if total > self.MAX_TOTAL_BYTES:
                    raise RotplValidationError("Template package is too large.")
                if name in self._members:
                    raise RotplValidationError(f"Duplicate template member: {name}")
                self._members.add(name)
        else:
            raise RotplValidationError(f"Template package was not found: {source}")

        self.manifest = _json_object(self.read_bytes("manifest.json"), "manifest.json")
        entry = _safe_member_name(self.manifest.get("entry", "layout.json"))
        self.layout = _json_object(self.read_bytes(entry), entry)
        self._validate()

    @classmethod
    def open(cls, source: str | Path) -> "RotplPackage":
        return cls(Path(source).expanduser().resolve())

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __enter__(self) -> "RotplPackage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def template_id(self) -> str:
        return str(self.manifest["id"])

    @property
    def template_version(self) -> str:
        return str(self.manifest["template_version"])

    @property
    def canvas_size(self) -> tuple[int, int]:
        canvas = self.manifest["canvas"]
        return int(canvas["width"]), int(canvas["height"])

    def has_member(self, member: str) -> bool:
        try:
            return _safe_member_name(member) in self._members
        except RotplValidationError:
            return False

    def read_bytes(self, member: str) -> bytes:
        name = _safe_member_name(member)
        if name not in self._members:
            raise RotplValidationError(f"Missing template member: {name}")
        try:
            if self._zip is not None:
                return self._zip.read(name)
            return (self.source / PurePosixPath(name)).read_bytes()
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            raise RotplValidationError(
                f"Could not read template member {name}: {exc}"
            ) from exc

    def read_json(self, member: str) -> dict[str, Any]:
        return _json_object(self.read_bytes(member), member)

    def read_image(self, member: str) -> Image.Image:
        try:
            image = Image.open(io.BytesIO(self.read_bytes(member)))
            image.load()
            return image.convert("RGBA")
        except (OSError, ValueError) as exc:
            raise RotplValidationError(
                f"Invalid image in template member {member}: {exc}"
            ) from exc

    def font(self, font_id: str, size: int) -> ImageFont.FreeTypeFont:
        if size <= 0:
            raise RotplValidationError("Font size must be positive.")
        font_data = self._font_bytes.get(font_id)
        if font_data is None:
            raise RotplValidationError(f"Unknown template font_id: {font_id}")
        try:
            return ImageFont.truetype(io.BytesIO(font_data), size=size)
        except OSError as exc:
            raise RotplValidationError(
                f"Could not instantiate font {font_id}: {exc}"
            ) from exc

    def _validate(self) -> None:
        manifest = self.manifest
        if manifest.get("schema") != "rocket-overlay-template":
            raise RotplValidationError("Unsupported template manifest schema.")
        for name in ("id", "template_version", "canvas"):
            if name not in manifest:
                raise RotplValidationError(f"manifest.json is missing {name}.")
        canvas = manifest.get("canvas")
        if not isinstance(canvas, dict):
            raise RotplValidationError("manifest.canvas must be an object.")
        try:
            width, height = int(canvas["width"]), int(canvas["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RotplValidationError("Invalid template canvas dimensions.") from exc
        if width <= 0 or height <= 0 or width > 8192 or height > 8192:
            raise RotplValidationError("Template canvas dimensions are unsupported.")
        if canvas.get("alpha_mode", "straight") != "straight":
            raise RotplValidationError("Only straight RGBA alpha is supported.")

        layout_canvas = self.layout.get("canvas", {})
        if (
            int(layout_canvas.get("width", width)) != width
            or int(layout_canvas.get("height", height)) != height
        ):
            raise RotplValidationError(
                "manifest.json and layout.json canvas dimensions do not match."
            )
        elements = self.layout.get("elements")
        if not isinstance(elements, list):
            raise RotplValidationError("layout.elements must be an array.")
        seen: set[str] = set()
        for element in elements:
            if not isinstance(element, dict):
                raise RotplValidationError("Every layout element must be an object.")
            element_id = str(element.get("id", "")).strip()
            if not element_id or element_id in seen:
                raise RotplValidationError(
                    "Layout element IDs must be present and unique."
                )
            seen.add(element_id)
            kind = element.get("type")
            if kind not in SUPPORTED_ELEMENT_TYPES:
                raise RotplValidationError(
                    f"Unsupported element type {kind!r} in {element_id}."
                )
            try:
                float(element.get("z", 0))
            except (TypeError, ValueError) as exc:
                raise RotplValidationError(
                    f"Invalid z value in element {element_id}."
                ) from exc
        for element in elements:
            fallback = element.get("fallback_element")
            if fallback is not None and str(fallback) not in seen:
                raise RotplValidationError(
                    f"Unknown fallback element {fallback!r} in {element['id']}."
                )

        fonts = manifest.get("fonts", [])
        if not isinstance(fonts, list):
            raise RotplValidationError("manifest.fonts must be an array.")
        font_ids: set[str] = set()
        for item in fonts:
            if not isinstance(item, dict):
                raise RotplValidationError("Every font declaration must be an object.")
            font_id = str(item.get("font_id", "")).strip()
            if not font_id or font_id in font_ids:
                raise RotplValidationError("Font IDs must be present and unique.")
            font_ids.add(font_id)
            path = _safe_member_name(item.get("file"))
            if not self.has_member(path):
                raise RotplValidationError(
                    f"Required template font is missing: {path} ({font_id})"
                )
            data = self.read_bytes(path)
            try:
                ImageFont.truetype(io.BytesIO(data), size=16)
            except OSError as exc:
                raise RotplValidationError(
                    f"Required template font is invalid: {path} ({font_id})"
                ) from exc
            self._font_bytes[font_id] = data
        for element in elements:
            if element.get("type") == "text":
                font_id = str(element.get("font_id", ""))
                if font_id not in font_ids:
                    raise RotplValidationError(
                        f"Unknown font_id {font_id!r} in {element['id']}."
                    )
            if element.get("type") == "phase_list":
                columns = element.get("columns", {})
                for column in columns.values() if isinstance(columns, dict) else ():
                    if isinstance(column, dict) and "font_id" in column:
                        if str(column["font_id"]) not in font_ids:
                            raise RotplValidationError(
                                f"Unknown phase-list font_id in {element['id']}."
                            )


class TemplateContext:
    """Flat binding map plus an adapter for the application's runtime data."""

    def __init__(self, values: Optional[Mapping[str, Any]] = None):
        self.values: dict[str, Any] = dict(values or {})

    def copy(self) -> "TemplateContext":
        return TemplateContext(self.values)

    def set_default(self, path: str, value: Any) -> None:
        if not self.available(path):
            self.values[path] = value

    def resolve(self, path: object, default: Any = None) -> Any:
        key = str(path or "")
        if key in self.values:
            return self.values[key]
        current: Any = self.values
        for part in key.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            else:
                return default
        return current

    def available(self, path: object) -> bool:
        key = str(path or "")
        value = self.resolve(key, _MISSING)
        if value is _MISSING or not _finite(value):
            return False
        parts = key.split(".")
        if len(parts) >= 3 and parts[0] == "telemetry" and parts[2] != "available":
            available = self.resolve(f"telemetry.{parts[1]}.available", True)
            if available is False:
                return False
        if len(parts) >= 3 and parts[0] == "metrics":
            available = self.resolve(f"telemetry.{parts[1]}.available", True)
            if available is False:
                return False
        return True

    @classmethod
    def from_runtime(
        cls,
        cfg: object,
        assets: object,
        t: float,
        pressure: float,
        thrust: Optional[float],
        telemetry: Optional[object] = None,
        metrics: Optional[object] = None,
    ) -> "TemplateContext":
        """Adapt the existing render-loop objects to stable ROTPL bindings.

        ``assets`` may be the current ``broadcast_overlay.OverlayAssets`` or a
        mapping with equivalent keys.  ``metrics`` may be ``TestMetrics`` or a
        dictionary.  No renderer imports are needed, avoiding a circular
        dependency between the legacy compositor and this module.
        """

        def read(source: object, *names: str, default: Any = None) -> Any:
            for name in names:
                if isinstance(source, Mapping) and name in source:
                    return source[name]
                if source is not None and hasattr(source, name):
                    return getattr(source, name)
            return default

        def cfg_text(*names: str, default: str = "") -> str:
            value = read(cfg, *names, default=default)
            return str(value).strip() if value is not None else ""

        def numeric(value: object, default: float = 0.0) -> float:
            try:
                result = float(value)
            except (TypeError, ValueError):
                return default
            return result if math.isfinite(result) else default

        mission_t = numeric(t)
        p_value = numeric(pressure)
        has_thrust = bool(read(assets, "has_thrust", default=True))
        if telemetry is not None and hasattr(telemetry, "attrs"):
            has_thrust = bool(telemetry.attrs.get("has_thrust", has_thrust))
        f_value = numeric(thrust) if has_thrust and thrust is not None else None

        p_peak = numeric(
            read(metrics, "peak_pressure", "pressure_peak", default=None),
            numeric(read(assets, "p_peak", default=max(p_value, 1.0)), max(p_value, 1.0)),
        )
        f_peak_raw = read(metrics, "peak_thrust", "thrust_peak", default=None)
        if f_peak_raw is None:
            f_peak_raw = read(assets, "f_peak", default=None)
        f_peak = numeric(f_peak_raw) if has_thrust and f_peak_raw is not None else None
        burn_end = numeric(
            read(metrics, "burn_duration", default=None),
            numeric(read(assets, "t_end", default=0.0)),
        )
        p_limit_raw = read(cfg, "pressure_limit", default=None)
        p_limit = numeric(p_limit_raw) if p_limit_raw not in (None, "") else None
        # The supplied broadcast contract uses a calibrated 70-bar tape.  An
        # explicit safety/scale limit always wins; otherwise retain that
        # professional fixed scale while still expanding safely for tests that
        # exceed it.  A moving ``peak * 1.05`` scale made the same pressure jump
        # to a different vertical position from one dataset to another and no
        # longer matched the declared tick labels.
        p_scale = max(p_limit or 70.0, p_peak, 1.0)
        f_scale = max((f_peak or 0.0) * 1.05, abs(f_value or 0.0), 1.0)

        if p_limit is not None and p_value > p_limit:
            status_code, status_label, status_color = "abort", "ABORT", "#FF4747"
        elif mission_t < 0:
            status_code, status_label, status_color = (
                "pre_ignition", "PRE-IGNITION", "#94A3B8"
            )
        elif mission_t <= burn_end + 0.1:
            status_code, status_label, status_color = "hot_fire", "HOT FIRE", "#4ADE80"
        else:
            status_code, status_label, status_color = (
                "test_complete", "TEST COMPLETE", "#FACC15"
            )
        pulse = min(1.0, max(0.0, 0.55 + 0.45 * math.sin(mission_t * 4.2)))

        full_thrust_time = min(0.6, burn_end * 0.1) if burn_end > 0 else 0.6
        phase_specs = [
            ("auto_sequence", "AUTO SEQUENCE", -4.0),
            ("ignition", "IGNITION", 0.0),
            (
                "full_thrust" if has_thrust else "pressure_rise",
                "FULL THRUST" if has_thrust else "PRESSURE RISE",
                full_thrust_time,
            ),
            ("burnout", "BURNOUT", burn_end),
        ]
        reached = [index for index, item in enumerate(phase_specs) if mission_t >= item[2]]
        active_index = reached[-1] if reached else 0
        phase_items: list[dict[str, Any]] = []
        for index, (phase_id, label, phase_t) in enumerate(phase_specs):
            is_reached = mission_t >= phase_t
            is_active = index == active_index
            phase_items.append(
                {
                    "id": phase_id,
                    "label": label,
                    "time_s": phase_t,
                    "time_text": (
                        f"T-{abs(phase_t):g}s" if phase_t < 0 else f"T+{phase_t:.1f}s"
                    ),
                    "state": "active" if is_active else ("complete" if is_reached else "pending"),
                    "reached": is_reached,
                    "active": is_active,
                    "available": True,
                    "progress": 1.0 if is_reached and not is_active else 0.0,
                }
            )

        propellant = cfg_text("propellant")
        oxidizer = cfg_text("oxidizer")
        fuel = cfg_text("fuel")
        if propellant and not oxidizer and not fuel:
            pieces = [piece.strip() for piece in propellant.replace("|", "/").split("/")]
            if pieces:
                oxidizer = pieces[0]
            if len(pieces) > 1:
                fuel = pieces[1]

        logo: Any = read(assets, "logo", default=None)
        if logo is None:
            logo = read(cfg, "logo", default=None)
        peak_pressure_time = read(metrics, "peak_pressure_time", default=None)
        peak_thrust_time = read(metrics, "peak_thrust_time", default=None)
        total_impulse = read(metrics, "total_impulse", default=None)
        accent = cfg_text("accent", default="#F59E0B") or "#F59E0B"
        pressure_unit = cfg_text("pressure_unit", default="bar") or "bar"
        thrust_unit = cfg_text("thrust_unit", default="N") or "N"

        values: dict[str, Any] = {
            "brand.logo": logo,
            "brand.organization_name": cfg_text(
                "organization_name", default="STELLAR KINETICS"
            ),
            "brand.footer_tagline": cfg_text(
                "footer_tagline", default="STELLAR KINETICS — ENGINEERING THE VOID"
            ),
            "brand.accent_color": accent,
            "test.title": cfg_text("title", default="ROCKET MOTOR STATIC TEST"),
            "test.subtitle": cfg_text("subtitle", default="STATIC FIRE TEST"),
            "test.run_number": cfg_text("run_number"),
            "test.motor_type": cfg_text("motor_type"),
            "test.date": cfg_text("test_date", "date"),
            "test.site": cfg_text("test_site", "site"),
            "test.coordinates_text": cfg_text("coordinates_text", "coordinates"),
            "test.oxidizer": oxidizer,
            "test.fuel": fuel,
            "test.ablative_material": cfg_text("ablative_material"),
            "camera.label": cfg_text("camera_label", default="CAM 01"),
            "camera.capture_fps_label": cfg_text(
                "capture_fps", "capture_fps_label", default="120 FPS"
            ),
            "units.pressure": pressure_unit,
            "units.thrust": thrust_unit,
            "frame.mission_time_s": mission_t,
            "frame.mission_clock": _format_mission_clock(mission_t),
            "status.code": status_code,
            "status.label": status_label,
            "status.color": status_color,
            "status.pulse": pulse,
            "telemetry.pressure.available": True,
            "telemetry.pressure.value": p_value,
            "telemetry.pressure.formatted": f"{p_value:.1f}",
            "telemetry.pressure.unit": pressure_unit,
            "telemetry.pressure.normalized": min(1.0, max(0.0, p_value / p_scale)),
            "telemetry.pressure.scale_max": p_scale,
            "telemetry.thrust.available": has_thrust,
            "telemetry.thrust.value": f_value,
            "telemetry.thrust.formatted": f"{f_value:.0f}" if f_value is not None else "N/A",
            "telemetry.thrust.unit": thrust_unit if has_thrust else None,
            "telemetry.thrust.normalized": (
                min(1.0, max(0.0, f_value / f_scale)) if f_value is not None else None
            ),
            "telemetry.thrust.scale_max": f_scale if has_thrust else None,
            "metrics.pressure.peak": p_peak,
            "metrics.pressure.peak_time_s": peak_pressure_time,
            "metrics.thrust.peak": f_peak,
            "metrics.thrust.peak_time_s": peak_thrust_time if has_thrust else None,
            "metrics.thrust.total_impulse": total_impulse if has_thrust else None,
            "metrics.burn.duration_s": burn_end,
            "phases.active_id": phase_specs[active_index][0],
            "phases.active_index": active_index,
            "phases.progress": 0.0,
            "phases.items": phase_items,
        }
        if telemetry is not None:
            try:
                # Telemetry is immutable during a render.  Cache the series on
                # the DataFrame attrs so a long video does not rebuild tens of
                # thousands of chart points for every frame.
                series_cache = telemetry.attrs.get("_rotpl_series_bindings")
                if not isinstance(series_cache, Mapping):
                    series_cache = {
                        "telemetry.pressure.series": [
                            {"time_s": float(row_time), "value": float(row_value)}
                            for row_time, row_value in zip(
                                telemetry["time"].to_numpy(),
                                telemetry["pressure"].to_numpy(),
                            )
                        ],
                        "telemetry.thrust.series": [
                            {
                                "time_s": float(row_time),
                                "value": float(row_value),
                            }
                            for row_time, row_value in zip(
                                telemetry["time"].to_numpy(),
                                telemetry["thrust"].to_numpy(),
                            )
                        ]
                        if has_thrust
                        else None,
                    }
                    telemetry.attrs["_rotpl_series_bindings"] = series_cache
                values.update(series_cache)
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
        return cls(values)


def _format_mission_clock(seconds: float) -> str:
    sign = "T-" if seconds < 0 else "T+"
    value = abs(float(seconds))
    return f"{sign} {int(value // 60):02d}:{value % 60:05.2f}"


def _parse_color(value: object, opacity: float = 1.0) -> tuple[int, int, int, int]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        channels = [int(max(0, min(255, round(float(v))))) for v in value]
        if len(channels) == 3:
            channels.append(255)
        if len(channels) != 4:
            raise RotplValidationError(f"Invalid color value: {value!r}")
        channels[3] = int(round(channels[3] * max(0.0, min(1.0, opacity))))
        return tuple(channels)  # type: ignore[return-value]
    text = str(value or "#FFFFFF").strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) == 6:
        text += "FF"
    if len(text) != 8:
        raise RotplValidationError(f"Invalid color value: {value!r}")
    try:
        channels = [int(text[index:index + 2], 16) for index in range(0, 8, 2)]
    except ValueError as exc:
        raise RotplValidationError(f"Invalid color value: {value!r}") from exc
    channels[3] = int(round(channels[3] * max(0.0, min(1.0, opacity))))
    return tuple(channels)  # type: ignore[return-value]


@dataclass(frozen=True)
class _Scale:
    sx: float
    sy: float

    @property
    def uniform(self) -> float:
        return min(self.sx, self.sy)

    def x(self, value: object) -> int:
        return int(round(float(value) * self.sx))

    def y(self, value: object) -> int:
        return int(round(float(value) * self.sy))

    def size(self, value: object, minimum: int = 1) -> int:
        return max(minimum, int(round(float(value) * self.uniform)))


class RotplRenderer:
    """Render a validated declarative package with Pillow."""

    def __init__(self, package: RotplPackage):
        self.package = package
        self._font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
        elements = list(package.layout.get("elements", []))
        self.elements = [
            element
            for _, element in sorted(
                enumerate(elements), key=lambda item: (float(item[1].get("z", 0)), item[0])
            )
        ]

    def sample_context(self) -> TemplateContext:
        return TemplateContext(self.package.read_json("preview/sample.json"))

    def render(
        self,
        context: TemplateContext | Mapping[str, Any],
        output_size: Optional[tuple[int, int]] = None,
        background: Optional[Image.Image | np.ndarray] = None,
    ) -> Image.Image:
        """Render one frame using master-first downsampling for small outputs.

        Text and one-pixel chrome lose substantially more detail when Pillow
        rasterizes them directly at 720p/540p.  For every target smaller than
        the template canvas we therefore rasterize a transparent overlay at
        the authored master size and resize that *single* overlay.  Resizing
        happens in premultiplied-alpha mode, preventing dark/bright fringes at
        the edge of glyphs.  Larger targets still render directly so 4K gains
        real vector/font resolution instead of upscaled master pixels.
        """
        if not isinstance(context, TemplateContext):
            context = TemplateContext(context)
        context = self._prepare_context(context)

        base_w, base_h = self.package.canvas_size
        width, height = output_size or (base_w, base_h)
        width, height = int(width), int(height)
        if width <= 0 or height <= 0:
            raise RotplValidationError("Output dimensions must be positive.")
        target_size = (width, height)
        if width < base_w or height < base_h:
            overlay = self._render_overlay_at_size(context, (base_w, base_h))
            overlay = self._resize_straight_alpha(overlay, target_size)
        else:
            overlay = self._render_overlay_at_size(context, target_size)

        if background is None:
            return overlay
        background_image = self._background_image(background, target_size)
        return Image.alpha_composite(background_image, overlay)

    def _prepare_context(self, context: TemplateContext) -> TemplateContext:
        context = context.copy()
        variables = self.package.manifest.get("variables", {})
        if isinstance(variables, Mapping):
            for path, declaration in variables.items():
                if isinstance(declaration, Mapping) and "default" in declaration:
                    context.set_default(str(path), declaration["default"])
        required_bindings = self.package.manifest.get("required_bindings", [])
        if not isinstance(required_bindings, list):
            raise RotplValidationError("manifest.required_bindings must be an array.")
        missing_required = [
            str(binding)
            for binding in required_bindings
            if not context.available(binding)
        ]
        if missing_required:
            raise RotplBindingError(
                "Required template binding(s) are missing: "
                + ", ".join(missing_required)
            )
        return context

    def _render_overlay_at_size(
        self, context: TemplateContext, size: tuple[int, int]
    ) -> Image.Image:
        base_w, base_h = self.package.canvas_size
        width, height = size
        scale = _Scale(width / base_w, height / base_h)
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))

        suppressed: set[str] = set()
        for element in self.elements:
            fallback_id = element.get("fallback_element")
            if not fallback_id:
                continue
            binding = element.get("bind")
            if binding and context.available(binding):
                suppressed.add(str(fallback_id))
            else:
                suppressed.add(str(element["id"]))

        for element in self.elements:
            if str(element["id"]) in suppressed:
                continue
            canvas = self._draw_element(canvas, element, context, scale)
        return canvas

    @staticmethod
    def _resize_straight_alpha(
        overlay: Image.Image, size: tuple[int, int]
    ) -> Image.Image:
        """LANCZOS resize without filtering invisible straight-RGB values."""
        if overlay.size == size:
            return overlay
        # Pillow's RGBa mode stores premultiplied RGB.  Filtering it keeps the
        # RGB/alpha energy coupled, then conversion restores straight RGBA for
        # FFmpeg's overlay input and the browser PNG contract.
        return (
            overlay.convert("RGBa")
            .resize(size, Image.Resampling.LANCZOS)
            .convert("RGBA")
        )

    def render_bgra(
        self,
        context: TemplateContext | Mapping[str, Any],
        output_size: Optional[tuple[int, int]] = None,
    ) -> np.ndarray:
        rgba = np.asarray(self.render(context, output_size=output_size), dtype=np.uint8)
        return rgba[..., [2, 1, 0, 3]].copy()

    def compose_bgr(
        self,
        video_frame: np.ndarray,
        context: TemplateContext | Mapping[str, Any],
    ) -> np.ndarray:
        if not isinstance(video_frame, np.ndarray) or video_frame.ndim != 3:
            raise RotplValidationError("Video frame must be a BGR numpy array.")
        if video_frame.shape[2] not in (3, 4):
            raise RotplValidationError("Video frame must have three or four channels.")
        if video_frame.shape[2] == 3:
            rgb = video_frame[..., ::-1]
        else:
            rgb = video_frame[..., [2, 1, 0, 3]]
        background = Image.fromarray(np.ascontiguousarray(rgb)).convert("RGBA")
        rendered = np.asarray(
            self.render(
                context,
                output_size=(video_frame.shape[1], video_frame.shape[0]),
                background=background,
            ).convert("RGB"),
            dtype=np.uint8,
        )
        return rendered[..., ::-1].copy()

    def _background_image(
        self,
        background: Optional[Image.Image | np.ndarray],
        size: tuple[int, int],
    ) -> Image.Image:
        if background is None:
            return Image.new("RGBA", size, (0, 0, 0, 0))
        image = self._coerce_image(background)
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        return image.convert("RGBA")

    def _draw_element(
        self,
        canvas: Image.Image,
        element: Mapping[str, Any],
        context: TemplateContext,
        scale: _Scale,
    ) -> Image.Image:
        kind = str(element["type"])
        if kind == "gradient_scrim":
            return self._draw_gradient(canvas, element, context, scale)
        if kind == "rect":
            return self._draw_rect(canvas, element, context, scale)
        if kind == "line":
            return self._draw_line(canvas, element, context, scale)
        if kind == "text":
            return self._draw_text(canvas, element, context, scale)
        if kind in {"logo", "image"}:
            return self._draw_image(canvas, element, context, scale)
        if kind in {"vertical_gauge", "bar_gauge"}:
            return self._draw_gauge(canvas, element, context, scale)
        if kind == "phase_list":
            return self._draw_phase_list(canvas, element, context, scale)
        return canvas

    @staticmethod
    def _composite(canvas: Image.Image, layer: Image.Image) -> Image.Image:
        return Image.alpha_composite(canvas, layer)

    def _opacity(
        self, element: Mapping[str, Any], context: TemplateContext, default: float = 1.0
    ) -> float:
        value = element.get("opacity", default)
        binding = element.get("opacity_bind")
        if binding and context.available(binding):
            value = context.resolve(binding)
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    def _color(
        self,
        element: Mapping[str, Any],
        context: TemplateContext,
        *,
        key: str = "color",
        bind_key: str = "color_bind",
        default: str = "#FFFFFF",
        opacity: float = 1.0,
    ) -> tuple[int, int, int, int]:
        value = element.get(key, default)
        binding = element.get(bind_key)
        if binding and context.available(binding):
            value = context.resolve(binding)
        return _parse_color(value, opacity)

    def _binding_value(
        self, element: Mapping[str, Any], context: TemplateContext
    ) -> tuple[bool, Any]:
        binding = element.get("bind")
        if not binding:
            return True, element.get("text")
        if context.available(binding):
            return True, context.resolve(binding)
        policy = str(element.get("missing_policy", "hide"))
        if policy == "required_error":
            raise RotplBindingError(
                f"Required binding {binding!r} is missing for element {element['id']}."
            )
        if policy == "show_na":
            return True, "N/A"
        return False, None

    def _draw_gradient(
        self,
        canvas: Image.Image,
        element: Mapping[str, Any],
        context: TemplateContext,
        scale: _Scale,
    ) -> Image.Image:
        x, y = scale.x(element.get("x", 0)), scale.y(element.get("y", 0))
        width, height = scale.x(element.get("w", 0)), scale.y(element.get("h", 0))
        if width <= 0 or height <= 0:
            return canvas
        color = self._color(element, context, opacity=1.0)
        start = max(0.0, min(1.0, float(element.get("opacity_start", 1.0))))
        end = max(0.0, min(1.0, float(element.get("opacity_end", 0.0))))
        direction = str(element.get("direction", "down"))
        if direction in {"up", "left"}:
            start, end = end, start
        if direction in {"left", "right"}:
            alpha = np.linspace(start, end, width, dtype=np.float32)[None, :]
            alpha = np.repeat(alpha, height, axis=0)
        else:
            alpha = np.linspace(start, end, height, dtype=np.float32)[:, None]
            alpha = np.repeat(alpha, width, axis=1)
        array = np.empty((height, width, 4), dtype=np.uint8)
        array[..., :3] = color[:3]
        array[..., 3] = np.clip(np.rint(alpha * color[3]), 0, 255).astype(np.uint8)
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        layer.alpha_composite(Image.fromarray(array, "RGBA"), (x, y))
        return self._composite(canvas, layer)

    def _draw_rect(
        self,
        canvas: Image.Image,
        element: Mapping[str, Any],
        context: TemplateContext,
        scale: _Scale,
    ) -> Image.Image:
        x, y = scale.x(element.get("x", 0)), scale.y(element.get("y", 0))
        width, height = scale.x(element.get("w", 0)), scale.y(element.get("h", 0))
        if width <= 0 or height <= 0:
            return canvas
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        opacity = self._opacity(element, context)
        fill_value = element.get("fill", element.get("color"))
        fill = None
        if fill_value is not None or element.get("color_bind"):
            fill = self._color(
                element,
                context,
                key="fill" if "fill" in element else "color",
                default="#FFFFFF",
                opacity=opacity,
            )
        stroke = None
        if element.get("stroke") is not None:
            stroke = _parse_color(
                element.get("stroke"), float(element.get("stroke_opacity", 1.0))
            )
        radius = scale.size(element.get("radius", 0), minimum=0)
        box = (x, y, x + width, y + height)
        stroke_width = scale.size(element.get("stroke_width", 1))
        if radius > 0:
            draw.rounded_rectangle(
                box, radius=radius, fill=fill, outline=stroke, width=stroke_width
            )
        else:
            draw.rectangle(box, fill=fill, outline=stroke, width=stroke_width)
        return self._composite(canvas, layer)

    def _draw_line(
        self,
        canvas: Image.Image,
        element: Mapping[str, Any],
        context: TemplateContext,
        scale: _Scale,
    ) -> Image.Image:
        x1, y1 = float(element.get("x", 0)), float(element.get("y", 0))
        x2, y2 = float(element.get("x2", x1)), float(element.get("y2", y1))
        if element.get("bind_type") == "translate_y":
            visible, value = self._binding_value(element, context)
            if not visible:
                return canvas
            mapping = element.get("translate_map", {})
            domain = context.resolve(mapping.get("domain_bind"), None)
            range_px = mapping.get("range_px", [y1, y1])
            try:
                fraction = max(0.0, min(1.0, float(value) / float(domain)))
                target_y = float(range_px[0]) + (
                    float(range_px[1]) - float(range_px[0])
                ) * fraction
            except (TypeError, ValueError, ZeroDivisionError, IndexError):
                return canvas
            delta = target_y - y1
            y1 += delta
            y2 += delta
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        color = self._color(
            element, context, opacity=self._opacity(element, context)
        )
        draw.line(
            (scale.x(x1), scale.y(y1), scale.x(x2), scale.y(y2)),
            fill=color,
            width=scale.size(element.get("width", 1)),
        )
        return self._composite(canvas, layer)

    def _font(self, font_id: object, size: int) -> ImageFont.FreeTypeFont:
        key = (str(font_id), int(size))
        font = self._font_cache.get(key)
        if font is None:
            font = self.package.font(key[0], key[1])
            self._font_cache[key] = font
        return font

    @staticmethod
    def _text_width(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont,
        spacing: float,
    ) -> float:
        if not text:
            return 0.0
        return sum(draw.textlength(char, font=font) for char in text) + spacing * max(
            0, len(text) - 1
        )

    def _draw_spaced_text(
        self,
        layer: Image.Image,
        text: str,
        x: int,
        baseline_y: int,
        font: ImageFont.FreeTypeFont,
        color: tuple[int, int, int, int],
        spacing: float,
        anchor: str,
        max_width: Optional[int] = None,
    ) -> None:
        draw = ImageDraw.Draw(layer, "RGBA")
        total = self._text_width(draw, text, font, spacing)
        if anchor == "right":
            cursor = x - total
        elif anchor == "center":
            cursor = x - total / 2
        else:
            cursor = float(x)
        text_layer = layer
        text_draw = draw
        if max_width is not None and total > max_width:
            text_layer = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            text_draw = ImageDraw.Draw(text_layer, "RGBA")
        for char in text:
            text_draw.text(
                (int(round(cursor)), baseline_y), char, font=font, fill=color, anchor="ls"
            )
            cursor += text_draw.textlength(char, font=font) + spacing
        if text_layer is not layer:
            if anchor == "right":
                left, right = x - max_width, x
            elif anchor == "center":
                left, right = x - max_width // 2, x + max_width // 2
            else:
                left, right = x, x + max_width
            alpha = np.asarray(text_layer.getchannel("A"), dtype=np.uint8).copy()
            alpha[:, :max(0, left)] = 0
            alpha[:, min(alpha.shape[1], right):] = 0
            text_layer.putalpha(Image.fromarray(alpha, "L"))
            layer.alpha_composite(text_layer)

    def _draw_text(
        self,
        canvas: Image.Image,
        element: Mapping[str, Any],
        context: TemplateContext,
        scale: _Scale,
    ) -> Image.Image:
        visible, value = self._binding_value(element, context)
        if not visible:
            return canvas
        if value is None:
            return canvas
        if isinstance(value, (int, float, np.integer, np.floating)):
            decimals = element.get("decimals")
            text = f"{float(value):.{int(decimals)}f}" if decimals is not None else str(value)
        else:
            text = str(value)
        suffix_bind = element.get("unit_suffix_bind")
        if suffix_bind and context.available(suffix_bind) and text != "N/A":
            suffix = str(context.resolve(suffix_bind)).strip()
            if suffix:
                text += " " + suffix

        x, y = scale.x(element.get("x", 0)), scale.y(element.get("y", 0))
        anchor = str(element.get("anchor", "left"))
        max_width = scale.x(element.get("max_width", 0)) if element.get("max_width") else None
        letter_spacing = float(element.get("letter_spacing", 0)) * scale.uniform
        size = scale.size(element.get("font_size", 16))
        min_size = scale.size(element.get("min_font_size", element.get("font_size", 16)))
        font_id = element.get("font_id")
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        font = self._font(font_id, size)
        if element.get("overflow") == "shrink" and max_width:
            while (
                size > min_size
                and self._text_width(probe, text, font, letter_spacing) > max_width
            ):
                size -= 1
                font = self._font(font_id, size)
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        color = self._color(
            element, context, opacity=self._opacity(element, context)
        )
        clip_width = max_width if element.get("overflow") == "clip" else None
        self._draw_spaced_text(
            layer, text, x, y, font, color, letter_spacing, anchor, clip_width
        )
        return self._composite(canvas, layer)

    def _coerce_image(self, value: object) -> Image.Image:
        if isinstance(value, Image.Image):
            return value.copy().convert("RGBA")
        if isinstance(value, np.ndarray):
            array = np.asarray(value, dtype=np.uint8)
            if array.ndim == 2:
                return Image.fromarray(array, "L").convert("RGBA")
            if array.ndim != 3 or array.shape[2] not in (3, 4):
                raise RotplValidationError("Unsupported numpy image shape.")
            indices = [2, 1, 0] if array.shape[2] == 3 else [2, 1, 0, 3]
            return Image.fromarray(np.ascontiguousarray(array[..., indices])).convert("RGBA")
        if isinstance(value, (bytes, bytearray)):
            try:
                image = Image.open(io.BytesIO(bytes(value)))
                image.load()
                return image.convert("RGBA")
            except OSError as exc:
                raise RotplValidationError(f"Invalid bound image: {exc}") from exc
        if isinstance(value, Path):
            try:
                with Image.open(value) as image:
                    return image.convert("RGBA")
            except OSError as exc:
                raise RotplValidationError(f"Invalid bound image {value}: {exc}") from exc
        if isinstance(value, str):
            if self.package.has_member(value):
                return self.package.read_image(value)
            path = Path(value)
            if path.is_file():
                try:
                    with Image.open(path) as image:
                        return image.convert("RGBA")
                except OSError as exc:
                    raise RotplValidationError(f"Invalid bound image {value}: {exc}") from exc
        raise RotplValidationError("Bound logo/image could not be loaded.")

    def _draw_image(
        self,
        canvas: Image.Image,
        element: Mapping[str, Any],
        context: TemplateContext,
        scale: _Scale,
    ) -> Image.Image:
        visible, value = self._binding_value(element, context)
        if not visible:
            value = element.get("asset")
        if value is None:
            return canvas
        image = self._coerce_image(value)
        max_width = scale.x(element.get("max_width", element.get("w", image.width)))
        max_height = scale.y(element.get("max_height", element.get("h", image.height)))
        ratio = min(max_width / image.width, max_height / image.height)
        width = max(1, int(round(image.width * ratio)))
        height = max(1, int(round(image.height * ratio)))
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        opacity = self._opacity(element, context)
        if opacity < 1.0:
            alpha = image.getchannel("A").point(lambda value: round(value * opacity))
            image.putalpha(alpha)
        x, y = scale.x(element.get("x", 0)), scale.y(element.get("y", 0))
        anchor = str(element.get("anchor", "left"))
        if anchor == "right":
            x -= width
        elif anchor == "center":
            x -= width // 2
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        layer.alpha_composite(image, (x, y))
        return self._composite(canvas, layer)

    def _draw_gauge(
        self,
        canvas: Image.Image,
        element: Mapping[str, Any],
        context: TemplateContext,
        scale: _Scale,
    ) -> Image.Image:
        visible, value = self._binding_value(element, context)
        if not visible:
            return canvas
        try:
            fraction = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return canvas
        x, y = scale.x(element.get("x", 0)), scale.y(element.get("y", 0))
        width, height = scale.x(element.get("w", 0)), scale.y(element.get("h", 0))
        if width <= 0 or height <= 0:
            return canvas
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        if element.get("track_color") is not None:
            draw.rectangle(
                (x, y, x + width, y + height),
                fill=_parse_color(
                    element["track_color"], float(element.get("track_opacity", 1.0))
                ),
            )
        fill = self._color(element, context, opacity=self._opacity(element, context))
        direction = str(element.get("fill_from", "left"))
        if element.get("type") == "vertical_gauge":
            filled = int(round(height * fraction))
            if filled > 0:
                top = y + height - filled if direction == "bottom" else y
                draw.rectangle((x, top, x + width, top + filled), fill=fill)
        else:
            filled = int(round(width * fraction))
            if filled > 0:
                left = x + width - filled if direction == "right" else x
                draw.rectangle((left, y, left + filled, y + height), fill=fill)
        return self._composite(canvas, layer)

    def _draw_phase_list(
        self,
        canvas: Image.Image,
        element: Mapping[str, Any],
        context: TemplateContext,
        scale: _Scale,
    ) -> Image.Image:
        visible, value = self._binding_value(element, context)
        if not visible or not isinstance(value, Sequence):
            return canvas
        x, y = scale.x(element.get("x", 0)), scale.y(element.get("y", 0))
        row_height = scale.y(element.get("row_height", 30))
        max_rows = int(element.get("max_rows", len(value)))
        columns = element.get("columns", {})
        if not isinstance(columns, Mapping):
            return canvas
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        for index, item in enumerate(value[:max_rows]):
            if not isinstance(item, Mapping) or item.get("available", True) is False:
                continue
            state = str(item.get("state", "pending"))
            reached = bool(item.get("reached", state in {"complete", "active"}))
            row_y = y + index * row_height

            number = columns.get("number", {})
            number_color_key = (
                "color_active_bind" if state in {"complete", "active"} else "color_pending"
            )
            number_color = self._phase_color(number, number_color_key, context)
            number_font = self._font(
                number.get("font_id"), scale.size(number.get("font_size", 12))
            )
            self._draw_spaced_text(
                layer,
                f"{index + 1:02d}",
                x + scale.x(number.get("x_offset", 0)),
                row_y,
                number_font,
                number_color,
                0,
                "left",
            )

            diamond = columns.get("diamond", {})
            if state == "complete":
                diamond_key = "color_complete_bind"
            elif state == "active":
                diamond_key = "color_active"
            else:
                diamond_key = "color_pending"
            diamond_color = self._phase_color(diamond, diamond_key, context)
            center_x = x + scale.x(diamond.get("x_offset", 0))
            center_y = row_y - scale.size(4)
            radius = max(1, scale.size(diamond.get("size", 10)) // 2)
            ImageDraw.Draw(layer, "RGBA").polygon(
                [
                    (center_x, center_y - radius),
                    (center_x + radius, center_y),
                    (center_x, center_y + radius),
                    (center_x - radius, center_y),
                ],
                fill=diamond_color,
            )

            label = columns.get("label", {})
            label_key = f"color_{state}"
            label_color = self._phase_color(label, label_key, context)
            label_font = self._font(
                label.get("font_id"), scale.size(label.get("font_size", 13))
            )
            self._draw_spaced_text(
                layer,
                str(item.get("label", "")),
                x + scale.x(label.get("x_offset", 0)),
                row_y,
                label_font,
                label_color,
                float(label.get("letter_spacing", 0)) * scale.uniform,
                "left",
            )

            time_column = columns.get("time_text", {})
            time_key = "color_reached" if reached else "color_pending"
            time_color = self._phase_color(time_column, time_key, context)
            time_font = self._font(
                time_column.get("font_id"),
                scale.size(time_column.get("font_size", 12)),
            )
            self._draw_spaced_text(
                layer,
                str(item.get("time_text", "")),
                x + scale.x(time_column.get("x_offset", element.get("width", 0))),
                row_y,
                time_font,
                time_color,
                0,
                str(time_column.get("anchor", "right")),
            )
        return self._composite(canvas, layer)

    def _phase_color(
        self,
        declaration: Mapping[str, Any],
        key: str,
        context: TemplateContext,
    ) -> tuple[int, int, int, int]:
        value = declaration.get(key, "#FFFFFF")
        if key.endswith("_bind"):
            value = context.resolve(value, "#FFFFFF")
        return _parse_color(value)


__all__ = [
    "RotplBindingError",
    "RotplError",
    "RotplPackage",
    "RotplRenderer",
    "RotplValidationError",
    "SUPPORTED_ELEMENT_TYPES",
    "TemplateContext",
]
