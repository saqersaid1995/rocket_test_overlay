from __future__ import annotations

import io

from PIL import Image, ImageDraw


class SceneCompositorError(ValueError):
    pass


def extract_mjpeg_jpeg(part: bytes) -> bytes:
    """Extract the JPEG payload from one multipart MJPEG frame."""
    marker = b"\r\n\r\n"
    header_end = part.find(marker)
    if header_end < 0:
        raise SceneCompositorError("camera frame is not a valid MJPEG part")
    payload = part[header_end + len(marker):]
    if payload.endswith(b"\r\n"):
        payload = payload[:-2]
    if not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        raise SceneCompositorError("camera frame does not contain a complete JPEG")
    return payload


def compose_scene_jpeg(camera_part: bytes, overlay_png: bytes | None,
                       quality: int = 88) -> bytes:
    """Burn a rendered ROTPL layer into a camera frame.

    The returned JPEG is the actual composited frame consumed by Preview and
    Program. It is deliberately independent of HTML/CSS layering.
    """
    try:
        camera = Image.open(io.BytesIO(extract_mjpeg_jpeg(camera_part))).convert("RGBA")
        if overlay_png:
            overlay = Image.open(io.BytesIO(overlay_png)).convert("RGBA")
            if overlay.size != camera.size:
                overlay = overlay.resize(camera.size, Image.Resampling.LANCZOS)
            camera = Image.alpha_composite(camera, overlay)
        output = io.BytesIO()
        camera.convert("RGB").save(output, format="JPEG", quality=max(60, min(quality, 95)))
        return output.getvalue()
    except SceneCompositorError:
        raise
    except Exception as exc:
        raise SceneCompositorError(f"scene frame composition failed: {exc}") from exc


def mjpeg_part(jpeg: bytes) -> bytes:
    return (b"--frame\r\nContent-Type: image/jpeg\r\n"
            b"Cache-Control: no-store\r\n\r\n" + jpeg + b"\r\n")


def slate_jpeg(title: str, subtitle: str = "STELLAR KINETICS",
               size: tuple[int, int] = (960, 540)) -> bytes:
    """Create an intentional broadcast slate as a real encoded video frame."""
    image = Image.new("RGB", size, "#07131b")
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.rectangle((0, 0, width, 8), fill="#31d4ff")
    draw.rectangle((0, height - 8, width, height), fill="#31d4ff")
    draw.text((width * 0.08, height * 0.40), title[:80], fill="white")
    draw.text((width * 0.08, height * 0.50), subtitle[:80], fill="#8faab7")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()
