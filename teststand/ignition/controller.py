"""Ignition section: the firing sequence and its safety rules.

States
  SAFE       default. Output off. Nothing can fire.
  ARMED      operator armed the system. Keep-alives go to the ESP32.
  COUNTDOWN  T-minus running. ABORT at any time returns to SAFE.
  FIRING     igniter pulse on (pulse_ms). Afterwards -> SAFE automatically.

Automatic aborts (system returns to SAFE on its own)
  * telemetry link lost while ARMED or in COUNTDOWN (if require_telemetry)
  * ESP32 does not confirm ARMED before T-0 (esp32 driver)
  * ARMED with no action for arm_timeout_s
  * ESP32 dead-man: keep-alives stop -> the ESP returns to SAFE by itself
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .. import events

SAFE, ARMED, COUNTDOWN, FIRING = "SAFE", "ARMED", "COUNTDOWN", "FIRING"


class IgnitionError(RuntimeError):
    pass


class IgnitionController:
    def __init__(self, cfg: dict, driver, link_ok: Callable[[], bool]):
        self.cfg = cfg
        self.driver = driver
        self.link_ok = link_ok
        self._lock = threading.RLock()
        self.state = SAFE
        self.armed_at = 0.0
        self.t_zero = 0.0           # wall time of planned ignition
        self.fire_end = 0.0
        self.last_fire: float | None = None
        self.last_abort_reason: str | None = None
        self.last_abort_t: float | None = None
        self._last_keepalive = 0.0
        self._hooks: dict[str, list[Callable[..., None]]] = {"countdown": [], "fire": [], "safe": []}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ignition", daemon=True)

    # ---------- lifecycle ----------
    def start(self) -> None:
        self.driver.safe()
        self._thread.start()
        events.log("IGNITION", f"Ignition ready ({self.driver.name} driver, "
                               f"{'PHYSICAL OUTPUT' if self.driver.physical else 'no physical output'})")

    def stop(self) -> None:
        self.abort("system shutdown")
        self._stop.set()

    def on(self, hook: str, fn: Callable[..., None]) -> None:
        self._hooks[hook].append(fn)

    def _emit(self, hook: str, *args) -> None:
        for fn in self._hooks[hook]:
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001 - a hook must never break the sequence
                events.log("IGNITION", f"{hook} hook error: {exc}", "WARN")

    # ---------- operator commands ----------
    def arm(self) -> None:
        with self._lock:
            if self.state != SAFE:
                raise IgnitionError(f"Cannot ARM from {self.state}")
            if self.cfg["require_telemetry"] and not self.link_ok():
                raise IgnitionError("Cannot ARM: no live telemetry from the test stand")
            if not self.driver.arm():
                raise IgnitionError("Cannot ARM: ignition controller not reachable")
            self.state = ARMED
            self.armed_at = time.time()
            self._last_keepalive = time.monotonic()
            self.last_abort_reason = None
        events.log("IGNITION", "SYSTEM ARMED", "WARN")

    def start_countdown(self) -> None:
        with self._lock:
            if self.state != ARMED:
                raise IgnitionError("Countdown needs the system ARMED first")
            self.state = COUNTDOWN
            self.t_zero = time.time() + self.cfg["countdown_s"]
        events.log("IGNITION", f"COUNTDOWN STARTED: T-{self.cfg['countdown_s']} s", "WARN")
        self._emit("countdown", self.t_zero)

    def abort(self, reason: str = "operator ABORT") -> None:
        with self._lock:
            was = self.state
            self.driver.safe()
            self.state = SAFE
            if was != SAFE:
                self.last_abort_reason = reason
                self.last_abort_t = time.time()
        if was != SAFE:
            events.log("IGNITION", f"ABORT → SAFE ({reason})", "ALARM")
            self._emit("safe", reason)

    def test_relay(self, relay: str) -> int:
        """Click one relay on its own (bench check). Only from SAFE."""
        if relay not in ("arm", "fire"):
            raise IgnitionError("Unknown relay")
        ms = int(self.cfg.get("relay_test_ms", 300))
        ms = max(50, min(ms, 1000))
        with self._lock:
            if self.state != SAFE:
                raise IgnitionError("Relay test only when ignition is SAFE")
            if not self.driver.test_relay(relay, ms):
                raise IgnitionError("Relay test failed: the ESP32 is not connected")
        events.log("IGNITION", f"Relay test: {relay.upper()} relay {ms} ms"
                               + ("" if self.driver.physical else " (simulation)"), "WARN")
        return ms

    def disarm(self) -> None:
        with self._lock:
            if self.state not in (ARMED,):
                if self.state == SAFE:
                    return
                raise IgnitionError("Use ABORT during countdown")
            self.driver.safe()
            self.state = SAFE
        events.log("IGNITION", "Disarmed → SAFE")
        self._emit("safe", "disarmed")

    # ---------- background loop ----------
    def _run(self) -> None:
        while not self._stop.wait(0.02):
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.abort(f"internal error: {exc}")

    def _tick(self) -> None:
        now = time.time()
        abort_reason = None
        fired = False
        with self._lock:
            st = self.state
            if st in (ARMED, COUNTDOWN):
                abort_reason = self._check_armed(st, now)
                if abort_reason is None and st == COUNTDOWN and now >= self.t_zero:
                    abort_reason = self._fire_locked(now)
                    fired = abort_reason is None
            elif st == FIRING and now >= self.fire_end:
                self.driver.safe()
                self.state = SAFE
                events.log("IGNITION", "Pulse complete → SAFE")
        # hooks and aborts run outside the lock so a slow hook never delays the sequence
        if abort_reason:
            self.abort(abort_reason)
        elif fired:
            events.log("IGNITION", f"FIRE — igniter pulse {self.cfg['pulse_ms']} ms", "ALARM")
            self._emit("fire", now)

    def _check_armed(self, st: str, now: float) -> str | None:
        if self.cfg["require_telemetry"] and not self.link_ok():
            return "telemetry link lost"
        if st == ARMED and now - self.armed_at > self.cfg["arm_timeout_s"]:
            return "arm timeout"
        if time.monotonic() - self._last_keepalive >= self.cfg["keepalive_ms"] / 1000:
            self._last_keepalive = time.monotonic()
            if not self.driver.keepalive():
                return "lost contact with ignition controller"
        return None

    def _fire_locked(self, now: float) -> str | None:
        hw = self.driver.hardware_state()
        if not (hw["confirmed"] and hw["state"] == "ARMED"):
            return f"ignition hardware not ARMED at T-0 ({hw['state']})"
        if not self.driver.fire(self.cfg["pulse_ms"]):
            return "FIRE command could not be sent"
        self.state = FIRING
        self.last_fire = now
        self.fire_end = now + self.cfg["pulse_ms"] / 1000
        return None

    # ---------- status ----------
    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "state": self.state,
                "driver": self.driver.name,
                "physical": self.driver.physical,
                "t_minus": round(self.t_zero - now, 2) if self.state == COUNTDOWN else None,
                "countdown_s": self.cfg["countdown_s"],
                "pulse_ms": self.cfg["pulse_ms"],
                "hold_to_start_s": self.cfg["hold_to_start_s"],
                "arm_remaining_s": round(self.cfg["arm_timeout_s"] - (now - self.armed_at), 0)
                if self.state == ARMED else None,
                "last_fire": self.last_fire,
                "last_abort_reason": self.last_abort_reason,
                "last_abort_t": self.last_abort_t,
                "hardware": self.driver.hardware_state(),
            }
