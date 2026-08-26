from __future__ import annotations

import hashlib
import io
import math
import tempfile
import threading
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from rotpl_renderer import RotplPackage, RotplRenderer, TemplateContext


_CACHE_DIR = Path(tempfile.gettempdir()) / "stellar-ops-rotpl-preview"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_RENDERERS: dict[str, tuple[RotplPackage, RotplRenderer]] = {}
_LOCK = threading.RLock()


class OverlayPreviewError(ValueError):
    pass


def _renderer(package: dict[str, Any]) -> RotplRenderer:
    digest = str(package["sha256"])
    with _LOCK:
        cached = _RENDERERS.get(digest)
        if cached:
            return cached[1]
        archive = bytes(package["archive_blob"])
        if hashlib.sha256(archive).hexdigest() != digest:
            raise OverlayPreviewError("overlay package checksum mismatch")
        path = _CACHE_DIR / f"{digest}.rotpl"
        if not path.exists():
            path.write_bytes(archive)
        parsed = RotplPackage.open(path)
        renderer = RotplRenderer(parsed)
        _RENDERERS[digest] = (parsed, renderer)
        return renderer


def _simulated_camera(size: tuple[int, int], mission_time: float) -> Image.Image:
    width, height = size
    image = Image.new("RGBA", size, "#171b20")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        shade = int(20 + 30 * y / max(1, height))
        draw.line((0, y, width, y), fill=(shade, shade + 4, shade + 7, 255))
    horizon = int(height * .64)
    draw.rectangle((0, horizon, width, height), fill=(42, 38, 31, 255))
    draw.rectangle((int(width*.34), int(height*.24), int(width*.67), horizon), fill=(25, 28, 31, 255))
    draw.rectangle((int(width*.44), int(height*.13), int(width*.56), horizon), fill=(72, 76, 78, 255))
    draw.polygon([
        (int(width*.5), int(height*.06)),
        (int(width*.44), int(height*.18)),
        (int(width*.56), int(height*.18)),
    ], fill=(105, 109, 111, 255))
    flame = max(0.0, math.sin(max(0.0, mission_time) * 2.8))
    if 0 <= mission_time <= 5.5:
        length = int(height * (.10 + .12 * flame))
        draw.polygon([
            (int(width*.465), horizon),
            (int(width*.535), horizon),
            (int(width*.50), horizon + length),
        ], fill=(255, 150, 30, 235))
        draw.ellipse((int(width*.36), horizon, int(width*.64), int(height*.92)), fill=(120, 120, 120, 45))
    image = image.filter(ImageFilter.GaussianBlur(radius=1.1))
    draw = ImageDraw.Draw(image)
    draw.rectangle((18, 18, 265, 49), fill=(0, 0, 0, 145))
    draw.text((30, 27), "SIMULATED CAMERA · NO DEVICE", fill=(210, 225, 230, 255))
    return image


def render_overlay_preview(
    db: Any,
    operation_id: str,
    package_id: int,
    mission_time: float,
    pressure: float,
    thrust: float,
    mode: str = "VIDEO",
    width: int = 960,
) -> bytes:
    package = db.execute(
        """SELECT id,name,sha256,archive_blob,state,public_safe
           FROM overlay_packages WHERE operation_id=? AND id=?""",
        (operation_id, package_id),
    ).fetchone()
    if not package:
        raise OverlayPreviewError("overlay package not found")
    package = dict(package)
    if package["state"] != "VALIDATED" or not package["public_safe"]:
        raise OverlayPreviewError("preview requires a public-safe validated package")
    width = max(640, min(int(width), 1920))
    height = round(width * 9 / 16)
    mission_time = max(-10.0, min(float(mission_time), 120.0))
    pressure = max(0.0, min(float(pressure), 200.0))
    thrust = max(0.0, min(float(thrust), 100000.0))
    renderer = _renderer(package)
    cfg = {
        "title": "DUQM ENGINE QUALIFICATION",
        "subtitle": "SIMULATION PREVIEW · NO LIVE CAMERA",
        "run_number": "PREVIEW-001",
        "motor_type": "RNX-71V",
        "test_site": "DUQM TEST RANGE",
        "coordinates_text": "SIMULATED DATA",
        "oxidizer": "KNO3",
        "fuel": "EPOXY",
        "camera_label": "SIM CAM 01",
        "capture_fps": "30 FPS",
        "pressure_unit": "bar",
        "thrust_unit": "N",
        "pressure_limit": 70.0,
    }
    assets = {"has_thrust": True, "p_peak": max(70.0, pressure), "f_peak": max(900.0, thrust), "t_end": 5.5}
    metrics = {
        "peak_pressure": max(70.0, pressure),
        "peak_thrust": max(900.0, thrust),
        "burn_duration": 5.5,
    }
    context = TemplateContext.from_runtime(
        cfg, assets, mission_time, pressure, thrust, metrics=metrics
    )
    mode = str(mode).upper()
    if mode == "OVERLAY":
        background = None
    elif mode == "REFERENCE" and renderer.package.has_member("preview/background.png"):
        background = renderer.package.read_image("preview/background.png")
    else:
        background = _simulated_camera((width, height), mission_time)
    with _LOCK:
        image = renderer.render(context, output_size=(width, height), background=background)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
