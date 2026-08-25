from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass
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
        normalized = member.filename.replace("\\", "/")
        parts = normalized.split("/")
        if not normalized or normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise PackageValidationError("package contains an unsafe path")
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


def _channel_ids(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PackageValidationError(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        channel_id = item.get("id") if isinstance(item, dict) else item
        channel_id = str(channel_id or "").strip().lower()
        if not CHANNEL_ID_RE.fullmatch(channel_id):
            raise PackageValidationError(f"{field} contains an invalid channel identifier")
        if channel_id not in result:
            result.append(channel_id)
    return result


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
        layout = _read_json(archive, "layout.json")

    template_id = str(manifest.get("template_id", "")).strip().lower()
    version = str(manifest.get("version", "")).strip()
    name = str(manifest.get("name") or template_id).strip()
    canvas = str(manifest.get("canvas", "")).strip().lower()
    if not TEMPLATE_ID_RE.fullmatch(template_id):
        raise PackageValidationError("manifest template_id is invalid")
    if not VERSION_RE.fullmatch(version):
        raise PackageValidationError("manifest version must use semantic versioning")
    match = CANVAS_RE.fullmatch(canvas)
    if not match or not (640 <= int(match.group(1)) <= 7680 and 360 <= int(match.group(2)) <= 4320):
        raise PackageValidationError("manifest canvas must be a supported WIDTHxHEIGHT or WIDTHxHEIGHT@FPS value")
    if not name or len(name) > 100:
        raise PackageValidationError("manifest name is required and must be 100 characters or fewer")

    required = _channel_ids(manifest.get("required_channels"), "required_channels")
    optional = _channel_ids(manifest.get("optional_channels"), "optional_channels")
    if set(required) & set(optional):
        raise PackageValidationError("a channel cannot be both required and optional")

    elements = layout.get("elements", layout.get("widgets", []))
    if not isinstance(elements, list):
        raise PackageValidationError("layout elements must be a list")
    declared = set(required) | set(optional)
    for element in elements:
        if not isinstance(element, dict):
            raise PackageValidationError("every layout element must be an object")
        binding = element.get("binding")
        if isinstance(binding, str) and binding and binding not in declared:
            raise PackageValidationError(f"layout binding is not declared in manifest: {binding}")
        if isinstance(binding, dict):
            channel_id = str(binding.get("channel", "")).strip().lower()
            if channel_id and channel_id not in declared:
                raise PackageValidationError(f"layout binding is not declared in manifest: {channel_id}")

    by_id = {item["channel_id"]: item for item in catalog if item.get("enabled")}
    missing = sorted(set(required) - set(by_id))
    if missing:
        raise PackageValidationError("required telemetry channels are not registered: " + ", ".join(missing))
    public_safe = all(by_id[channel]["classification"] in {"PUBLIC", "DELAYED_PUBLIC"} for channel in required)
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
           VALUES(?,?,?,?,?,?,?,?,?,?,?,'VALIDATED',?,?,?)
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
