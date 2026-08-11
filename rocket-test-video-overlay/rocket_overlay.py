#!/usr/bin/env python3
"""Compatibility entry point for the canonical renderer in the repository root."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


CORE_PATH = Path(__file__).resolve().parents[1] / "rocket_overlay.py"
SPEC = importlib.util.spec_from_file_location("_rocket_overlay_core", CORE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load renderer: {CORE_PATH}")
CORE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CORE
SPEC.loader.exec_module(CORE)

for name in dir(CORE):
    if not name.startswith("_"):
        globals()[name] = getattr(CORE, name)


if __name__ == "__main__":
    CORE.main()
