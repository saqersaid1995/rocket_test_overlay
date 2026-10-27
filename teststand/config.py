"""Load the test-stand configuration (config.yaml) with safe defaults.

Every section reads only its own block of the config, so a mistake in the
camera block can never change how ignition or telemetry behaves.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "web": {"host": "0.0.0.0", "port": 5050},
    "telemetry": {
        "listen_host": "0.0.0.0",
        "listen_port": 9100,          # ESP32 connects to this port (receiver_port on the ESP)
        "device_id": "PT-01",
        "pressure_channel": "pressure_mbar",
        "pressure_scale": 0.001,      # mbar -> bar
        "stale_after_s": 1.0,         # no data for this long = link LOST
        "history_s": 60,              # seconds kept in memory for the chart and pre-trigger
    },
    "pressure": {
        "unit": "bar",
        "max_expected_bar": 100,      # sensor range, used for the gauge
        "alarm_bar": 80,              # over-pressure warning on screen
    },
    "ignition": {
        "driver": "simulation",       # "simulation" or "esp32"
        "device_id": "PT-01",
        "countdown_s": 10,
        "pulse_ms": 1000,             # igniter on-time, firmware caps at 3000 ms
        "hold_to_start_s": 1.5,       # operator must hold START this long
        "arm_timeout_s": 120,         # armed but idle -> back to SAFE
        "keepalive_ms": 300,          # ARM keep-alive sent to the ESP
        "require_telemetry": True,    # cannot ARM without live pressure data
    },
    "recorder": {
        "directory": "data/sessions",
        "pre_trigger_s": 10,          # pressure kept from before START RECORDING
        "auto_record": True,          # countdown start -> start recording
        "auto_stop_after_fire_s": 30, # stop recording this long after the fire pulse
    },
    "cameras": [],
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> dict:
    data: dict = {}
    if path and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, data)
    ign = cfg["ignition"]
    ign["countdown_s"] = max(3, int(ign["countdown_s"]))
    ign["pulse_ms"] = max(100, min(int(ign["pulse_ms"]), 3000))
    if ign["driver"] not in ("simulation", "esp32"):
        raise ValueError(f"ignition.driver must be 'simulation' or 'esp32', got {ign['driver']!r}")
    for i, cam in enumerate(cfg["cameras"]):
        cam.setdefault("id", f"cam{i + 1}")
        cam.setdefault("name", cam["id"].upper())
        cam.setdefault("url", cam.get("preview_url") or cam.get("record_url"))
        if not cam.get("url"):
            raise ValueError(f"camera {cam['id']} needs a url (or preview_url / record_url)")
    return cfg
