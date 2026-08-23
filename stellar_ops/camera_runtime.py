from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

SERVICE_NAME = "stellar-ops-camera"
_status_lock = threading.Lock()
_statuses: dict[str, dict] = {}
_secret_presence: dict[str, bool] = {}
_recorders: dict[str, dict] = {}
_recorder_lock = threading.Lock()


@dataclass(frozen=True)
class CameraResult:
    ok: bool
    status: str
    message: str
    latency_ms: float | None = None
    main_url: str | None = None
    preview_url: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    time_offset_ms: float | None = None
    time_status: str = "UNVERIFIED"
    width: int | None = None
    height: int | None = None
    fps: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def save_password(device_id: str, password: str) -> None:
    backend = _keyring()
    if backend is None:
        raise RuntimeError("secure credential storage is unavailable; install the keyring package")
    key = device_id.strip().upper()
    try:
        backend.set_password(SERVICE_NAME, key, password)
    except Exception as exc:
        raise RuntimeError("the operating system secure credential store is unavailable") from exc
    _secret_presence[key] = True


def load_password(device_id: str) -> str | None:
    env_name = "STELLAR_CAMERA_" + "".join(c if c.isalnum() else "_" for c in device_id.upper()) + "_PASSWORD"
    if os.environ.get(env_name):
        return os.environ[env_name]
    backend = _keyring()
    if backend is None:
        return None
    try:
        value = backend.get_password(SERVICE_NAME, device_id.strip().upper())
        _secret_presence[device_id.strip().upper()] = bool(value)
        return value
    except Exception:
        return None


def has_password(device_id: str) -> bool:
    key = device_id.strip().upper()
    if key in _secret_presence:
        return _secret_presence[key]
    return bool(load_password(key))


def _credential_url(url: str, username: str, password: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _set_status(device_id: str, status: str, message: str, **extra) -> None:
    with _status_lock:
        current = dict(_statuses.get(device_id.upper(), {}))
        current.update({"status": status, "message": message, "updated_at": time.time(), **extra})
        _statuses[device_id.upper()] = current


def camera_status(device_id: str) -> dict:
    with _status_lock:
        value = dict(_statuses.get(device_id.upper(), {}))
    if not value:
        return {"status": "NOT_CONNECTED", "message": "camera has not completed an authenticated test"}
    if time.time() - value.get("updated_at", 0) > 15 and value.get("status") == "STREAMING":
        value["status"] = "STALE"
    return value


def camera_recording_status(device_id: str) -> dict:
    with _recorder_lock:
        item = _recorders.get(device_id.upper())
        if not item:
            return {"state": "STOPPED", "file": None, "started_at": None}
        process = item["process"]
        state = "RECORDING" if process.poll() is None else "FAILED"
        return {"state": state, "file": str(item["final_path"]),
                "started_at": item["started_at"], "session_id": item["session_id"]}


def _ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise RuntimeError("FFmpeg recording engine is unavailable") from exc


def start_camera_recordings(cameras: list[dict], directory: Path, session_id: int) -> list[dict]:
    """Start one native RTSP recorder per configured camera.

    Preview continues to use the sub-stream; evidence recording remuxes the main
    stream without transcoding so the camera's original H.264 quality is retained.
    """
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    for camera in cameras:
        device_id = camera["device_id"].upper()
        password = load_password(device_id)
        if not password or not camera.get("username"):
            results.append({"device_id": device_id, "state": "SKIPPED", "message": "secure credentials are not configured"})
            continue
        known = camera_status(device_id)
        main_url = known.get("main_url")
        if not main_url:
            tested = test_camera(device_id, camera["adapter"], camera["endpoint"], camera["username"])
            if not tested.ok or not tested.main_url:
                results.append({"device_id": device_id, "state": "FAILED", "message": tested.message})
                continue
            main_url = tested.main_url
        final_path = directory / f"camera-{device_id.lower()}-session-{session_id:06d}.mkv"
        partial_path = final_path.with_suffix(".partial.mkv")
        log_path = final_path.with_suffix(".ffmpeg.log")
        log_handle = log_path.open("ab")
        command = [
            _ffmpeg_executable(), "-hide_banner", "-loglevel", "warning", "-y",
            "-rtsp_transport", "tcp", "-i", _credential_url(main_url, camera["username"], password),
            "-map", "0:v:0", "-c:v", "copy", "-an", "-f", "matroska", str(partial_path),
        ]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=log_handle, start_new_session=True)
        time.sleep(0.35)
        if process.poll() is not None:
            log_handle.close()
            detail = log_path.read_text(errors="replace")[-500:] if log_path.exists() else "recorder exited during startup"
            results.append({"device_id": device_id, "state": "FAILED", "message": detail})
            continue
        item = {"process": process, "log_handle": log_handle, "partial_path": partial_path,
                "final_path": final_path, "log_path": log_path, "started_at": time.time(),
                "session_id": session_id}
        with _recorder_lock:
            previous = _recorders.get(device_id)
            if previous and previous["process"].poll() is None:
                process.send_signal(signal.SIGINT)
                log_handle.close()
                results.append({"device_id": device_id, "state": "FAILED", "message": "camera recorder is already active"})
                continue
            _recorders[device_id] = item
        results.append({"device_id": device_id, "state": "RECORDING", "file": str(final_path)})
    return results


