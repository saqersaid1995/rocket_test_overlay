from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_PACKAGE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
CHANNEL_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
CANVAS_RE = re.compile(r"^(\d{3,5})x(\d{3,5})(?:@(\d{1,3}))?$")

DATA_TYPES = {"number", "boolean", "text", "enum", "timestamp", "geo", "vector", "quaternion", "event", "series"}
CLASSIFICATIONS = {"PUBLIC", "DELAYED_PUBLIC", "INTERNAL", "RESTRICTED"}
SOURCE_KINDS = {"MEASURED", "DERIVED", "ESTIMATED", "EVENT", "SYSTEM"}
BROADCAST_PHASES = (
    "STANDBY", "CHECKOUT", "COUNTDOWN", "HOLD", "IGNITION", "LIFTOFF",
    "POWERED_ASCENT", "FIRING", "BURNOUT", "COAST", "APOGEE", "DESCENT",
    "RECOVERY", "POST_FIRE", "LANDING", "IMPACT", "COMPLETE", "CLOSED",
    "ABORT",
)
PHASE_TRANSITIONS = {"CUT", "DISSOLVE", "FADE"}

# Backward-compatible mapping for packages exported by the original Overlay Studio.
LEGACY_BINDING_CHANNELS = {
    "pressure": "motor.chamber_pressure",
    "thrust": "motor.thrust",
    "mission_clock": "mission.elapsed_time",
    "mission_time": "mission.elapsed_time",
    "mission_time_s": "mission.elapsed_time",
    "phase": "mission.phase",
    "status": "mission.phase",
    "telemetry.pressure": "motor.chamber_pressure",
    "telemetry.thrust": "motor.thrust",
    "frame.mission_time_s": "mission.elapsed_time",
    "frame.mission_clock": "mission.elapsed_time",
    "status.code": "mission.phase",
    "status.label": "mission.phase",
    "phases.active_id": "mission.phase",
    "phases.active_index": "mission.phase",
    "phases.progress": "mission.progress",
}


def _binding_channel(binding: object) -> str | None:
    value = str(binding or "").strip().lower()
    # Legacy bindings also look like dotted canonical IDs, so translate them
    # before accepting a value as a native channel identifier.
    for prefix, channel_id in LEGACY_BINDING_CHANNELS.items():
        if value == prefix or value.startswith(prefix + "."):
            return channel_id
    if value.startswith("channels."):
        candidate = value[len("channels."):].rsplit(".", 1)[0]
        if CHANNEL_ID_RE.fullmatch(candidate):
            return candidate
    if CHANNEL_ID_RE.fullmatch(value):
        return value
    return None


def _channel(channel_id: str, label: str, unit: str, category: str,
             classification: str = "PUBLIC", data_type: str = "number",
             source_kind: str = "MEASURED", description: str = "") -> dict[str, str]:
    return {
        "channel_id": channel_id,
        "label": label,
        "canonical_unit": unit,
        "category": category,
        "classification": classification,
        "data_type": data_type,
        "source_kind": source_kind,
        "description": description,
    }


