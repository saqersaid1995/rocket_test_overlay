"""Ignition output drivers.

A driver only moves the command to the hardware. All safety decisions
(arming, countdown, timeouts, aborts) live in controller.py.

  simulation  no physical output at all; for training and screen tests
  esp32       sends IGN_ARM / IGN_FIRE / IGN_SAFE frames to the ESP32 over the
              same Ethernet link as the pressure data. The ESP32 has its own
              dead-man timer: if ARM keep-alives stop, it returns to SAFE.
"""
from __future__ import annotations

import threading
import time


class SimulationDriver:
    name = "simulation"
    physical = False

    def __init__(self):
        self._armed = False

    def arm(self) -> bool:
        self._armed = True
        return True

    def keepalive(self) -> bool:
        return self._armed

    def fire(self, pulse_ms: int) -> bool:
        return self._armed

    def safe(self) -> None:
        self._armed = False

    def hardware_state(self) -> dict:
        return {"state": "ARMED" if self._armed else "SAFE", "age_s": 0.0, "confirmed": True}


class Esp32Driver:
    name = "esp32"
    physical = True

    def __init__(self, hub, target: str):
        self.hub = hub
        self.target = target
        self._seq = 0
        self._lock = threading.Lock()
        self._status = {"state": "UNKNOWN", "t": 0.0, "fire_count": None}
        hub.add_frame_listener(self._on_frame)

    def _on_frame(self, msg: dict) -> None:
        if msg.get("type") == "IGN_STATUS" and msg.get("device_id") == self.target:
            with self._lock:
                self._status = {"state": msg.get("state", "UNKNOWN"), "t": time.time(),
                                "fire_count": msg.get("fire_count")}

    def _send(self, kind: str, **extra) -> bool:
        with self._lock:
            self._seq += 1
            seq = self._seq
        return self.hub.send({"type": kind, "device_id": "STAND", "target": self.target, "seq": seq, **extra})

    def arm(self) -> bool:
        return self._send("IGN_ARM")

    def keepalive(self) -> bool:
        return self._send("IGN_ARM")

    def fire(self, pulse_ms: int) -> bool:
        return self._send("IGN_FIRE", pulse_ms=int(pulse_ms))

    def safe(self) -> None:
        # sent twice: a lost SAFE is the one frame we never want to lose
        self._send("IGN_SAFE")
        self._send("IGN_SAFE")

    def hardware_state(self) -> dict:
        with self._lock:
            st = dict(self._status)
        age = time.time() - st["t"] if st["t"] else None
        return {"state": st["state"], "age_s": None if age is None else round(age, 2),
                "confirmed": age is not None and age < 1.5, "fire_count": st["fire_count"]}


def make_driver(cfg: dict, hub):
    if cfg["driver"] == "esp32":
        return Esp32Driver(hub, cfg["device_id"])
    return SimulationDriver()
