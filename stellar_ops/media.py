from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Blueprint, jsonify, render_template, request

from .control import OPERATION_ID, connect, event, init_control_db, snapshot

media = Blueprint("media", __name__)


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


def init_media_db() -> None:
    init_control_db()
    with connect() as db:
        db.executescript(SCHEMA)
        stamp = now()
        if not db.execute("SELECT 1 FROM graph_definitions WHERE operation_id=?", (OPERATION_ID,)).fetchone():
            graphs = [
                ("Propulsion Live", ["motor.chamber_pressure", "motor.thrust"], 60, {"linked_cursor": True, "show_limits": True}),
                ("Thermal Watch", ["motor.case_temperature"], 300, {"linked_cursor": True, "show_limits": True}),
                ("Ignition & Pressure", ["ignition.continuity", "motor.chamber_pressure"], 30, {"event_markers": True}),
            ]
            for name, channels, window, options in graphs:
                db.execute("INSERT INTO graph_definitions(operation_id,name,channels_json,time_window,options_json,updated_at) VALUES(?,?,?,?,?,?)",
                           (OPERATION_ID, name, json.dumps(channels), window, json.dumps(options), stamp))
        cameras = db.execute("SELECT id,name FROM devices WHERE operation_id=? AND device_type='IP-CAMERA'", (OPERATION_ID,)).fetchall()
        for camera in cameras:
            db.execute("""INSERT OR IGNORE INTO camera_profiles(operation_id,device_id,name,mode,main_url,preview_url,capabilities_json,updated_at)
                VALUES(?,?,?,'SIMULATION',NULL,NULL,?,?)""",
                       (OPERATION_ID, camera["id"], camera["name"], json.dumps({"ptz": False, "iso_record": True}), stamp))
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
        if not db.execute("SELECT 1 FROM broadcast_scenes WHERE operation_id=?", (OPERATION_ID,)).fetchone():
            scenes = [
                ("Standby", "SLATE", [{"kind": "title", "value": "QUALIFICATION TEST — STANDBY"}], "CUT"),
                ("Test Stand", "LIVE", [{"kind": "camera", "source": "CAM-01", "slot": "camera_main"}, {"kind": "graph", "source": "Propulsion Live", "slot": "telemetry_primary"}], "DISSOLVE"),
                ("Countdown", "LIVE", [{"kind": "camera", "source": "CAM-01", "slot": "camera_main"}, {"kind": "clock", "source": "MISSION_CLOCK", "slot": "mission_clock"}], "CUT"),
                ("Technical Hold", "SLATE", [{"kind": "title", "value": "TECHNICAL HOLD"}], "CUT"),
                ("Emergency", "EMERGENCY", [{"kind": "title", "value": "TRANSMISSION PAUSED"}], "CUT"),
            ]
            for name, kind, sources, transition in scenes:
                db.execute("""INSERT INTO broadcast_scenes(operation_id,name,scene_type,template_id,sources_json,transition,public_safe,updated_at)
                    VALUES(?,?,?,?,?,?,1,?)""", (OPERATION_ID, name, kind, template["id"], json.dumps(sources), transition, stamp))
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
        session = db.execute("SELECT * FROM broadcast_sessions WHERE operation_id=?", (OPERATION_ID,)).fetchone()
        result["broadcast"] = dict(session)
    core = snapshot()
    result["operation"] = core["operation"]
    result["channels"] = core["channels"]
    result["devices"] = core["devices"]
    result["telemetry"] = core["telemetry"]
    return result


@media.get("/media")
def media_console():
    return render_template("media.html", initial=media_snapshot(), display_slug=None)


@media.get("/display/<slug>")
def display_output(slug: str):
    with connect() as db:
        page = db.execute("SELECT 1 FROM display_pages WHERE operation_id=? AND slug=?", (OPERATION_ID, slug)).fetchone()
    if not page:
        return "Display page not found", 404
    return render_template("media.html", initial=media_snapshot(), display_slug=slug)


@media.get("/api/media/snapshot")
def api_media_snapshot():
    return jsonify(media_snapshot())


def body() -> dict:
    return request.get_json(silent=True) or {}


