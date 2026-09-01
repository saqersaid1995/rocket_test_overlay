from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from .control import OPERATION_ID, connect, init_control_db

weather = Blueprint("weather", __name__)

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, object] = {"key": None, "at": 0.0, "payload": None}
CACHE_SECONDS = 300


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_schema() -> None:
    init_control_db()
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_settings(
              operation_id TEXT PRIMARY KEY,
              site_name TEXT NOT NULL DEFAULT '',
              latitude REAL,
              longitude REAL,
              updated_at TEXT NOT NULL
            )
            """
        )
        row = db.execute(
            "SELECT 1 FROM weather_settings WHERE operation_id=?", (OPERATION_ID,)
        ).fetchone()
        if row is None:
            lat = os.environ.get("STELLAR_OPS_SITE_LAT", "").strip()
            lon = os.environ.get("STELLAR_OPS_SITE_LON", "").strip()
            site = os.environ.get("STELLAR_OPS_SITE_NAME", "Operation Site").strip()
            try:
                lat_value = float(lat) if lat else None
                lon_value = float(lon) if lon else None
            except ValueError:
                lat_value = lon_value = None
            db.execute(
                "INSERT INTO weather_settings(operation_id,site_name,latitude,longitude,updated_at) VALUES(?,?,?,?,?)",
                (OPERATION_ID, site, lat_value, lon_value, utc_now()),
            )
        db.commit()


def settings() -> dict:
    ensure_schema()
    with connect() as db:
        row = db.execute(
            "SELECT site_name,latitude,longitude,updated_at FROM weather_settings WHERE operation_id=?",
            (OPERATION_ID,),
        ).fetchone()
    return dict(row) if row else {
        "site_name": "Operation Site",
        "latitude": None,
        "longitude": None,
        "updated_at": utc_now(),
    }


def _nearest_hour_index(times: list[str]) -> int:
    if not times:
        return 0
    now = datetime.now(timezone.utc)
    best_index = 0
    best_delta = float("inf")
    for index, value in enumerate(times):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            delta = abs((stamp.astimezone(timezone.utc) - now).total_seconds())
        except (TypeError, ValueError):
            continue
        if delta < best_delta:
            best_delta = delta
            best_index = index
    return best_index


def _at(values: dict, key: str, index: int):
    items = values.get(key) or []
    return items[index] if index < len(items) else None


def _fetch_open_meteo(latitude: float, longitude: float) -> dict:
    params = {
        "latitude": f"{latitude:.6f}",
        "longitude": f"{longitude:.6f}",
        "timezone": "UTC",
        "forecast_days": "2",
        "wind_speed_unit": "kmh",
        "current": ",".join(
            [
                "temperature_2m",
                "relative_humidity_2m",
                "precipitation",
                "weather_code",
                "cloud_cover",
                "surface_pressure",
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
            ]
        ),
        "hourly": ",".join(
            [
                "visibility",
                "precipitation_probability",
                "wind_speed_80m",
                "wind_direction_80m",
                "wind_speed_120m",
                "wind_direction_120m",
                "wind_speed_180m",
                "wind_direction_180m",
            ]
        ),
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Stellar-Ops/2 Open-Meteo integration",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"Open-Meteo returned HTTP {response.status}")
        raw = json.loads(response.read().decode("utf-8"))

    current = raw.get("current") or {}
    hourly = raw.get("hourly") or {}
    index = _nearest_hour_index(hourly.get("time") or [])
    visibility = _at(hourly, "visibility", index)

    return {
        "provider": "Open-Meteo",
        "model_note": "Forecast data — not an on-site sensor",
        "latitude": raw.get("latitude", latitude),
        "longitude": raw.get("longitude", longitude),
        "elevation_m": raw.get("elevation"),
        "forecast_time_utc": current.get("time"),
        "fetched_at_utc": utc_now(),
        "temperature_c": current.get("temperature_2m"),
        "relative_humidity_percent": current.get("relative_humidity_2m"),
        "surface_pressure_hpa": current.get("surface_pressure"),
        "cloud_cover_percent": current.get("cloud_cover"),
        "precipitation_mm": current.get("precipitation"),
        "precipitation_probability_percent": _at(hourly, "precipitation_probability", index),
        "visibility_m": visibility,
        "weather_code": current.get("weather_code"),
        "wind": {
            "10m": {
                "speed_kmh": current.get("wind_speed_10m"),
                "direction_deg": current.get("wind_direction_10m"),
                "gust_kmh": current.get("wind_gusts_10m"),
            },
            "80m": {
                "speed_kmh": _at(hourly, "wind_speed_80m", index),
                "direction_deg": _at(hourly, "wind_direction_80m", index),
            },
            "120m": {
                "speed_kmh": _at(hourly, "wind_speed_120m", index),
                "direction_deg": _at(hourly, "wind_direction_120m", index),
            },
            "180m": {
                "speed_kmh": _at(hourly, "wind_speed_180m", index),
                "direction_deg": _at(hourly, "wind_direction_180m", index),
            },
        },
    }


def weather_snapshot(force: bool = False) -> dict:
    cfg = settings()
    latitude = cfg.get("latitude")
    longitude = cfg.get("longitude")
    if latitude is None or longitude is None:
        return {
            "ok": False,
            "status": "NOT_CONFIGURED",
            "message": "Set the operation-site latitude and longitude to enable Open-Meteo.",
            "settings": cfg,
        }

    key = f"{float(latitude):.6f},{float(longitude):.6f}"
    with _CACHE_LOCK:
        cached = _CACHE.get("payload")
        if (
            not force
            and _CACHE.get("key") == key
            and cached
            and time.time() - float(_CACHE.get("at") or 0) < CACHE_SECONDS
        ):
            return {"ok": True, "status": "FORECAST", "cached": True, "settings": cfg, "weather": cached}

    try:
        payload = _fetch_open_meteo(float(latitude), float(longitude))
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, RuntimeError) as exc:
        with _CACHE_LOCK:
            cached = _CACHE.get("payload") if _CACHE.get("key") == key else None
        if cached:
            return {
                "ok": True,
                "status": "STALE_FORECAST",
                "cached": True,
                "message": f"Open-Meteo refresh failed; showing last cached forecast: {exc}",
                "settings": cfg,
                "weather": cached,
            }
        return {
            "ok": False,
            "status": "UNAVAILABLE",
            "message": f"Open-Meteo is unavailable: {exc}",
            "settings": cfg,
        }

    with _CACHE_LOCK:
        _CACHE.update({"key": key, "at": time.time(), "payload": payload})
    return {"ok": True, "status": "FORECAST", "cached": False, "settings": cfg, "weather": payload}


@weather.get("/api/weather")
def get_weather():
    return jsonify(weather_snapshot(force=request.args.get("refresh") == "1"))


@weather.post("/api/weather/location")
def save_weather_location():
    payload = request.get_json(silent=True) or {}
    site_name = str(payload.get("site_name", "Operation Site")).strip()[:120] or "Operation Site"
    try:
        latitude = float(payload.get("latitude"))
        longitude = float(payload.get("longitude"))
    except (TypeError, ValueError):
        return jsonify(error="latitude and longitude must be numeric"), 400
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return jsonify(error="latitude or longitude is outside the valid range"), 400

    ensure_schema()
    with connect() as db:
        db.execute(
            """
            UPDATE weather_settings
               SET site_name=?,latitude=?,longitude=?,updated_at=?
             WHERE operation_id=?
            """,
            (site_name, latitude, longitude, utc_now(), OPERATION_ID),
        )
        db.commit()

    with _CACHE_LOCK:
        _CACHE.update({"key": None, "at": 0.0, "payload": None})
    return jsonify(ok=True, settings=settings(), weather=weather_snapshot(force=True))
