#!/usr/bin/env python3
"""Build a .rotpl package from a source directory and validate it for real.

Usage:
    python3 build_rotpl.py <source_dir> <output.rotpl>

<source_dir> must contain manifest.json and layout.json at its root, plus any
files they reference (fonts under fonts/, images, etc.) at the exact relative
paths declared in manifest.json.

What this does that hand-zipping doesn't:
  - Computes each declared font's real sha256/size_bytes from the actual file
    bytes and writes them into manifest.json before packaging - a hand-typed
    hash is the single most common way a package fails to activate.
  - Runs the real validate_rotpl() from this repo's rotpl_registry.py against
    the finished archive, so "did I get the schema right" is answered by the
    actual validator this app uses, not by re-reading the rules by eye.
  - Prints a clear PASS/DRAFT/FAIL report and exits 0 only when the package
    will both install and activate cleanly, so it composes in scripts.
"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "rotpl_registry.py").is_file():
            return candidate
    raise SystemExit(
        "Could not locate rotpl_registry.py - run this from within the "
        "rocket_test_overlay repo (or pass a source_dir inside it)."
    )


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
sys.path.insert(0, str(REPO_ROOT))

from rotpl_registry import RotplValidationError, validate_rotpl  # noqa: E402


def fill_font_hashes(source_dir: Path, manifest_path: Path, manifest: dict) -> None:
    filled = []
    for font in manifest.get("fonts", []):
        font_file = font.get("file")
        if not font_file:
            continue
        font_path = source_dir / font_file
        if not font_path.is_file():
            print(f"WARNING: declared font file not found on disk: {font_file}")
            continue
        data = font_path.read_bytes()
        font["sha256"] = hashlib.sha256(data).hexdigest()
        font["size_bytes"] = len(data)
        filled.append(font_file)
    if filled:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"Filled sha256/size_bytes for: {', '.join(filled)}")


def build(source_dir: Path, output_path: Path) -> int:
    manifest_path = source_dir / "manifest.json"
    layout_path = source_dir / "layout.json"
    if not manifest_path.is_file():
        print(f"ERROR: {manifest_path} not found.")
        return 1
    if not layout_path.is_file():
        print(f"ERROR: {layout_path} not found.")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fill_font_hashes(source_dir, manifest_path, manifest)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(source_dir).as_posix())

    print(f"\nBuilt: {output_path} ({output_path.stat().st_size:,} bytes)")

    try:
        report = validate_rotpl(output_path).report
    except RotplValidationError as exc:
        report = exc.report

    print("\n--- Validation report ---")
    print(f"valid:       {report['valid']}")
    print(f"activatable: {report['activatable']}")
    for label in ("errors", "warnings", "blocked_reasons"):
        items = report.get(label) or []
        if items:
            print(f"{label}:")
            for message in items:
                print(f"  - {message}")

    if report["valid"] and report["activatable"]:
        print("\nREADY: this package will install and activate cleanly.")
        return 0
    if report["valid"]:
        print("\nDRAFT ONLY: fix the blocked_reasons above before it can activate.")
        return 2
    print("\nFAILED: fix the errors above and rebuild.")
    return 1


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    source_dir = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()
    if not source_dir.is_dir():
        print(f"ERROR: source directory not found: {source_dir}")
        return 1
    return build(source_dir, output_path)


if __name__ == "__main__":
    raise SystemExit(main())
