"""Start the test-stand console.

    python -m teststand.app --config config.yaml

Wiring between the sections happens here and only here:

    telemetry  → recorder (samples)        telemetry → ignition (link OK?)
    ignition   → recorder (countdown/fire/abort → start/stop recording)
    recorder   → cameras  (start/stop video files)
    web        → all of them (status + operator commands)
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from . import events, updater
from .cameras.camera import CameraBank
from .config import load_config
from .ignition.controller import IgnitionController
from .ignition.drivers import make_driver
from .recorder.session import Recorder
from .telemetry.receiver import TelemetryHub


class System:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.hub = TelemetryHub(cfg["telemetry"])
        self.cameras = CameraBank(cfg["cameras"])
        self.recorder = Recorder(cfg["recorder"], self.hub, self.cameras)
        driver = make_driver(cfg["ignition"], self.hub)
        self.ignition = IgnitionController(cfg["ignition"], driver, self.hub.link_ok)
        self.ignition.on("countdown", self.recorder.on_countdown)
        self.ignition.on("fire", self.recorder.on_fire)
        self.ignition.on("safe", self.recorder.on_safe)

    def start(self) -> None:
        self.hub.start()
        self.ignition.start()
        self.cameras.start()

    def stop(self) -> None:
        self.ignition.stop()
        self.recorder.stop("shutdown")
        self.cameras.stop()
        self.hub.stop()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _wait_for_ports(ports: list[int], timeout: float = 20) -> None:
    """After an update-restart, wait until the old process has released its ports."""
    end = time.time() + timeout
    while time.time() < end and not all(_port_free(p) for p in ports):
        time.sleep(0.25)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SFT STELLAR - test-stand console")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--port", type=int, help="web port (overrides config)")
    parser.add_argument("--log", help="write console output to this file (used by the desktop launcher)")
    args = parser.parse_args(argv)

    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        log_file = open(args.log, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log_file
    elif sys.stdout is None:  # started with pythonw and no log file
        sys.stdout = sys.stderr = open(os.devnull, "w")

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"[!] {cfg_path} not found — running with defaults (simulation ignition, no cameras).")
        print("    Copy config.example.yaml to config.yaml and edit it.")
    cfg = load_config(cfg_path)
    if args.port:
        cfg["web"]["port"] = args.port

    if os.environ.pop("SFT_WAIT_FOR_PORTS", None):
        _wait_for_ports([cfg["web"]["port"], cfg["telemetry"]["listen_port"]])

    system = System(cfg)
    system.start()

    from .web.server import create_app
    app = create_app(system)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def shutdown(*_):
        events.log("SYSTEM", "Shutting down")
        system.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    ign = cfg["ignition"]
    print(f"\n  SFT STELLAR {updater.version()} - Stellar Kinetics test-stand console")
    print(f"  Console:    http://localhost:{cfg['web']['port']}")
    print(f"  Telemetry:  waiting for ESP32 on TCP port {cfg['telemetry']['listen_port']}")
    print(f"  Ignition:   {ign['driver'].upper()} driver"
          + ("  (PHYSICAL OUTPUT)" if ign["driver"] == "esp32" else "  (no physical output)"))
    print(f"  Cameras:    {len(cfg['cameras'])}\n")
    events.log("SYSTEM", f"SFT STELLAR {updater.version()} started")
    app.run(host=cfg["web"]["host"], port=cfg["web"]["port"], threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
