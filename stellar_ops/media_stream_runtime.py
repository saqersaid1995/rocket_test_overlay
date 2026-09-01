from __future__ import annotations

import importlib
import json
import time

from .camera_runtime import mjpeg_frames
from .control import OPERATION_ID, connect, telemetry
from .overlay_preview import render_overlay_preview
from .scene_compositor import (
    SceneCompositorError,
    compose_scene_jpeg,
    dissolve_jpegs,
    extract_mjpeg_jpeg,
    mjpeg_part,
    slate_jpeg,
)
from .telemetry_runtime import runtime_snapshot


def install_media_stream_optimizations() -> None:
    """Install low-overhead broadcast stream generators.

    The original media stream path queried SQLite on every video frame and called
    the full control snapshot (including alarms/audit/incidents) five times per
    second per Preview/Program stream just to obtain overlay telemetry. With two
    browser monitors that can create dozens of database transactions per second
    and starve ordinary page navigation.

    These replacements keep video frame delivery independent from control-system
    snapshot work: bus routing is polled at 10 Hz and overlays read telemetry using
    runtime_snapshot only. Preview/Program still switch promptly, but normal HTTP
    routes are no longer competing with the video hot loop.
    """

    media_module = importlib.import_module(".media", __package__)

    def lightweight_telemetry() -> dict:
        with connect() as db:
            operation = db.execute(
                "SELECT * FROM operations WHERE id=?", (OPERATION_ID,)
            ).fetchone()
            if not operation:
                return {
                    "elapsed": 0.0,
                    "channels": {},
                    "pressure": 0.0,
                    "thrust": 0.0,
                    "temperature": 0.0,
                }
            op = dict(operation)
            return runtime_snapshot(db, op, telemetry(operation))

    def cached_overlay(package_id: int, state: dict) -> bytes:
        channels = state.get("channels", {})
        pressure = channels.get("motor.chamber_pressure", {})
        thrust = channels.get("motor.thrust", {})
        pressure = pressure.get("value", 0) if isinstance(pressure, dict) else pressure
        thrust = thrust.get("value", 0) if isinstance(thrust, dict) else thrust
        elapsed = float(state.get("elapsed", 0) or 0)
        pressure = float(pressure or 0)
        thrust = float(thrust or 0)

        key = (
            package_id,
            "OVERLAY",
            960,
            round(elapsed, 1),
            round(pressure, 1),
            round(thrust, 0),
        )
        with media_module._PREVIEW_CACHE_LOCK:
            image = media_module._PREVIEW_CACHE.get(key)
            if image is not None:
                media_module._PREVIEW_CACHE.move_to_end(key)
                return image

        with connect() as db:
            image = render_overlay_preview(
                db,
                OPERATION_ID,
                package_id,
                elapsed,
                pressure,
                thrust,
                mode="OVERLAY",
                width=960,
            )

        with media_module._PREVIEW_CACHE_LOCK:
            media_module._PREVIEW_CACHE[key] = image
            media_module._PREVIEW_CACHE.move_to_end(key)
            while len(media_module._PREVIEW_CACHE) > 48:
                media_module._PREVIEW_CACHE.popitem(last=False)
        return image

    def optimized_scene_stream(scene_id: int):
        with connect() as db:
            scene = db.execute(
                "SELECT * FROM broadcast_scenes WHERE operation_id=? AND id=?",
                (OPERATION_ID, scene_id),
            ).fetchone()
            if not scene:
                raise SceneCompositorError("broadcast scene not found")
            sources = json.loads(scene["sources_json"] or "[]")
            camera_id = next(
                (
                    str(item.get("source"))
                    for item in sources
                    if item.get("kind") == "camera"
                ),
                "",
            )
            camera = db.execute(
                """SELECT d.id,d.endpoint,i.adapter_type,i.config_json,i.enabled
                   FROM devices d JOIN device_integrations i
                     ON i.operation_id=d.operation_id AND i.device_id=d.id
                   WHERE d.operation_id=? AND d.id=? AND d.device_type='IP-CAMERA'""",
                (OPERATION_ID, camera_id),
            ).fetchone()
            overlay_package_id = scene["overlay_package_id"]
            config = json.loads(camera["config_json"] or "{}") if camera else {}
            scene_name = scene["name"]
            scene_type = scene["scene_type"]

        if not camera_id:
            frame = mjpeg_part(slate_jpeg(scene_name, scene_type))
            while True:
                yield frame
                time.sleep(0.5)

        if not camera or not camera["enabled"]:
            raise SceneCompositorError("scene camera is not registered and enabled")

        camera_frames = mjpeg_frames(
            camera["id"],
            camera["adapter_type"],
            config.get("endpoint") or camera["endpoint"] or "",
            config.get("username", ""),
            "preview",
            config.get("profile", ""),
        )
        overlay = None
        overlay_rendered_at = 0.0

        try:
            for camera_part in camera_frames:
                stamp = time.monotonic()
                if overlay_package_id and stamp - overlay_rendered_at >= 0.2:
                    overlay = cached_overlay(overlay_package_id, lightweight_telemetry())
                    overlay_rendered_at = stamp
                yield mjpeg_part(compose_scene_jpeg(camera_part, overlay))
        finally:
            close = getattr(camera_frames, "close", None)
            if close:
                close()

    def optimized_broadcast_bus_stream(column: str):
        if column not in {"preview_scene_id", "program_scene_id"}:
            raise SceneCompositorError("invalid broadcast bus")

        active_scene_id = None
        active_stream = None
        last_part = None
        scene_id = None
        transition_ms = 500
        next_route_poll = 0.0

        try:
            while True:
                stamp = time.monotonic()
                if scene_id is None or stamp >= next_route_poll:
                    with connect() as db:
                        row = db.execute(
                            f"""SELECT s.{column} AS scene_id,
                                      COALESCE(bs.transition_ms,500) AS transition_ms
                                 FROM broadcast_sessions s
                                 LEFT JOIN broadcast_settings bs
                                   ON bs.operation_id=s.operation_id
                                WHERE s.operation_id=?""",
                            (OPERATION_ID,),
                        ).fetchone()
                    scene_id = row["scene_id"] if row else None
                    transition_ms = int(row["transition_ms"] if row else 500)
                    next_route_poll = stamp + 0.1

                if not scene_id:
                    raise SceneCompositorError("broadcast bus has no assigned scene")

                if scene_id != active_scene_id:
                    previous_part = last_part
                    if active_stream is not None:
                        active_stream.close()

                    active_scene_id = scene_id
                    active_stream = optimized_scene_stream(scene_id)
                    first_part = next(active_stream)

                    with connect() as db:
                        selected_scene = db.execute(
                            "SELECT transition FROM broadcast_scenes WHERE id=?",
                            (scene_id,),
                        ).fetchone()
                    transition = str(
                        selected_scene["transition"] if selected_scene else "CUT"
                    ).upper()

                    if previous_part and transition == "DISSOLVE" and transition_ms > 0:
                        steps = max(2, min(30, round(transition_ms / 33.3)))
                        for jpeg in dissolve_jpegs(
                            extract_mjpeg_jpeg(previous_part),
                            extract_mjpeg_jpeg(first_part),
                            steps,
                        ):
                            last_part = mjpeg_part(jpeg)
                            yield last_part
                        continue

                    last_part = first_part
                    yield first_part
                    continue

                last_part = next(active_stream)
                yield last_part
        finally:
            if active_stream is not None:
                active_stream.close()

    media_module._scene_stream = optimized_scene_stream
    media_module._broadcast_bus_stream = optimized_broadcast_bus_stream
