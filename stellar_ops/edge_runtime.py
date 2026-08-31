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

    The gateway is deliberately inbound-only: field devices push telemetry to
    Stellar Ops over a persistent TCP connection. Operator/engineering control
    remains on the device's local commissioning interface.
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
