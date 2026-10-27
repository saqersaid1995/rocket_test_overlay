"""Find IP cameras on the local network and test a camera address.

scan()        ONVIF WS-Discovery (most IP cameras answer) + a quick check of
              RTSP port 554 on the PC's local /24 networks.
grab_frame()  read one picture from an RTSP/HTTP address with FFmpeg.
build_url()   make an RTSP address from IP, user, password and brand preset.
"""
from __future__ import annotations

import concurrent.futures as futures
import re
import socket
import subprocess
import uuid
from urllib.parse import quote, urlsplit, urlunsplit

from .camera import _NO_WINDOW, _input_args, ffmpeg_path

# Stream paths per brand: (main stream, sub stream). Sub stream = low resolution for preview.
PRESETS: dict[str, tuple[str, str]] = {
    "hikvision": ("/Streaming/Channels/101", "/Streaming/Channels/102"),
    "dahua":     ("/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1"),
    "tapo":      ("/stream1", "/stream2"),
    "reolink":   ("/h264Preview_01_main", "/h264Preview_01_sub"),
    "uniview":   ("/unicast/c1/s0/live", "/unicast/c1/s1/live"),
    "axis":      ("/axis-media/media.amp", "/axis-media/media.amp?resolution=640x360"),
    "generic":   ("/live/ch0", "/live/ch1"),
}

PRESET_LABELS = {
    "hikvision": "Hikvision / HiLook",
    "dahua": "Dahua / IMOU",
    "tapo": "TP-Link Tapo / VIGI",
    "reolink": "Reolink",
    "uniview": "Uniview",
    "axis": "Axis",
    "generic": "Generic / Baseus / other (edit path)",
}


def build_url(ip: str, user: str = "", password: str = "", port: int = 554, path: str = "/") -> str:
    auth = ""
    if user:
        auth = quote(user, safe="") + (":" + quote(password, safe="") if password else "") + "@"
    path = path if path.startswith("/") else "/" + path
    return f"rtsp://{auth}{ip}:{int(port)}{path}"


def mask_url(url: str) -> str:
    """Hide the password in an address for logs and screens."""
    try:
        parts = urlsplit(url)
        if parts.password:
            netloc = parts.netloc.replace(":" + parts.password + "@", ":****@")
            return urlunsplit(parts._replace(netloc=netloc))
    except ValueError:
        pass
    return url


def grab_frame(url: str, timeout: float = 12) -> tuple[bytes | None, str]:
    """Return (jpeg bytes, '') or (None, error message)."""
    exe = ffmpeg_path()
    if not exe:
        return None, "FFmpeg not found"
    cmd = [exe, "-hide_banner", "-loglevel", "error", *(_input_args(url) if "://" in url else ["-i", url]),
           "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", "-f", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return None, "No answer from the camera (timeout). Check the IP address and port."
    except OSError as exc:
        return None, str(exc)
    if p.returncode == 0 and p.stdout[:2] == b"\xff\xd8":
        return p.stdout, ""
    err = p.stderr.decode(errors="replace").strip().splitlines()
    msg = err[-1] if err else "no picture"
    low = msg.lower()
    if "401" in low or "unauthorized" in low:
        msg = "Wrong user name or password (401 Unauthorized)."
    elif "404" in low or "not found" in low:
        msg = "Stream path not found (404). Try another brand preset or path."
    elif "refused" in low:
        msg = "Connection refused: RTSP is off on the camera or the port is wrong."
    elif "timed out" in low or "timeout" in low:
        msg = "No answer from the camera (timeout). Check the IP address and port."
    return None, msg


# ------------------------------------------------------------------ discovery
_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header><w:MessageID>uuid:{id}</w:MessageID>
 <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
 <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""


def _local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def onvif_discover(timeout: float = 2.5) -> dict[str, str]:
    """{ip: description} for cameras answering ONVIF WS-Discovery."""
    found: dict[str, str] = {}
    sources = _local_ips() or ["0.0.0.0"]
    for src in sources:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if src != "0.0.0.0":
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(src))
            s.bind((src, 0))
            s.settimeout(timeout)
            s.sendto(_PROBE.format(id=uuid.uuid4()).encode(), ("239.255.255.250", 3702))
            while True:
                try:
                    data, addr = s.recvfrom(65535)
                except socket.timeout:
                    break
                text = data.decode(errors="replace")
                scopes = " ".join(re.findall(r"onvif://www\.onvif\.org/(?:name|hardware)/([^\s<]+)", text))
                found[addr[0]] = ("ONVIF " + scopes.replace("%20", " ")).strip()
            s.close()
        except OSError:
            continue
    return found


def rtsp_port_scan(timeout: float = 0.35) -> dict[str, str]:
    """{ip: 'RTSP port 554 open'} for hosts on the PC's local /24 networks."""
    targets = []
    for ip in _local_ips():
        base = ip.rsplit(".", 1)[0]
        targets += [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != ip]

    def probe(host: str) -> str | None:
        try:
            with socket.create_connection((host, 554), timeout=timeout):
                return host
        except OSError:
            return None

    found: dict[str, str] = {}
    with futures.ThreadPoolExecutor(max_workers=64) as pool:
        for host in pool.map(probe, targets[:1024]):
            if host:
                found[host] = "RTSP port 554 open"
    return found


def scan() -> list[dict]:
    result: dict[str, str] = {}
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(onvif_discover)
        b = pool.submit(rtsp_port_scan)
        for ip, desc in b.result().items():
            result[ip] = desc
        for ip, desc in a.result().items():
            result[ip] = desc + ("" if ip not in result else " · RTSP 554")
    return [{"ip": ip, "info": info} for ip, info in sorted(result.items(), key=lambda kv: socket.inet_aton(kv[0]))]
