from __future__ import annotations

import threading
import time

from flask import Blueprint, jsonify

from .control import connect, event
from .edge_runtime import send_bench_led_state

bench_ignition = Blueprint("bench_ignition", __name__)
_lock = threading.RLock()
_state = {
    "active": False,
    "last_changed_at": None,
    "last_result": None,
}


def _snapshot() -> dict:
    with _lock:
        return {
            "mode": "BENCH_LED_ETHERNET_LATCHED",
            "active": bool(_state["active"]),
            "last_changed_at": _state["last_changed_at"],
            "last_result": _state["last_result"],
            "physical_output": "BENCH_LED_ONLY",
        }


def _set_state(on: bool):
    result = send_bench_led_state(device_id="PT-01", on=on)

    if not result.get("ok"):
        with _lock:
            _state["last_result"] = result.get("error", "Ethernet bench LED command failed")
        return jsonify(ok=False, error=_state["last_result"], **_snapshot()), 503

    with _lock:
        _state["active"] = on
        _state["last_changed_at"] = time.time()
        _state["last_result"] = f"BENCH_LED_SET {'ON' if on else 'OFF'} sent over SMTCS Ethernet session"

    with connect() as db:
        event(
            db,
            "BENCH_LED_ON" if on else "BENCH_LED_OFF",
            "TEST_DIRECTOR",
            "INFO",
            f"Bench LED {'enabled' if on else 'disabled'} on PT-01 over the established Ethernet edge session",
        )

    return jsonify(ok=True, **_snapshot())


@bench_ignition.get("/api/bench/ignition")
def bench_ignition_status():
    return jsonify(_snapshot())


@bench_ignition.post("/api/bench/ignition/on")
def bench_ignition_on():
    return _set_state(True)


@bench_ignition.post("/api/bench/ignition/off")
def bench_ignition_off():
    return _set_state(False)
