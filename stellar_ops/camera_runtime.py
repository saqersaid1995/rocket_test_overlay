from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass
from urllib.parse import quote, urlparse, urlunparse

SERVICE_NAME = "stellar-ops-camera"
_status_lock = threading.Lock()
_statuses: dict[str, dict] = {}
_secret_presence: dict[str, bool] = {}


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
        _statuses[device_id.upper()] = {"status": status, "message": message, "updated_at": time.time(), **extra}


def camera_status(device_id: str) -> dict:
    with _status_lock:
        value = dict(_statuses.get(device_id.upper(), {}))
    if not value:
        return {"status": "NOT_CONNECTED", "message": "camera has not completed an authenticated test"}
    if time.time() - value.get("updated_at", 0) > 15 and value.get("status") == "STREAMING":
        value["status"] = "STALE"
    return value


def _probe_frame(url: str, username: str, password: str) -> tuple[bool, str, float | None]:
    try:
        import cv2
    except ImportError:
        return False, "OpenCV is not installed", None
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|timeout;5000000")
    started = time.monotonic()
    capture = cv2.VideoCapture(_credential_url(url, username, password), cv2.CAP_FFMPEG)
    try:
        ok, frame = capture.read()
        latency = round((time.monotonic() - started) * 1000, 1)
        if not ok or frame is None:
            return False, "RTSP endpoint returned no decodable video frame", latency
        height, width = frame.shape[:2]
        return True, f"decoded H.264 video frame {width}x{height}", latency
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
        else:
            main_url, preview_url, manufacturer, model = endpoint, None, None, None
        candidate = preview_url or main_url
        ok, detail, latency = _probe_frame(candidate, username, password)
        result = CameraResult(ok, "STREAMING" if ok else "NO_VIDEO", detail, latency,
                              main_url, preview_url, manufacturer, model)
        _set_status(device_id, result.status, result.message, main_url=main_url, preview_url=preview_url,
                    manufacturer=manufacturer, model=model, latency_ms=latency)
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
