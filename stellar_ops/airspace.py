from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from flask import Blueprint, Response, jsonify, request

from .weather import settings as weather_settings


airspace = Blueprint("airspace", __name__)

TRAFFIC_CACHE_SECONDS = 8
_TILE_CACHE_SECONDS = 3600
_traffic_cache: dict[str, tuple[float, dict]] = {}
_tile_cache: dict[str, tuple[float, bytes, str]] = {}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aircraft_item(raw: dict, site_lat: float, site_lon: float) -> dict | None:
    lat = _number(raw.get("lat"))
    lon = _number(raw.get("lon"))
    if lat is None or lon is None:
        return None
    distance = _haversine_km(site_lat, site_lon, lat, lon)
    altitude_ft = raw.get("alt_baro")
    if isinstance(altitude_ft, str) and altitude_ft.lower() == "ground":
        altitude_ft = 0
    altitude_ft = _number(altitude_ft)
    if altitude_ft is None:
        altitude_ft = _number(raw.get("alt_geom"))
    speed_kt = _number(raw.get("gs"))
    track = _number(raw.get("track"))
    seen = _number(raw.get("seen"))
    return {
        "hex": str(raw.get("hex") or "").strip(),
        "callsign": str(raw.get("flight") or "").strip() or "—",
        "registration": str(raw.get("r") or "").strip() or "—",
        "aircraft_type": str(raw.get("t") or "").strip() or "—",
        "lat": lat,
        "lon": lon,
        "altitude_ft": altitude_ft,
        "ground_speed_kt": speed_kt,
        "ground_speed_kmh": round(speed_kt * 1.852, 1) if speed_kt is not None else None,
        "track_deg": track,
        "vertical_rate_fpm": _number(raw.get("baro_rate")),
        "squawk": str(raw.get("squawk") or "").strip() or None,
        "distance_km": round(distance, 2),
        "bearing_deg": round(_bearing_deg(site_lat, site_lon, lat, lon), 1),
        "seen_seconds": seen,
    }


def _read_provider(url: str, provider: str) -> tuple[dict, str]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "Stellar-Ops/2 air-traffic-situational-awareness",
        },
    )
    with urllib.request.urlopen(req, timeout=3.5) as response:
        if response.status != 200:
            raise RuntimeError(f"{provider} returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8")), provider


def _fetch_traffic(site_lat: float, site_lon: float, radius_km: float) -> dict:
    radius_nm = max(1, min(250, int(math.ceil(radius_km / 1.852))))
    providers = [
        ("ADSB.lol", f"https://api.adsb.lol/v2/point/{site_lat:.6f}/{site_lon:.6f}/{radius_nm}", "ODbL 1.0"),
        ("ADSB.one", f"https://api.adsb.one/v2/point/{site_lat:.6f}/{site_lon:.6f}/{radius_nm}", "provider terms apply"),
    ]
    payload = None
    provider = None
    license_note = None
    provider_errors = []
    for name, url, license_value in providers:
        try:
            payload, provider = _read_provider(url, name)
            license_note = license_value
            break
        except Exception as exc:
            provider_errors.append(f"{name}: {exc}")
    if payload is None:
        raise RuntimeError("; ".join(provider_errors) or "No ADS-B provider responded")
    raw_aircraft = payload.get("ac") or payload.get("aircraft") or []
    aircraft = []
    for raw in raw_aircraft:
        if not isinstance(raw, dict):
            continue
        item = _aircraft_item(raw, site_lat, site_lon)
        if item and item["distance_km"] <= radius_km:
            aircraft.append(item)
    aircraft.sort(key=lambda item: item["distance_km"])
    nearest = aircraft[0] if aircraft else None
    return {
        "provider": provider,
        "license": license_note,
        "observational_only": True,
        "message": "ADS-B/MLAT observations only — absence of targets does not mean airspace is clear.",
        "radius_km": radius_km,
        "aircraft_count": len(aircraft),
        "nearest_distance_km": nearest["distance_km"] if nearest else None,
        "aircraft": aircraft,
        "source_message": payload.get("msg"),
        "source_now": payload.get("now"),
        "fetched_at_epoch": time.time(),
        "provider_errors": provider_errors,
    }


@airspace.get("/api/airspace/traffic")
def traffic():
    stored = weather_settings()
    lat = _number(request.args.get("lat"))
    lon = _number(request.args.get("lon"))
    site_name = (request.args.get("site_name") or stored.get("site_name") or "Operation Site").strip()[:120]
    if lat is None:
        lat = _number(stored.get("latitude"))
    if lon is None:
        lon = _number(stored.get("longitude"))
    cfg = {"site_name": site_name, "latitude": lat, "longitude": lon}
    if lat is None or lon is None:
        return jsonify(ok=False, status="NOT_CONFIGURED", message="Operation-site coordinates are required.", site=cfg), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify(ok=False, status="INVALID_LOCATION", message="Operation-site coordinates are outside the valid range.", site=cfg), 400
    try:
        radius_km = float(request.args.get("radius_km", "50"))
    except ValueError:
        return jsonify(error="radius_km must be numeric"), 400
    radius_km = max(5.0, min(250.0, radius_km))
    key = f"{lat:.5f}:{lon:.5f}:{radius_km:.1f}"
    cached = _traffic_cache.get(key)
    if cached and time.time() - cached[0] < TRAFFIC_CACHE_SECONDS:
        return jsonify(ok=True, status="OBSERVATIONAL", site=cfg, cached=True, traffic=cached[1])
    try:
        payload = _fetch_traffic(lat, lon, radius_km)
    except Exception as exc:
        if cached:
            return jsonify(ok=True, status="STALE_OBSERVATION", site=cfg, cached=True, message=f"Traffic refresh failed; showing cached observations: {exc}", traffic=cached[1])
        return jsonify(ok=False, status="UNAVAILABLE", site=cfg, message=f"Live ADS-B sources unavailable: {exc}"), 503
    _traffic_cache[key] = (time.time(), payload)
    return jsonify(ok=True, status="OBSERVATIONAL", site=cfg, cached=False, traffic=payload)


@airspace.get("/api/airspace/tile/<int:z>/<int:x>/<int:y>.png")
def osm_tile(z: int, x: int, y: int):
    if not (0 <= z <= 18):
        return jsonify(error="invalid zoom"), 400
    n = 2 ** z
    if not (0 <= x < n and 0 <= y < n):
        return jsonify(error="invalid tile"), 400
    key = f"{z}/{x}/{y}"
    cached = _tile_cache.get(key)
    if cached and time.time() - cached[0] < _TILE_CACHE_SECONDS:
        return Response(cached[1], mimetype=cached[2], headers={"Cache-Control": "public, max-age=3600"})
    url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    req = urllib.request.Request(url, headers={"User-Agent": "Stellar-Ops/2 (+OpenStreetMap tile proxy)"})
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            body = response.read()
            content_type = response.headers.get_content_type() or "image/png"
    except (OSError, urllib.error.URLError):
        return Response(status=502)
    _tile_cache[key] = (time.time(), body, content_type)
    return Response(body, mimetype=content_type, headers={"Cache-Control": "public, max-age=3600"})
