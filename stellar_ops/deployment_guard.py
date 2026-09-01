from __future__ import annotations

import os
from pathlib import Path

from flask import jsonify, request

from .build_info import system_identity


DEFAULT_SECRET = "development-only-change-me"


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def deployment_assessment(
    *,
    secret_key: str,
    database_path: Path,
) -> dict:
    identity = system_identity()
    environment = identity["environment"]
    checks = []

    def check(code: str, passed: bool, severity: str, detail: str) -> None:
        checks.append(
            {
                "code": code,
                "status": "PASS" if passed else severity,
                "detail": detail,
            }
        )

    secret_ok = secret_key != DEFAULT_SECRET and len(secret_key) >= 32
    check(
        "APPLICATION_SECRET",
        secret_ok,
        "BLOCK",
        "Configured secret is at least 32 characters"
        if secret_ok
        else "Set STELLAR_OPS_SECRET to a unique value of at least 32 characters",
    )

    commit_ok = identity["commit"] != "LOCAL"
    check(
        "BUILD_IDENTITY",
        commit_ok,
        "BLOCK" if environment == "PRODUCTION" else "WARN",
        f"Build commit: {identity['commit']}",
    )

    public_url = os.environ.get("STELLAR_OPS_PUBLIC_URL", "").strip()
    https_ok = public_url.startswith("https://")
    check(
        "HTTPS_ORIGIN",
        https_ok,
        "BLOCK" if environment == "PRODUCTION" else "WARN",
        public_url or "STELLAR_OPS_PUBLIC_URL is not configured",
    )

    data_parent = database_path.parent
    data_ok = data_parent.exists() and os.access(data_parent, os.W_OK)
    check(
        "DATA_DIRECTORY",
        data_ok,
        "BLOCK",
        f"{data_parent} is writable" if data_ok else f"{data_parent} is not writable",
    )

    backup_path = Path(
        os.environ.get("STELLAR_OPS_BACKUPS", data_parent / "backups")
    )
    backup_separate = backup_path.resolve() != (data_parent / "backups").resolve()
    check(
        "OFF_HOST_BACKUP_PATH",
        backup_separate,
        "BLOCK" if environment == "PRODUCTION" else "WARN",
        str(backup_path),
    )

    debug_disabled = os.environ.get("FLASK_DEBUG", "0") != "1"
    check(
        "DEBUG_DISABLED",
        debug_disabled,
        "BLOCK",
        "Flask debug is disabled" if debug_disabled else "FLASK_DEBUG must be disabled",
    )

    sqlite_allowed = environment != "PRODUCTION"
    check(
        "PRODUCTION_DATASTORE",
        sqlite_allowed,
        "BLOCK",
        "SQLite approved for development/training"
        if sqlite_allowed
        else "PostgreSQL and dedicated telemetry storage are required for production",
    )

    blockers = [item for item in checks if item["status"] == "BLOCK"]
    warnings = [item for item in checks if item["status"] == "WARN"]
    return {
        "environment": environment,
        "status": "BLOCKED" if blockers else ("WARN" if warnings else "READY"),
        "production_authorized": environment == "PRODUCTION" and not blockers,
        "maintenance_mode": truthy("STELLAR_OPS_MAINTENANCE"),
        "checks": checks,
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
    }


def mutation_guard(app, database_path: Path):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if request.path in {
        "/api/control/backups",
        "/api/control/diagnostics/self-test",
        "/api/control/integrity/verify",
    }:
        return None

    assessment = deployment_assessment(
        secret_key=app.config["SECRET_KEY"],
        database_path=database_path,
    )
    if assessment["maintenance_mode"]:
        return jsonify(
            error="system is in maintenance mode; operational mutations are disabled",
            deployment=assessment,
        ), 503
    if (
        assessment["environment"] == "PRODUCTION"
        and assessment["status"] == "BLOCKED"
    ):
        return jsonify(
            error="production mutation refused because deployment checks are blocked",
            deployment=assessment,
        ), 503
    return None


def apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; form-action 'self'",
    )
    identity = system_identity()
    if request.path.startswith("/api/") or request.path.startswith("/health"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    elif request.path.startswith("/static/") and identity["environment"] != "PRODUCTION":
        # Development assets change frequently. Do not let the browser keep an
        # older JavaScript bundle after a git pull/restart.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    if identity["environment"] == "PRODUCTION" and request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response
