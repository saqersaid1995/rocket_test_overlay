from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .camera_runtime import _ffmpeg_executable

SERVICE_NAME = "stellar-ops-broadcast"
_lock = threading.Lock()
_outputs: dict[int, dict] = {}
_secret_cache: dict[int, str] = {}
_program_recording: dict | None = None
_audio_config = {"device": "", "volume_db": 0.0, "muted": False, "av_sync_ms": 0}


def configure_audio(device: str = "", volume_db: float = 0.0,
                    muted: bool = False, av_sync_ms: int = 0) -> dict:
    _audio_config.update(
        device=str(device).strip(),
        volume_db=max(-60.0, min(float(volume_db), 12.0)),
        muted=bool(muted),
        av_sync_ms=max(-2000, min(int(av_sync_ms), 2000)),
    )
    return dict(_audio_config)


def audio_source() -> tuple[str, list[str]]:
    """Return the configured programme audio input and its FFmpeg input flags.

    A silent stereo source is used when no mixer/camera audio URL is configured.
    This keeps recordings and RTMP outputs standards-compliant without claiming
    that an operator microphone is active.
    """
    source = (_audio_config.get("device") or
              os.environ.get("STELLAR_BROADCAST_AUDIO_SOURCE", "")).strip()
    if not source:
        return "SILENCE", ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    if source.startswith("rtsp://"):
        return source, ["-rtsp_transport", "tcp", "-i", source]
    if source.startswith(("http://", "https://", "rtmp://", "rtmps://")):
        return source, ["-i", source]
    if os.name == "posix" and os.uname().sysname == "Darwin":
        return source, ["-f", "avfoundation", "-i", f":{source}"]
    return source, ["-f", "alsa", "-i", source]


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


def program_bus_url() -> str:
    configured = os.environ.get("STELLAR_PROGRAM_BUS_URL", "").strip()
    if configured:
        return configured
    port = int(os.environ.get("PORT", "5001"))
    return f"http://127.0.0.1:{port}/api/media/bus/program/stream.mjpg"


def probe_program_bus(timeout: float = 6.0) -> dict:
    """Verify the exact Program output produces multipart JPEG video."""
    started = time.monotonic()
    request = Request(program_bus_url(), headers={"User-Agent": "stellar-ops-encoder-preflight"})
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type", "")).lower()
            sample = response.read(4096)
        if "multipart/x-mixed-replace" not in content_type or b"Content-Type: image/jpeg" not in sample:
            raise RuntimeError("Program Bus did not return composited MJPEG video")
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "url": program_bus_url()}
    except Exception as exc:
        raise RuntimeError(f"Program Bus preflight failed: {exc}") from exc


def probe_destination_network(ingest_url: str, timeout: float = 4.0) -> dict:
    """Verify DNS/TCP reachability of the configured public ingest endpoint."""
    parsed = urlparse(ingest_url)
    if parsed.scheme not in {"rtmp", "rtmps"} or not parsed.hostname:
        raise RuntimeError("stream destination is not a valid RTMP/RTMPS endpoint")
    port = parsed.port or (443 if parsed.scheme == "rtmps" else 1935)
    started = time.monotonic()
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            pass
    except OSError as exc:
        raise RuntimeError(f"stream destination network preflight failed: {exc}") from exc
    return {"host": parsed.hostname, "port": port,
            "latency_ms": round((time.monotonic() - started) * 1000, 1)}


def _program_command(cameras: list[dict], scene: dict, target: str,
                     source_url: str | None = None) -> list[str]:
    """Encode the already-composited Program Bus; never bypass it for RTSP."""
    width = max(640, int(os.environ.get("STELLAR_BROADCAST_WIDTH", "1280")))
    height = max(360, int(os.environ.get("STELLAR_BROADCAST_HEIGHT", "720")))
    fps = max(1, min(int(os.environ.get("STELLAR_BROADCAST_FPS", "30")), 60))
    bitrate = os.environ.get("STELLAR_BROADCAST_VIDEO_BITRATE", "4500k")
    _audio_name, audio_flags = audio_source()
    sync_seconds = float(_audio_config.get("av_sync_ms", 0)) / 1000.0
    if sync_seconds:
        audio_flags = ["-itsoffset", f"{sync_seconds:.3f}", *audio_flags]
    volume = -60.0 if _audio_config.get("muted") else float(_audio_config.get("volume_db", 0))
    command = [
        _ffmpeg_executable(), "-hide_banner", "-loglevel", "warning",
        "-nostats", "-progress", "pipe:2", "-fflags", "nobuffer",
        "-flags", "low_delay", "-thread_queue_size", "512",
        "-f", "mpjpeg", "-i", source_url or program_bus_url(),
        *audio_flags,
        "-vf", f"scale={width}:{height}:flags=lanczos,fps={fps}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-af", (f"volume={volume:.1f}dB,astats=metadata=1:reset=1,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level:file='pipe\\:2'"),
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-r", str(fps), "-pix_fmt", "yuv420p", "-g", str(fps * 2),
        "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", "9000k",
        "-c:a", "aac", "-b:a", os.environ.get("STELLAR_BROADCAST_AUDIO_BITRATE", "160k"),
        "-ar", "48000", "-ac", "2", "-shortest",
    ]
    if target.lower().endswith(".mkv"):
        command += ["-f", "matroska", target]
    else:
        command += ["-f", "flv", target]
    return command


