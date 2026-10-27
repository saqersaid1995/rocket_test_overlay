"""System check: finds out *why* something is not working and says what to do.

Each check returns {"id", "name", "status": OK | WARN | FAIL | INFO, "detail", "fix"}.
Checks are read-only. The only action here is fix_firewall(), which the operator
starts with a button (Windows asks for administrator permission).
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .cameras.camera import ffmpeg_path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IS_WIN = os.name == "nt"
RULE_NAME = "SFT STELLAR telemetry"


def _run(cmd: list[str], timeout: float = 8) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW, errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


def _check(cid: str, name: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": cid, "name": name, "status": status, "detail": detail, "fix": fix}


# ---------------------------------------------------------------- network
def local_ip_towards(ip: str) -> str | None:
    """The address this PC uses to reach `ip` (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ip, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def local_ipv4s() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips)


def same_subnet24(a: str | None, b: str) -> bool:
    return bool(a) and a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


def ping(ip: str, timeout_ms: int = 800) -> bool:
    if IS_WIN:
        code, out = _run(["ping", "-n", "1", "-w", str(timeout_ms), ip], timeout=5)
        return code == 0 and "TTL=" in out.upper()
    code, _ = _run(["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip], timeout=5)
    return code == 0


def port_owner(port: int) -> str | None:
    """Name and PID of the program listening on a TCP port (Windows), else None."""
    if not IS_WIN:
        return None
    code, out = _run(["netstat", "-ano", "-p", "TCP"])
    pid = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper().startswith("LISTEN"):
            pid = parts[4]
            break
    if not pid:
        return None
    code, out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    row = next(csv.reader(io.StringIO(out.strip())), None) if out.strip() else None
    name = row[0] if row and len(row) > 1 else "unknown program"
    return f"{name} (PID {pid})"


# ---------------------------------------------------------------- firewall (Windows)
def _ps(script: str, timeout: float = 20) -> tuple[int, str]:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=timeout)


def firewall_status(port: int) -> dict:
    """{'allow_rule': bool|None, 'blocked_programs': [...]} — None when it cannot be read."""
    if not IS_WIN:
        return {"allow_rule": None, "blocked_programs": [], "profile": ""}
    script = (
        f"$a = Get-NetFirewallRule -DisplayName '{RULE_NAME}' -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Enabled -eq 'True' };"
        "if ($a) { 'ALLOW=1'; 'PROFILE=' + (($a | Select-Object -First 1).Profile) } else { 'ALLOW=0' };"
        "Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True -ErrorAction SilentlyContinue | "
        "Get-NetFirewallApplicationFilter | Where-Object { $_.Program -match 'python' } | "
        "ForEach-Object { 'BLOCK=' + $_.Program }"
    )
    code, out = _ps(script)
    if "ALLOW=" not in out:
        return {"allow_rule": None, "blocked_programs": [], "profile": ""}
    profile = next((ln[8:].strip() for ln in out.splitlines() if ln.startswith("PROFILE=")), "")
    return {"allow_rule": "ALLOW=1" in out, "profile": profile,
            "blocked_programs": sorted({ln[6:].strip() for ln in out.splitlines() if ln.startswith("BLOCK=")})}


def fix_firewall(port: int) -> str:
    """Open TCP `port` and allow this Python, removing Windows 'block' rules for Python.
    Windows shows a permission (UAC) prompt. Returns a short message."""
    if not IS_WIN:
        return "Firewall fix is only needed on Windows"
    folder = Path(sys.executable).parent
    script = Path(os.environ.get("TEMP", str(folder))) / "sft_stellar_firewall.ps1"
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "Get-NetFirewallRule -Direction Inbound -Action Block | Where-Object {",
        "  ($_ | Get-NetFirewallApplicationFilter).Program -match 'python' } | Remove-NetFirewallRule",
        f"Get-NetFirewallRule -DisplayName '{RULE_NAME}*' | Remove-NetFirewallRule",
        f"New-NetFirewallRule -DisplayName '{RULE_NAME}' -Direction Inbound -Protocol TCP "
        f"-LocalPort {port} -Action Allow -Profile Any | Out-Null",
    ]
    for exe in ("pythonw.exe", "python.exe"):
        lines.append(f"New-NetFirewallRule -DisplayName '{RULE_NAME} ({exe})' -Direction Inbound "
                     f"-Program '{folder / exe}' -Action Allow -Profile Any | Out-Null")
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    code, out = _ps("Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
                    f"'-NoProfile -ExecutionPolicy Bypass -File \"{script}\"'", timeout=120)
    if code != 0:
        return "Firewall change was cancelled or failed"
    return "Firewall updated"