CHANNEL_CATALOG = [
    _channel("mission.countdown", "Mission countdown", "s", "MISSION"),
    _channel("mission.elapsed_time", "Mission elapsed time", "s", "MISSION"),
    _channel("mission.utc", "UTC", "iso8601", "MISSION", data_type="timestamp", source_kind="SYSTEM"),
    _channel("mission.phase", "Mission phase", "state", "MISSION", data_type="enum", source_kind="EVENT"),
    _channel("mission.hold_state", "Hold state", "state", "MISSION", data_type="enum", source_kind="EVENT"),
    _channel("mission.progress", "Mission progress", "%", "MISSION", source_kind="DERIVED"),
    _channel("position.latitude", "Latitude", "deg", "POSITION", "INTERNAL", data_type="geo"),
    _channel("position.longitude", "Longitude", "deg", "POSITION", "INTERNAL", data_type="geo"),
    _channel("position.altitude_agl", "Altitude AGL", "m", "POSITION"),
    _channel("position.altitude_msl", "Altitude MSL", "m", "POSITION"),
    _channel("position.downrange", "Downrange distance", "m", "POSITION"),
    _channel("position.crossrange", "Crossrange distance", "m", "POSITION", "INTERNAL"),
    _channel("position.distance_from_pad", "Distance from launch pad", "m", "POSITION"),
    _channel("position.bearing_from_pad", "Bearing from launch pad", "deg", "POSITION"),
    _channel("position.gps_satellites", "GPS satellites", "count", "POSITION", "INTERNAL"),
    _channel("position.gps_accuracy", "GPS accuracy", "m", "POSITION", "INTERNAL"),
    _channel("velocity.speed_3d", "Vehicle speed", "m/s", "VELOCITY"),
    _channel("velocity.ground_speed", "Ground speed", "m/s", "VELOCITY"),
    _channel("velocity.vertical_speed", "Vertical speed", "m/s", "VELOCITY"),
    _channel("velocity.horizontal_speed", "Horizontal speed", "m/s", "VELOCITY"),
    _channel("velocity.mach", "Mach number", "Mach", "VELOCITY", source_kind="DERIVED"),
    _channel("velocity.heading", "Vehicle heading", "deg", "VELOCITY"),
    _channel("velocity.flight_path_angle", "Flight path angle", "deg", "VELOCITY", source_kind="DERIVED"),
    _channel("acceleration.x", "Acceleration X", "m/s2", "ACCELERATION", "INTERNAL"),
    _channel("acceleration.y", "Acceleration Y", "m/s2", "ACCELERATION", "INTERNAL"),
    _channel("acceleration.z", "Acceleration Z", "m/s2", "ACCELERATION", "INTERNAL"),
    _channel("acceleration.total", "Total acceleration", "m/s2", "ACCELERATION"),
    _channel("acceleration.g_load", "G load", "g", "ACCELERATION"),
    _channel("acceleration.max_g", "Maximum G load", "g", "ACCELERATION", source_kind="DERIVED"),
    _channel("attitude.pitch", "Pitch", "deg", "ATTITUDE"),
    _channel("attitude.roll", "Roll", "deg", "ATTITUDE"),
    _channel("attitude.yaw", "Yaw", "deg", "ATTITUDE"),
    _channel("attitude.azimuth", "Vehicle azimuth", "deg", "ATTITUDE"),
    _channel("attitude.tilt", "Vehicle tilt", "deg", "ATTITUDE", source_kind="DERIVED"),
    _channel("attitude.roll_rate", "Roll rate", "deg/s", "ATTITUDE", "INTERNAL"),
    _channel("attitude.pitch_rate", "Pitch rate", "deg/s", "ATTITUDE", "INTERNAL"),
    _channel("attitude.yaw_rate", "Yaw rate", "deg/s", "ATTITUDE", "INTERNAL"),
    _channel("attitude.quaternion", "Attitude quaternion", "quaternion", "ATTITUDE", "INTERNAL", data_type="quaternion"),
    _channel("motor.chamber_pressure", "Chamber pressure", "bar", "PROPULSION"),
    _channel("motor.thrust", "Motor thrust", "N", "PROPULSION"),
    _channel("motor.case_temperature", "Motor case temperature", "degC", "PROPULSION"),
    _channel("motor.nozzle_temperature", "Nozzle temperature", "degC", "PROPULSION"),
    _channel("motor.burn_time", "Burn time", "s", "PROPULSION", source_kind="DERIVED"),
    _channel("motor.total_impulse", "Total impulse", "N.s", "PROPULSION", source_kind="DERIVED"),
    _channel("motor.mass_flow", "Mass flow", "kg/s", "PROPULSION", "INTERNAL"),
    _channel("motor.ignition_state", "Ignition state", "state", "PROPULSION", data_type="enum", source_kind="EVENT"),
    _channel("motor.burn_state", "Burn state", "state", "PROPULSION", data_type="enum", source_kind="EVENT"),
    _channel("weather.wind_speed", "Wind speed", "m/s", "WEATHER"),
    _channel("weather.wind_gust", "Wind gust", "m/s", "WEATHER"),
    _channel("weather.wind_direction", "Wind direction", "deg", "WEATHER"),
    _channel("weather.temperature", "Ambient temperature", "degC", "WEATHER"),
    _channel("weather.pressure", "Atmospheric pressure", "hPa", "WEATHER"),
    _channel("weather.humidity", "Relative humidity", "%", "WEATHER"),
    _channel("weather.visibility", "Visibility", "km", "WEATHER"),
    _channel("vehicle.battery_voltage", "Vehicle battery voltage", "V", "VEHICLE", "INTERNAL"),
    _channel("vehicle.battery_percent", "Vehicle battery", "%", "VEHICLE", "INTERNAL"),
    _channel("vehicle.internal_temperature", "Avionics temperature", "degC", "VEHICLE", "INTERNAL"),
    _channel("vehicle.flight_computer_state", "Flight computer state", "state", "VEHICLE", "INTERNAL", data_type="enum"),
    _channel("vehicle.arming_state", "Vehicle arming state", "state", "VEHICLE", "RESTRICTED", data_type="enum"),
    _channel("vehicle.health", "Vehicle health", "state", "VEHICLE", data_type="enum", source_kind="SYSTEM"),
    _channel("recovery.deployment_state", "Recovery deployment state", "state", "RECOVERY", data_type="enum", source_kind="EVENT"),
    _channel("recovery.deployment_altitude", "Recovery deployment altitude", "m", "RECOVERY"),
    _channel("recovery.descent_rate", "Descent rate", "m/s", "RECOVERY"),
    _channel("recovery.landing_detected", "Landing detected", "state", "RECOVERY", data_type="boolean", source_kind="EVENT"),
    _channel("link.rssi", "Telemetry link strength", "dBm", "LINK", "INTERNAL"),
    _channel("link.latency", "Telemetry latency", "ms", "LINK", "INTERNAL"),
    _channel("link.packet_loss", "Packet loss", "%", "LINK", "INTERNAL"),
    _channel("link.last_packet_age", "Last packet age", "ms", "LINK", "INTERNAL", source_kind="SYSTEM"),
    _channel("link.status", "Telemetry link status", "state", "LINK", "INTERNAL", data_type="enum", source_kind="SYSTEM"),
    _channel("prediction.apogee", "Predicted apogee", "m", "PREDICTION", source_kind="ESTIMATED"),
    _channel("prediction.time_to_apogee", "Time to apogee", "s", "PREDICTION", source_kind="ESTIMATED"),
    _channel("prediction.impact_time", "Predicted impact time", "s", "PREDICTION", source_kind="ESTIMATED"),
    _channel("prediction.remaining_flight_time", "Remaining flight time", "s", "PREDICTION", source_kind="ESTIMATED"),
    _channel("prediction.trajectory_status", "Trajectory status", "state", "PREDICTION", data_type="enum", source_kind="ESTIMATED"),
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS media_channel_catalog(
 operation_id TEXT NOT NULL, channel_id TEXT NOT NULL, label TEXT NOT NULL,
 data_type TEXT NOT NULL, canonical_unit TEXT NOT NULL, category TEXT NOT NULL,
 classification TEXT NOT NULL, source_kind TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
 PRIMARY KEY(operation_id,channel_id));
CREATE TABLE IF NOT EXISTS overlay_packages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
 template_id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
 sha256 TEXT NOT NULL, canvas TEXT NOT NULL, manifest_json TEXT NOT NULL,
 layout_json TEXT NOT NULL, required_channels_json TEXT NOT NULL,
 optional_channels_json TEXT NOT NULL, archive_blob BLOB NOT NULL,
 state TEXT NOT NULL, public_safe INTEGER NOT NULL DEFAULT 0,
 uploaded_at TEXT NOT NULL, UNIQUE(operation_id,template_id,version));