def _capture_progress(destination_id: int, process) -> int:
    stream = process.stderr
    if stream is None:
        return process.wait()
    for raw in iter(stream.readline, ""):
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in {"frame", "fps", "bitrate", "drop_frames", "out_time_ms", "speed",
                       "lavfi.astats.Overall.RMS_level"}:
            continue
        if key == "lavfi.astats.Overall.RMS_level":
            key = "audio_rms_db"
        with _lock:
            item = _outputs.get(destination_id)
            if item is not None:
                item[key] = value
                item["last_progress_at"] = time.time()
    return process.wait()


def _supervise(destination: dict, cameras: list[dict], scene: dict, target: str) -> None:
    destination_id = int(destination["id"])
    attempt = 0
    while True:
        attempt += 1
        with _lock:
            current = _outputs.get(destination_id)
            if not current or current.get("stop"):
                return
        process = subprocess.Popen(
            _program_command(cameras, scene, target), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            bufsize=1, start_new_session=True,
        )
        with _lock:
            current = _outputs.get(destination_id)
            if not current or current.get("stop"):
                process.terminate(); return
            current.update(process=process, state="STREAMING", reconnects=attempt-1,
                           failovers=max(0, attempt - 1), encoder_generation=attempt,
                           started_at=current.get("started_at") or time.time(),
                           encoder_started_at=time.time(), audio_source=audio_source()[0],
                           audio_muted=bool(_audio_config.get("muted")),
                           audio_volume_db=float(_audio_config.get("volume_db", 0)),
                           av_sync_ms=int(_audio_config.get("av_sync_ms", 0)))
        if _capture_progress(destination_id, process) == 0:
            return
        with _lock:
            if destination_id in _outputs:
                _outputs[destination_id]["state"] = "RECONNECTING"
        # Never abandon an on-air destination.  A bounded exponential backoff
        # avoids a tight crash loop while still recovering after long outages.
        time.sleep(min(2 ** min(attempt, 5), 30))


def start_output(destination: dict, cameras: list[dict] | dict, scene: dict | None = None) -> dict:
    """Start an independent public encoder; it never consumes evidence files."""
    destination_id = int(destination["id"])
    key = load_stream_key(destination_id)
    if not key:
        raise RuntimeError("stream destination secret is not configured")
    cameras = [cameras] if isinstance(cameras, dict) else cameras
    scene = scene or {"name": "PROGRAM", "sources": [{"kind":"camera","source":cameras[0]["device_id"]}]}
    target = destination["ingest_url"].rstrip("/") + "/" + quote(key, safe="-_~.")
    network = probe_destination_network(destination["ingest_url"])
    with _lock:
        prior = _outputs.pop(destination_id, None)
        if prior and prior.get("process") and prior["process"].poll() is None:
            prior["process"].terminate()
        _outputs[destination_id] = {"process": None, "device_ids": [c["device_id"] for c in cameras],
                                    "state": "CONNECTING", "stop": False, "reconnects": 0,
                                    "failovers": 0, "encoder_generation": 0,
                                    "network_latency_ms": network["latency_ms"]}
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
        progress_age = (time.time() - item["last_progress_at"]
                        if item.get("last_progress_at") else None)
        state = item.get("state")
        health = ("FAILED" if state == "FAILED" else
                  "DEGRADED" if state == "RECONNECTING" or
                  (progress_age is not None and progress_age > 5) else
                  "HEALTHY" if state == "STREAMING" else "IDLE")
        return {"state":state,"health":health,
                "progress_age_seconds":round(progress_age, 1) if progress_age is not None else None,
                "reconnects":item.get("reconnects",0),
                "failovers":item.get("failovers",0),
                "encoder_generation":item.get("encoder_generation",0),
                "uptime_seconds":round(time.time()-item.get("started_at",time.time()),1),
                "device_ids":item.get("device_ids",[]),
                "frame":item.get("frame"),"fps":item.get("fps"),
                "bitrate":item.get("bitrate"),"drop_frames":item.get("drop_frames"),
                "speed":item.get("speed"),"last_progress_at":item.get("last_progress_at"),
                "audio_rms_db":item.get("audio_rms_db"),
                "audio_source":item.get("audio_source", audio_source()[0]),
                "audio_muted":item.get("audio_muted", bool(_audio_config.get("muted"))),
                "audio_volume_db":item.get("audio_volume_db", float(_audio_config.get("volume_db", 0))),
                "av_sync_ms":item.get("av_sync_ms", int(_audio_config.get("av_sync_ms", 0))),
                "network_latency_ms":item.get("network_latency_ms")}


