"""SFT STELLAR desktop launcher (Windows shortcut target).

Double-click the SFT STELLAR icon -> this script:
  1. starts the console server in the background if it is not already running
  2. waits until it answers
  3. opens it in its own app window (Microsoft Edge / Chrome app mode)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
LOG = ROOT / "data" / "logs" / "console.log"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def web_port() -> int:
    try:
        import yaml
        cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
        return int((cfg.get("web") or {}).get("port", 5050))
    except Exception:  # noqa: BLE001
        return 5050


def alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def message(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "SFT STELLAR", 0x10)
    except Exception:  # noqa: BLE001
        print(text)


def start_server() -> None:
    if not CONFIG.exists():
        example = ROOT / "config.example.yaml"
        if example.exists():
            CONFIG.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([sys.executable, "-m", "teststand.app", "--config", str(CONFIG), "--log", str(LOG)],
                     cwd=str(ROOT), creationflags=NO_WINDOW | DETACHED, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_window(url: str) -> None:
    local = os.environ.get("LOCALAPPDATA", str(Path.home()))
    profile = Path(local) / "SFT STELLAR" / "window"
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(local) / "Google/Chrome/Application/chrome.exe",
    ]
    for exe in candidates:
        if exe.exists():
            subprocess.Popen([str(exe), f"--app={url}", f"--user-data-dir={profile}",
                              "--window-size=1600,1000", "--no-first-run", "--disable-features=Translate"],
                             creationflags=DETACHED, close_fds=True)
            return
    webbrowser.open(url)


def main() -> None:
    url = f"http://127.0.0.1:{web_port()}"
    if not alive(url):
        start_server()
        end = time.time() + 40
        while time.time() < end and not alive(url):
            time.sleep(0.5)
        if not alive(url):
            tail = ""
            try:
                tail = "\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
            except OSError:
                pass
            message(f"SFT STELLAR could not start.\n\nLog: {LOG}\n\n{tail}")
            return
    open_window(url)


if __name__ == "__main__":
    main()
