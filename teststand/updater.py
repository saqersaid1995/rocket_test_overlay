"""Updater section: check GitHub for a newer version and install it.

Uses the Git installation on the PC (the install folder is a Git clone of the
repository). Only fast-forward updates are applied, so local files are never
overwritten: config.yaml and data/ are not tracked by Git.

Updating is refused while ignition is not SAFE or a recording is running.
After a successful update the console restarts itself.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import events

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"


def _git(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError("Git is not installed on this PC")
    return subprocess.run([exe, "-C", str(ROOT), *args], capture_output=True, text=True,
                          timeout=timeout, creationflags=_NO_WINDOW)


def _ok(proc: subprocess.CompletedProcess) -> str:
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f"git exited {proc.returncode}")
    return proc.stdout.strip()


def current() -> dict:
    info = {"version": version(), "commit": None, "branch": None, "git": bool(shutil.which("git"))}
    if info["git"] and (ROOT / ".git").exists():
        try:
            info["commit"] = _ok(_git("rev-parse", "--short", "HEAD"))
            info["branch"] = _ok(_git("rev-parse", "--abbrev-ref", "HEAD"))
        except Exception:  # noqa: BLE001
            pass
    return info


def check() -> dict:
    """Fetch from GitHub and report whether a newer version is available."""
    if not (ROOT / ".git").exists():
        raise RuntimeError("This copy was not installed from GitHub (no .git folder)")
    branch = _ok(_git("rev-parse", "--abbrev-ref", "HEAD"))
    _ok(_git("fetch", "--quiet", "origin", branch, timeout=90))
    behind = int(_ok(_git("rev-list", "--count", f"HEAD..origin/{branch}")) or 0)
    ahead = int(_ok(_git("rev-list", "--count", f"origin/{branch}..HEAD")) or 0)
    changes = []
    if behind:
        log = _ok(_git("log", "--format=%h|%cs|%s", "-n", "15", f"HEAD..origin/{branch}"))
        changes = [dict(zip(("commit", "date", "title"), line.split("|", 2))) for line in log.splitlines() if line]
    remote_version = None
    try:
        remote_version = _ok(_git("show", f"origin/{branch}:VERSION"))
    except RuntimeError:
        pass
    return {"branch": branch, "behind": behind, "ahead": ahead, "update_available": behind > 0,
            "remote_version": remote_version, "changes": changes, **current()}


def apply() -> dict:
    """Fast-forward to the latest version and reinstall requirements if they changed."""
    branch = _ok(_git("rev-parse", "--abbrev-ref", "HEAD"))
    before = _ok(_git("rev-parse", "HEAD"))
    _ok(_git("fetch", "--quiet", "origin", branch, timeout=90))
    _ok(_git("merge", "--ff-only", f"origin/{branch}", timeout=60))
    after = _ok(_git("rev-parse", "HEAD"))
    changed = _ok(_git("diff", "--name-only", before, after)).splitlines()
    if "requirements.txt" in changed:
        events.log("UPDATE", "Installing new Python packages…")
        pip = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
                             capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
        if pip.returncode != 0:
            raise RuntimeError("pip install failed: " + (pip.stderr.strip().splitlines() or ["?"])[-1])
    events.log("UPDATE", f"Updated {before[:7]} → {after[:7]} ({len(changed)} files)")
    return {"from": before[:7], "to": after[:7], "files": len(changed), "version": version()}


def restart_later(on_exit=None, delay: float = 1.0) -> None:
    """Start a fresh copy of the console and exit this one."""
    def _go():
        time.sleep(delay)
        if on_exit:
            try:
                on_exit()
            except Exception:  # noqa: BLE001
                pass
        env = dict(os.environ, SFT_WAIT_FOR_PORTS="1")
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | _NO_WINDOW
        subprocess.Popen([sys.executable, "-m", "teststand.app", *sys.argv[1:]], cwd=str(ROOT), env=env,
                         creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os._exit(0)

    threading.Thread(target=_go, daemon=True).start()
