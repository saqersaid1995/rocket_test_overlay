from __future__ import annotations

from flask import Blueprint, Response, jsonify

from .control import OPERATION_ID, connect
from .media import _scene_stream
from .scene_compositor import SceneCompositorError, extract_mjpeg_jpeg

media_frame_preview = Blueprint("media_frame_preview", __name__)


@media_frame_preview.get("/api/media/bus/<bus>/frame.jpg")
def broadcast_bus_frame(bus: str):
    column = {"preview": "preview_scene_id", "program": "program_scene_id"}.get(bus)
    if not column:
        return jsonify(error="broadcast bus must be preview or program"), 404

    with connect() as db:
        row = db.execute(
            f"SELECT {column} AS scene_id FROM broadcast_sessions WHERE operation_id=?",
            (OPERATION_ID,),
        ).fetchone()
    scene_id = row["scene_id"] if row else None
    if not scene_id:
        return jsonify(error="broadcast bus has no assigned scene"), 409

    stream = None
    try:
        stream = _scene_stream(int(scene_id))
        part = next(stream)
        jpeg = extract_mjpeg_jpeg(part)
    except (SceneCompositorError, StopIteration) as exc:
        return jsonify(error=str(exc) or "broadcast bus produced no video"), 409
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    return Response(
        jpeg,
        mimetype="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
