from __future__ import annotations

import os
import threading
from pathlib import Path

from .edge_gateway import Gateway


_GATEWAY_LOCK = threading.RLock()
_GATEWAY: Gateway | None = None
_GATEWAY_THREAD: threading.Thread | None = None


def ensure_edge_gateway(db_path: Path, host: str | None = None, port: int | None = None) -> dict:
    """Start the SMTCS Ethernet telemetry listener once per process.

    Field devices push telemetry to Stellar Ops over a persistent TCP
    connection. That same established connection may carry explicitly
    bench-only LED commands used by the commissioning UI.
    """
    global _GATEWAY, _GATEWAY_THREAD

    listen_host = host or os.environ.get("STELLAR_OPS_EDGE_HOST", "0.0.0.0")
    listen_port = int(port or os.environ.get("STELLAR_OPS_EDGE_PORT", "9100"))

    with _GATEWAY_LOCK:
        if _GATEWAY is not None and _GATEWAY_THREAD is not None and _GATEWAY_THREAD.is_alive():
            return {"host": listen_host, "port": listen_port, "status": "LISTENING"}

        server = Gateway((listen_host, listen_port), db_path)
        thread = threading.Thread(
            target=server.serve_forever,
            name="stellar-ops-edge-gateway",
            daemon=True,
        )
        thread.start()
        _GATEWAY = server
        _GATEWAY_THREAD = thread
        return {"host": listen_host, "port": listen_port, "status": "LISTENING"}


def send_bench_led_state(device_id: str = "PT-01", on: bool = False) -> dict:
    """Set the bench-only LED/relay state over the established Ethernet session."""
    with _GATEWAY_LOCK:
        gateway = _GATEWAY
    if gateway is None:
        return {"ok": False, "error": "Ethernet edge gateway is not running"}
    return gateway.send_bench_led_state(device_id=device_id, on=on)


def send_bench_led_pulse(device_id: str = "PT-01", duration_ms: int = 500) -> dict:
    """Send a BENCH_LED_PULSE over the already-connected Ethernet session."""
    with _GATEWAY_LOCK:
        gateway = _GATEWAY
    if gateway is None:
        return {"ok": False, "error": "Ethernet edge gateway is not running"}
    return gateway.send_bench_led_pulse(device_id=device_id, duration_ms=duration_ms)


def stop_edge_gateway() -> None:
    global _GATEWAY, _GATEWAY_THREAD
    with _GATEWAY_LOCK:
        if _GATEWAY is not None:
            _GATEWAY.shutdown()
            _GATEWAY.server_close()
        if _GATEWAY_THREAD is not None:
            _GATEWAY_THREAD.join(timeout=2.0)
        _GATEWAY = None
        _GATEWAY_THREAD = None
