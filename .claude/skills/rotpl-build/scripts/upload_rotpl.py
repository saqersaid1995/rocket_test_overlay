#!/usr/bin/env python3
"""Upload a .rotpl package to a running rocket_test_overlay app, and
optionally activate it in the same step.

Usage:
    python3 upload_rotpl.py <path.rotpl> [--host http://127.0.0.1:5000] [--activate]

Requires the app to actually be running (python3 app.py) at --host first.
"""
import argparse
import json
import sys
from pathlib import Path

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--host", default="http://127.0.0.1:5000")
    parser.add_argument("--activate", action="store_true", help="Activate immediately after a successful upload")
    args = parser.parse_args()

    if not args.package.is_file():
        print(f"ERROR: file not found: {args.package}")
        return 1

    with args.package.open("rb") as handle:
        try:
            response = requests.post(
                f"{args.host}/api/templates",
                files={"template": (args.package.name, handle)},
                timeout=30,
            )
        except requests.ConnectionError:
            print(f"ERROR: could not reach {args.host} - is the app running (python3 app.py)?")
            return 1

    print(f"POST /api/templates -> {response.status_code}")
    payload = response.json()
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    if response.status_code != 201:
        return 1

    record = payload["template"]
    validation = record.get("validation", {})
    if not args.activate:
        if not validation.get("activatable"):
            print("\nInstalled as a draft only - not activatable yet (see blocked_reasons above).")
        return 0

    if not validation.get("activatable"):
        print("\nCannot activate: package is not activatable (see blocked_reasons above).")
        return 2

    activate_response = requests.post(
        f"{args.host}/api/templates/{record['id']}/activate",
        json={"version": record["version"]},
        timeout=30,
    )
    print(f"\nPOST /api/templates/{record['id']}/activate -> {activate_response.status_code}")
    print(json.dumps(activate_response.json(), indent=2, ensure_ascii=False))
    return 0 if activate_response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
