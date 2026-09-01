from __future__ import annotations

import threading
import time

from flask import Blueprint, jsonify

from .control import connect, event
from .edge_runtime import send_bench_led_pulse

bench_ignition = Blueprint("bench_ignition", __name__)
_lock = threading.RLock()
_state = {
    "active_until": 0.0,
    "last_pulse_at": None,
    "pulse_ms": 500,
    "last_result": None,
}


def _snapshot() -> dict:
    now = time.monotonic()
    with _lock:
        active = now < float(_state["active_until"])
        return {
            "mode": "BENCH_LED_ETHERNET_ONLY",
            "active": active,
            "pulse_ms": int(_state["pulse_ms"]),
            "last_pulse_at": _state["last_pulse_at"],
            "last_result": _state["last_result"],
            "physical_output": "BENCH_LED_ONLY",
        }


@bench_ignition.get("/api/bench/ignition")
def bench_ignition_status():
    return jsonify(_snapshot())


@bench_ignition.post("/api/bench/ignition/pulse")
def bench_ignition_pulse():
    pulse_ms = 500
    result = send_bench_led_pulse(device_id="PT-01", duration_ms=pulse_ms)

    if not result.get("ok"):
        with _lock:
            _state["last_result"] = result.get("error", "Ethernet bench LED command failed")
        return jsonify(ok=False, error=_state["last_result"], **_snapshot()), 503

    now_wall = time.time()
    with _lock:
        _state["pulse_ms"] = pulse_ms
        _state["active_until"] = time.monotonic() + pulse_ms / 1000.0
        _state["last_pulse_at"] = now_wall
        _state["last_result"] = "BENCH_LED_PULSE sent over SMTCS Ethernet session"

    with connect() as db:
        event(
            db,
            "BENCH_LED_TEST",
            "TEST_DIRECTOR",
            "INFO",
            "Bench LED pulse sent to PT-01 over the established Ethernet edge session",
        )

    return jsonify(ok=True, **_snapshot())