CREATE TABLE IF NOT EXISTS bundled_overlay_imports(
 operation_id TEXT NOT NULL, file_name TEXT NOT NULL, package_id INTEGER NOT NULL,
 file_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL,
 PRIMARY KEY(operation_id,file_name));
CREATE TABLE IF NOT EXISTS broadcast_phase_overlays(
 operation_id TEXT NOT NULL, phase TEXT NOT NULL, package_id INTEGER NOT NULL,
 transition TEXT NOT NULL DEFAULT 'CUT', enabled INTEGER NOT NULL DEFAULT 1,
 updated_at TEXT NOT NULL, PRIMARY KEY(operation_id,phase));
CREATE TABLE IF NOT EXISTS broadcast_overlay_selection(
 operation_id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'AUTO',
 manual_package_id INTEGER, active_package_id INTEGER, active_phase TEXT,
 updated_at TEXT NOT NULL);
"""


class PackageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedPackage:
    template_id: str
    name: str
    version: str
    canvas: str
    sha256: str
    manifest: dict[str, Any]
    layout: dict[str, Any]
    required_channels: list[str]
    optional_channels: list[str]
    public_safe: bool
    archive: bytes


def ensure_broadcast_telemetry_schema(db: sqlite3.Connection, operation_id: str, stamp: str) -> None:
    db.executescript(SCHEMA)
    for item in CHANNEL_CATALOG:
        db.execute(
            """INSERT OR IGNORE INTO media_channel_catalog(
                   operation_id,channel_id,label,data_type,canonical_unit,category,
                   classification,source_kind,description,enabled,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
            (
                operation_id, item["channel_id"], item["label"], item["data_type"],
                item["canonical_unit"], item["category"], item["classification"],
                item["source_kind"], item["description"], stamp,
            ),
        )
    db.execute(
        """INSERT OR IGNORE INTO broadcast_overlay_selection(
               operation_id,mode,manual_package_id,active_package_id,active_phase,updated_at)
           VALUES(?,'AUTO',NULL,NULL,'STANDBY',?)""",
        (operation_id, stamp),
    )


