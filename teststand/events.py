"""Shared operator event log.

Small and dependency-free on purpose: every section can write an event, the
recorder copies them to the session folder and the web page shows the last ones.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

_lock = threading.Lock()
_events: deque[dict] = deque(maxlen=500)
_listeners: list[Callable[[dict], None]] = []
_counter = 0


def log(source: str, message: str, level: str = "INFO") -> dict:
    global _counter
    with _lock:
        _counter += 1
        item = {"id": _counter, "t": time.time(), "source": source, "level": level, "message": message}
        _events.append(item)
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(item)
        except Exception:  # a broken listener must never block the caller
            pass
    return item


def recent(after_id: int = 0, limit: int = 100) -> list[dict]:
    with _lock:
        items = [e for e in _events if e["id"] > after_id]
    return items[-limit:]


def subscribe(fn: Callable[[dict], None]) -> None:
    with _lock:
        _listeners.append(fn)


def unsubscribe(fn: Callable[[dict], None]) -> None:
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)
