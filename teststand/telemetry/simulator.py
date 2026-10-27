"""Fake ESP32 for testing the console without hardware.

    python -m teststand.telemetry.simulator --host 127.0.0.1 --port 9100

Behaves like the real pressure node + ignition firmware: sends HELLO and a
BATCH every 100 ms, answers IGN_ARM / IGN_FIRE / IGN_SAFE with IGN_STATUS,
uses the same 1.5 s dead-man timer, and simulates a motor burn after FIRE.
Use it with ``ignition.driver: esp32`` to exercise the full firing path.
"""
from __future__ import annotations

import argparse
import math
import random
import socket
import threading
import time

from .protocol import COMMAND_TYPES, ProtocolError, decode, encode

DEADMAN_S = 1.5


class FakeNode:
    def __init__(self, host: str, port: int, device_id: str = "PT-01", burn_s: float = 3.0, peak_bar: float = 45.0):
        self.host, self.port, self.device_id = host, port, device_id
        self.burn_s, self.peak_bar = burn_s, peak_bar
        self.boot_id = f"sim-{int(time.time())}"
        self.state = "SAFE"
        self.last_arm = 0.0
        self.fire_count = 0
        self.burn_start: float | None = None
        self.fire_end = 0.0
        self._stop = threading.Event()
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()

    def _send(self, msg: dict) -> None:
        with self.lock:
            if self.sock:
                self.sock.sendall(encode({"device_id": self.device_id, "boot_id": self.boot_id, **msg}))

    def _status(self) -> None:
        self._send({"type": "IGN_STATUS", "state": self.state, "fire_count": self.fire_count})

    def pressure(self, t: float) -> float:
        noise = random.gauss(0, 0.03)
        if self.burn_start is None:
            return max(0.0, 0.02 + noise)
        x = t - self.burn_start
        if x < 0 or x > self.burn_s + 0.6:
            return max(0.0, 0.02 + noise)
        if x < 0.15:
            p = self.peak_bar * (x / 0.15)
        elif x < self.burn_s:
            p = self.peak_bar * (0.85 + 0.15 * math.sin(math.pi * (x - 0.15) / (self.burn_s - 0.15)))
        else:
            p = self.peak_bar * 0.85 * math.exp(-(x - self.burn_s) * 8)
        return max(0.0, p + noise * 5)

    def _reader(self, f) -> None:
        for line in f:
            try:
                msg = decode(line, COMMAND_TYPES)
            except ProtocolError:
                continue
            kind = msg["type"]
            if msg.get("target") not in (None, self.device_id):
                continue
            if kind == "IGN_ARM":
                if self.state == "SAFE":
                    self.state = "ARMED"
                self.last_arm = time.time()
                self._status()
            elif kind == "IGN_FIRE":
                if self.state == "ARMED" and time.time() - self.last_arm < DEADMAN_S:
                    self.state = "FIRING"
                    self.fire_count += 1
                    self.fire_end = time.time() + min(int(msg.get("pulse_ms", 1000)), 3000) / 1000
                    self.burn_start = time.time() + 0.3
                    print("  >>> FIRE received — simulating burn")
                self._status()
            elif kind == "IGN_SAFE":
                self.state = "SAFE"
                self._status()

    def run_once(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(None)
        f = self.sock.makefile("rb")
        threading.Thread(target=self._reader, args=(f,), daemon=True).start()
        self._send({"type": "HELLO", "firmware": "sim-node/1", "channels": ["pressure_mbar", "voltage_uv"]})
        seq, period_us, n = 0, 5000, 20
        t_dev = 0
        next_t = time.time()
        while not self._stop.is_set():
            now = time.time()
            if self.state == "ARMED" and now - self.last_arm > DEADMAN_S:
                self.state = "SAFE"
                print("  dead-man: ARM keep-alive stopped → SAFE")
                self._status()
            if self.state == "FIRING" and now >= self.fire_end:
                self.state = "SAFE"
                self._status()
            p = [self.pressure(now - (n - 1 - i) * period_us / 1e6) for i in range(n)]
            self._send({"type": "BATCH", "sequence": seq, "first_sample_us": t_dev,
                        "sample_period_us": period_us, "sample_count": n,
                        "channels": {"pressure_mbar": [round(x * 1000) for x in p],
                                     "voltage_uv": [round((x / 14.63 + 0.03) * 1e6) for x in p]}})
            if seq % 5 == 0:
                self._status()   # the real firmware reports every 500 ms
            seq += 1
            t_dev += n * period_us
            next_t += n * period_us / 1e6
            time.sleep(max(0.0, next_t - time.time()))

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                print(f"Connecting to {self.host}:{self.port} ...")
                self.run_once()
            except OSError as exc:
                print(f"  link error: {exc}; retrying")
                self.state = "SAFE"
                time.sleep(1)

    def stop(self) -> None:
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake ESP32 pressure + ignition node")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--peak", type=float, default=45.0, help="simulated peak pressure, bar")
    ap.add_argument("--burn", type=float, default=3.0, help="simulated burn time, s")
    a = ap.parse_args()
    FakeNode(a.host, a.port, burn_s=a.burn, peak_bar=a.peak).run()


if __name__ == "__main__":
    main()
