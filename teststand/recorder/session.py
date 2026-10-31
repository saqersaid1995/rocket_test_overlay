"""Recorder section: saves each test to its own folder.

    data/sessions/20261028_141500/
        pressure.csv      every 200 Hz sample (incl. pre-trigger); t_rel_s = seconds from
                          recording start, fire is at summary.fire_t_rel_s
        events.log        everything the operator and the system did
        cam1.mkv ...      camera recordings (full quality, no re-encoding)
        summary.json      peak pressure, fire time, burn time estimate

CSV writing happens on its own thread, so a slow disk never slows down the
telemetry receiver or the ignition sequence.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from .. import events


class Recorder:
    def __init__(self, cfg: dict, hub, cameras, thrust=None):
        self.cfg = cfg
        self.hub = hub
        self.cameras = cameras
        self.thrust = thrust          # ThrustModel or None
        self.impulse = 0.0            # N·s while recording (thrust estimate)
        self.peak_thrust = 0.0
        self.base = Path(cfg["directory"])
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue()
        self.session: Path | None = None
        self.started_at = 0.0
        self.fire_at: float | None = None
        self.peak = (0.0, None)  # (bar, wall time)
        self.samples = 0
        self._writer: threading.Thread | None = None
        self._stop_timer: threading.Timer | None = None
        self.last_session: dict | None = None
        hub.add_sample_listener(self._on_samples)
        events.subscribe(self._on_event)

    @property
    def active(self) -> bool:
        return self.session is not None

    # ---------- start / stop ----------
    def start(self, reason: str = "manual") -> Path:
        with self._lock:
            self._cancel_timer()
            if self.session:
                return self.session
            name = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session = self.base / name
            self.session.mkdir(parents=True, exist_ok=True)
            self.started_at = time.time()
            self.fire_at = None
            self.peak = (0.0, None)
            self.impulse, self.peak_thrust = 0.0, 0.0
            self.samples = 0
            pre = self.hub.samples_since(self.started_at - self.cfg["pre_trigger_s"])
            self._queue = queue.Queue()
            self._writer = threading.Thread(target=self._write_loop, args=(self.session, self._queue),
                                            name="recorder", daemon=True)
            self._writer.start()
            for ev in events.recent(limit=500):   # what led up to this recording (arming, etc.)
                if ev["t"] >= self.started_at - 120:
                    self._queue.put(ev)
            if pre:
                self._queue.put(pre)
        self.cameras.start_recording(self.session)
        events.log("RECORDER", f"Recording started ({reason}) → {self.session}")
        return self.session

    def stop(self, reason: str = "manual") -> dict | None:
        with self._lock:
            self._cancel_timer()
            if not self.session:
                return None
            session, q, writer = self.session, self._queue, self._writer
            self.session = None
        self.cameras.stop_recording()
        q.put(None)
        if writer:
            writer.join(timeout=10)
        summary = self._summary(session, reason)
        (session / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self.last_session = summary
        events.log("RECORDER", f"Recording saved ({reason}): {session.name}")
        return summary

    def stop_after(self, seconds: float, reason: str) -> None:
        with self._lock:
            self._cancel_timer()
            if not self.session:
                return
            self._stop_timer = threading.Timer(seconds, self.stop, args=(reason,))
            self._stop_timer.daemon = True
            self._stop_timer.start()
        events.log("RECORDER", f"Recording will stop in {seconds:.0f} s")

    def _cancel_timer(self) -> None:
        if self._stop_timer:
            self._stop_timer.cancel()
            self._stop_timer = None

    # ---------- ignition hooks ----------
    def on_countdown(self, t_zero: float) -> None:
        if self.cfg["auto_record"]:
            self.start("countdown")

    def on_fire(self, t: float) -> None:
        self.fire_at = t
        if self.cfg["auto_record"]:
            self.start("fire")  # no-op if already running
            self.stop_after(self.cfg["auto_stop_after_fire_s"], "auto stop after fire")

    def on_safe(self, reason: str) -> None:
        if self.cfg["auto_record"] and self.active and self.fire_at is None:
            self.stop_after(5, f"abort: {reason}")

    # ---------- data path ----------
    def _on_samples(self, samples) -> None:
        if self.session is not None:
            self._queue.put(samples)

    def _on_event(self, ev: dict) -> None:
        session = self.session
        if session is not None:
            self._queue.put(ev)

    def _write_loop(self, session: Path, q: queue.Queue) -> None:
        csv = open(session / "pressure.csv", "w", encoding="utf-8", buffering=1 << 16)
        log = open(session / "events.log", "a", encoding="utf-8")
        th = self.thrust if self.thrust is not None and self.thrust.enabled else None
        csv.write("local_time,t_rel_s,device_us,pressure_bar" + (",thrust_n_estimated" if th else "") + "\n")
        t0 = self.started_at
        last_t, last_f = None, 0.0
        last_flush = time.monotonic()
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                if isinstance(item, dict):
                    stamp = datetime.fromtimestamp(item["t"]).isoformat(timespec="milliseconds")
                    log.write(f"{stamp} [{item['level']}] {item['source']}: {item['message']}\n")
                    log.flush()
                    continue
                for t, dev_us, p in item:
                    f = th.force(p) if th else None
                    csv.write(f"{datetime.fromtimestamp(t).isoformat(timespec='milliseconds')},"
                              f"{t - t0:.4f},{dev_us},{p:.4f}" + (f",{f:.2f}" if th else "") + "\n")
                    if th:
                        if last_t is not None and 0 < t - last_t < 1:
                            self.impulse += (f + last_f) / 2 * (t - last_t)
                        last_t, last_f = t, f
                        self.peak_thrust = max(self.peak_thrust, f)
                    self.samples += 1
                    if p > self.peak[0]:
                        self.peak = (p, t)
                if time.monotonic() - last_flush > 1:
                    csv.flush()
                    last_flush = time.monotonic()
        finally:
            csv.close()
            log.close()

    def _summary(self, session: Path, reason: str) -> dict:
        peak_bar, peak_t = self.peak
        burn = None
        if peak_bar > 1 and self.fire_at:
            above = [s for s in self.hub.samples_since(self.fire_at - 1) if s[2] >= 0.1 * peak_bar]
            if above:
                burn = round(above[-1][0] - above[0][0], 3)
        return {
            "session": session.name,
            "folder": str(session.resolve()),
            "started": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
            "stopped": datetime.now().isoformat(timespec="seconds"),
            "stop_reason": reason,
            "fired": self.fire_at is not None,
            "fire_time": datetime.fromtimestamp(self.fire_at).isoformat(timespec="milliseconds")
            if self.fire_at else None,
            "fire_t_rel_s": round(self.fire_at - self.started_at, 4) if self.fire_at else None,
            "samples": self.samples,
            "peak_pressure_bar": round(peak_bar, 3),
            "time_to_peak_s": round(peak_t - self.fire_at, 3) if peak_t and self.fire_at else None,
            "burn_time_s_estimate": burn,
            "note": "burn time = time pressure stayed above 10% of peak (estimate)",
            "cameras": [c["id"] for c in self.cameras.snapshot() if c.get("status") != "DISABLED"],
            **self._thrust_summary(),
        }

    def _thrust_summary(self) -> dict:
        th = self.thrust
        if th is None or not th.enabled:
            return {}
        from ..thrust import motor_class
        imp = round(self.impulse, 2)
        isp = th.isp(imp)
        return {"thrust_estimated": True, "peak_thrust_n": round(self.peak_thrust, 1), "total_impulse_ns": imp,
                "motor_class": motor_class(imp), "isp_s": round(isp, 1) if isp else None,
                "thrust_note": "thrust estimated from chamber pressure: F = eff x Cf x Pc x At"}

    def snapshot(self) -> dict:
        return {"active": self.active, "session": self.session.name if self.session else None,
                "elapsed_s": round(time.time() - self.started_at) if self.active else None,
                "samples": self.samples, "auto_stop_pending": self._stop_timer is not None,
                "last": self.last_session}