def stop_camera_recordings(session_id: int) -> list[dict]:
    results = []
    with _recorder_lock:
        selected = [(device_id, item) for device_id, item in _recorders.items()
                    if item["session_id"] == session_id]
    for device_id, item in selected:
        process = item["process"]
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        item["log_handle"].close()
        partial_path, final_path = item["partial_path"], item["final_path"]
        if partial_path.exists() and partial_path.stat().st_size:
            partial_path.replace(final_path)
            state, message = "RECORDED", "video file finalized"
        else:
            state, message = "FAILED", "recorder produced no video data"
        results.append({"device_id": device_id, "state": state, "message": message,
                        "file": str(final_path), "bytes": final_path.stat().st_size if final_path.exists() else 0})
        with _recorder_lock:
            _recorders.pop(device_id, None)
    return results


def _probe_frame(url: str, username: str, password: str) -> tuple[bool, str, float | None, dict]:
    try:
        import cv2
    except ImportError:
        return False, "OpenCV is not installed", None, {}
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|timeout;5000000")
    started = time.monotonic()
    capture = cv2.VideoCapture(_credential_url(url, username, password), cv2.CAP_FFMPEG)
    try:
        ok, frame = capture.read()
        latency = round((time.monotonic() - started) * 1000, 1)
        if not ok or frame is None:
            return False, "RTSP endpoint returned no decodable video frame", latency, {}
        height, width = frame.shape[:2]
        fps = round(float(capture.get(cv2.CAP_PROP_FPS) or 0), 3)
        return True, f"decoded H.264 video frame {width}x{height} at {fps:g} fps", latency, {
            "width": width, "height": height, "fps": fps}
    finally:
        capture.release()


