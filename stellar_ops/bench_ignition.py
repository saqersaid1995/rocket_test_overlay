from __future__ import annotations

import threading
import time

from flask import Blueprint, jsonify

from .control import OPERATION_ID, connect, event

bench_ignition = Blueprint("bench_ignition", __name__)
_lock = threading.RLock()
_state = {
    "active_until": 0.0,
    "last_pulse_at": None,
    "pulse_ms": 500,
}


def _snapshot() -> dict:
    now = time.monotonic()
    with _lock:
        active = now < float(_state["active_until"])
        return {
            "mode": "BENCH_SIMULATION_ONLY",
            "active": active,
            "pulse_ms": int(_state["pulse_ms"]),
            "last_pulse_at": _state["last_pulse_at"],
            "physical_output": False,
        }


@bench_ignition.get("/api/bench/ignition")
def bench_ignition_status():
    return jsonify(_snapshot())


@bench_ignition.post("/api/bench/ignition/pulse")
def bench_ignition_pulse():
    pulse_ms = 500
    now_wall = time.time()
    with _lock:
        _state["pulse_ms"] = pulse_ms
        _state["active_until"] = time.monotonic() + pulse_ms / 1000.0
        _state["last_pulse_at"] = now_wall

    with connect() as db:
        event(
            db,
            "BENCH_IGNITION_TEST",
            "TEST_DIRECTOR",
            "INFO",
            "Bench ignition simulation pulse requested; no physical output is driven",
        )

    return jsonify(ok=True, **_snapshot())
