#!/usr/bin/env python3
"""Validate an existing .rotpl package against this repo's real ROTPL rules.

Usage:
    python3 validate_rotpl_package.py <path.rotpl>

Runs the same validate_rotpl() the app itself uses at upload time, so a
"why won't this activate" question gets the real, current answer instead
of a guess from re-reading the schema by eye.
"""
import sys
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "rotpl_registry.py").is_file():
            return candidate
    raise SystemExit(
        "Could not locate rotpl_registry.py - run this from within the "
        "rocket_test_overlay repo."
    )


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
sys.path.insert(0, str(REPO_ROOT))

from rotpl_registry import RotplValidationError, validate_rotpl  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1]).resolve()
    if not path.is_file():
        print(f"ERROR: file not found: {path}")
        return 1

    try:
        report = validate_rotpl(path).report
    except RotplValidationError as exc:
        report = exc.report

    print(f"Package: {path}")
    print(f"valid:       {report['valid']}")
    print(f"activatable: {report['activatable']}")
    for label in ("errors", "warnings", "blocked_reasons"):
        items = report.get(label) or []
        if items:
            print(f"{label}:")
            for message in items:
                print(f"  - {message}")

    if report["valid"] and report["activatable"]:
        return 0
    if report["valid"]:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
