"""Shared workspace paths and the singleton ROTPL template registry.

Extracted from app.py so both the existing editor routes and the new Live
Studio blueprint (live_routes.py) can import the exact same registry
instance without a circular import between app.py and live_session.py.
"""

from __future__ import annotations

from pathlib import Path

from rotpl_registry import RotplRegistry

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "workspace"
UPLOAD_DIR = WORK_DIR / "uploads"
OUTPUT_DIR = WORK_DIR / "outputs"
PREVIEW_DIR = WORK_DIR / "previews"
TEMPLATE_PACKAGE_DIR = WORK_DIR / "template_packages"

for directory in (UPLOAD_DIR, OUTPUT_DIR, PREVIEW_DIR, TEMPLATE_PACKAGE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

template_registry = RotplRegistry(TEMPLATE_PACKAGE_DIR)
