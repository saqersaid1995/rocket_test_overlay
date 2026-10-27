"""Read and change Hikvision camera settings over ISAPI (HTTP, digest login).

Used by Settings → Cameras → "Camera settings":
  * stream resolution and frame rate (main 101 / sub 102), with the values
    the camera itself says it supports
  * on-screen text: date/time, camera name, extra text lines
"""
from __future__ import annotations

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

STREAMS = {"main": "101", "sub": "102"}


class CameraError(RuntimeError):
    pass


def _opener(ip: str, user: str, password: str):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"http://{ip}/", user, password)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr),
                                       urllib.request.HTTPBasicAuthHandler(mgr))


def _request(ip, user, password, path, data: bytes | None = None, method="GET", timeout=6) -> ET.Element:
    req = urllib.request.Request(f"http://{ip}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/xml"} if data else {})
    try:
        with _opener(ip, user, password).open(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise CameraError("Wrong user name or password") from e
        detail = e.read().decode(errors="replace")
        sub = _text(ET.fromstring(detail), "subStatusCode") if detail.strip().startswith("<") else ""
        raise CameraError(f"Camera refused ({e.code} {sub or e.reason})") from e
    except (urllib.error.URLError, OSError) as e:
        raise CameraError(f"Camera not reachable: {e}") from e
    return ET.fromstring(body)


# ---- small XML helpers that ignore namespaces ----
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element | None, path: str) -> ET.Element | None:
    for name in path.split("/"):
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == name), None)
    return el


def _text(el, path: str, default: str = "") -> str:
    node = _find(el, path)
    return (node.text or "").strip() if node is not None and node.text else default


def _set(el, path: str, value) -> bool:
    node = _find(el, path)
    if node is None:
        return False
    node.text = str(value)
    return True


def _opts(el, path: str) -> list[str]:
    node = _find(el, path)
    if node is None:
        return []
    return [x for x in (node.get("opt") or "").split(",") if x]


def _xml(root: ET.Element) -> bytes:
    ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
    if ns:
        ET.register_namespace("", ns)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ---- read ----
def _stream(ip, user, password, sid: str) -> dict:
    cur = _request(ip, user, password, f"/ISAPI/Streaming/channels/{sid}")
    caps = _request(ip, user, password, f"/ISAPI/Streaming/channels/{sid}/capabilities")
    widths, heights = _opts(caps, "Video/videoResolutionWidth"), _opts(caps, "Video/videoResolutionHeight")
    resolutions = []
    for w, h in zip(widths, heights):
        if f"{w}x{h}" not in resolutions:
            resolutions.append(f"{w}x{h}")
    fps_opts = sorted({int(x) for x in _opts(caps, "Video/maxFrameRate") if x.isdigit()}, reverse=True)
    cur_fps = int(_text(cur, "Video/maxFrameRate", "0") or 0)
    return {
        "id": sid,
        "resolution": f"{_text(cur, 'Video/videoResolutionWidth')}x{_text(cur, 'Video/videoResolutionHeight')}",
        "resolutions": resolutions,
        "fps": cur_fps / 100 if cur_fps else None,
        "fps_options": [f / 100 for f in fps_opts if f >= 100] or [],
        "codec": _text(cur, "Video/videoCodecType"),
        "bitrate_kbps": _text(cur, "Video/vbrUpperCap") or _text(cur, "Video/constantBitRate"),
    }


def _overlays(ip, user, password) -> dict:
    ov = _request(ip, user, password, "/ISAPI/System/Video/inputs/channels/1/overlays")
    texts = []
    tl = _find(ov, "TextOverlayList")
    for t in (list(tl) if tl is not None else []):
        texts.append({"id": _text(t, "id"), "enabled": _text(t, "enabled") == "true",
                      "text": _text(t, "displayText")})
    return {
        "date_time": _text(ov, "DateTimeOverlay/enabled") == "true",
        "camera_name": _text(ov, "channelNameOverlay/enabled") == "true",
        "texts": texts,
    }


def read(ip: str, user: str, password: str) -> dict:
    info = _request(ip, user, password, "/ISAPI/System/deviceInfo")
    out = {"model": _text(info, "model"), "firmware": _text(info, "firmwareVersion"),
           "streams": {}, "osd": None, "errors": []}
    for name, sid in STREAMS.items():
        try:
            out["streams"][name] = _stream(ip, user, password, sid)
        except CameraError as exc:
            out["errors"].append(f"{name} stream: {exc}")
    try:
        out["osd"] = _overlays(ip, user, password)
    except CameraError as exc:
        out["errors"].append(f"on-screen text: {exc}")
    return out


# ---- write ----
def apply(ip: str, user: str, password: str, changes: dict) -> list[str]:
    """changes = {"streams": {"main": {"resolution": "3200x1800", "fps": 20}, ...},
                  "osd": {"date_time": bool, "camera_name": bool, "texts": [{"id", "enabled", "text"}]}}"""
    done = []
    for name, want in (changes.get("streams") or {}).items():
        sid = STREAMS.get(name)
        if not sid or not want:
            continue
        cur = _request(ip, user, password, f"/ISAPI/Streaming/channels/{sid}")
        if want.get("resolution"):
            w, h = str(want["resolution"]).lower().split("x")
            _set(cur, "Video/videoResolutionWidth", int(w))
            _set(cur, "Video/videoResolutionHeight", int(h))
        if want.get("fps"):
            _set(cur, "Video/maxFrameRate", int(round(float(want["fps"]) * 100)))
        _request(ip, user, password, f"/ISAPI/Streaming/channels/{sid}", _xml(cur), "PUT")
        done.append(f"{name} stream: {want.get('resolution', '')} {want.get('fps', '')} fps".strip())
    osd = changes.get("osd")
    if osd:
        ov = _request(ip, user, password, "/ISAPI/System/Video/inputs/channels/1/overlays")
        if "date_time" in osd:
            _set(ov, "DateTimeOverlay/enabled", "true" if osd["date_time"] else "false")
        if "camera_name" in osd:
            _set(ov, "channelNameOverlay/enabled", "true" if osd["camera_name"] else "false")
        tl = _find(ov, "TextOverlayList")
        for t in (osd.get("texts") or []):
            for node in (list(tl) if tl is not None else []):
                if _text(node, "id") == str(t.get("id")):
                    _set(node, "enabled", "true" if t.get("enabled") else "false")
                    if "text" in t:
                        _set(node, "displayText", t["text"])
        _request(ip, user, password, "/ISAPI/System/Video/inputs/channels/1/overlays", _xml(ov), "PUT")
        done.append("on-screen text")
    return done