def clean_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")


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
    p = body(); device_id = str(p.get("device_id", "")).strip().upper(); mode = str(p.get("mode", "SIMULATION")).upper()
    if mode not in {"SIMULATION", "LIVE", "DISABLED"}: return jsonify(error="camera mode must be SIMULATION, LIVE or DISABLED"), 400
    main_url = str(p.get("main_url", "")).strip() or None; preview_url = str(p.get("preview_url", "")).strip() or None
    if mode == "LIVE" and (not main_url or urlparse(main_url).scheme.lower() != "rtsp"):
        return jsonify(error="LIVE camera requires an RTSP main stream URL"), 400
    with connect() as db:
        device = db.execute("SELECT name FROM devices WHERE operation_id=? AND id=? AND device_type='IP-CAMERA'", (OPERATION_ID, device_id)).fetchone()
        if not device: return jsonify(error="camera device must first exist in Engineering Setup"), 404
        db.execute("""INSERT INTO camera_profiles(operation_id,device_id,name,mode,main_url,preview_url,capabilities_json,enabled,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET name=excluded.name,mode=excluded.mode,
            main_url=excluded.main_url,preview_url=excluded.preview_url,capabilities_json=excluded.capabilities_json,
            enabled=excluded.enabled,updated_at=excluded.updated_at""", (OPERATION_ID, device_id, str(p.get("name") or device["name"]), mode, main_url, preview_url,
            json.dumps(p.get("capabilities", {})), int(mode != "DISABLED"), now()))
        event(db, "CAMERA_CONFIG", "VIDEO_ENGINEER", "INFO", f"Camera {device_id} configured for {mode}")
    return jsonify(ok=True)


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
    p = body(); name = str(p.get("name", "")).strip(); sources = p.get("sources", [])
    if not name or not isinstance(sources, list): return jsonify(error="scene name and sources are required"), 400
    with connect() as db:
        template_id = p.get("template_id")
        if template_id and not db.execute("SELECT 1 FROM published_templates WHERE operation_id=? AND id=?", (OPERATION_ID, template_id)).fetchone():
            return jsonify(error="published template not found"), 404
        db.execute("""INSERT INTO broadcast_scenes(operation_id,name,scene_type,template_id,sources_json,transition,public_safe,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(operation_id,name) DO UPDATE SET scene_type=excluded.scene_type,
            template_id=excluded.template_id,sources_json=excluded.sources_json,transition=excluded.transition,
            public_safe=excluded.public_safe,updated_at=excluded.updated_at""", (OPERATION_ID, name, str(p.get("scene_type", "LIVE")), template_id,
            json.dumps(sources), str(p.get("transition", "CUT")), int(bool(p.get("public_safe", True))), now()))
        event(db, "SCENE_CONFIG", "BROADCAST_DIRECTOR", "INFO", f"Broadcast scene saved: {name}")
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
        elif action == "EMERGENCY":
            emergency = db.execute("SELECT * FROM broadcast_scenes WHERE operation_id=? AND scene_type='EMERGENCY' ORDER BY id LIMIT 1", (OPERATION_ID,)).fetchone()
            db.execute("UPDATE broadcast_sessions SET program_scene_id=?,preview_scene_id=?,emergency=1,updated_at=? WHERE operation_id=?", (emergency["id"], emergency["id"], now(), OPERATION_ID)); detail = "Emergency slate taken to program"
        elif action == "START_STREAM":
            enabled = db.execute("SELECT count(*) FROM stream_destinations WHERE operation_id=? AND enabled=1", (OPERATION_ID,)).fetchone()[0]
            if not enabled: return jsonify(error="enable and test at least one stream destination before going on air"), 409
            db.execute("UPDATE broadcast_sessions SET state='ON_AIR',started_at=?,updated_at=? WHERE operation_id=?", (now(), now(), OPERATION_ID)); detail = "Broadcast session placed ON AIR"
        elif action == "STOP_STREAM":
            db.execute("UPDATE broadcast_sessions SET state='OFF_AIR',recording=0,updated_at=? WHERE operation_id=?", (now(), OPERATION_ID)); detail = "Broadcast session stopped"
        elif action in {"START_RECORDING", "STOP_RECORDING"}:
            value = int(action == "START_RECORDING"); db.execute("UPDATE broadcast_sessions SET recording=?,updated_at=? WHERE operation_id=?", (value, now(), OPERATION_ID)); detail = action.replace("_", " ").title()
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
