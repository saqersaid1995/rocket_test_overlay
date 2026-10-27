"""Cameras section: live preview and recording of IP cameras.

Each camera runs in its own FFmpeg processes, completely outside the Python
process that handles pressure and ignition. If a camera freezes, crashes or
disconnects, only that camera's picture is affected; it restarts by itself.

  preview   FFmpeg → small MJPEG frames → shown on the console (url / preview_url)
  record    FFmpeg stream copy (no re-encoding, full quality) → .mkv file (record_url)

.mkv is used because it stays readable even if the recording is cut off.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .. import events


def ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def _input_args(url: str) -> list[str]:
    if url.startswith("rtsp://"):
        return ["-rtsp_transport", "tcp", "-timeout", "5000000", "-i", url]
    if "://" not in url:  # local video file: play in real time, loop (screen tests / replay)
        return ["-re", "-stream_loop", "-1", "-i", url]
    return ["-i", url]


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Camera:
    def __init__(self, cfg: dict):
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.preview_url = cfg.get("preview_url") or cfg["url"]
        self.record_url = cfg.get("record_url") or cfg["url"]
        self.preview_fps = int(cfg.get("preview_fps", 10))
        self.preview_width = int(cfg.get("preview_width", 640))   # 0 = full camera resolution
        self.enabled = bool(cfg.get("enabled", True))
        self._frame: bytes | None = None
        self._frame_t = 0.0
        self._frame_cond = threading.Condition()
        self._preview_proc: subprocess.Popen | None = None
        self._rec_proc: subprocess.Popen | None = None
        self._rec_file: Path | None = None
        self._rec_started = 0.0
        self._stop = threading.Event()
        self.status = "STARTING" if self.enabled else "DISABLED"
        self.error: str | None = None

    # ---------- preview ----------
    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._preview_loop, name=f"cam-{self.id}", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.stop_recording()
        p = self._preview_proc
        if p and p.poll() is None:
            p.kill()

    def _preview_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            exe = ffmpeg_path()
            if not exe:
                self.status, self.error = "ERROR", "FFmpeg not found (pip install imageio-ffmpeg)"
                self._stop.wait(10)
                continue
            cmd = [exe, "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
                   *_input_args(self.preview_url), "-an",
                   "-vf", f"fps={self.preview_fps}" + (f",scale={self.preview_width}:-2" if self.preview_width > 0 else ""),
                   "-q:v", "6", "-f", "mjpeg", "pipe:1"]
            started = time.time()
            try:
                self._preview_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                      stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
                self._read_mjpeg(self._preview_proc)
                err = self._preview_proc.stderr.read().decode(errors="replace").strip().splitlines()
                self.error = err[-1] if err else "stream ended"
            except OSError as exc:
                self.error = str(exc)
            if self._stop.is_set():
                break
            if self.status != "OFFLINE":
                events.log("CAMERA", f"{self.name} preview lost: {self.error}", "WARN")
            self.status = "OFFLINE"
            backoff = 1.0 if time.time() - started > 30 else min(backoff * 2, 15)
            self._stop.wait(backoff)

    def _read_mjpeg(self, proc: subprocess.Popen) -> None:
        buf = b""
        while not self._stop.is_set():
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2)
                if start < 0 or end < 0:
                    if len(buf) > 8_000_000:
                        buf = b""
                    break
                frame, buf = buf[start:end + 2], buf[end + 2:]
                with self._frame_cond:
                    self._frame, self._frame_t = frame, time.time()
                    self._frame_cond.notify_all()
                if self.status != "LIVE":
                    self.status, self.error = "LIVE", None
                    events.log("CAMERA", f"{self.name} live")

    def wait_frame(self, last_t: float, timeout: float = 2.0) -> tuple[bytes | None, float]:
        with self._frame_cond:
            self._frame_cond.wait_for(lambda: self._frame_t > last_t or self._stop.is_set(), timeout)
            return self._frame, self._frame_t

    # ---------- recording ----------
    def start_recording(self, directory: Path) -> Path | None:
        if not self.enabled or self.recording:
            return self._rec_file
        exe = ffmpeg_path()
        if not exe:
            events.log("CAMERA", f"{self.name}: cannot record, FFmpeg not found", "WARN")
            return None
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.id}.mkv"
        log = open(directory / f"{self.id}.ffmpeg.log", "wb")
        cmd = [exe, "-hide_banner", "-loglevel", "warning", "-y", *_input_args(self.record_url),
               "-map", "0:v:0", "-map", "0:a?", "-c", "copy", str(path)]
        try:
            self._rec_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                              creationflags=_NO_WINDOW)
        except OSError as exc:
            events.log("CAMERA", f"{self.name}: record start failed: {exc}", "WARN")
            return None
        finally:
            log.close()
        self._rec_file, self._rec_started = path, time.time()
        events.log("CAMERA", f"{self.name} recording → {path.name}")
        return path

    def stop_recording(self) -> Path | None:
        proc, path = self._rec_proc, self._rec_file
        if proc is None:
            return None
        self._rec_proc = None
        try:
            if proc.poll() is None:
                proc.stdin.write(b"q")  # ask FFmpeg to finish the file cleanly
                proc.stdin.flush()
                proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
        events.log("CAMERA", f"{self.name} recording stopped")
        return path

    @property
    def recording(self) -> bool:
        return self._rec_proc is not None and self._rec_proc.poll() is None

    def snapshot(self) -> dict:
        age = time.time() - self._frame_t if self._frame_t else None
        status = self.status
        if status == "LIVE" and age is not None and age > 3:
            status = "FROZEN"
        rec_failed = self._rec_proc is not None and self._rec_proc.poll() is not None
        error = re.sub(r"//[^/@\s]+@", "//****@", self.error) if self.error else None
        return {"id": self.id, "name": self.name, "status": status, "error": error,
                "recording": self.recording, "record_failed": rec_failed,
                "record_s": round(time.time() - self._rec_started) if self.recording else None}


class CameraBank:
    def __init__(self, cfgs: list[dict]):
        self.cameras = {c["id"]: Camera(c) for c in cfgs}

    def start(self) -> None:
        for cam in self.cameras.values():
            cam.start()
        if self.cameras:
            events.log("CAMERA", f"{len(self.cameras)} camera(s) configured"
                                 + ("" if ffmpeg_path() else " — FFmpeg NOT FOUND"))

    def stop(self) -> None:
        for cam in self.cameras.values():
            cam.stop()

    def start_recording(self, directory: Path) -> None:
        for cam in self.cameras.values():
            cam.start_recording(directory)

    def stop_recording(self) -> None:
        threads = [threading.Thread(target=c.stop_recording) for c in self.cameras.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=8)

    def snapshot(self) -> list[dict]:
        return [c.snapshot() for c in self.cameras.values()]
