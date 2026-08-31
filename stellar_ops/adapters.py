from __future__ import annotations

import csv
import io
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class ConnectionResult:
    ok: bool
    status: str
    message: str
    latency_ms: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _tcp_probe(host: str, port: int, timeout: float = 1.5) -> ConnectionResult:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = round((time.monotonic() - started) * 1000, 1)
            return ConnectionResult(True, "REACHABLE", f"TCP connection accepted by {host}:{port}", latency)
    except (OSError, ValueError) as exc:
        return ConnectionResult(False, "UNREACHABLE", f"Could not reach {host}:{port}: {exc}")


def _ntp_probe(host: str, timeout: float = 1.5) -> ConnectionResult:
    if host.upper() == "LOCAL":
        return ConnectionResult(True, "LOCAL_CLOCK", "Using the host UTC clock; external NTP is not configured", 0.0)
    started = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(timeout)
            client.sendto(b"\x1b" + 47 * b"\0", (host, 123))
            payload, _ = client.recvfrom(512)
        if len(payload) < 48:
            return ConnectionResult(False, "INVALID_RESPONSE", "NTP response was shorter than 48 bytes")
        return ConnectionResult(True, "REACHABLE", f"NTP response received from {host}", round((time.monotonic()-started)*1000,1))
    except OSError as exc:
        return ConnectionResult(False, "UNREACHABLE", f"Could not query NTP host {host}: {exc}")


def _http_json_probe(endpoint: str, timeout: float = 1.5) -> ConnectionResult:
    value = endpoint.strip()
    if not value:
        return ConnectionResult(False, "INVALID_CONFIG", "HTTP telemetry endpoint is required")
    if "://" not in value:
        value = f"http://{value}"
    if not value.rstrip("/").endswith("/reading"):
        value = value.rstrip("/") + "/reading"
    started = time.monotonic()
    try:
        request = urllib.request.Request(value, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return ConnectionResult(False, "INVALID_RESPONSE", f"HTTP telemetry returned status {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return ConnectionResult(False, "INVALID_RESPONSE", "HTTP telemetry response is not a JSON object")
        missing = [key for key in ("pressure", "voltage", "time") if key not in payload]
        if missing:
            return ConnectionResult(False, "INVALID_RESPONSE", f"HTTP telemetry is missing fields: {', '.join(missing)}")
        latency = round((time.monotonic() - started) * 1000, 1)
        return ConnectionResult(True, "STREAMING", f"Valid pressure telemetry received from {value}", latency)
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return ConnectionResult(False, "UNREACHABLE", f"Could not read HTTP telemetry from {value}: {exc}")


def test_adapter(adapter_type: str, endpoint: str) -> ConnectionResult:
    adapter = adapter_type.upper()
    endpoint = endpoint.strip()
    if adapter == "SIMULATOR":
        return ConnectionResult(True, "SIMULATED", "Simulator adapter is available; no physical device was contacted", 0.0)
    if adapter in {"HTTP_JSON", "ESP32_HTTP"}:
        return _http_json_probe(endpoint)
    if adapter == "SMTCS_EDGE_TCP" and endpoint.lower().startswith(("http://", "https://")):
        return _http_json_probe(endpoint)
    if adapter in {"MODBUS_TCP", "TCP", "OPC_UA", "SMTCS_EDGE_TCP"}:
        if ":" not in endpoint:
            return ConnectionResult(False, "INVALID_CONFIG", "Endpoint must be HOST:PORT")
        host, port = endpoint.rsplit(":", 1)
        try:
            port_number = int(port)
        except ValueError:
            return ConnectionResult(False, "INVALID_CONFIG", "Endpoint port must be numeric")
        if not host or not 1 <= port_number <= 65535:
            return ConnectionResult(False, "INVALID_CONFIG", "Endpoint must contain a valid host and TCP port")
        return _tcp_probe(host, port_number)
    if adapter in {"ONVIF", "RTSP"}:
        parsed = urlparse(endpoint if "://" in endpoint else f"rtsp://{endpoint}")
        if not parsed.hostname:
            return ConnectionResult(False, "INVALID_CONFIG", "A valid camera hostname or RTSP URL is required")
        return _tcp_probe(parsed.hostname, parsed.port or (80 if adapter == "ONVIF" else 554))
    if adapter in {"SERIAL", "MODBUS_RTU", "CAN"}:
        if not endpoint:
            return ConnectionResult(False, "INVALID_CONFIG", "A device path or interface name is required")
        return ConnectionResult(False, "DRIVER_REQUIRED", f"{adapter} configuration saved; host driver verification is required")
    if adapter == "CSV_REPLAY":
        return ConnectionResult(True, "READY", "CSV replay adapter is ready for an uploaded dataset")
    if adapter == "NTP":
        return _ntp_probe(endpoint or "LOCAL")
    return ConnectionResult(False, "UNSUPPORTED", f"Adapter {adapter_type} is not registered")


def inspect_csv(content: bytes) -> dict:
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("Replay file exceeds the 20 MB Phase 1 limit")
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file has no header row")
    rows = []
    preview = []
    for index, row in enumerate(reader):
        rows.append(row)
        if index < 5:
            preview.append(row)
        if index >= 100_000:
            raise ValueError("Replay file exceeds 100,000 rows")
    return {"columns": reader.fieldnames, "row_count": index + 1 if 'index' in locals() else 0,
            "preview": preview, "rows": rows}
