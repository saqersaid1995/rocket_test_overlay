"""Minimal ONVIF client: ask a camera for its RTSP stream addresses.

No extra packages: plain SOAP over HTTP with WS-Security UsernameToken (digest).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import re
import socket
import urllib.error
import urllib.request

ONVIF_PORTS = (80, 8000, 8080, 8899, 2020, 5000, 10080)
RTSP_PORTS = (554, 8554, 10554, 5543, 7070)


def _security(user: str, password: str) -> str:
    if not user:
        return ""
    nonce = os.urandom(16)
    created = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
        f"<Username>{user}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0'
        f'#PasswordDigest">{digest}</Password>'
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0'
        f'#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"{created}</Created></UsernameToken></Security></s:Header>"
    )


def _soap(url: str, body: str, user: str = "", password: str = "", timeout: float = 4) -> str:
    env = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
           'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
           'xmlns:tt="http://www.onvif.org/ver10/schema">'
           f"{_security(user, password)}<s:Body>{body}</s:Body></s:Envelope>")
    req = urllib.request.Request(url, data=env.encode(), method="POST",
                                 headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        if "NotAuthorized" in text or e.code in (400, 401):
            raise PermissionError("not authorized") from e
        raise


def _tag(xml: str, name: str) -> list[str]:
    return re.findall(rf"<(?:\w+:)?{name}[^>]*>(.*?)</(?:\w+:)?{name}>", xml, re.S)


def open_ports(ip: str, ports, timeout: float = 0.6) -> list[int]:
    found = []
    for p in ports:
        try:
            with socket.create_connection((ip, p), timeout=timeout):
                found.append(p)
        except OSError:
            pass
    return found


def probe(ip: str, user: str = "", password: str = "", onvif_url: str | None = None) -> dict:
    """Return {'onvif_url', 'model', 'rtsp_ports', 'streams': [{name, width, height, uri}], 'error'}."""
    out: dict = {"ip": ip, "onvif_url": None, "model": None, "streams": [], "error": None,
                 "rtsp_ports": open_ports(ip, RTSP_PORTS)}
    device = None
    if not onvif_url:
        from .discovery import ONVIF_XADDRS
        onvif_url = ONVIF_XADDRS.get(ip)
    candidates = [onvif_url] if onvif_url else []
    candidates += [f"http://{ip}:{p}/onvif/device_service" for p in open_ports(ip, ONVIF_PORTS)]
    for url in candidates:
        try:
            _soap(url, "<tds:GetSystemDateAndTime/>")
            device = url
            break
        except PermissionError:
            device = url
            break
        except Exception:  # noqa: BLE001
            continue
    if not device:
        out["error"] = "The camera does not answer ONVIF on the usual ports."
        return out
    out["onvif_url"] = device
    try:
        info = _soap(device, "<tds:GetDeviceInformation/>", user, password)
        out["model"] = " ".join(x for x in (_tag(info, "Manufacturer")[:1] + _tag(info, "Model")[:1]) if x)
        caps = _soap(device, '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>',
                     user, password)
        media = (re.findall(r"<(?:\w+:)?Media>.*?<(?:\w+:)?XAddr>(.*?)</", caps, re.S) or [device])[0]
        profiles = _soap(media, "<trt:GetProfiles/>", user, password)
        blocks = re.findall(r"<(?:\w+:)?Profiles\b[^>]*token=\"([^\"]+)\"[^>]*>(.*?)</(?:\w+:)?Profiles>", profiles, re.S)
        for token, block in blocks:
            name = (_tag(block, "Name") or [token])[0]
            w = (_tag(block, "Width") or [""])[0]
            h = (_tag(block, "Height") or [""])[0]
            body = ('<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>'
                    f'<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>'
                    f'<trt:ProfileToken>{token}</trt:ProfileToken></trt:GetStreamUri>')
            uri = (_tag(_soap(media, body, user, password), "Uri") or [""])[0].replace("&amp;", "&")
            if uri:
                out["streams"].append({"name": name, "width": w, "height": h, "uri": uri})
    except PermissionError:
        out["error"] = "The camera needs the correct user name and password."
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"ONVIF error: {exc}"
    return out


def with_credentials(uri: str, user: str, password: str) -> str:
    """Put user/password into an rtsp:// address returned by ONVIF."""
    from urllib.parse import quote
    if not user or "@" in uri.split("//", 1)[-1].split("/", 1)[0]:
        return uri
    scheme, rest = uri.split("//", 1)
    return f"{scheme}//{quote(user, safe='')}:{quote(password, safe='')}@{rest}"