def _onvif_discover(endpoint: str, username: str, password: str) -> tuple[str, str | None, str | None, str | None]:
    try:
        from onvif import ONVIFCamera
    except ImportError as exc:
        raise RuntimeError("ONVIF client is not installed; run pip install onvif-zeep") from exc
    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    if not parsed.hostname:
        raise ValueError("camera endpoint must contain a valid hostname or IP address")
    camera = ONVIFCamera(parsed.hostname, parsed.port or 80, username, password)
    information = camera.devicemgmt.GetDeviceInformation()
    media = camera.create_media_service()
    profiles = media.GetProfiles()
    if not profiles:
        raise RuntimeError("camera returned no ONVIF media profiles")
    uris = []
    for profile in profiles[:3]:
        response = media.GetStreamUri({"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}, "ProfileToken": profile.token})
        if getattr(response, "Uri", None):
            uris.append(str(response.Uri))
    if not uris:
        raise RuntimeError("camera returned no RTSP stream URI")
    return (uris[0], uris[1] if len(uris) > 1 else None,
            str(getattr(information, "Manufacturer", "") or ""), str(getattr(information, "Model", "") or ""))


def _onvif_time_offset(endpoint: str, username: str, password: str) -> tuple[float | None, str]:
    try:
        from onvif import ONVIFCamera
        parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        response = ONVIFCamera(parsed.hostname, parsed.port or 80, username, password).devicemgmt.GetSystemDateAndTime()
        stamp = getattr(response, "UTCDateTime", None)
        if not stamp:
            return None, "UNVERIFIED"
        camera_time = datetime(stamp.Date.Year, stamp.Date.Month, stamp.Date.Day,
                               stamp.Time.Hour, stamp.Time.Minute, stamp.Time.Second,
                               tzinfo=timezone.utc).timestamp()
        offset = round((camera_time - time.time()) * 1000, 1)
        return offset, "VERIFIED" if abs(offset) <= 1500 else "OUT_OF_TOLERANCE"
    except Exception:
        return None, "UNVERIFIED"


def test_camera(device_id: str, adapter: str, endpoint: str, username: str) -> CameraResult:
    device_id = device_id.upper()
    password = load_password(device_id)
    if not username.strip() or not password:
        result = CameraResult(False, "MISSING_CREDENTIALS", "camera username and secure password are required")
        _set_status(device_id, result.status, result.message)
        return result
    started = time.monotonic()
    try:
        if adapter.upper() == "ONVIF":
            main_url, preview_url, manufacturer, model = _onvif_discover(endpoint, username, password)
            time_offset_ms, time_status = _onvif_time_offset(endpoint, username, password)
        else:
            main_url, preview_url, manufacturer, model = endpoint, None, None, None
            time_offset_ms, time_status = None, "UNVERIFIED"
        candidate = preview_url or main_url
        probe = _probe_frame(candidate, username, password)
        ok, detail, latency, metrics = (*probe, {}) if len(probe) == 3 else probe
        result = CameraResult(ok, "STREAMING" if ok else "NO_VIDEO", detail, latency,
                              main_url, preview_url, manufacturer, model, time_offset_ms, time_status,
                              metrics.get("width"), metrics.get("height"), metrics.get("fps"))
        _set_status(device_id, result.status, result.message, main_url=main_url, preview_url=preview_url,
                    manufacturer=manufacturer, model=model, latency_ms=latency,
                    time_offset_ms=time_offset_ms, time_status=time_status, **metrics)
        return result
    except Exception as exc:
        message = str(exc).replace(password, "***")
        result = CameraResult(False, "AUTH_OR_STREAM_FAILED", message[:300], round((time.monotonic()-started)*1000, 1))
        _set_status(device_id, result.status, result.message)
        return result


def mjpeg_frames(device_id: str, adapter: str, endpoint: str, username: str, profile: str = "preview"):
    try:
        import cv2
    except ImportError:
        return
    password = load_password(device_id)
    if not username or not password:
        _set_status(device_id, "MISSING_CREDENTIALS", "camera credentials are not configured")
        return
    known = camera_status(device_id)
    url = known.get("preview_url") if profile == "preview" else known.get("main_url")
    if not url:
        tested = test_camera(device_id, adapter, endpoint, username)
        if not tested.ok:
            return
        url = tested.preview_url if profile == "preview" and tested.preview_url else tested.main_url
        known = camera_status(device_id)
    failures = 0
    while failures < 20:
        capture = cv2.VideoCapture(_credential_url(url, username, password), cv2.CAP_FFMPEG)
        try:
            while True:
                ok, frame = capture.read()
                if not ok or frame is None:
                    failures += 1
                    _set_status(device_id, "RECONNECTING", "video frame lost; reconnecting", main_url=known.get("main_url"), preview_url=known.get("preview_url"))
                    break
                failures = 0
                encoded, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                if encoded:
                    _set_status(device_id, "STREAMING", "browser preview active", main_url=known.get("main_url"), preview_url=known.get("preview_url"))
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nCache-Control: no-store\r\n\r\n" + jpeg.tobytes() + b"\r\n"
        finally:
            capture.release()
        time.sleep(min(2.0, 0.2 * max(1, failures)))
    _set_status(device_id, "DISCONNECTED", "camera reconnect limit reached")
