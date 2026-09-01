from __future__ import annotations

from flask import Blueprint, jsonify

from .weather import settings

site_config = Blueprint("site_config", __name__)


@site_config.get("/api/site")
def get_site():
    """Return operation-site coordinates only; never call external weather services."""
    return jsonify(ok=True, settings=settings())
