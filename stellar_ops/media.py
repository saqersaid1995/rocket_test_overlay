from __future__ import annotations

import io
import json
import re
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (Blueprint, Response, current_app, jsonify, render_template,
                   request, send_file, stream_with_context)

from .control import OPERATION_ID, connect, event, init_control_db, snapshot
from .database import add_column
from .overlay_preview import OverlayPreviewError, render_overlay_preview
from .camera_runtime import mjpeg_frames
from .scene_compositor import (SceneCompositorError, compose_scene_jpeg,
                               mjpeg_part, slate_jpeg)
from .broadcast_runtime import (load_stream_key, output_metrics, output_status,
                                probe_program_bus, program_recording_status,
                                save_stream_key, start_output, start_program_recording, stop_outputs,
                                stop_program_recording)
from .broadcast_telemetry import (
    PackageValidationError,
    channel_catalog,
    ensure_broadcast_telemetry_schema,
    install_bundled_packages,
    overlay_packages,
    phase_overlay_assignments,
    register_channel,
    resolve_overlay_selection,
    save_package,
    save_phase_overlay,
    set_overlay_selection,
    validate_package,
)

media = Blueprint("media", __name__)
_PREVIEW_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_PREVIEW_CACHE_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_definitions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, name TEXT NOT NULL,
 channels_json TEXT NOT NULL, time_window INTEGER NOT NULL DEFAULT 60,
 options_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
 UNIQUE(operation_id,name));
