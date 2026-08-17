from __future__ import annotations

import json
import zlib

PROTOCOL = "SMTCS-EDGE/1"
MAX_FRAME_BYTES = 1_000_000
MAX_BATCH_SAMPLES = 2_000


class ProtocolError(ValueError):
    pass


def canonical_bytes(message: dict) -> bytes:
    body = {key: value for key, value in message.items() if key != "crc32"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def with_crc(message: dict) -> dict:
    result = dict(message)
    result["crc32"] = f"{zlib.crc32(canonical_bytes(result)) & 0xFFFFFFFF:08x}"
    return result


def encode_frame(message: dict) -> bytes:
    payload = json.dumps(with_crc(message), separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    return payload


def decode_frame(frame: bytes) -> dict:
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    try:
        message = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON frame: {exc}") from exc
    if message.get("protocol") != PROTOCOL:
        raise ProtocolError("unsupported protocol version")
    supplied = str(message.get("crc32", "")).lower()
    expected = f"{zlib.crc32(canonical_bytes(message)) & 0xFFFFFFFF:08x}"
    if supplied != expected:
        raise ProtocolError("CRC32 mismatch")
    kind = message.get("type")
    if kind not in {"HELLO", "BATCH", "HEARTBEAT", "ACK", "NACK"}:
        raise ProtocolError("unsupported message type")
    for key in ("device_id", "boot_id"):
        if not isinstance(message.get(key), str) or not message[key]:
            raise ProtocolError(f"{key} is required")
    if kind == "BATCH":
        count = message.get("sample_count")
        if not isinstance(count, int) or not 1 <= count <= MAX_BATCH_SAMPLES:
            raise ProtocolError("sample_count outside protocol limits")
        if not isinstance(message.get("sequence"), int) or message["sequence"] < 0:
            raise ProtocolError("non-negative integer sequence is required")
        if not isinstance(message.get("sample_period_us"), int) or message["sample_period_us"] <= 0:
            raise ProtocolError("positive sample_period_us is required")
        channels = message.get("channels")
        if not isinstance(channels, dict) or not channels:
            raise ProtocolError("channels object is required")
        if any(not isinstance(values, list) or len(values) != count for values in channels.values()):
            raise ProtocolError("every channel array must match sample_count")
    return message


def hello(device_id: str, boot_id: str, firmware: str, channels: list[str]) -> dict:
    return {"protocol": PROTOCOL, "type": "HELLO", "device_id": device_id, "boot_id": boot_id,
            "firmware": firmware, "channels": channels}


def batch(device_id: str, boot_id: str, sequence: int, first_sample_us: int,
          sample_period_us: int, channels: dict[str, list[float]]) -> dict:
    count = len(next(iter(channels.values())))
    return {"protocol": PROTOCOL, "type": "BATCH", "device_id": device_id, "boot_id": boot_id,
            "sequence": sequence, "first_sample_us": first_sample_us,
            "sample_period_us": sample_period_us, "sample_count": count, "channels": channels}
