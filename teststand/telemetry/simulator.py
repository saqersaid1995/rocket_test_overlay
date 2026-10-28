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
        self.test_relays: list[str] = []
        self.ota_images: list[bytes] = []
        self.extra_i2c: list[int] = []          # tests: pretend a sensor is plugged in
        self.pin_states: dict[int, str] = {}
        self._stop = threading.Event()
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()

    def _send(self, msg: dict) -> None:
        with self.lock:
            if self.sock:
                self.sock.sendall(encode({"device_id": self.device_id, "boot_id": self.boot_id, **msg}))

    def _status(self) -> None:
        self._send({"type": "IGN_STATUS", "state": self.state, "fire_count": self.fire_count,
                    "ads_ready": True, "sd_ready": True})

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
            if kind == "OTA_BEGIN":
                self._ota(f, msg)
                return
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
            elif kind == "IGN_TEST":
                if self.state == "SAFE" and msg.get("relay") in ("arm", "fire"):
                    self.state = "TEST"
                    self.test_relays.append(msg["relay"])
                    self.fire_end = time.time() + min(int(msg.get("pulse_ms", 300)), 1000) / 1000
                    print(f"  relay test: {msg['relay']}")
                self._status()
            elif kind == "HW_SCAN":
                self._send({"type": "HW_REPORT", "i2c": [0x48] + self.extra_i2c, "i2c_used": [0x48],
                            "ads_ready": True, "sd_ready": True,
                            "used": [{"pin": 21, "use": "I2C SDA"}, {"pin": 22, "use": "I2C SCL"}],
                            "pins": [{"pin": p, "state": self.pin_states.get(p, "open")} for p in (2, 4, 12, 13, 14, 15, 16, 17)]
                                    + [{"pin": p, "state": "analog", "mv": 0} for p in (34, 35, 36, 39)]})
            elif kind == "IGN_SAFE":
                self.state = "SAFE"
                self._status()

    def _ota(self, f, msg) -> None:
        """Receive a firmware image like the real ESP32, then 'restart'."""
        import hashlib
        size = int(msg["size"])
        self._send({"type": "OTA_READY"})
        got = b""
        while len(got) < size:
            chunk = f.read(min(65536, size - len(got)))
            if not chunk:
                return
            got += chunk
            self._send({"type": "OTA_PROGRESS", "received": len(got)})
        if hashlib.md5(got).hexdigest() != msg.get("md5"):
            self._send({"type": "OTA_FAIL", "message": "MD5 check failed"})
            return
        self.ota_images.append(got)
        self._send({"type": "OTA_DONE", "received": len(got)})
        print(f"  OTA image received ({size} bytes) — restarting")
        self.boot_id = f"sim-{time.time():.3f}"
        try:
            self.sock.shutdown(socket.SHUT_RDWR)   # run() reconnects with a new boot_id, like a reboot
            self.sock.close()
        except OSError:
            pass

    def run_once(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(None)
        f = self.sock.makefile("rb")
        threading.Thread(target=self._reader, args=(f,), daemon=True).start()
        self._send({"type": "HELLO", "firmware": "sim-node/1+ota+hw", "channels": ["pressure_mbar", "voltage_uv"]})
        seq, period_us, n = 0, 5000, 20
        t_dev = 0
        next_t = time.time()
        while not self._stop.is_set():
            now = time.time()
            if self.state == "ARMED" and now - self.last_arm > DEADMAN_S:
                self.state = "SAFE"
                print("  dead-man: ARM keep-alive stopped → SAFE")
                self._status()
            if self.state in ("FIRING", "TEST") and now >= self.fire_end:
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