def channel_catalog(db: sqlite3.Connection, operation_id: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in db.execute(
            """SELECT channel_id,label,data_type,canonical_unit,category,
                      classification,source_kind,description,enabled,updated_at
               FROM media_channel_catalog WHERE operation_id=?
               ORDER BY category,channel_id""",
            (operation_id,),
        )
    ]


def overlay_packages(db: sqlite3.Connection, operation_id: str) -> list[dict[str, Any]]:
    result = []
    for row in db.execute(
        """SELECT id,template_id,name,version,sha256,canvas,manifest_json,layout_json,
                  required_channels_json,optional_channels_json,state,public_safe,uploaded_at
           FROM overlay_packages WHERE operation_id=? ORDER BY uploaded_at DESC,id DESC""",
        (operation_id,),
    ):
        item = dict(row)
        for field in ("manifest_json", "layout_json", "required_channels_json", "optional_channels_json"):
            item[field[:-5]] = json.loads(item.pop(field))
        result.append(item)
    return result


def register_channel(db: sqlite3.Connection, operation_id: str, payload: dict[str, Any], stamp: str) -> dict[str, Any]:
    channel_id = str(payload.get("channel_id", "")).strip().lower()
    data_type = str(payload.get("data_type", "number")).strip().lower()
    classification = str(payload.get("classification", "INTERNAL")).strip().upper()
    source_kind = str(payload.get("source_kind", "MEASURED")).strip().upper()
    label = str(payload.get("label", "")).strip()
    unit = str(payload.get("canonical_unit", "")).strip()
    category = str(payload.get("category", "CUSTOM")).strip().upper()
    if not CHANNEL_ID_RE.fullmatch(channel_id):
        raise ValueError("channel_id must use a dotted canonical identifier such as navigation.altitude_agl")
    if not label or len(label) > 100:
        raise ValueError("channel label is required and must be 100 characters or fewer")
    if data_type not in DATA_TYPES:
        raise ValueError("unsupported telemetry data type")
    if classification not in CLASSIFICATIONS:
        raise ValueError("unsupported data classification")
    if source_kind not in SOURCE_KINDS:
        raise ValueError("unsupported source kind")
    if not unit or len(unit) > 24:
        raise ValueError("canonical unit is required and must be 24 characters or fewer")
    db.execute(
        """INSERT INTO media_channel_catalog(
               operation_id,channel_id,label,data_type,canonical_unit,category,
               classification,source_kind,description,enabled,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,1,?)
           ON CONFLICT(operation_id,channel_id) DO UPDATE SET
             label=excluded.label,data_type=excluded.data_type,
             canonical_unit=excluded.canonical_unit,category=excluded.category,
             classification=excluded.classification,source_kind=excluded.source_kind,
             description=excluded.description,enabled=1,updated_at=excluded.updated_at""",
        (
            operation_id, channel_id, label, data_type, unit, category,
            classification, source_kind,
            str(payload.get("description", "")).strip()[:500], stamp,
        ),
    )
    return dict(db.execute(
        "SELECT * FROM media_channel_catalog WHERE operation_id=? AND channel_id=?",
        (operation_id, channel_id),
    ).fetchone())


