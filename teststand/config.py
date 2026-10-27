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
        "esp_ip": "192.168.1.50",     # ESP32 Ethernet address (used by System check)
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
        "relay_test_ms": 300,         # relay test click length (max 1000)
    },
    "recorder": {
        "directory": "data/sessions",
        "pre_trigger_s": 10,          # pressure kept from before START RECORDING
        "auto_record": True,          # countdown start -> start recording
        "auto_stop_after_fire_s": 30, # stop recording this long after the fire pulse
    },
    "firmware": {
        "fqbn": "esp32:esp32:esp32",  # board for arduino-cli (ESP32 Dev Module)
        "sketch": "esp32_pressure_ota",
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


def load_user_config(path: str | Path | None) -> dict:
    """The settings exactly as written in config.yaml (no defaults added)."""
    if path and Path(path).exists():
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {}


def validate(data: dict) -> dict:
    """Merge with defaults, check and complete the settings. Raises ValueError."""
    from .cameras.discovery import PRESETS, build_url

    cfg = _merge(DEFAULTS, data or {})
    cfg["cameras"] = [dict(c) for c in (data or {}).get("cameras") or []]
    ign = cfg["ignition"]
    ign["countdown_s"] = max(3, int(ign["countdown_s"]))
    ign["pulse_ms"] = max(100, min(int(ign["pulse_ms"]), 3000))
    ign["relay_test_ms"] = max(50, min(int(ign.get("relay_test_ms", 300)), 1000))
    if ign["driver"] not in ("simulation", "esp32"):
        raise ValueError(f"ignition.driver must be 'simulation' or 'esp32', got {ign['driver']!r}")
    port = int(cfg["telemetry"]["listen_port"])
    if not 0 <= port <= 65535:   # 0 = any free port (tests)
        raise ValueError("telemetry.listen_port must be 1-65535")
    ids = set()
    for i, cam in enumerate(cfg["cameras"]):
        cam.setdefault("id", f"cam{i + 1}")
        if cam["id"] in ids:
            cam["id"] = f"cam{i + 1}_{len(ids)}"
        ids.add(cam["id"])
        cam.setdefault("name", cam["id"].upper())
        if cam.get("ip"):   # simple form from the Settings page
            main, sub = PRESETS.get(cam.get("brand") or "generic", PRESETS["generic"])
            args = (cam["ip"], cam.get("user", ""), cam.get("password", ""), int(cam.get("port") or 554))
            cam["record_url"] = build_url(*args, cam.get("record_path") or main)
            cam["preview_url"] = build_url(*args, cam.get("preview_path") or sub)
        cam.setdefault("url", cam.get("preview_url") or cam.get("record_url"))
        if not cam.get("url"):
            raise ValueError(f"camera {cam['name']} needs an IP address or a URL")
    return cfg


def load_config(path: str | Path | None = None) -> dict:
    return validate(load_user_config(path))


def save_user_config(path: str | Path, data: dict) -> None:
    """Write config.yaml (keeps one backup as config.yaml.bak)."""
    validate(data)  # never write settings that would not load
    path = Path(path)
    if path.exists():
        path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
    text = ("# SFT STELLAR settings — edited from the Settings page.\n"
            "# See config.example.yaml for every option with comments.\n\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
