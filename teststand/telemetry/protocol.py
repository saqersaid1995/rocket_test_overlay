"""SMTCS-EDGE/1 line protocol shared with the ESP32 firmware.

One JSON object per line. The ``crc32`` field is a CRC-32 of the *canonical*
JSON (keys sorted, no spaces, crc32 removed). Frames we send put ``crc32`` last
so the ESP32 can verify a frame by cutting the tail off, without a JSON parser.
"""
from __future__ import annotations

import json
import math
import zlib

PROTOCOL = "SMTCS-EDGE/1"
MAX_FRAME_BYTES = 64_000
MAX_BATCH_SAMPLES = 2_000
INBOUND_TYPES = {"HELLO", "BATCH", "HEARTBEAT", "IGN_STATUS", "ACK", "NACK"}   # ESP32 -> console
COMMAND_TYPES = {"ACK", "NACK", "IGN_ARM", "IGN_FIRE", "IGN_SAFE", "IGN_TEST"}             # console -> ESP32


class ProtocolError(ValueError):
    pass


def canonical(message: dict) -> str:
    body = {k: v for k, v in message.items() if k != "crc32"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def crc_hex(text: str) -> str:
    return f"{zlib.crc32(text.encode('ascii')) & 0xFFFFFFFF:08x}"


def encode(message: dict) -> bytes:
    """Encode a frame: canonical JSON with crc32 appended as the last key."""
    msg = {"protocol": PROTOCOL, **message}
    text = canonical(msg)
    return (text[:-1] + f',"crc32":"{crc_hex(text)}"}}\n').encode("ascii")


def decode(line: bytes, allowed: set[str] = INBOUND_TYPES) -> dict:
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    try:
        msg = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"bad JSON: {exc}") from exc
    if not isinstance(msg, dict) or msg.get("protocol") != PROTOCOL:
        raise ProtocolError("wrong protocol")
    if str(msg.get("crc32", "")).lower() != crc_hex(canonical(msg)):
        raise ProtocolError("CRC32 mismatch")
    kind = msg.get("type")
    if kind not in allowed:
        raise ProtocolError(f"unknown frame type {kind!r}")
    if not isinstance(msg.get("device_id"), str) or not msg["device_id"]:
        raise ProtocolError("device_id required")
    if kind == "BATCH":
        count = msg.get("sample_count")
        if not isinstance(count, int) or not 1 <= count <= MAX_BATCH_SAMPLES:
            raise ProtocolError("bad sample_count")
        for key in ("sequence", "first_sample_us", "sample_period_us"):
            if not isinstance(msg.get(key), int) or msg[key] < 0:
                raise ProtocolError(f"bad {key}")
        if msg["sample_period_us"] == 0:
            raise ProtocolError("bad sample_period_us")
        channels = msg.get("channels")
        if not isinstance(channels, dict) or not channels:
            raise ProtocolError("channels required")
        for values in channels.values():
            if not isinstance(values, list) or len(values) != count:
                raise ProtocolError("channel length mismatch")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                raise ProtocolError("non-numeric sample")
    return msg
