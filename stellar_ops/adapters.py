from __future__ import annotations

import csv
import io
import socket
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
    import time
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = round((time.monotonic() - started) * 1000, 1)
            return ConnectionResult(True, "REACHABLE", f"TCP connection accepted by {host}:{port}", latency)
    except (OSError, ValueError) as exc:
        return ConnectionResult(False, "UNREACHABLE", f"Could not reach {host}:{port}: {exc}")


def test_adapter(adapter_type: str, endpoint: str) -> ConnectionResult:
    adapter = adapter_type.upper()
    endpoint = endpoint.strip()
    if adapter == "SIMULATOR":
        return ConnectionResult(True, "SIMULATED", "Simulator adapter is available; no physical device was contacted", 0.0)
    if adapter in {"MODBUS_TCP", "TCP", "OPC_UA", "SMTCS_EDGE_TCP"}:
        if ":" not in endpoint:
            return ConnectionResult(False, "INVALID_CONFIG", "Endpoint must be HOST:PORT")
        host, port = endpoint.rsplit(":", 1)
        return _tcp_probe(host, int(port))
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
