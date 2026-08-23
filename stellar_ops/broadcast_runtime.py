from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

from .camera_runtime import _credential_url, _ffmpeg_executable, camera_status, load_password

SERVICE_NAME = "stellar-ops-broadcast"
_lock = threading.Lock()
_outputs: dict[int, dict] = {}
_secret_cache: dict[int, str] = {}
_program_recording: dict | None = None


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


def _source(camera: dict) -> str:
    password = load_password(camera["device_id"])
    status = camera_status(camera["device_id"])
    source = status.get("preview_url") or status.get("main_url")
    if not source or not password or not camera.get("username"):
        raise RuntimeError(f"authenticated live source unavailable: {camera['device_id']}")
    return _credential_url(source, camera["username"], password)


def _program_command(cameras: list[dict], scene: dict, target: str) -> list[str]:
    selected = [c for c in cameras if any(s.get("kind") == "camera" and s.get("source") == c["device_id"]
                                          for s in scene.get("sources", []))] or cameras[:1]
    if not selected:
        raise RuntimeError("program scene has no authenticated camera source")
    command = [_ffmpeg_executable(), "-hide_banner", "-loglevel", "warning", "-stats_period", "1"]
    for camera in selected[:2]:
        command += ["-rtsp_transport", "tcp", "-i", _source(camera)]
    title = str(scene.get("name", "PROGRAM")).replace("'", "")[:80]
    if len(selected) > 1:
        command += ["-filter_complex", f"[0:v]scale=960:1080[left];[1:v]scale=960:1080[right];[left][right]hstack=inputs=2,drawtext=text='{title}':x=30:y=30:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.55[v]", "-map", "[v]"]
    else:
        command += ["-vf", f"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,drawtext=text='{title}':x=30:y=30:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.55"]
    command += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-r", "30",
                "-pix_fmt", "yuv420p", "-g", "60", "-b:v", "4500k", "-maxrate", "4500k",
                "-bufsize", "9000k", "-an"]
    if target.lower().endswith(".mkv"):
        command += ["-f", "matroska", target]
    else:
        command += ["-f", "flv", target]
    return command


def _supervise(destination: dict, cameras: list[dict], scene: dict, target: str) -> None:
    destination_id = int(destination["id"])
    for attempt in range(1, 6):
        with _lock:
            current = _outputs.get(destination_id)
            if not current or current.get("stop"):
                return
        process = subprocess.Popen(_program_command(cameras, scene, target), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        with _lock:
            current = _outputs.get(destination_id)
            if not current or current.get("stop"):
                process.terminate(); return
            current.update(process=process, state="STREAMING", reconnects=attempt-1, started_at=time.time())
        if process.wait() == 0:
            return
        with _lock:
            if destination_id in _outputs:
                _outputs[destination_id]["state"] = "RECONNECTING"
        time.sleep(min(attempt * 2, 10))
    with _lock:
        if destination_id in _outputs:
            _outputs[destination_id]["state"] = "FAILED"


def start_output(destination: dict, cameras: list[dict] | dict, scene: dict | None = None) -> dict:
    """Start an independent public encoder; it never consumes evidence files."""
    destination_id = int(destination["id"])
    key = load_stream_key(destination_id)
    if not key:
        raise RuntimeError("stream destination secret is not configured")
    cameras = [cameras] if isinstance(cameras, dict) else cameras
    scene = scene or {"name": "PROGRAM", "sources": [{"kind":"camera","source":cameras[0]["device_id"]}]}
    for camera in cameras:
        _source(camera)
    target = destination["ingest_url"].rstrip("/") + "/" + quote(key, safe="-_~.")
    with _lock:
        prior = _outputs.pop(destination_id, None)
        if prior and prior.get("process") and prior["process"].poll() is None:
            prior["process"].terminate()
        _outputs[destination_id] = {"process": None, "device_ids": [c["device_id"] for c in cameras],
                                    "state": "CONNECTING", "stop": False, "reconnects": 0}
    threading.Thread(target=_supervise, args=(destination, cameras, scene, target), daemon=True).start()
    return {"destination_id": destination_id, "state": "CONNECTING", "device_ids": [c["device_id"] for c in cameras]}


def stop_outputs() -> list[dict]:
    with _lock:
        selected = list(_outputs.items())
        _outputs.clear()
    result = []
    for destination_id, item in selected:
        process = item["process"]
        item["stop"] = True
        if process and process.poll() is None:
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
    return item.get("state", "CONNECTING")


def output_metrics(destination_id: int) -> dict:
    with _lock:
        item = _outputs.get(int(destination_id))
        if not item:
            return {"state":"STOPPED","reconnects":0,"uptime_seconds":0}
        return {"state":item.get("state"),"reconnects":item.get("reconnects",0),
                "uptime_seconds":round(time.time()-item.get("started_at",time.time()),1),
                "device_ids":item.get("device_ids",[])}


def start_program_recording(cameras: list[dict], scene: dict, directory: Path) -> dict:
    global _program_recording
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"public-program-{int(time.time())}.mkv"
    process = subprocess.Popen(_program_command(cameras, scene, str(path)), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    _program_recording = {"process":process,"path":str(path),"started_at":time.time()}
    return {"state":"RECORDING","path":str(path)}


def stop_program_recording() -> dict:
    global _program_recording
    item, _program_recording = _program_recording, None
    if not item:
        return {"state":"STOPPED"}
    process=item["process"]
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill()
    return {"state":"RECORDED","path":item["path"],"duration_seconds":round(time.time()-item["started_at"],1)}