# ---------------------------------------------------------------- full check
def run_all(system) -> list[dict]:
    cfg = system.cfg
    tcfg = cfg["telemetry"]
    port = int(tcfg["listen_port"])
    esp_ip = tcfg.get("esp_ip") or "192.168.1.50"
    snap = system.hub.snapshot()
    ign = system.ignition.snapshot()
    hw = ign["hardware"]
    checks: list[dict] = []

    # 1. PC network
    pc_ip = local_ip_towards(esp_ip)
    all_ips = local_ipv4s()
    if pc_ip and same_subnet24(pc_ip, esp_ip):
        checks.append(_check("network", "PC network", "OK",
                             f"This PC is {pc_ip} on the ESP32 network ({esp_ip.rsplit('.', 1)[0]}.x)."))
    else:
        checks.append(_check("network", "PC network", "FAIL",
                             f"No network card on {esp_ip.rsplit('.', 1)[0]}.x. PC addresses: "
                             f"{', '.join(all_ips) or 'none'}.",
                             f"Connect the Ethernet cable and give the PC a fixed IP such as "
                             f"{esp_ip.rsplit('.', 1)[0]}.10 (mask 255.255.255.0)."))

    # 2. Receiver port
    if snap["port_error"]:
        owner = port_owner(port)
        checks.append(_check("port", f"Receiver port {port}", "FAIL",
                             f"Port {port} is used by {owner or 'another program'}.",
                             "Close the other program (for example the old Stellar Ops / edge gateway). "
                             "SFT STELLAR connects by itself within 5 seconds."))
    else:
        checks.append(_check("port", f"Receiver port {port}", "OK", f"Listening on TCP {port}."))

    # 3. Firewall
    fw = firewall_status(port)
    if fw["allow_rule"] is None:
        checks.append(_check("firewall", "Windows firewall", "INFO",
                             "Not Windows, or the firewall could not be read."))
    elif fw["blocked_programs"]:
        checks.append(_check("firewall", "Windows firewall", "FAIL",
                             "Windows is BLOCKING Python: " + "; ".join(fw["blocked_programs"][:3]),
                             "Press 'Fix firewall' and accept the Windows prompt."))
    elif not fw["allow_rule"]:
        checks.append(_check("firewall", "Windows firewall", "WARN",
                             f"No rule that opens port {port}. The ESP32 may be blocked.",
                             "Press 'Fix firewall' and accept the Windows prompt."))
    elif fw["profile"] and fw["profile"] != "Any":
        checks.append(_check("firewall", "Windows firewall", "WARN",
                             f"Port {port} is open only on '{fw['profile']}' networks. A direct Ethernet "
                             "link to the ESP32 is usually a 'Public' network, so it may be blocked.",
                             "Press 'Fix firewall' and accept the Windows prompt."))
    else:
        checks.append(_check("firewall", "Windows firewall", "OK", f"Port {port} is allowed."))

    # 4. ESP32 reachable
    connected = snap["link"] in ("LIVE", "NO DATA", "STALE")
    reachable = connected or ping(esp_ip)
    if connected:
        checks.append(_check("esp_ping", "ESP32 on the network", "OK", "The ESP32 is connected."))
    elif reachable:
        checks.append(_check("esp_ping", "ESP32 on the network", "OK", f"{esp_ip} answers ping."))
    else:
        checks.append(_check("esp_ping", "ESP32 on the network", "FAIL",
                             f"{esp_ip} does not answer.",
                             "Check ESP32 power, the Ethernet cable / switch and the ESP32 IP "
                             "(Wi-Fi 'PressureMonitor' → http://192.168.4.1)."))

    # 5. ESP32 connected to us
    attempt = snap.get("last_attempt")
    if snap["link"] in ("LIVE", "NO DATA", "STALE"):
        checks.append(_check("esp_link", "ESP32 → SFT STELLAR link", "OK",
                             f"Connected from {snap['remote']} ({snap['firmware']})."))
    elif snap.get("ignored_device"):
        checks.append(_check("esp_link", "ESP32 → SFT STELLAR link", "FAIL",
                             f"An ESP32 called '{snap['ignored_device']}' connected, but this console "
                             f"expects '{tcfg['device_id']}'.",
                             "Set the device ID in Settings → ESP32 to match."))
    else:
        hint = (f"On the ESP32 page set 'Stellar Ops receiver IP' = {pc_ip or 'this PC IP'} "
                f"and port = {port}, then press SAVE & RESTART ETHERNET.")
        detail = "The ESP32 has not connected."
        if attempt:
            detail += f" Last connection attempt: {attempt['from']}."
        elif reachable:
            detail += " The ESP32 is on the network but sends nothing here."
        checks.append(_check("esp_link", "ESP32 → SFT STELLAR link", "FAIL", detail, hint))

    # 6. Pressure data / ADC
    if snap["link"] == "LIVE":
        v = snap.get("voltage_v")
        if v is not None and v < 0.01:
            checks.append(_check("adc", "Pressure sensor", "WARN",
                                 f"Data arriving at {snap['rate_hz']} Hz but the sensor reads {v:.3f} V.",
                                 "Check the sensor power and wiring (VCC / signal / shunt resistor)."))
        elif v is not None and v > 4.9:
            checks.append(_check("adc", "Pressure sensor", "WARN",
                                 f"Sensor at {v:.2f} V — at the top of the ADC range (saturated).",
                                 "Check the sensor wiring and the ADS1115 gain."))
        else:
            checks.append(_check("adc", "Pressure sensor", "OK",
                                 f"{snap['rate_hz']} Hz, {snap['pressure_bar']:.2f} bar"
                                 + (f", {v:.3f} V" if v is not None else "") + "."))
    elif snap["link"] == "NO DATA":
        checks.append(_check("adc", "Pressure sensor", "FAIL",
                             "The ESP32 is connected but sends no samples — the ADS1115 was not found.",
                             "Check the ADS1115 wiring (SDA 21, SCL 22, 3.3 V, GND) and restart the ESP32."))
    else:
        checks.append(_check("adc", "Pressure sensor", "INFO", "Waiting for the ESP32 link."))

    # 7. Ignition module
    if ign["driver"] == "simulation":
        checks.append(_check("ignition", "Ignition", "INFO",
                             "SIMULATION mode — no relay will switch.",
                             "When the bench test is done: Settings → Ignition → Output = ESP32."))
    elif snap["link"] == "OFFLINE":
        checks.append(_check("ignition", "Ignition", "FAIL", "ESP32 not connected."))
    elif not hw.get("module"):
        checks.append(_check("ignition", "Ignition", "FAIL",
                             "The ESP32 does not report an ignition module — the firmware on it is the "
                             "old version without ignition.ino.",
                             "Upload firmware/esp32_pressure_node (both tabs) from the SFT STELLAR folder."))
    else:
        checks.append(_check("ignition", "Ignition", "OK",
                             f"ESP32 ignition module answering, state {hw['state']}."))

    # 8. Cameras
    cams = system.cameras.snapshot()
    if not cams:
        checks.append(_check("cameras", "Cameras", "WARN", "No cameras configured.",
                             "Settings → Cameras → Add camera."))
    for c in cams:
        if c["status"] == "DISABLED":
            checks.append(_check(f"cam_{c['id']}", f"Camera {c['name']}", "INFO", "Disabled in settings."))
        elif c["status"] == "LIVE":
            checks.append(_check(f"cam_{c['id']}", f"Camera {c['name']}", "OK", "Live picture."))
        else:
            checks.append(_check(f"cam_{c['id']}", f"Camera {c['name']}", "FAIL",
                                 f"{c['status']}: {c['error'] or 'no picture'}",
                                 "Settings → Cameras → Test. Check IP, user, password and stream path."))
    if not ffmpeg_path():
        checks.append(_check("ffmpeg", "FFmpeg", "FAIL", "FFmpeg not found — cameras cannot work.",
                             "Run the installer again."))

    # 9. Disk
    base = Path(cfg["recorder"]["directory"]).resolve()
    probe = base if base.exists() else base.anchor
    try:
        free_gb = shutil.disk_usage(probe).free / 1e9
        st = "OK" if free_gb > 20 else "WARN" if free_gb > 5 else "FAIL"
        checks.append(_check("disk", "Recording disk", st, f"{free_gb:.0f} GB free.",
                             "" if st == "OK" else "Free disk space before a test (video uses ~1 GB per 10 min)."))
    except OSError as exc:
        checks.append(_check("disk", "Recording disk", "WARN", str(exc)))
    return checks


def summary(checks: list[dict]) -> str:
    if any(c["status"] == "FAIL" for c in checks):
        return "FAIL"
    if any(c["status"] == "WARN" for c in checks):
        return "WARN"
    return "OK"