def _safe_members(archive: zipfile.ZipFile) -> None:
    total = 0
    seen: set[str] = set()
    for member in archive.infolist():
        normalized = member.filename.replace("\\", "/").rstrip("/")
        parts = normalized.split("/")
        if not normalized or normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise PackageValidationError("package contains an unsafe path")
        if member.is_dir():
            continue
        if member.flag_bits & 0x1:
            raise PackageValidationError("encrypted package members are not supported")
        total += member.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise PackageValidationError("uncompressed package exceeds 25 MB")
        if normalized in seen:
            raise PackageValidationError("package contains duplicate paths")
        seen.add(normalized)


def _read_json(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        value = json.loads(archive.read(name).decode("utf-8"))
    except KeyError as exc:
        raise PackageValidationError(f"{name} is required") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageValidationError(f"{name} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PackageValidationError(f"{name} must contain a JSON object")
    return value


def _channel_ids(value: Any, field: str, legacy: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PackageValidationError(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            raw = item.get("id") or item.get("channel") or item.get("binding")
        else:
            raw = item
        channel_id = str(raw or "").strip().lower()
        if legacy:
            channel_id = _binding_channel(channel_id) or ""
            # Visual-only Studio variables (colors, labels, asset toggles) may
            # appear beside telemetry declarations. They are not data channels.
            if not channel_id:
                continue
        if not CHANNEL_ID_RE.fullmatch(channel_id):
            raise PackageValidationError(f"{field} contains an invalid channel identifier")
        if channel_id not in result:
            result.append(channel_id)
    return result


def install_bundled_packages(
    db: sqlite3.Connection, operation_id: str, stamp: str,
    package_dir: Path | None = None,
) -> list[int]:
    directory = package_dir or Path(__file__).resolve().parent.parent / "UPLOAD_TEMPLATE_HERE"
    if not directory.is_dir():
        return []
    imported: list[int] = []
    catalog = channel_catalog(db, operation_id)
    for path in sorted(directory.glob("*.rotpl")):
        known = db.execute(
            """SELECT package_id FROM bundled_overlay_imports
               WHERE operation_id=? AND file_name=?""",
            (operation_id, path.name),
        ).fetchone()
        if known:
            continue
        package_bytes = path.read_bytes()
        package = validate_package(path.name, package_bytes, catalog)
        package_id = save_package(db, operation_id, package, stamp)
        db.execute(
            """INSERT INTO bundled_overlay_imports(
                   operation_id,file_name,package_id,file_sha256,imported_at)
               VALUES(?,?,?,?,?)""",
            (operation_id, path.name, package_id, package.sha256, stamp),
        )
        imported.append(package_id)
    first = db.execute(
        """SELECT id FROM overlay_packages WHERE operation_id=?
           ORDER BY public_safe DESC,id LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if first:
        for phase in BROADCAST_PHASES:
            db.execute(
                """INSERT OR IGNORE INTO broadcast_phase_overlays(
                       operation_id,phase,package_id,transition,enabled,updated_at)
                   VALUES(?,?,?,'CUT',1,?)""",
                (operation_id, phase, first["id"], stamp),
            )
    return imported


def phase_overlay_assignments(db: sqlite3.Connection, operation_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        """SELECT mapping.phase,mapping.package_id,mapping.transition,mapping.enabled,
                  package.template_id,package.name AS package_name,package.version,
                  package.canvas,package.public_safe
           FROM broadcast_phase_overlays mapping
           JOIN overlay_packages package ON package.id=mapping.package_id
           WHERE mapping.operation_id=? ORDER BY
             CASE mapping.phase
               WHEN 'STANDBY' THEN 1 WHEN 'CHECKOUT' THEN 2
               WHEN 'COUNTDOWN' THEN 3 WHEN 'HOLD' THEN 4
               WHEN 'IGNITION' THEN 5 WHEN 'LIFTOFF' THEN 6
               WHEN 'POWERED_ASCENT' THEN 7 WHEN 'FIRING' THEN 8
               WHEN 'BURNOUT' THEN 9 WHEN 'COAST' THEN 10
               WHEN 'APOGEE' THEN 11 WHEN 'DESCENT' THEN 12
               WHEN 'RECOVERY' THEN 13 WHEN 'POST_FIRE' THEN 14
               WHEN 'LANDING' THEN 15 WHEN 'IMPACT' THEN 16
               WHEN 'COMPLETE' THEN 17 WHEN 'CLOSED' THEN 18
               ELSE 19 END""",
        (operation_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def save_phase_overlay(
    db: sqlite3.Connection, operation_id: str, phase: str,
    package_id: int, transition: str, stamp: str,
) -> None:
    phase = phase.strip().upper()
    transition = transition.strip().upper()
    if phase not in BROADCAST_PHASES:
        raise ValueError("unsupported broadcast mission phase")
    if transition not in PHASE_TRANSITIONS:
        raise ValueError("transition must be CUT, DISSOLVE or FADE")
    package = db.execute(
        """SELECT public_safe FROM overlay_packages
           WHERE operation_id=? AND id=? AND state='VALIDATED'""",
        (operation_id, package_id),
    ).fetchone()
    if not package:
        raise ValueError("validated overlay package not found")
    if not package["public_safe"]:
        raise ValueError("internal or restricted overlay package cannot be assigned to public broadcast")
    db.execute(
        """INSERT INTO broadcast_phase_overlays(
               operation_id,phase,package_id,transition,enabled,updated_at)
           VALUES(?,?,?,?,1,?)
           ON CONFLICT(operation_id,phase) DO UPDATE SET
             package_id=excluded.package_id,transition=excluded.transition,
             enabled=1,updated_at=excluded.updated_at""",
        (operation_id, phase, package_id, transition, stamp),
    )


def resolve_overlay_selection(
    db: sqlite3.Connection, operation_id: str, runtime_phase: str, stamp: str,
) -> dict[str, Any]:
    selection = db.execute(
        "SELECT * FROM broadcast_overlay_selection WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    mode = selection["mode"] if selection else "AUTO"
    active = None
    transition = "CUT"
    if mode == "MANUAL" and selection and selection["manual_package_id"]:
        active = db.execute(
            "SELECT * FROM overlay_packages WHERE operation_id=? AND id=?",
            (operation_id, selection["manual_package_id"]),
        ).fetchone()
    else:
        mapping = db.execute(
            """SELECT package.*,mapping.transition
               FROM broadcast_phase_overlays mapping
               JOIN overlay_packages package ON package.id=mapping.package_id
               WHERE mapping.operation_id=? AND mapping.phase=? AND mapping.enabled=1""",
            (operation_id, runtime_phase),
        ).fetchone()
        if mapping:
            active = mapping
            transition = mapping["transition"]
    active_id = active["id"] if active else None
    db.execute(
        """UPDATE broadcast_overlay_selection
           SET active_package_id=?,active_phase=?,updated_at=?
           WHERE operation_id=?""",
        (active_id, runtime_phase, stamp, operation_id),
    )
    public_package = dict(active) if active else None
    if public_package:
        public_package.pop("archive_blob", None)
    return {
        "mode": mode,
        "active_phase": runtime_phase,
        "active_package_id": active_id,
        "transition": transition,
        "package": public_package,
    }


def set_overlay_selection(
    db: sqlite3.Connection, operation_id: str, mode: str,
    package_id: int | None, stamp: str,
) -> None:
    mode = mode.strip().upper()
    if mode not in {"AUTO", "MANUAL"}:
        raise ValueError("overlay selection mode must be AUTO or MANUAL")
    if mode == "MANUAL":
        package = db.execute(
            """SELECT 1 FROM overlay_packages
               WHERE operation_id=? AND id=? AND state='VALIDATED' AND public_safe=1""",
            (operation_id, package_id),
        ).fetchone()
        if not package:
            raise ValueError("manual selection requires a public-safe validated package")
    db.execute(
        """UPDATE broadcast_overlay_selection
           SET mode=?,manual_package_id=?,updated_at=? WHERE operation_id=?""",
        (mode, package_id if mode == "MANUAL" else None, stamp, operation_id),
    )


def validate_package(filename: str, package_bytes: bytes, catalog: list[dict[str, Any]]) -> ValidatedPackage:
    if not filename.lower().endswith((".rotpl", ".zip")):
        raise PackageValidationError("overlay package must use .rotpl or .zip")
    if not package_bytes:
        raise PackageValidationError("overlay package is empty")
    if len(package_bytes) > MAX_PACKAGE_BYTES:
        raise PackageValidationError("overlay package exceeds 10 MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(package_bytes))
    except zipfile.BadZipFile as exc:
        raise PackageValidationError("overlay package is not a valid ZIP/ROTPL archive") from exc
    with archive:
        _safe_members(archive)
        manifest = _read_json(archive, "manifest.json")
        entry = str(manifest.get("entry", "layout.json")).replace("\\", "/")
        if not entry or entry.startswith("/") or ".." in entry.split("/"):
            raise PackageValidationError("manifest entry is unsafe")
        layout = _read_json(archive, entry)

    raw_template_id = str(manifest.get("template_id") or manifest.get("id") or "").strip().lower()
    if manifest.get("schema") == "rocket-overlay-template":
        # Studio v1 allowed display-like identifiers (spaces, underscores and
        # punctuation). Preserve compatibility while storing one canonical ID.
        template_id = re.sub(r"[^a-z0-9-]+", "-", raw_template_id).strip("-")
        if not template_id:
            template_id = re.sub(r"[^a-z0-9-]+", "-", Path(filename).stem.lower()).strip("-")
    else:
        template_id = raw_template_id
    version = str(manifest.get("version") or manifest.get("template_version") or "").strip()
    name = str(manifest.get("name") or manifest.get("display_name") or template_id).strip()
    canvas_value = manifest.get("canvas", "")
    if isinstance(canvas_value, dict):
        try:
            canvas = f"{int(canvas_value['width'])}x{int(canvas_value['height'])}"
        except (KeyError, TypeError, ValueError) as exc:
            raise PackageValidationError("manifest canvas dimensions are invalid") from exc
    else:
        canvas = str(canvas_value).strip().lower()
    if not TEMPLATE_ID_RE.fullmatch(template_id):
        raise PackageValidationError("manifest template_id is invalid")
    if not VERSION_RE.fullmatch(version):
        raise PackageValidationError("manifest version must use semantic versioning")
    match = CANVAS_RE.fullmatch(canvas)
    if not match or not (640 <= int(match.group(1)) <= 7680 and 360 <= int(match.group(2)) <= 4320):
        raise PackageValidationError("manifest canvas must be a supported WIDTHxHEIGHT or WIDTHxHEIGHT@FPS value")
    if not name or len(name) > 100:
        raise PackageValidationError("manifest name is required and must be 100 characters or fewer")

    if "required_channels" in manifest:
        required = _channel_ids(
            manifest.get("required_channels"), "required_channels",
            legacy=manifest.get("schema") == "rocket-overlay-template",
        )
    else:
        required = []
        bindings = manifest.get("required_bindings", [])
        if not isinstance(bindings, list):
            raise PackageValidationError("required_bindings must be a list")
        for binding in bindings:
            channel_id = _binding_channel(binding)
            if channel_id and channel_id not in required:
                required.append(channel_id)
    if "optional_channels" in manifest:
        optional = _channel_ids(
            manifest.get("optional_channels"), "optional_channels",
            legacy=manifest.get("schema") == "rocket-overlay-template",
        )
    else:
        optional = []
        variables = manifest.get("variables", {})
        if isinstance(variables, dict):
            for binding in variables:
                channel_id = _binding_channel(binding)
                if channel_id and channel_id not in required and channel_id not in optional:
                    optional.append(channel_id)
    if set(required) & set(optional):
        raise PackageValidationError("a channel cannot be both required and optional")

    elements = layout.get("elements", layout.get("widgets", []))
    if not isinstance(elements, list):
        raise PackageValidationError("layout elements must be a list")
    declared = set(required) | set(optional)
    for element in elements:
        if not isinstance(element, dict):
            raise PackageValidationError("every layout element must be an object")
        binding = element.get("binding", element.get("bind"))
        if isinstance(binding, dict):
            binding = binding.get("channel")
        channel_id = _binding_channel(binding)
        if channel_id and channel_id not in declared:
            # Legacy Studio packages may declare a value as a variable rather
            # than a required binding. Treat it as optional and preserve it.
            if manifest.get("schema") == "rocket-overlay-template":
                optional.append(channel_id)
                declared.add(channel_id)
            else:
                raise PackageValidationError(f"layout binding is not declared in manifest: {channel_id}")

    by_id = {item["channel_id"]: item for item in catalog if item.get("enabled")}
    missing = sorted(set(required) - set(by_id))
    if missing:
        raise PackageValidationError("required telemetry channels are not registered: " + ", ".join(missing))
    declared_channels = set(required) | set(optional)
    public_safe = bool(declared_channels) and all(
        channel in by_id
        and by_id[channel]["classification"] in {"PUBLIC", "DELAYED_PUBLIC"}
        for channel in declared_channels
    )
    digest = hashlib.sha256(package_bytes).hexdigest()
    return ValidatedPackage(
        template_id=template_id,
        name=name,
        version=version,
        canvas=canvas,
        sha256=digest,
        manifest=manifest,
        layout=layout,
        required_channels=required,
        optional_channels=optional,
        public_safe=public_safe,
        archive=package_bytes,
    )


def save_package(db: sqlite3.Connection, operation_id: str,
                 package: ValidatedPackage, stamp: str) -> int:
    db.execute(
        """INSERT INTO overlay_packages(
               operation_id,template_id,name,version,sha256,canvas,manifest_json,
               layout_json,required_channels_json,optional_channels_json,
               archive_blob,state,public_safe,uploaded_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,'VALIDATED',?,?)
           ON CONFLICT(operation_id,template_id,version) DO UPDATE SET
             name=excluded.name,sha256=excluded.sha256,canvas=excluded.canvas,
             manifest_json=excluded.manifest_json,layout_json=excluded.layout_json,
             required_channels_json=excluded.required_channels_json,
             optional_channels_json=excluded.optional_channels_json,
             archive_blob=excluded.archive_blob,state='VALIDATED',
             public_safe=excluded.public_safe,uploaded_at=excluded.uploaded_at""",
        (
            operation_id, package.template_id, package.name, package.version,
            package.sha256, package.canvas, json.dumps(package.manifest),
            json.dumps(package.layout), json.dumps(package.required_channels),
            json.dumps(package.optional_channels), package.archive,
            int(package.public_safe), stamp,
        ),
    )
    return int(db.execute(
        """SELECT id FROM overlay_packages
           WHERE operation_id=? AND template_id=? AND version=?""",
        (operation_id, package.template_id, package.version),
    ).fetchone()["id"])