CREATE TABLE IF NOT EXISTS display_pages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, name TEXT NOT NULL,
 slug TEXT NOT NULL UNIQUE, purpose TEXT NOT NULL, resolution TEXT NOT NULL,
 layout_json TEXT NOT NULL, public_safe INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS display_endpoints(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL, page_slug TEXT, resolution TEXT NOT NULL, status TEXT NOT NULL,
 last_seen TEXT, locked INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS operator_video_preferences(
 operation_id TEXT NOT NULL, operator_key TEXT NOT NULL, wall_id INTEGER,
 grid TEXT NOT NULL DEFAULT '2x2', updated_at TEXT NOT NULL,
 PRIMARY KEY(operation_id,operator_key));
CREATE TABLE IF NOT EXISTS camera_profiles(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, device_id TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL, mode TEXT NOT NULL, main_url TEXT, preview_url TEXT,
 capabilities_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS video_walls(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, name TEXT NOT NULL,
 grid TEXT NOT NULL, tiles_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(operation_id,name));
CREATE TABLE IF NOT EXISTS published_templates(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, template_key TEXT NOT NULL,
 name TEXT NOT NULL, version TEXT NOT NULL, sha256 TEXT NOT NULL, canvas TEXT NOT NULL,
 slots_json TEXT NOT NULL, state TEXT NOT NULL, published_at TEXT NOT NULL,
 UNIQUE(operation_id,template_key,version));
CREATE TABLE IF NOT EXISTS broadcast_scenes(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, name TEXT NOT NULL,
 scene_type TEXT NOT NULL, template_id INTEGER, sources_json TEXT NOT NULL,
 transition TEXT NOT NULL, public_safe INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
 UNIQUE(operation_id,name));
CREATE TABLE IF NOT EXISTS stream_destinations(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, name TEXT NOT NULL,
 provider TEXT NOT NULL, ingest_url TEXT NOT NULL, stream_key_hint TEXT,
 enabled INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(operation_id,name));
CREATE TABLE IF NOT EXISTS broadcast_sessions(
 operation_id TEXT PRIMARY KEY, preview_scene_id INTEGER, program_scene_id INTEGER,
 state TEXT NOT NULL, recording INTEGER NOT NULL DEFAULT 0, emergency INTEGER NOT NULL DEFAULT 0,
 started_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS broadcast_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
 action TEXT NOT NULL, detail TEXT NOT NULL);
"""


_INITIALIZED_DATABASES: set[str] = set()
_INITIALIZATION_LOCK = threading.RLock()


def _database_key() -> str:
    with connect() as db:
        return str(db.execute("PRAGMA database_list").fetchone()[2])


def init_media_db() -> None:
    init_control_db()
    key = _database_key()
    if key in _INITIALIZED_DATABASES:
        return
    with _INITIALIZATION_LOCK:
        if key in _INITIALIZED_DATABASES:
            return
        _initialize_media_db()
        _INITIALIZED_DATABASES.add(key)


def _initialize_media_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        add_column(db, "broadcast_scenes", "overlay_package_id INTEGER")
        stamp = now()
        ensure_broadcast_telemetry_schema(db, OPERATION_ID, stamp)
        # Unit tests create many isolated databases; browser acceptance and
        # deployed environments still exercise the real bundled 4.3 MB asset.
        if not current_app.config.get("TESTING"):
            install_bundled_packages(db, OPERATION_ID, stamp)
        if not db.execute("SELECT 1 FROM graph_definitions WHERE operation_id=?", (OPERATION_ID,)).fetchone():
            graphs = [
                ("Propulsion Live", ["motor.chamber_pressure", "motor.thrust"], 60, {"linked_cursor": True, "show_limits": True}),
                ("Thermal Watch", ["motor.case_temperature"], 300, {"linked_cursor": True, "show_limits": True}),
                ("Ignition & Pressure", ["ignition.continuity", "motor.chamber_pressure"], 30, {"event_markers": True}),
            ]
            for name, channels, window, options in graphs:
                db.execute("INSERT INTO graph_definitions(operation_id,name,channels_json,time_window,options_json,updated_at) VALUES(?,?,?,?,?,?)",
                           (OPERATION_ID, name, json.dumps(channels), window, json.dumps(options), stamp))
        # Camera connection ownership lives in System Configuration. Media keeps only
        # stable logical identities/capabilities and never duplicates RTSP credentials.
        cameras = db.execute("""SELECT d.id,d.name,d.endpoint,COALESCE(i.enabled,0) AS integration_enabled
            FROM devices d LEFT JOIN device_integrations i
              ON i.operation_id=d.operation_id AND i.device_id=d.id
            WHERE d.operation_id=? AND d.device_type='IP-CAMERA'""", (OPERATION_ID,)).fetchall()
        for camera in cameras:
            endpoint = str(camera["endpoint"] or "")
            if not camera["integration_enabled"]:
                camera_mode = "DISABLED"
            elif endpoint.upper().startswith("SIM:") or endpoint.upper() == "UNASSIGNED":
                camera_mode = "SIMULATION"
            else:
                camera_mode = "LIVE"
            db.execute("""INSERT INTO camera_profiles(operation_id,device_id,name,mode,main_url,preview_url,capabilities_json,enabled,updated_at)
                VALUES(?,?,?,?,NULL,NULL,?,?,?) ON CONFLICT(device_id) DO UPDATE SET
                name=excluded.name,mode=excluded.mode,main_url=NULL,preview_url=NULL,
                enabled=excluded.enabled,updated_at=excluded.updated_at""",
                       (OPERATION_ID, camera["id"], camera["name"], camera_mode,
                        json.dumps({"ptz": False, "iso_record": True}),
                        int(camera_mode != "DISABLED"), stamp))
        if not db.execute("SELECT 1 FROM video_walls WHERE operation_id=?", (OPERATION_ID,)).fetchone():
            db.execute("INSERT INTO video_walls(operation_id,name,grid,tiles_json,updated_at) VALUES(?,?,?,?,?)",
                       (OPERATION_ID, "Test Stand Video Wall", "2x2", json.dumps([
                           {"slot": 1, "kind": "camera", "source": "CAM-01"},
                           {"slot": 2, "kind": "camera", "source": "CAM-02"},
                           {"slot": 3, "kind": "graph", "source": "Propulsion Live"},
                           {"slot": 4, "kind": "clock", "source": "MISSION_CLOCK"},
                       ]), stamp))
        if not db.execute("SELECT 1 FROM published_templates WHERE operation_id=?", (OPERATION_ID,)).fetchone():
            db.execute("""INSERT INTO published_templates(operation_id,template_key,name,version,sha256,canvas,slots_json,state,published_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (OPERATION_ID, "STUDIO-LAUNCH", "Launch Broadcast", "1.0.0", "UNPUBLISHED-STUDIO-REFERENCE", "1920x1080@30",
                    json.dumps(["camera_main", "camera_pip", "mission_clock", "lower_third", "telemetry_primary"]), "REFERENCE", stamp))
        template = db.execute("SELECT id FROM published_templates WHERE operation_id=? ORDER BY id LIMIT 1", (OPERATION_ID,)).fetchone()
        overlay = db.execute("""SELECT id FROM overlay_packages WHERE operation_id=?
            AND state='VALIDATED' AND public_safe=1 ORDER BY id LIMIT 1""", (OPERATION_ID,)).fetchone()
        if not db.execute("SELECT 1 FROM broadcast_scenes WHERE operation_id=?", (OPERATION_ID,)).fetchone():
            scenes = [
                ("Standby", "SLATE", [{"kind": "title", "value": "QUALIFICATION TEST — STANDBY"}], "CUT"),
                ("Test Stand", "LIVE", [{"kind": "camera", "source": "CAM-01", "slot": "camera_main"}, {"kind": "telemetry_overlay", "source": "motor.chamber_pressure", "slot": "telemetry_primary"}], "DISSOLVE"),
                ("Countdown", "LIVE", [{"kind": "camera", "source": "CAM-01", "slot": "camera_main"}, {"kind": "clock", "source": "MISSION_CLOCK", "slot": "mission_clock"}], "CUT"),
                ("Technical Hold", "SLATE", [{"kind": "title", "value": "TECHNICAL HOLD"}], "CUT"),
                ("Emergency", "EMERGENCY", [{"kind": "title", "value": "TRANSMISSION PAUSED"}], "CUT"),
            ]
            for name, kind, sources, transition in scenes:
                db.execute("""INSERT INTO broadcast_scenes(operation_id,name,scene_type,template_id,overlay_package_id,sources_json,transition,public_safe,updated_at)
                    VALUES(?,?,?,?,?,?,?,1,?)""", (OPERATION_ID, name, kind, template["id"],
                    overlay["id"] if overlay and kind == "LIVE" else None,
                    json.dumps(sources), transition, stamp))
        if overlay:
            db.execute("""UPDATE broadcast_scenes SET overlay_package_id=?
                WHERE operation_id=? AND scene_type='LIVE' AND overlay_package_id IS NULL""",
                (overlay["id"], OPERATION_ID))
        # Operational graph definitions belong to control-room displays. Broadcast Program
        # consumes only slots explicitly exposed by an immutable Studio template.
        for stored in db.execute("SELECT id,sources_json FROM broadcast_scenes WHERE operation_id=?", (OPERATION_ID,)).fetchall():
            sources = json.loads(stored["sources_json"] or "[]")
            changed = False
            for source in sources:
                if source.get("kind") == "graph":
                    source.update(kind="telemetry_overlay", source="motor.chamber_pressure", slot="telemetry_primary")
                    changed = True
            if changed:
                db.execute("UPDATE broadcast_scenes SET sources_json=?,updated_at=? WHERE id=?", (json.dumps(sources), stamp, stored["id"]))
        first = db.execute("SELECT id FROM broadcast_scenes WHERE operation_id=? ORDER BY id LIMIT 1", (OPERATION_ID,)).fetchone()
        db.execute("""INSERT OR IGNORE INTO broadcast_sessions(operation_id,preview_scene_id,program_scene_id,state,recording,emergency,updated_at)
            VALUES(?,?,?,'OFF_AIR',0,0,?)""", (OPERATION_ID, first["id"], first["id"], stamp))
        if not db.execute("SELECT 1 FROM display_pages WHERE operation_id=?", (OPERATION_ID,)).fetchone():
            graph_ids = [row["id"] for row in db.execute("SELECT id FROM graph_definitions WHERE operation_id=? ORDER BY id", (OPERATION_ID,))]
            wall = db.execute("SELECT id FROM video_walls WHERE operation_id=? ORDER BY id LIMIT 1", (OPERATION_ID,)).fetchone()
            pages = [
                ("Propulsion Engineering", "propulsion", "ENGINEERING", "1920x1080", [{"instance_id": "graph-a", "type": "graph", "ref_id": graph_ids[0], "x": 0, "y": 0, "w": 8, "h": 6}, {"instance_id": "graph-b", "type": "graph", "ref_id": graph_ids[1], "x": 8, "y": 0, "w": 4, "h": 6}], 0),
                ("Test Stand Video", "test-stand-video", "VIDEO", "1920x1080", [{"instance_id": "wall-a", "type": "video_wall", "ref_id": wall["id"], "x": 0, "y": 0, "w": 12, "h": 8}], 0),
                ("Public Program", "public-program", "PUBLIC", "1920x1080", [{"instance_id": "program-a", "type": "program", "x": 0, "y": 0, "w": 12, "h": 8}], 1),
            ]
            for row in pages:
                db.execute("INSERT INTO display_pages(operation_id,name,slug,purpose,resolution,layout_json,public_safe,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (OPERATION_ID, row[0], row[1], row[2], row[3], json.dumps(row[4]), row[5], stamp))


@media.before_request
def ensure_media() -> None:
    init_media_db()


def rows(db, table: str) -> list[dict]:
    result = [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE operation_id=? ORDER BY id", (OPERATION_ID,))]
    for item in result:
        for key in list(item):
            if key.endswith("_json"):
                item[key[:-5]] = json.loads(item.pop(key) or "{}")
    return result


def media_snapshot() -> dict:
    with connect() as db:
        result = {name: rows(db, name) for name in (
            "graph_definitions", "display_pages", "display_endpoints", "camera_profiles",
            "video_walls", "published_templates", "broadcast_scenes", "stream_destinations", "broadcast_events")}
        result["telemetry_catalog"] = channel_catalog(db, OPERATION_ID)
        result["overlay_packages"] = overlay_packages(db, OPERATION_ID)
        result["phase_overlay_assignments"] = phase_overlay_assignments(db, OPERATION_ID)
        session = db.execute("SELECT * FROM broadcast_sessions WHERE operation_id=?", (OPERATION_ID,)).fetchone()
        result["broadcast"] = dict(session)
        result["broadcast"]["recording_runtime"] = program_recording_status()
        operation_state = db.execute(
            "SELECT state FROM operations WHERE id=?", (OPERATION_ID,)
        ).fetchone()
        runtime_phase = str(operation_state["state"] if operation_state else "STANDBY").upper()
        result["overlay_selection"] = resolve_overlay_selection(
            db, OPERATION_ID, runtime_phase, now()
        )
        cutoff = datetime.now(timezone.utc).timestamp() - 30
        for endpoint in result["display_endpoints"]:
            try:
                alive = bool(endpoint.get("last_seen") and datetime.fromisoformat(endpoint["last_seen"]).timestamp() >= cutoff)
            except (TypeError, ValueError):
                alive = False
            endpoint["status"] = "ONLINE" if alive else "OFFLINE"
        preference = db.execute("SELECT * FROM operator_video_preferences WHERE operation_id=? AND operator_key='LOCAL_ADMIN'",
                                (OPERATION_ID,)).fetchone()
        result["operator_video_preference"] = dict(preference) if preference else {"operator_key":"LOCAL_ADMIN","wall_id":None,"grid":"2x2"}
        for destination in result["stream_destinations"]:
            destination["secret_configured"] = bool(load_stream_key(destination["id"]))
            runtime = output_status(destination["id"])
            if runtime != "STOPPED" or destination["status"] == "STREAMING":
                destination["status"] = runtime
            destination["runtime"] = output_metrics(destination["id"])
    core = snapshot()
    result["operation"] = core["operation"]
    result["channels"] = core["channels"]
    result["devices"] = core["devices"]
    result["telemetry"] = core["telemetry"]
    device_map = {item["id"]: item for item in core["devices"]}
    integration_map = {item["device_id"]: item for item in core.get("integrations", [])}
    for profile in result["camera_profiles"]:
        device = device_map.get(profile["device_id"], {})
        integration = integration_map.get(profile["device_id"], {})
        endpoint = str(device.get("endpoint") or "")
        profile["runtime_status"] = device.get("health", "NOT_CONNECTED")
        profile["recording"] = device.get("recording", "STOPPED")
        profile["configured"] = bool(integration.get("enabled") and endpoint not in {"", "UNASSIGNED"})
        profile["runtime_live"] = bool(profile["configured"]
                                       and device.get("health") in {"STREAMING", "RECONNECTING"})
        profile["source_owner"] = "SYSTEM_CONFIGURATION"
        profile["stream_url"] = f"/api/control/camera/{profile['device_id']}/stream.mjpg?profile=preview"
        profile["popout_url"] = f"/control/camera/{profile['device_id']}/popout"
    return result


@media.get("/media")
def media_console():
    return render_template("media.html", initial=media_snapshot(), display_slug=None)


@media.get("/media/overlays")
def overlay_studio():
    return render_template("overlay_studio.html", initial=media_snapshot())


@media.get("/display/<slug>")
def display_output(slug: str):
    with connect() as db:
        page = db.execute("SELECT 1 FROM display_pages WHERE operation_id=? AND slug=?", (OPERATION_ID, slug)).fetchone()
    if not page:
        return "Display page not found", 404
    return render_template("media.html", initial=media_snapshot(), display_slug=slug)


@media.post("/api/media/display/<code>/heartbeat")
def display_heartbeat(code: str):
    p = body()
    with connect() as db:
        endpoint = db.execute("SELECT * FROM display_endpoints WHERE operation_id=? AND code=?",
                              (OPERATION_ID, code.upper())).fetchone()
        if not endpoint:
            return jsonify(error="display endpoint is not registered"), 404
        db.execute("UPDATE display_endpoints SET status='ONLINE',last_seen=? WHERE id=?",
                   (now(), endpoint["id"]))
        routed = endpoint["page_slug"]
    return jsonify(ok=True, page_slug=routed,
                   redirect=f"/display/{routed}?endpoint={code.upper()}" if routed else None)


@media.post("/api/media/operator-video-preference")
def save_operator_video_preference():
    p = body(); grid = str(p.get("grid", "2x2")); wall_id = p.get("wall_id")
    if grid not in {"1x1", "2x2", "3x3", "hero+4"}:
        return jsonify(error="unsupported operator video layout"), 400
    with connect() as db:
        if wall_id and not db.execute("SELECT 1 FROM video_walls WHERE operation_id=? AND id=?",
                                      (OPERATION_ID, wall_id)).fetchone():
            return jsonify(error="video wall not found"), 404
        db.execute("""INSERT INTO operator_video_preferences(operation_id,operator_key,wall_id,grid,updated_at)
            VALUES(?,'LOCAL_ADMIN',?,?,?) ON CONFLICT(operation_id,operator_key) DO UPDATE SET
            wall_id=excluded.wall_id,grid=excluded.grid,updated_at=excluded.updated_at""",
                   (OPERATION_ID, wall_id, grid, now()))
    return jsonify(ok=True)


@media.get("/api/media/snapshot")
def api_media_snapshot():
    return jsonify(media_snapshot())


def body() -> dict:
    return request.get_json(silent=True) or {}


def _program_cameras(db, scene: dict) -> list[dict]:
    required = {source.get("source") for source in json.loads(scene.get("sources_json") or "[]")
                if source.get("kind") == "camera"}
    result = []
    for row in db.execute("""SELECT d.id AS device_id,i.config_json FROM devices d JOIN device_integrations i
        ON i.operation_id=d.operation_id AND i.device_id=d.id WHERE d.operation_id=? AND d.device_type='IP-CAMERA'
        AND i.enabled=1 AND d.endpoint!='UNASSIGNED' ORDER BY d.required DESC,d.rowid""", (OPERATION_ID,)):
        if required and row["device_id"] not in required:
            continue
        config = json.loads(row["config_json"])
        result.append({"device_id":row["device_id"],"username":config.get("username","")})
    return result


def _scene_payload(scene) -> dict:
    return {
        "id": scene["id"],
        "name": scene["name"],
        "scene_type": scene["scene_type"],
        "overlay_package_id": scene["overlay_package_id"],
        "sources": json.loads(scene["sources_json"] or "[]"),
    }


def clean_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")


@media.get("/api/media/overlay-preview/<int:package_id>.png")
def overlay_preview_image(package_id: int):
    try:
        mission_time = float(request.args.get("t", "1.2"))
        pressure = float(request.args.get("pressure", "42"))
        thrust = float(request.args.get("thrust", "720"))
        mode = str(request.args.get("mode", "VIDEO"))
        width = int(request.args.get("width", "960"))
        # Preview and Program commonly request the same frame. Quantizing the
        # live values avoids rendering an expensive ROTPL package several times
        # per UI refresh while remaining visually real-time.
        cache_key = (package_id, mode.upper(), width, round(mission_time, 1),
                     round(pressure, 1), round(thrust, 0))
        with _PREVIEW_CACHE_LOCK:
            image = _PREVIEW_CACHE.get(cache_key)
            if image is not None:
                _PREVIEW_CACHE.move_to_end(cache_key)
        if image is None:
            with connect() as db:
                image = render_overlay_preview(
                    db, OPERATION_ID, package_id, mission_time,
                    pressure, thrust, mode=mode, width=width,
                )
            with _PREVIEW_CACHE_LOCK:
                _PREVIEW_CACHE[cache_key] = image
                _PREVIEW_CACHE.move_to_end(cache_key)
                while len(_PREVIEW_CACHE) > 48:
                    _PREVIEW_CACHE.popitem(last=False)
    except (TypeError, ValueError, OverlayPreviewError) as exc:
        return jsonify(error=str(exc)), 400
    return send_file(
        io.BytesIO(image), mimetype="image/png",
        max_age=0, download_name=f"overlay-preview-{package_id}.png",
    )


def _scene_stream(scene_id: int):
    with connect() as db:
        scene = db.execute(
            "SELECT * FROM broadcast_scenes WHERE operation_id=? AND id=?",
            (OPERATION_ID, scene_id),
        ).fetchone()
        if not scene:
            raise SceneCompositorError("broadcast scene not found")
        sources = json.loads(scene["sources_json"] or "[]")
        camera_id = next((str(item.get("source")) for item in sources
                          if item.get("kind") == "camera"), "")
        camera = db.execute(
            """SELECT d.id,d.endpoint,i.adapter_type,i.config_json,i.enabled
               FROM devices d JOIN device_integrations i
                 ON i.operation_id=d.operation_id AND i.device_id=d.id
               WHERE d.operation_id=? AND d.id=? AND d.device_type='IP-CAMERA'""",
            (OPERATION_ID, camera_id),
        ).fetchone()
        overlay_package_id = scene["overlay_package_id"]
        config = json.loads(camera["config_json"] or "{}") if camera else {}

    if not camera_id:
        frame = mjpeg_part(slate_jpeg(scene["name"], scene["scene_type"]))
        while True:
            yield frame
            import time
            time.sleep(0.5)
    if not camera or not camera["enabled"]:
        raise SceneCompositorError("scene camera is not registered and enabled")

    camera_frames = mjpeg_frames(
        camera["id"], camera["adapter_type"],
        config.get("endpoint") or camera["endpoint"] or "",
        config.get("username", ""), "preview", config.get("profile", ""),
    )
    overlay = None
    overlay_rendered_at = 0.0
    for camera_part in camera_frames:
        stamp = datetime.now(timezone.utc).timestamp()
        if overlay_package_id and stamp - overlay_rendered_at >= 0.2:
            state = snapshot()["telemetry"]
            channels = state.get("channels", {})
            pressure = channels.get("motor.chamber_pressure", {})
            thrust = channels.get("motor.thrust", {})
            pressure = pressure.get("value", 0) if isinstance(pressure, dict) else pressure
            thrust = thrust.get("value", 0) if isinstance(thrust, dict) else thrust
            with connect() as db:
                overlay = render_overlay_preview(
                    db, OPERATION_ID, overlay_package_id,
                    float(state.get("elapsed", 0)), float(pressure or 0),
                    float(thrust or 0), mode="OVERLAY", width=960,
                )
            overlay_rendered_at = stamp
        yield mjpeg_part(compose_scene_jpeg(camera_part, overlay))


@media.get("/api/media/scene/<int:scene_id>/stream.mjpg")
def scene_stream(scene_id: int):
    try:
        stream = _scene_stream(scene_id)
        # Resolve configuration errors before returning an endless response.
        first = next(stream)
    except (SceneCompositorError, StopIteration) as exc:
        return jsonify(error=str(exc) or "scene produced no video"), 409

    def frames():
        yield first
        yield from stream

    return Response(
        stream_with_context(frames()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


def _broadcast_bus_stream(column: str):
    if column not in {"preview_scene_id", "program_scene_id"}:
        raise SceneCompositorError("invalid broadcast bus")
    active_scene_id = None
    active_stream = None
    while True:
        with connect() as db:
            session = db.execute(
                f"SELECT {column} AS scene_id FROM broadcast_sessions WHERE operation_id=?",
                (OPERATION_ID,),
            ).fetchone()
        scene_id = session["scene_id"] if session else None
        if not scene_id:
            raise SceneCompositorError("broadcast bus has no assigned scene")
        if scene_id != active_scene_id:
            if active_stream is not None:
                active_stream.close()
            active_scene_id = scene_id
            active_stream = _scene_stream(scene_id)
        yield next(active_stream)


@media.get("/api/media/bus/<bus>/stream.mjpg")
def broadcast_bus_stream(bus: str):
    column = {"preview": "preview_scene_id", "program": "program_scene_id"}.get(bus)
    if not column:
        return jsonify(error="broadcast bus must be preview or program"), 404
    try:
        stream = _broadcast_bus_stream(column)
        first = next(stream)
    except (SceneCompositorError, StopIteration) as exc:
        return jsonify(error=str(exc) or "broadcast bus produced no video"), 409

    def frames():
        yield first
        yield from stream

    return Response(
        stream_with_context(frames()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@media.post("/api/media/telemetry-channel")
def save_telemetry_channel():
    p = body()
    try:
        with connect() as db:
            saved = register_channel(db, OPERATION_ID, p, now())
            event(
                db, "TELEMETRY_CATALOG", "BROADCAST_ENGINEER", "INFO",
                f"Broadcast telemetry channel registered: {saved['channel_id']}",
            )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, channel=saved)


@media.post("/api/media/phase-overlay")
def assign_phase_overlay():
    p = body()
    try:
        package_id = int(p.get("package_id"))
        with connect() as db:
            save_phase_overlay(
                db, OPERATION_ID, str(p.get("phase", "")), package_id,
                str(p.get("transition", "CUT")), now(),
            )
            event(
                db, "OVERLAY_PHASE_MAP", "BROADCAST_ENGINEER", "INFO",
                f"Broadcast phase {str(p.get('phase', '')).upper()} mapped to package {package_id}",
            )
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, detail="Phase overlay assignment saved")


@media.post("/api/media/overlay-selection")
def change_overlay_selection():
    p = body()
    package_id = p.get("package_id")
    try:
        package_id = int(package_id) if package_id not in (None, "") else None
        with connect() as db:
            set_overlay_selection(
                db, OPERATION_ID, str(p.get("mode", "AUTO")), package_id, now()
            )
            event(
                db, "OVERLAY_SELECTION", "BROADCAST_DIRECTOR", "INFO",
                f"Overlay selection mode changed to {str(p.get('mode', 'AUTO')).upper()}",
            )
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, detail="Overlay selection updated")


@media.post("/api/media/overlay-package")
def upload_overlay_package():
    uploaded = request.files.get("package")
    if not uploaded or not uploaded.filename:
        return jsonify(error="select a .rotpl package to upload"), 400
    package_bytes = uploaded.read()
    try:
        with connect() as db:
            catalog = channel_catalog(db, OPERATION_ID)
            package = validate_package(uploaded.filename, package_bytes, catalog)
            package_id = save_package(db, OPERATION_ID, package, now())
            event(
                db, "OVERLAY_PACKAGE", "BROADCAST_ENGINEER", "INFO",
                f"ROTPL package validated: {package.template_id} v{package.version}",
            )
    except PackageValidationError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(
        ok=True,
        package_id=package_id,
        template_id=package.template_id,
        version=package.version,
        sha256=package.sha256,
        public_safe=package.public_safe,
        detail=f"{package.name} v{package.version} validated",
    )


@media.post("/api/media/graph")
def save_graph():
    p = body(); name = str(p.get("name", "")).strip(); channels = p.get("channels", [])
    available = {x["id"] for x in snapshot()["channels"]}
    if not name or not isinstance(channels, list) or not channels or not set(channels) <= available:
        return jsonify(error="name and valid telemetry channels are required"), 400
    window = max(5, min(int(p.get("time_window", 60)), 3600)); options = p.get("options", {})
    with connect() as db:
        db.execute("""INSERT INTO graph_definitions(operation_id,name,channels_json,time_window,options_json,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(operation_id,name) DO UPDATE SET channels_json=excluded.channels_json,
            time_window=excluded.time_window,options_json=excluded.options_json,updated_at=excluded.updated_at""",
            (OPERATION_ID, name, json.dumps(channels), window, json.dumps(options), now()))
        event(db, "GRAPH_CONFIG", "DISPLAY_ENGINEER", "INFO", f"Graph definition saved: {name}")
    return jsonify(ok=True)


@media.post("/api/media/display-page")
def save_display_page():
    p = body(); name = str(p.get("name", "")).strip(); slug = clean_slug(str(p.get("slug") or name)); layout = p.get("layout", [])
    if not name or not slug or not isinstance(layout, list) or not layout:
        return jsonify(error="name and at least one display instance are required"), 400
    instance_ids = [str(item.get("instance_id", "")) for item in layout if isinstance(item, dict)]
    if len(instance_ids) != len(layout) or len(set(instance_ids)) != len(instance_ids):
        return jsonify(error="every display instance requires a unique instance_id"), 400
    allowed = {"graph", "video_wall", "program", "clock", "alarms"}
    if any(item.get("type") not in allowed or any(int(item.get(k, 0)) < 0 for k in ("x", "y")) or any(int(item.get(k, 0)) < 1 for k in ("w", "h")) for item in layout):
        return jsonify(error="display layout contains an invalid item"), 400
    with connect() as db:
        existing = db.execute("SELECT id FROM display_pages WHERE operation_id=? AND slug=?", (OPERATION_ID, slug)).fetchone()
        values = (name, str(p.get("purpose", "OPERATIONS"))[:32], str(p.get("resolution", "1920x1080"))[:32], json.dumps(layout), int(bool(p.get("public_safe"))), now())
        if existing:
            db.execute("UPDATE display_pages SET name=?,purpose=?,resolution=?,layout_json=?,public_safe=?,updated_at=? WHERE id=?", (*values, existing["id"]))
        else:
            db.execute("INSERT INTO display_pages(operation_id,name,slug,purpose,resolution,layout_json,public_safe,updated_at) VALUES(?,?,?,?,?,?,?,?)", (OPERATION_ID, name, slug, *values[1:]))
        event(db, "DISPLAY_CONFIG", "DISPLAY_ENGINEER", "INFO", f"Display page saved: {slug}")
    return jsonify(ok=True, slug=slug, url=f"/display/{slug}")


@media.post("/api/media/display/<code>/route")
def route_display(code: str):
    p = body(); slug = str(p.get("page_slug", "")); resolution = str(p.get("resolution", "1920x1080"))
    with connect() as db:
        if not db.execute("SELECT 1 FROM display_pages WHERE operation_id=? AND slug=?", (OPERATION_ID, slug)).fetchone():
            return jsonify(error="display page not found"), 404
        db.execute("""INSERT INTO display_endpoints(operation_id,code,name,page_slug,resolution,status,last_seen,locked)
            VALUES(?,?,?,?,?,'ONLINE',?,1) ON CONFLICT(code) DO UPDATE SET page_slug=excluded.page_slug,
            resolution=excluded.resolution,status='ONLINE',last_seen=excluded.last_seen""",
            (OPERATION_ID, code.upper(), str(p.get("name", code))[:80], slug, resolution, now()))
        event(db, "DISPLAY_ROUTE", "DISPLAY_ENGINEER", "INFO", f"{code.upper()} routed to {slug}")
    return jsonify(ok=True)


@media.post("/api/media/camera")
def save_camera():
    # Kept as an explicit guard for older clients. Camera endpoints and secrets
    # are configured once in System Configuration, never in Broadcast.
    return jsonify(
        error="camera configuration is owned by System Configuration",
        configure_url="/control?panel=cameras",
    ), 409


@media.post("/api/media/video-wall")
def save_video_wall():
    p = body(); name = str(p.get("name", "")).strip(); grid = str(p.get("grid", "2x2")); tiles = p.get("tiles", [])
    if not name or grid not in {"1x1", "2x2", "3x3", "hero+4"} or not isinstance(tiles, list):
        return jsonify(error="valid wall name, grid and tiles are required"), 400
    with connect() as db:
        db.execute("""INSERT INTO video_walls(operation_id,name,grid,tiles_json,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(operation_id,name) DO UPDATE SET grid=excluded.grid,tiles_json=excluded.tiles_json,updated_at=excluded.updated_at""",
            (OPERATION_ID, name, grid, json.dumps(tiles), now()))
        event(db, "VIDEO_WALL_CONFIG", "VIDEO_ENGINEER", "INFO", f"Video wall saved: {name}")
    return jsonify(ok=True)


@media.post("/api/media/scene")
def save_scene():
    p = body()
    name = str(p.get("name", "")).strip()
    scene_type = str(p.get("scene_type", "LIVE")).upper()
    transition = str(p.get("transition", "CUT")).upper()
    sources = p.get("sources", [])
    public_safe = bool(p.get("public_safe", True))
    if not name or not isinstance(sources, list):
        return jsonify(error="scene name and controlled sources are required"), 400
    if scene_type not in {"LIVE", "SLATE", "EMERGENCY"}:
        return jsonify(error="scene type must be LIVE, SLATE or EMERGENCY"), 400
    if transition not in {"CUT", "DISSOLVE"}:
        return jsonify(error="transition must be CUT or DISSOLVE"), 400

    camera_ids = [
        str(source.get("source", "")).upper()
        for source in sources
        if source.get("kind") == "camera"
    ]
    telemetry_ids = [
        str(source.get("source", ""))
        for source in sources
        if source.get("kind") == "telemetry_overlay"
    ]
    if scene_type == "LIVE" and not camera_ids:
        return jsonify(error="a LIVE scene requires a registered camera source"), 400
    if len(camera_ids) > 2:
        return jsonify(error="a public scene supports no more than two camera sources"), 400

    with connect() as db:
        for camera_id in camera_ids:
            camera = db.execute("""SELECT 1 FROM devices d JOIN device_integrations i
                ON i.operation_id=d.operation_id AND i.device_id=d.id
                WHERE d.operation_id=? AND d.id=? AND d.device_type='IP-CAMERA'
                  AND i.enabled=1 AND d.endpoint!='UNASSIGNED'""",
                (OPERATION_ID, camera_id)).fetchone()
            if not camera:
                return jsonify(error=f"camera source is not registered and enabled: {camera_id}"), 409
        for channel_id in telemetry_ids:
            if not db.execute(
                "SELECT 1 FROM channels WHERE operation_id=? AND id=?",
                (OPERATION_ID, channel_id),
            ).fetchone():
                return jsonify(error=f"telemetry channel not found: {channel_id}"), 404

        overlay_package_id = p.get("overlay_package_id")
        if overlay_package_id:
            package = db.execute(
                """SELECT state,public_safe FROM overlay_packages
                   WHERE operation_id=? AND id=?""",
                (OPERATION_ID, overlay_package_id),
            ).fetchone()
            if not package:
                return jsonify(error="overlay package not found"), 404
            if package["state"] != "VALIDATED":
                return jsonify(error="scene overlay must be a validated package"), 409
            if public_safe and not package["public_safe"]:
                return jsonify(error="public scenes require a public-safe overlay"), 409

        db.execute("""INSERT INTO broadcast_scenes(
                operation_id,name,scene_type,template_id,overlay_package_id,
                sources_json,transition,public_safe,updated_at)
            VALUES(?,?,?,NULL,?,?,?,?,?)
            ON CONFLICT(operation_id,name) DO UPDATE SET
                scene_type=excluded.scene_type,
                template_id=NULL,
                overlay_package_id=excluded.overlay_package_id,
                sources_json=excluded.sources_json,
                transition=excluded.transition,
                public_safe=excluded.public_safe,
                updated_at=excluded.updated_at""",
            (OPERATION_ID, name, scene_type, overlay_package_id,
             json.dumps(sources), transition, int(public_safe), now()))
        event(db, "SCENE_CONFIG", "BROADCAST_DIRECTOR", "INFO",
              f"Broadcast scene saved: {name}")
    return jsonify(ok=True)


@media.post("/api/media/destination")
def save_destination():
    p = body(); name = str(p.get("name", "")).strip(); provider = str(p.get("provider", "CUSTOM_RTMP")).upper(); ingest = str(p.get("ingest_url", "")).strip()
    if not name or urlparse(ingest).scheme.lower() not in {"rtmp", "rtmps"}:
        return jsonify(error="destination requires a valid RTMP or RTMPS ingest URL"), 400
    key = str(p.get("stream_key", "")); hint = ("••••" + key[-4:]) if key else None
    with connect() as db:
        db.execute("""INSERT INTO stream_destinations(operation_id,name,provider,ingest_url,stream_key_hint,enabled,status,updated_at)
            VALUES(?,?,?,?,?,0,'NOT_TESTED',?) ON CONFLICT(operation_id,name) DO UPDATE SET provider=excluded.provider,
            ingest_url=excluded.ingest_url,stream_key_hint=COALESCE(excluded.stream_key_hint,stream_destinations.stream_key_hint),updated_at=excluded.updated_at""",
            (OPERATION_ID, name, provider, ingest, hint, now()))
        destination_id = db.execute("SELECT id FROM stream_destinations WHERE operation_id=? AND name=?",
                                    (OPERATION_ID, name)).fetchone()["id"]
        if key:
            try:
                save_stream_key(destination_id, key)
            except RuntimeError:
                if not current_app.config.get("TESTING"):
                    raise
        event(db, "DESTINATION_CONFIG", "STREAM_ENGINEER", "INFO", f"Stream destination saved: {name}; secret not logged")
    return jsonify(ok=True)


@media.post("/api/media/broadcast")
def broadcast_action():
    p = body(); action = str(p.get("action", "")).upper(); scene_id = p.get("scene_id")
    with connect() as db:
        session = db.execute("SELECT * FROM broadcast_sessions WHERE operation_id=?", (OPERATION_ID,)).fetchone()
        scene = db.execute("SELECT * FROM broadcast_scenes WHERE operation_id=? AND id=?", (OPERATION_ID, scene_id)).fetchone() if scene_id else None
        if action == "PREVIEW":
            if not scene: return jsonify(error="scene not found"), 404
            db.execute("UPDATE broadcast_sessions SET preview_scene_id=?,updated_at=? WHERE operation_id=?", (scene_id, now(), OPERATION_ID)); detail = f"Preview selected: {scene['name']}"
        elif action in {"TAKE", "CUT"}:
            scene = scene or db.execute("SELECT * FROM broadcast_scenes WHERE id=?", (session["preview_scene_id"],)).fetchone()
            if not scene: return jsonify(error="preview scene is not available"), 409
            db.execute("UPDATE broadcast_sessions SET program_scene_id=?,emergency=0,updated_at=? WHERE operation_id=?", (scene["id"], now(), OPERATION_ID)); detail = f"Program changed to {scene['name']} via {action}"
            # Encoders remain connected to the stable Program Bus. TAKE/CUT
            # changes that bus and must never restart YouTube or recording.
        elif action == "EMERGENCY":
            emergency = db.execute("SELECT * FROM broadcast_scenes WHERE operation_id=? AND scene_type='EMERGENCY' ORDER BY id LIMIT 1", (OPERATION_ID,)).fetchone()
            db.execute("UPDATE broadcast_sessions SET program_scene_id=?,preview_scene_id=?,emergency=1,updated_at=? WHERE operation_id=?", (emergency["id"], emergency["id"], now(), OPERATION_ID)); detail = "Emergency slate taken to program"
        elif action == "START_STREAM":
            destinations = [dict(row) for row in db.execute("SELECT * FROM stream_destinations WHERE operation_id=? AND enabled=1", (OPERATION_ID,))]
            if not destinations: return jsonify(error="enable and test at least one stream destination before going on air"), 409
            missing = [item["name"] for item in destinations if not load_stream_key(item["id"])]
            if missing: return jsonify(error="secure stream key missing: " + ", ".join(missing)), 409
            started = []
            if not current_app.config.get("TESTING"):
                program_scene = db.execute("SELECT * FROM broadcast_scenes WHERE id=?", (session["program_scene_id"],)).fetchone()
                cameras = _program_cameras(db, dict(program_scene))
                try:
                    probe_program_bus()
                    started = [start_output(destination, cameras, _scene_payload(program_scene)) for destination in destinations]
                except RuntimeError as exc:
                    stop_outputs()
                    return jsonify(error=str(exc)), 409
            for destination in destinations:
                db.execute("UPDATE stream_destinations SET status='STREAMING',updated_at=? WHERE id=?", (now(), destination["id"]))
            db.execute("UPDATE broadcast_sessions SET state='ON_AIR',started_at=?,updated_at=? WHERE operation_id=?", (now(), now(), OPERATION_ID)); detail = "Broadcast session placed ON AIR"
        elif action == "STOP_STREAM":
            stop_outputs()
            db.execute("UPDATE stream_destinations SET status=CASE WHEN enabled=1 THEN 'READY' ELSE 'DISABLED' END,updated_at=? WHERE operation_id=?", (now(), OPERATION_ID))
            db.execute("UPDATE broadcast_sessions SET state='OFF_AIR',recording=0,updated_at=? WHERE operation_id=?", (now(), OPERATION_ID)); detail = "Broadcast session stopped"
        elif action in {"START_RECORDING", "STOP_RECORDING"}:
            value = int(action == "START_RECORDING")
            if not current_app.config.get("TESTING"):
                if value:
                    program_scene = db.execute("SELECT * FROM broadcast_scenes WHERE id=?", (session["program_scene_id"],)).fetchone()
                    cameras = _program_cameras(db, dict(program_scene))
                    try:
                        probe_program_bus()
                        start_program_recording(cameras, _scene_payload(program_scene), Path(current_app.instance_path) / "public-program")
                    except RuntimeError as exc:
                        return jsonify(error=str(exc)), 409
                else:
                    stop_program_recording()
            db.execute("UPDATE broadcast_sessions SET recording=?,updated_at=? WHERE operation_id=?", (value, now(), OPERATION_ID)); detail = action.replace("_", " ").title()
        else: return jsonify(error="unsupported broadcast action"), 400
        db.execute("INSERT INTO broadcast_events(operation_id,occurred_at,action,detail) VALUES(?,?,?,?)", (OPERATION_ID, now(), action, detail))
        event(db, "BROADCAST", "BROADCAST_DIRECTOR", "WARNING" if action == "EMERGENCY" else "INFO", detail)
    return jsonify(ok=True, detail=detail)


@media.post("/api/media/destination/<int:destination_id>/state")
def destination_state(destination_id: int):
    enabled = int(bool(body().get("enabled")))
    with connect() as db:
        target = db.execute("SELECT * FROM stream_destinations WHERE operation_id=? AND id=?", (OPERATION_ID, destination_id)).fetchone()
        if not target: return jsonify(error="destination not found"), 404
        db.execute("UPDATE stream_destinations SET enabled=?,status=?,updated_at=? WHERE id=?", (enabled, "READY" if enabled else "DISABLED", now(), destination_id))
        event(db, "DESTINATION_STATE", "STREAM_ENGINEER", "INFO", f"Destination {target['name']} {'enabled' if enabled else 'disabled'}")
    return jsonify(ok=True)
