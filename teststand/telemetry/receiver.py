"""Telemetry section: receives pressure data from the ESP32 over Ethernet.

The ESP32 is the TCP *client*; it connects to this receiver (port 9100 by
default), sends HELLO, then a BATCH every 100 ms (20 samples @ 200 Hz).
This module only receives data and keeps the latest values. It knows nothing
about ignition, cameras or the web page. Other sections can:

  * read ``snapshot()`` / ``history()``
  * register ``add_sample_listener(fn)`` to get every raw sample (the recorder)
  * send a frame to the ESP with ``send(frame)`` (the ignition driver)
"""
from __future__ import annotations

import os
import socket
import socketserver
import threading
import time
from collections import deque
from typing import Callable

from .. import events
from .protocol import ProtocolError, decode, encode

Sample = tuple[float, int, float]  # (wall time, device time µs, pressure bar)


class TelemetryHub:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.device_id = cfg["device_id"]
        self.channel = cfg["pressure_channel"]
        self.scale = float(cfg["pressure_scale"])
        self.stale_after = float(cfg["stale_after_s"])
        self._lock = threading.RLock()
        self._history: deque[Sample] = deque(maxlen=int(cfg["history_s"] * 250))
        self._listeners: list[Callable[[list[Sample]], None]] = []
        self._frame_listeners: list[Callable[[dict], None]] = []
        self._conn: socket.socket | None = None
        self._conn_lock = threading.Lock()
        self.tare_bar = 0.0
        self.stats = {
            "connected": False, "remote": None, "firmware": None, "boot_id": None,
            "last_rx": 0.0, "batches": 0, "samples": 0, "gaps": 0, "bad_frames": 0,
            "last_sequence": None, "rate_hz": 0.0, "voltage_v": None,
            "last_batch": 0.0, "last_attempt": None, "ignored_device": None,
        }
        self.port_error: str | None = None
        self.peak_bar = 0.0
        self._server: socketserver.ThreadingTCPServer | None = None

    # ---------- lifecycle ----------
    def start(self) -> None:
        """Open the receiver port. If another program holds it, keep retrying in the background
        and report it (the console keeps running)."""
        if not self._try_bind():
            threading.Thread(target=self._bind_retry_loop, name="telemetry-bind", daemon=True).start()

    def _try_bind(self) -> bool:
        hub = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):  # noqa: D401 - socketserver API
                hub._serve(self.request, self.rfile, f"{self.client_address[0]}:{self.client_address[1]}")

        class Server(socketserver.ThreadingTCPServer):
            # On Windows SO_REUSEADDR lets two programs share a port silently (the ESP32 may then
            # talk to the other one). Use an exclusive bind there instead.
            allow_reuse_address = os.name != "nt"
            daemon_threads = True

            def server_bind(self):
                if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        port = int(self.cfg["listen_port"])
        try:
            self._server = Server((self.cfg["listen_host"], port), Handler)
        except OSError as exc:
            if self.port_error is None:
                events.log("TELEMETRY", f"Cannot open port {port}: it is used by another program ({exc})", "ALARM")
            self.port_error = f"Port {port} is used by another program"
            return False
        if self.port_error:
            events.log("TELEMETRY", f"Port {port} is free again")
        self.port_error = None
        threading.Thread(target=self._server.serve_forever, name="telemetry-rx", daemon=True).start()
        events.log("TELEMETRY", f"Receiver listening on port {self.port}")
        return True

    def _bind_retry_loop(self) -> None:
        while self._server is None:
            time.sleep(5)
            if self._try_bind():
                return

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    # ---------- connection handling ----------
    def _serve(self, sock: socket.socket, rfile, remote: str) -> None:
        with self._lock:
            self.stats["last_attempt"] = {"t": time.time(), "from": remote}
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(5.0)  # a silent ESP is dropped after 5 s
        registered = False
        try:
            for line in rfile:
                try:
                    msg = decode(line)
                except ProtocolError as exc:
                    with self._lock:
                        self.stats["bad_frames"] += 1
                    self._reply(sock, {"type": "NACK", "device_id": "STAND", "reason": str(exc)})
                    continue
                if msg["device_id"] != self.device_id:
                    if self.stats["ignored_device"] != msg["device_id"] and msg["type"] == "HELLO":
                        events.log("TELEMETRY", f"Ignored device '{msg['device_id']}' from {remote} "
                                                f"(expected '{self.device_id}')", "WARN")
                    with self._lock:
                        self.stats["ignored_device"] = msg["device_id"]
                    continue
                if msg["type"] == "HELLO":
                    if self._conn is not None and self._conn is not sock and self.link_ok():
                        # another node with the same ID is already streaming: keep the live one
                        events.log("TELEMETRY", f"Rejected second {self.device_id} connection from {remote} "
                                                "(one is already live)", "WARN")
                        self._reply(sock, {"type": "NACK", "device_id": "STAND", "reason": "duplicate device"})
                        return
                    with self._conn_lock:
                        old, self._conn = self._conn, sock
                    if old is not None and old is not sock:
                        try:
                            old.close()
                        except OSError:
                            pass
                    registered = True
                    with self._lock:
                        self.stats.update(connected=True, remote=remote, firmware=msg.get("firmware"),
                                          boot_id=msg.get("boot_id"), last_sequence=None, last_rx=time.time())
                    events.log("TELEMETRY", f"{self.device_id} connected from {remote} ({msg.get('firmware')})")
                    self._reply(sock, {"type": "ACK", "device_id": "STAND", "ack_sequence": -1})
                elif not registered or self._conn is not sock:
                    continue  # only the active, registered connection may deliver data
                elif msg["type"] == "BATCH":
                    self._on_batch(msg)
                    self._reply(sock, {"type": "ACK", "device_id": "STAND", "ack_sequence": msg["sequence"]})
                else:
                    with self._lock:
                        self.stats["last_rx"] = time.time()
                for fn in list(self._frame_listeners):
                    try:
                        fn(msg)
                    except Exception as exc:  # noqa: BLE001
                        events.log("TELEMETRY", f"frame listener error: {exc}", "WARN")
        except (OSError, ValueError):
            pass
        finally:
            with self._conn_lock:
                was_active = self._conn is sock
                if was_active:
                    self._conn = None
            if registered and was_active:
                with self._lock:
                    self.stats["connected"] = False
                events.log("TELEMETRY", f"{self.device_id} disconnected", "WARN")

    def _reply(self, sock: socket.socket, frame: dict) -> None:
        try:
            with self._conn_lock:
                sock.sendall(encode(frame))
        except OSError:
            pass

    def _on_batch(self, msg: dict) -> None:
        now = time.time()
        values = msg["channels"].get(self.channel)
        if values is None:
            return
        period = msg["sample_period_us"]
        n = msg["sample_count"]
        volts = msg["channels"].get("voltage_uv")
        samples: list[Sample] = []
        with self._lock:
            tare = self.tare_bar
            for i, raw in enumerate(values):
                p = raw * self.scale - tare
                samples.append((now - (n - 1 - i) * period / 1e6, msg["first_sample_us"] + i * period, p))
            self._history.extend(samples)
            peak = max(s[2] for s in samples)
            if peak > self.peak_bar:
                self.peak_bar = peak
            last = self.stats["last_sequence"]
            if last is not None and msg["sequence"] > last + 1:
                self.stats["gaps"] += msg["sequence"] - last - 1
            self.stats.update(last_sequence=msg["sequence"], last_rx=now, last_batch=now,
                              batches=self.stats["batches"] + 1, samples=self.stats["samples"] + n,
                              rate_hz=round(1e6 / period, 1),
                              voltage_v=(volts[-1] / 1e6) if volts else None)
        for fn in list(self._listeners):
            try:
                fn(samples)
            except Exception as exc:  # noqa: BLE001
                events.log("TELEMETRY", f"sample listener error: {exc}", "WARN")

    # ---------- public API ----------
    def add_sample_listener(self, fn: Callable[[list[Sample]], None]) -> None:
        self._listeners.append(fn)

    def add_frame_listener(self, fn: Callable[[dict], None]) -> None:
        self._frame_listeners.append(fn)

    def send(self, frame: dict) -> bool:
        """Send a command frame to the connected ESP32. Returns False if not connected."""
        with self._conn_lock:
            sock = self._conn
            if sock is None:
                return False
            try:
                sock.sendall(encode(frame))
                return True
            except OSError:
                return False

    def link_ok(self) -> bool:
        """True only while pressure samples are actually arriving."""
        with self._lock:
            return self.stats["connected"] and (time.time() - self.stats["last_batch"]) < self.stale_after

    def current_bar(self) -> float | None:
        with self._lock:
            return self._history[-1][2] if self._history else None

    def snapshot(self) -> dict:
        with self._lock:
            age = time.time() - self.stats["last_rx"] if self.stats["last_rx"] else None
            state = "OFFLINE"
            batch_age = time.time() - self.stats["last_batch"] if self.stats["last_batch"] else None
            if self.stats["connected"]:
                if batch_age is not None and batch_age < self.stale_after:
                    state = "LIVE"
                elif age is not None and age < 3:
                    state = "NO DATA"   # ESP32 connected but sends no pressure samples
                else:
                    state = "STALE"
            return {
                "link": state, "age_s": None if age is None else round(age, 2),
                "pressure_bar": self._history[-1][2] if self._history and state == "LIVE" else None,
                "port": int(self.cfg["listen_port"]), "port_error": self.port_error,
                "last_attempt": self.stats["last_attempt"], "ignored_device": self.stats["ignored_device"],
                "boot_id": self.stats["boot_id"],
                "peak_bar": self.peak_bar, "tare_bar": self.tare_bar,
                **{k: self.stats[k] for k in ("remote", "firmware", "batches", "samples", "gaps",
                                              "bad_frames", "rate_hz", "voltage_v")},
            }

    def history(self, seconds: float = 30, points: int = 600) -> list[list[float]]:
        """Last ``seconds`` of pressure, reduced to about ``points`` points (keeps peaks)."""
        cutoff = time.time() - seconds
        with self._lock:
            data = [(t, p) for t, _, p in self._history if t >= cutoff]
        if len(data) <= points:
            return [[round(t, 3), round(p, 3)] for t, p in data]
        step = len(data) / points
        out = []
        for k in range(points):
            chunk = data[int(k * step):int((k + 1) * step)] or data[-1:]
            t, p = max(chunk, key=lambda s: s[1])
            out.append([round(t, 3), round(p, 3)])
        return out

    def samples_since(self, t0: float) -> list[Sample]:
        with self._lock:
            return [s for s in self._history if s[0] >= t0]

    def tare(self) -> float | None:
        """Zero the display at the current reading (software offset only)."""
        with self._lock:
            if not self._history:
                return None
            recent = [s[2] + self.tare_bar for s in list(self._history)[-100:]]
            self.tare_bar = sum(recent) / len(recent)
            self.peak_bar = 0.0
        events.log("TELEMETRY", f"Pressure zeroed (offset {self.tare_bar:.3f} bar)")
        return self.tare_bar

    def clear_tare(self) -> None:
        with self._lock:
            self.tare_bar = 0.0
        events.log("TELEMETRY", "Pressure zero offset cleared")

    def reset_peak(self) -> None:
        with self._lock:
            self.peak_bar = 0.0
