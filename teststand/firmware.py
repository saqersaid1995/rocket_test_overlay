"""Firmware section: edit, compile and upload the ESP32 code over Ethernet.

  * The working copy of the sketch lives in data/firmware/<name>/<name>.ino
    (not tracked by Git, so SFT STELLAR updates never overwrite your edits).
    It starts as a copy of firmware/esp32_pressure_ota from SFT STELLAR.
  * Compile uses arduino-cli (bundled with Arduino IDE 2) with the boards and
    libraries already installed in Arduino IDE.
  * Upload sends the .bin to the ESP32 over the telemetry TCP link (OTA_BEGIN →
    OTA_READY → raw bytes → OTA_DONE). The ESP32 checks the MD5 and restarts.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import events

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "firmware" / "esp32_pressure_ota"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def arduino_cli() -> str | None:
    found = shutil.which("arduino-cli")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    for base in (Path(local) / "Programs" / "Arduino IDE", Path(r"C:\Program Files\Arduino IDE")):
        exe = base / "resources" / "app" / "lib" / "backend" / "resources" / "arduino-cli.exe"
        if exe.exists():
            return str(exe)
    return None


class FirmwareManager:
    def __init__(self, cfg: dict, data_dir: Path, hub):
        self.fqbn = cfg.get("fqbn", "esp32:esp32:esp32")
        self.name = cfg.get("sketch", "esp32_pressure_ota")
        self.work = Path(data_dir) / "firmware" / self.name
        self.build = Path(data_dir) / "firmware" / "build"
        self.hub = hub
        self._lock = threading.Lock()
        self.job = {"kind": None, "state": "IDLE", "log": "", "progress": 0, "started": None, "result": None}

    # ---------- source ----------
    @property
    def sketch(self) -> Path:
        return self.work / f"{self.name}.ino"

    def _ensure(self) -> None:
        if not self.sketch.exists():
            self.work.mkdir(parents=True, exist_ok=True)
            seed = SEED / f"{SEED.name}.ino"
            text = seed.read_text(encoding="utf-8") if seed.exists() else "void setup() {}\nvoid loop() {}\n"
            self.sketch.write_text(text.replace("\r\n", "\n"), encoding="utf-8")

    def read(self) -> str:
        self._ensure()
        return self.sketch.read_text(encoding="utf-8")

    def write(self, text: str) -> None:
        self._ensure()
        self.sketch.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
        events.log("FIRMWARE", "Code saved")

    # ---------- jobs ----------
    def busy(self) -> bool:
        return self.job["state"] == "RUNNING"

    def _start(self, kind: str, fn, *args) -> None:
        with self._lock:
            if self.busy():
                raise RuntimeError(f"{self.job['kind']} is already running")
            self.job = {"kind": kind, "state": "RUNNING", "log": "", "progress": 0,
                        "started": time.time(), "result": None}
        threading.Thread(target=self._run, args=(fn, *args), name=f"fw-{kind}", daemon=True).start()

    def _run(self, fn, *args) -> None:
        try:
            self.job["result"] = fn(*args)
            self.job["state"] = "DONE"
        except Exception as exc:  # noqa: BLE001
            self.job["log"] += f"\nERROR: {exc}\n"
            self.job["state"] = "FAILED"
            events.log("FIRMWARE", f"{self.job['kind']} failed: {exc}", "WARN")

    def _log(self, text: str) -> None:
        self.job["log"] += text

    # ---------- compile ----------
    def start_compile(self, then_upload: bool = False) -> None:
        self._start("compile+upload" if then_upload else "compile", self._compile, then_upload)

    def _compile(self, then_upload: bool) -> dict:
        exe = arduino_cli()
        if not exe:
            raise RuntimeError("arduino-cli not found. Install Arduino IDE 2 (it includes arduino-cli).")
        self._ensure()
        self.build.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "compile", "--fqbn", self.fqbn, "--output-dir", str(self.build), str(self.work)]
        ide_cfg = Path.home() / ".arduinoIDE" / "arduino-cli.yaml"   # same boards/libraries as Arduino IDE
        if ide_cfg.exists():
            cmd[1:1] = ["--config-file", str(ide_cfg)]
        # Arduino IDE 2 keeps its built-in libraries (Ethernet, Servo, ...) here and passes the
        # folder to arduino-cli itself; do the same.
        builtin = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Arduino15" / "libraries"
        if builtin.is_dir():
            cmd[-1:-1] = ["--libraries", str(builtin)]
        self._log(f"> arduino-cli compile --fqbn {self.fqbn}\n")
        events.log("FIRMWARE", "Compiling…")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", creationflags=NO_WINDOW)
        for line in proc.stdout:
            self._log(line)
        if proc.wait() != 0:
            import re
            missing = re.findall(r"fatal error: ([\w./-]+\.h): No such file", self.job["log"])
            if missing:
                raise RuntimeError(f"Library not installed for {', '.join(sorted(set(missing)))} — "
                                   "install it in Arduino IDE (Library Manager) and compile again")
            raise RuntimeError("Compile failed — see the messages above")
        binary = self.build / f"{self.name}.ino.bin"
        if not binary.exists():
            raise RuntimeError("Compile finished but no .bin was produced")
        events.log("FIRMWARE", f"Compiled OK ({binary.stat().st_size} bytes)")
        result = {"bin": str(binary), "size": binary.stat().st_size}
        if then_upload:
            self.job["kind"] = "upload"
            result["upload"] = self._upload(binary.read_bytes())
        return result

    # ---------- upload ----------
    def start_upload(self, data: bytes | None = None) -> None:
        if data is None:
            binary = self.build / f"{self.name}.ino.bin"
            if not binary.exists():
                raise RuntimeError("Compile first (no .bin yet)")
            data = binary.read_bytes()
        self._start("upload", self._upload, data)

    def _upload(self, data: bytes) -> dict:
        if not data or data[0] != 0xE9:
            raise RuntimeError("This is not an ESP32 firmware file (.bin)")
        fw = self.hub.snapshot().get("firmware") or ""
        if "ota" not in fw:
            raise RuntimeError(f"The ESP32 firmware '{fw or 'not connected'}' cannot receive updates over Ethernet")
        md5 = hashlib.md5(data).hexdigest()
        self._log(f"> upload {len(data)} bytes over Ethernet (md5 {md5})\n")
        events.log("FIRMWARE", f"Uploading {len(data)} bytes to the ESP32…", "WARN")

        def progress(sent: int) -> None:
            self.job["progress"] = round(100 * sent / len(data))

        self.hub.ota_upload(data, md5, progress, self._log)
        self._log("ESP32 accepted the new firmware and is restarting…\n")
        # the ESP32's boot_id can be the same after a restart, so wait for a NEW connection
        old_session = self.hub.snapshot().get("sessions", 0)
        end = time.time() + 40
        while time.time() < end:
            snap = self.hub.snapshot()
            if snap["link"] == "LIVE" and snap.get("sessions", 0) > old_session:
                self._log(f"ESP32 is back: {snap['firmware']}\n")
                events.log("FIRMWARE", f"ESP32 updated and running ({snap['firmware']})")
                return {"bytes": len(data), "firmware": snap["firmware"]}
            time.sleep(0.5)
        self._log("ESP32 did not reconnect within 40 s — check it (power, cable) and the Serial Monitor.\n")
        raise RuntimeError("ESP32 did not come back after the update")

    def status(self) -> dict:
        snap = self.hub.snapshot()
        binary = self.build / f"{self.name}.ino.bin"
        return {"job": dict(self.job), "device_firmware": snap.get("firmware"), "link": snap["link"],
                "ota_capable": "ota" in (snap.get("firmware") or ""), "cli": bool(arduino_cli()),
                "fqbn": self.fqbn, "sketch": self.name,
                "bin": {"size": binary.stat().st_size,
                        "time": binary.stat().st_mtime} if binary.exists() else None}