def _capture_recording_progress(item: dict) -> None:
    """Drain FFmpeg stderr so the encoder cannot block and expose live metrics."""
    process = item["process"]
    stream = process.stderr
    if stream is None:
        return
    for raw in iter(stream.readline, ""):
        line = raw.strip()
        if not line:
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if key in {"frame", "fps", "bitrate", "drop_frames", "out_time_ms", "speed"}:
                with _lock:
                    item[key] = value
                    item["last_progress_at"] = time.time()
                continue
            if key == "lavfi.astats.Overall.RMS_level":
                with _lock:
                    item["audio_rms_db"] = value
                    item["last_progress_at"] = time.time()
                continue
        # Keep a small diagnostic tail for start/stop failures without allowing
        # unbounded FFmpeg output to accumulate in memory.
        with _lock:
            errors = item.setdefault("errors", [])
            errors.append(line)
            del errors[:-12]


def _validate_recording_file(path: Path) -> tuple[bool, str]:
    """Require a non-empty Matroska file with at least one decodable video frame."""
    if not path.exists() or path.stat().st_size <= 0:
        return False, "recording file is empty"
    command = [
        _ffmpeg_executable(), "-hide_banner", "-loglevel", "error",
        "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
        "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"recording verification failed: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        return False, detail[-1] if detail else "recorded video is not decodable"
    return True, ""


def start_program_recording(cameras: list[dict], scene: dict, directory: Path) -> dict:
    global _program_recording
    with _lock:
        prior = _program_recording
        if prior and prior["process"].poll() is None:
            raise RuntimeError("Program recording is already active")

    directory.mkdir(parents=True, exist_ok=True)
    # Millisecond precision avoids overwriting a previous clip when operators
    # stop/start twice within the same second.
    path = directory / f"public-program-{int(time.time() * 1000)}.mkv"
    process = subprocess.Popen(
        _program_command(cameras, scene, str(path)),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    item = {
        "process": process,
        "path": str(path),
        "started_at": time.time(),
        "last_progress_at": None,
        "errors": [],
    }
    with _lock:
        _program_recording = item
    thread = threading.Thread(target=_capture_recording_progress, args=(item,), daemon=True)
    item["progress_thread"] = thread
    thread.start()

    # Do not report RECORDING until FFmpeg has actually started producing the
    # file (or encoder progress). This catches missing codecs/permissions/etc.
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        code = process.poll()
        size = path.stat().st_size if path.exists() else 0
        if code is not None:
            with _lock:
                errors = list(item.get("errors", []))
                if _program_recording is item:
                    _program_recording = None
            if path.exists() and path.stat().st_size == 0:
                path.unlink(missing_ok=True)
            detail = errors[-1] if errors else f"FFmpeg exited with code {code}"
            raise RuntimeError(f"Program recording failed to start: {detail}")
        if size > 1024 or item.get("frame"):
            return {"state": "RECORDING", "path": str(path), "bytes": size}
        time.sleep(0.05)

    # Some cameras deliver their first frame slowly. If the encoder is still
    # alive after the preflight window, keep recording but expose STARTING.
    return {"state": "RECORDING", "path": str(path),
            "bytes": path.stat().st_size if path.exists() else 0}


def program_recording_status() -> dict:
    with _lock:
        item = _program_recording
        if not item:
            return {"state": "STOPPED", "path": None, "duration_seconds": 0, "bytes": 0}
        snapshot = {
            key: item.get(key)
            for key in ("frame", "fps", "bitrate", "drop_frames", "speed",
                        "last_progress_at", "audio_rms_db")
        }
        errors = list(item.get("errors", []))
    process = item["process"]
    path = Path(item["path"])
    running = process.poll() is None
    state = "RECORDING" if running else "FAILED"
    result = {
        "state": state,
        "path": str(path),
        "duration_seconds": round(time.time() - item["started_at"], 1),
        "bytes": path.stat().st_size if path.exists() else 0,
        **snapshot,
    }
    if not running:
        result["error"] = errors[-1] if errors else f"FFmpeg exited with code {process.returncode}"
    return result


def stop_program_recording() -> dict:
    global _program_recording
    with _lock:
        item = _program_recording
    if not item:
        return {"state": "STOPPED"}

    process = item["process"]
    # Ask FFmpeg to quit through its command input first. This lets Matroska
    # write indexes/trailers cleanly instead of depending on an OS signal.
    if process.poll() is None:
        try:
            if process.stdin is not None:
                process.stdin.write("q\n")
                process.stdin.flush()
            process.wait(timeout=10)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    thread = item.get("progress_thread")
    if thread and thread.is_alive():
        thread.join(timeout=1)

    path = Path(item["path"])
    size = path.stat().st_size if path.exists() else 0
    valid, verification_error = _validate_recording_file(path)
    with _lock:
        errors = list(item.get("errors", []))
        if _program_recording is item:
            _program_recording = None

    result = {
        "state": "RECORDED" if valid else "FAILED",
        "path": str(path),
        "duration_seconds": round(time.time() - item["started_at"], 1),
        "bytes": size,
        "verified": valid,
    }
    if not valid:
        result["error"] = verification_error or (errors[-1] if errors else "recording failed verification")
    return result
