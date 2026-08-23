from __future__ import annotations

import subprocess
import threading
from urllib.parse import quote

from .camera_runtime import _credential_url, _ffmpeg_executable, camera_status, load_password

SERVICE_NAME = "stellar-ops-broadcast"
_lock = threading.Lock()
_outputs: dict[int, dict] = {}
_secret_cache: dict[int, str] = {}


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def save_stream_key(destination_id: int, key: str) -> None:
    _secret_cache[int(destination_id)] = key
    backend = _keyring()
    if backend is None:
        raise RuntimeError("secure credential storage is unavailable")
    try:
        backend.set_password(SERVICE_NAME, str(destination_id), key)
    except Exception as exc:
        raise RuntimeError("the operating-system secure credential store is unavailable") from exc


def load_stream_key(destination_id: int) -> str | None:
    if int(destination_id) in _secret_cache:
        return _secret_cache[int(destination_id)]
    backend = _keyring()
    if backend is None:
        return None
    try:
        return backend.get_password(SERVICE_NAME, str(destination_id))
    except Exception:
        return None


def start_output(destination: dict, camera: dict) -> dict:
    """Start an independent public encoder; it never consumes evidence files."""
    destination_id = int(destination["id"])
    key = load_stream_key(destination_id)
    if not key:
        raise RuntimeError("stream destination secret is not configured")
    password = load_password(camera["device_id"])
    status = camera_status(camera["device_id"])
    source = status.get("preview_url") or status.get("main_url")
    if not source or not password or not camera.get("username"):
        raise RuntimeError("an authenticated live camera source is required")
    target = destination["ingest_url"].rstrip("/") + "/" + quote(key, safe="-_~.")
    command = [_ffmpeg_executable(), "-hide_banner", "-loglevel", "warning", "-re",
               "-rtsp_transport", "tcp", "-i", _credential_url(source, camera["username"], password),
               "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
               "-pix_fmt", "yuv420p", "-g", "60", "-b:v", "4500k", "-maxrate", "4500k",
               "-bufsize", "9000k", "-an", "-f", "flv", target]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    with _lock:
        prior = _outputs.pop(destination_id, None)
        if prior and prior["process"].poll() is None:
            prior["process"].terminate()
        _outputs[destination_id] = {"process": process, "device_id": camera["device_id"]}
    return {"destination_id": destination_id, "state": "CONNECTING", "device_id": camera["device_id"]}


def stop_outputs() -> list[dict]:
    with _lock:
        selected = list(_outputs.items())
        _outputs.clear()
    result = []
    for destination_id, item in selected:
        process = item["process"]
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        result.append({"destination_id": destination_id, "state": "STOPPED"})
    return result


def output_status(destination_id: int) -> str:
    with _lock:
        item = _outputs.get(int(destination_id))
    if not item:
        return "STOPPED"
    return "STREAMING" if item["process"].poll() is None else "FAILED"
