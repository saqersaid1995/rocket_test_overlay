"""Thrust estimated from chamber pressure (when no load cell is fitted).

    F = efficiency x Cf x Pc x At

With the exit-pressure ratio fixed by the nozzle's expansion ratio, Cf x Pc is
linear in the absolute chamber pressure, so the whole model reduces to

    F = max(0, A x Pc_abs - B)      (N, Pc_abs in Pa)

which the browser can also use for the live chart.
"""
from __future__ import annotations

import math

G0 = 9.80665

# ratio of specific heats for two-phase nozzle flow (R. Nakka, nakka-rocketry.net)
PROPELLANTS = {
    "KNSB": 1.04,
    "KNSU": 1.044,
    "KNDX": 1.04,
    "APCP": 1.20,
}

# NAR/TRA motor classes by total impulse (N·s)
_CLASSES = [(2.5, "A"), (5, "B"), (10, "C"), (20, "D"), (40, "E"), (80, "F"), (160, "G"), (320, "H"),
            (640, "I"), (1280, "J"), (2560, "K"), (5120, "L"), (10240, "M"), (20480, "N"), (40960, "O")]


def motor_class(impulse_ns: float) -> str | None:
    if not impulse_ns or impulse_ns <= 0:
        return None
    for limit, letter in _CLASSES:
        if impulse_ns <= limit:
            return letter
    return "P+"


def area_ratio(mach: float, k: float) -> float:
    return (1 / mach) * ((2 / (k + 1)) * (1 + (k - 1) / 2 * mach ** 2)) ** ((k + 1) / (2 * (k - 1)))


def exit_mach(eps: float, k: float) -> float:
    """Supersonic exit Mach number for expansion ratio eps (bisection)."""
    if eps <= 1.0:
        return 1.0
    lo, hi = 1.0, 50.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if area_ratio(mid, k) < eps:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


class ThrustModel:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled"))
        self.k = float(self.cfg.get("gamma") or 1.04)
        self.dt = float(self.cfg.get("throat_mm") or 0) / 1000
        self.de = float(self.cfg.get("exit_mm") or 0) / 1000
        self.pa_bar = float(self.cfg.get("ambient_bar") or 1.013)
        self.eff = float(self.cfg.get("efficiency") or 0.95)
        self.prop_kg = float(self.cfg.get("propellant_g") or 0) / 1000
        if self.dt <= 0:
            self.enabled = False
        self.at = math.pi * self.dt ** 2 / 4
        self.eps = (self.de / self.dt) ** 2 if self.dt > 0 and self.de > self.dt else 1.0
        k = self.k
        self.mach_e = exit_mach(self.eps, k)
        self.pe_ratio = (1 + (k - 1) / 2 * self.mach_e ** 2) ** (-k / (k - 1))   # Pe / Pc
        self.cf_mom = math.sqrt(2 * k * k / (k - 1) * (2 / (k + 1)) ** ((k + 1) / (k - 1))
                                * (1 - self.pe_ratio ** ((k - 1) / k)))
        pa = self.pa_bar * 1e5
        # F = eff * At * (Pc*(cf_mom + pe_ratio*eps) - Pa*eps)
        self.A = self.eff * self.at * (self.cf_mom + self.pe_ratio * self.eps)
        self.B = self.eff * self.at * pa * self.eps

    def force(self, p_gauge_bar: float | None) -> float | None:
        if not self.enabled or p_gauge_bar is None:
            return None
        pc = (p_gauge_bar + self.pa_bar) * 1e5
        return max(0.0, self.A * pc - self.B)

    def cf(self, p_gauge_bar: float) -> float:
        pc = (p_gauge_bar + self.pa_bar) * 1e5
        return self.cf_mom + (self.pe_ratio * pc - self.pa_bar * 1e5) / pc * self.eps

    def isp(self, impulse_ns: float) -> float | None:
        if self.prop_kg <= 0 or not impulse_ns:
            return None
        return impulse_ns / (self.prop_kg * G0)

    def info(self, ref_bar: float = 50.0) -> dict:
        return {"enabled": self.enabled, "A": self.A, "B": self.B, "ambient_bar": self.pa_bar,
                "expansion_ratio": round(self.eps, 3), "exit_mach": round(self.mach_e, 3),
                "throat_area_mm2": round(self.at * 1e6, 2), "cf_at_ref": round(self.cf(ref_bar), 4) if self.at else None,
                "ref_bar": ref_bar, "force_at_ref_n": round(self.force(ref_bar) or 0, 1) if self.enabled else None}
