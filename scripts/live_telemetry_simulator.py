#!/usr/bin/env python3
"""Posts synthetic telemetry to a running Live Studio session's real ingestion
endpoint (POST /api/live/<id>/telemetry) on a timer.

This stands in for a real hardware bridge (Serial/Modbus/MQTT -> HTTP) so the
live pipeline can be exercised end-to-end without real sensors attached. It
periodically pushes an out-of-range wind reading so the auto weather-hold
path can be observed in the control room.

Usage:
    python3 scripts/live_telemetry_simulator.py <session_id> [--base-url URL]
        [--mission-type static_fire|launch] [--interval SECONDS]
"""

from __future__ import annotations

import argparse
import math
import sys
import time
import urllib.error
import urllib.request
import json


def post_telemetry(base_url: str, session_id: str, channels: dict[str, float]) -> None:
    url = f"{base_url}/api/live/{session_id}/telemetry"
    body = json.dumps({"channels": channels}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
    except urllib.error.URLError as exc:
        print(f"telemetry post failed: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--mission-type", choices=("static_fire", "launch"), default="static_fire")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--wind-spike-every", type=int, default=20,
                         help="every N ticks, push an out-of-range wind reading")
    args = parser.parse_args()

    tick = 0
    start = time.time()
    while True:
        elapsed = time.time() - start
        wind_spike = args.wind_spike_every > 0 and tick % args.wind_spike_every == 0 and tick > 0
        wind = 18.0 if wind_spike else 4.0 + 2.0 * math.sin(elapsed / 5.0)
        temp = 22.0 + math.sin(elapsed / 20.0)
        channels = {"wind_speed": round(wind, 2), "temp_c": round(temp, 2)}
        if args.mission_type == "static_fire":
            pressure = max(0.0, 15.0 + 20.0 * math.sin(elapsed / 8.0))
            channels["pressure"] = round(pressure, 2)
            channels["thrust"] = round(max(0.0, pressure * 30.0), 1)
        else:
            channels["altitude_m"] = round(max(0.0, elapsed * 120.0), 1)
            channels["velocity_mps"] = round(max(0.0, elapsed * 45.0), 1)
        post_telemetry(args.base_url, args.session_id, channels)
        print(f"tick {tick}: {channels}" + (" [WIND SPIKE]" if wind_spike else ""))
        tick += 1
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
