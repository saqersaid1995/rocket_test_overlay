from __future__ import annotations

import os

VERSION = "2.0.0-alpha.1"
RELEASE_CHANNEL = "development"
VALID_ENVIRONMENTS = {"DEVELOPMENT", "TRAINING", "PRODUCTION"}


def system_identity() -> dict[str, str]:
    environment = os.environ.get("STELLAR_OPS_ENV", "DEVELOPMENT").strip().upper()
    if environment not in VALID_ENVIRONMENTS:
        environment = "DEVELOPMENT"
    commit = (
        os.environ.get("STELLAR_OPS_COMMIT")
        or os.environ.get("GITHUB_SHA")
        or os.environ.get("CODESPACE_VSCODE_FOLDER", "")
    )
    if "/" in commit:
        commit = ""
    return {
        "name": "Stellar Mission & Test Control System",
        "version": VERSION,
        "channel": RELEASE_CHANNEL,
        "environment": environment,
        "commit": commit[:12] if commit else "LOCAL",
    }
