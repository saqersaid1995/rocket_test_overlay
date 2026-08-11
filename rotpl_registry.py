"""Safe, persistent registry for declarative ``.rotpl`` template packages.

The registry deliberately does not execute or render package content.  It only
validates a small, declarative archive format, installs immutable versions, and
tracks activation history.  Archive extraction is performed member-by-member
after validation; ``ZipFile.extractall`` is intentionally never used.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import threading
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable

from PIL import Image, UnidentifiedImageError


MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 256
MAX_COMPRESSION_RATIO = 200.0
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_LAYOUT_ELEMENTS = 1_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 100_000

ALLOWED_SUFFIXES = {
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".ttf",
    ".otf",
    ".md",
    ".txt",
}
ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
FONT_SUFFIXES = {".ttf", ".otf"}
FORBIDDEN_SUFFIXES = {
    ".py", ".pyc", ".pyo", ".js", ".mjs", ".cjs", ".html", ".htm",
    ".sh", ".bash", ".bat", ".cmd", ".ps1", ".exe", ".dll", ".so",
    ".dylib", ".wasm", ".jar", ".class", ".php", ".rb", ".pl",
    ".lua", ".com", ".msi", ".scr", ".app", ".lnk", ".desktop",
}
RESERVED_SEGMENTS = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
TEMPLATE_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
BINDING_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)*(?:\[\])?$")
ELEMENT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")
SUPPORTED_ELEMENT_TYPES = {
    "image", "group", "rect", "line", "gradient_scrim", "text", "logo",
    "arc_gauge", "bar_gauge", "vertical_gauge", "phase_list", "chart",
}
MISSING_POLICIES = {"hide", "show_na", "show_fallback", "required_error"}
FORBIDDEN_JSON_KEYS = {
    "script", "javascript", "code", "command", "exec", "execute", "eval",
    "shell", "subprocess", "plugin", "module", "import", "iframe",
    "foreignobject", "html", "url", "uri", "href",
}


class RotplError(Exception):
    """Base error safe to expose through the local API."""


class RotplValidationError(RotplError):
    def __init__(self, message: str, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.report = report or {
            "valid": False,
            "errors": [message],
            "warnings": [],
            "activatable": False,
            "blocked_reasons": [],
        }


class RotplConflictError(RotplError):
    pass


class RotplNotFoundError(RotplError):
    pass


class RotplActivationError(RotplError):
    pass


@dataclass(frozen=True)
class ValidatedPackage:
    manifest: dict[str, Any]
    layout: dict[str, Any]
    members: tuple[zipfile.ZipInfo, ...]
    report: dict[str, Any]
    sha256: str
    size_bytes: int
    used_bindings: tuple[str, ...]
    file_sha256: dict[str, str]


@dataclass(frozen=True)
class ResolvedTemplate:
    """Verified immutable selection consumed by preview/export renderers."""

    id: str
    version: str
    sha256: str
    files_path: Path
    package_path: Path
    manifest_path: Path
    layout_path: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _strict_json_loads(raw: bytes, filename: str) -> Any:
    if len(raw) > MAX_JSON_BYTES:
        raise RotplValidationError(f"{filename} exceeds the JSON size limit.")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RotplValidationError(f"Invalid {filename}: {exc}") from exc

    nodes = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise RotplValidationError(f"{filename} is too deeply nested or complex.")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise RotplValidationError(f"{filename} contains an invalid key.")
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, float) and not math.isfinite(item):
            raise RotplValidationError(f"{filename} contains a non-finite number.")

    walk(value, 0)
    return value


def _normalized_member_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise RotplValidationError("The archive contains an invalid member path.")
    if len(name) > 240 or any(ord(char) < 32 for char in name):
        raise RotplValidationError(f"Unsafe archive member path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts:
        raise RotplValidationError(f"Absolute archive paths are not allowed: {name}")
    clean_parts: list[str] = []
    for part in path.parts:
        if part in {"", ".", ".."} or len(part) > 128 or ":" in part:
            raise RotplValidationError(f"Unsafe archive member path: {name}")
        if part.rstrip(". ").casefold() in RESERVED_SEGMENTS:
            raise RotplValidationError(f"Reserved archive member path: {name}")
        clean_parts.append(unicodedata.normalize("NFC", part))
    return "/".join(clean_parts)


def _is_link_or_special(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    if not mode:
        return False
    kind = stat.S_IFMT(mode)
    return kind not in {0, stat.S_IFREG, stat.S_IFDIR}


def _archive_members(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(archive.infolist())
    if not infos or len(infos) > MAX_MEMBERS:
        raise RotplValidationError(
            f"The package must contain between 1 and {MAX_MEMBERS} members."
        )
    total_size = 0
    total_compressed = 0
    collision_keys: set[str] = set()
    files: list[zipfile.ZipInfo] = []
    for info in infos:
        normalized = _normalized_member_name(info.filename)
        collision_key = unicodedata.normalize("NFKC", normalized).casefold().rstrip("/ ")
        if collision_key in collision_keys:
            raise RotplValidationError(f"Duplicate archive member: {normalized}")
        collision_keys.add(collision_key)
        if _is_link_or_special(info):
            raise RotplValidationError(f"Links and special files are forbidden: {normalized}")
        if info.flag_bits & 0x1:
            raise RotplValidationError(f"Encrypted archive members are forbidden: {normalized}")
        if info.compress_type not in ALLOWED_COMPRESSION:
            raise RotplValidationError(f"Unsupported compression method: {normalized}")
        if info.is_dir():
            continue
        suffix = PurePosixPath(normalized).suffix.casefold()
        if suffix in FORBIDDEN_SUFFIXES or suffix not in ALLOWED_SUFFIXES:
            raise RotplValidationError(f"Forbidden or unsupported file type: {normalized}")
        if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
            raise RotplValidationError(f"Archive member is too large: {normalized}")
        ratio = info.file_size / max(info.compress_size, 1)
        if info.file_size > 1024 * 1024 and ratio > MAX_COMPRESSION_RATIO:
            raise RotplValidationError(f"Suspicious compression ratio: {normalized}")
        total_size += info.file_size
        total_compressed += info.compress_size
        files.append(info)
    if total_size > MAX_UNCOMPRESSED_BYTES:
        raise RotplValidationError("The expanded package exceeds the safe size limit.")
    if total_size > 8 * 1024 * 1024 and total_size / max(total_compressed, 1) > MAX_COMPRESSION_RATIO:
        raise RotplValidationError("The package has a suspicious aggregate compression ratio.")
    return tuple(files)


def _expect_mapping(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object.")
        return {}
    return value


def _expect_string(value: Any, label: str, errors: list[str], maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        errors.append(f"{label} must be a non-empty string (max {maximum} characters).")
        return ""
    return value.strip()


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate_canvas(canvas: Any, label: str, errors: list[str]) -> tuple[int, int]:
    canvas = _expect_mapping(canvas, label, errors)
    width, height = canvas.get("width"), canvas.get("height")
    if not isinstance(width, int) or isinstance(width, bool) or not 320 <= width <= 7680:
        errors.append(f"{label}.width must be an integer between 320 and 7680.")
        width = 0
    if not isinstance(height, int) or isinstance(height, bool) or not 180 <= height <= 4320:
        errors.append(f"{label}.height must be an integer between 180 and 4320.")
        height = 0
    return int(width or 0), int(height or 0)


def _binding_values(value: Any) -> set[str]:
    bindings: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if (key == "bind" or key.endswith("_bind")) and isinstance(child, str):
                bindings.add(child)
            bindings.update(_binding_values(child))
    elif isinstance(value, list):
        for child in value:
            bindings.update(_binding_values(child))
    return bindings


def _reject_executable_json(value: Any, label: str, errors: list[str]) -> None:
    """Reject executable or external-resource hooks anywhere in declarations."""
    if isinstance(value, dict):
        for key, child in value.items():
            folded = str(key).replace("_", "").replace("-", "").casefold()
            if folded in FORBIDDEN_JSON_KEYS or (
                folded.startswith("on")
                and folded[2:] in {"load", "click", "error", "message", "render"}
            ):
                errors.append(f"{label} contains forbidden executable key: {key}")
            _reject_executable_json(child, label, errors)
    elif isinstance(value, list):
        for child in value:
            _reject_executable_json(child, label, errors)
    elif isinstance(value, str):
        folded = value.strip().casefold()
        if folded.startswith(("javascript:", "vbscript:", "data:text/html")) or "<script" in folded:
            errors.append(f"{label} contains forbidden executable content.")


def _validate_manifest_layout(
    manifest: Any,
    layout: Any,
    member_names: set[str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    blocked: list[str] = []
    manifest = _expect_mapping(manifest, "manifest.json", errors)
    layout = _expect_mapping(layout, "layout.json", errors)
    _reject_executable_json(manifest, "manifest.json", errors)
    _reject_executable_json(layout, "layout.json", errors)

    if manifest.get("schema") != "rocket-overlay-template":
        errors.append("manifest.schema must equal 'rocket-overlay-template'.")
    schema_version = _expect_string(manifest.get("schema_version"), "manifest.schema_version", errors, 32)
    if schema_version and not VERSION_RE.fullmatch(schema_version):
        errors.append("manifest.schema_version must be a semantic version.")
    template_id = _expect_string(manifest.get("id"), "manifest.id", errors, 128)
    if template_id and not TEMPLATE_ID_RE.fullmatch(template_id):
        errors.append("manifest.id must use lowercase letters, digits, dots, dashes, or underscores.")
    version = _expect_string(manifest.get("template_version"), "manifest.template_version", errors, 64)
    if version and not VERSION_RE.fullmatch(version):
        errors.append("manifest.template_version must be a semantic version.")
    name = manifest.get("name")
    if isinstance(name, str):
        _expect_string(name, "manifest.name", errors, 200)
    elif isinstance(name, dict):
        if not name or not any(isinstance(item, str) and item.strip() for item in name.values()):
            errors.append("manifest.name must contain at least one localized name.")
        for language, localized in name.items():
            if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?", language):
                errors.append("manifest.name contains an invalid language key.")
            _expect_string(localized, f"manifest.name.{language}", errors, 200)
    else:
        errors.append("manifest.name must be a string or localized object.")

    manifest_width, manifest_height = _validate_canvas(manifest.get("canvas"), "manifest.canvas", errors)
    canvas = manifest.get("canvas") if isinstance(manifest.get("canvas"), dict) else {}
    if canvas.get("units") not in {None, "px"}:
        errors.append("manifest.canvas.units must be 'px'.")
    if canvas.get("color_space") not in {None, "sRGB", "Rec.709"}:
        errors.append("manifest.canvas.color_space must be sRGB or Rec.709.")
    if canvas.get("alpha_mode") not in {None, "straight"}:
        errors.append("manifest.canvas.alpha_mode must be 'straight'.")
    entry = _expect_string(manifest.get("entry"), "manifest.entry", errors, 200)
    if entry:
        try:
            entry = _normalized_member_name(entry)
        except RotplValidationError as exc:
            errors.append(str(exc))
        if entry not in member_names:
            errors.append(f"Manifest entry does not exist in the package: {entry}")
        if entry != "layout.json":
            errors.append("Version 1 packages must use layout.json as their entry.")

    engine = _expect_mapping(manifest.get("engine"), "manifest.engine", errors)
    if engine and engine.get("renderer") != "opencv-declarative-v1":
        errors.append("Only the opencv-declarative-v1 renderer is allowed.")

    fonts = manifest.get("fonts", [])
    font_ids: set[str] = set()
    if not isinstance(fonts, list) or len(fonts) > 32:
        errors.append("manifest.fonts must be an array with at most 32 entries.")
        fonts = []
    for index, font in enumerate(fonts):
        font = _expect_mapping(font, f"manifest.fonts[{index}]", errors)
        font_id = _expect_string(font.get("font_id"), f"manifest.fonts[{index}].font_id", errors, 64)
        if font_id:
            if not ELEMENT_ID_RE.fullmatch(font_id) or font_id.casefold() in {value.casefold() for value in font_ids}:
                errors.append(f"Invalid or duplicate font_id: {font_id}")
            font_ids.add(font_id)
        font_file = _expect_string(font.get("file"), f"manifest.fonts[{index}].file", errors, 200)
        if font_file:
            try:
                font_file = _normalized_member_name(font_file)
            except RotplValidationError as exc:
                errors.append(str(exc))
                continue
            if PurePosixPath(font_file).suffix.casefold() not in FONT_SUFFIXES:
                errors.append(f"Font file must be TTF or OTF: {font_file}")
            elif font_file not in member_names:
                reason = f"Missing declared font file: {font_file}"
                warnings.append(reason)
                blocked.append(reason)

    variables = manifest.get("variables", {})
    if not isinstance(variables, dict) or len(variables) > 256:
        errors.append("manifest.variables must be an object with at most 256 entries.")
        variables = {}
    for binding, definition in variables.items():
        if not isinstance(binding, str) or not BINDING_RE.fullmatch(binding):
            errors.append(f"Invalid variable binding name: {binding!r}")
        definition = _expect_mapping(definition, f"manifest.variables.{binding}", errors)
        if definition.get("type") not in {"text", "number", "color", "image", "boolean"}:
            errors.append(f"Unsupported variable type for {binding}.")
        if "required" in definition and not isinstance(definition["required"], bool):
            errors.append(f"Variable {binding}.required must be boolean.")

    binding_groups: dict[str, list[str]] = {}
    for field in ("required_bindings", "optional_bindings"):
        values = manifest.get(field, [])
        if not isinstance(values, list) or len(values) > 512:
            errors.append(f"manifest.{field} must be an array with at most 512 entries.")
            values = []
        clean: list[str] = []
        for value in values:
            if not isinstance(value, str) or not BINDING_RE.fullmatch(value):
                errors.append(f"manifest.{field} contains an invalid binding.")
            elif value in clean:
                errors.append(f"manifest.{field} contains duplicate binding {value}.")
            else:
                clean.append(value)
        binding_groups[field] = clean
    overlap = set(binding_groups.get("required_bindings", [])) & set(binding_groups.get("optional_bindings", []))
    if overlap:
        errors.append("Bindings cannot be both required and optional: " + ", ".join(sorted(overlap)))

    for field in ("required_channels", "optional_channels"):
        channels = manifest.get(field, [])
        if not isinstance(channels, list) or len(channels) > 64:
            errors.append(f"manifest.{field} must be an array with at most 64 entries.")
            continue
        if any(not isinstance(channel, str) or not ELEMENT_ID_RE.fullmatch(channel) for channel in channels):
            errors.append(f"manifest.{field} contains an invalid channel name.")
        if len(set(channels)) != len(channels):
            errors.append(f"manifest.{field} contains duplicate channels.")
    missing_data = manifest.get("missing_data", {})
    if not isinstance(missing_data, dict):
        errors.append("manifest.missing_data must be an object.")
    else:
        for channel, policy in missing_data.items():
            if not isinstance(channel, str) or policy not in MISSING_POLICIES:
                errors.append(f"Invalid missing-data policy for {channel}.")

    release_reason = manifest.get("release_blocked_reason")
    if release_reason:
        release_reason = _expect_string(release_reason, "manifest.release_blocked_reason", errors, 500)
        if release_reason:
            blocked.append(release_reason)

    if layout.get("schema") != "rocket-overlay-layout":
        errors.append("layout.schema must equal 'rocket-overlay-layout'.")
    layout_width, layout_height = _validate_canvas(layout.get("canvas"), "layout.canvas", errors)
    if (manifest_width, manifest_height) != (layout_width, layout_height):
        errors.append("The manifest and layout canvas dimensions must match.")
    layers = layout.get("layers")
    if not isinstance(layers, dict) or not layers:
        errors.append("layout.layers must be a non-empty object.")
    else:
        for layer_name, z_value in layers.items():
            if not isinstance(layer_name, str) or not ELEMENT_ID_RE.fullmatch(layer_name):
                errors.append(f"Invalid layout layer name: {layer_name!r}")
            if not _finite_number(z_value) or not -10_000 <= float(z_value) <= 10_000:
                errors.append(f"Invalid z value for layer {layer_name}.")

    elements = layout.get("elements")
    if not isinstance(elements, list) or not 1 <= len(elements) <= MAX_LAYOUT_ELEMENTS:
        errors.append(f"layout.elements must contain 1 to {MAX_LAYOUT_ELEMENTS} elements.")
        elements = []
    element_ids: set[str] = set()
    for index, element in enumerate(elements):
        element = _expect_mapping(element, f"layout.elements[{index}]", errors)
        element_id = _expect_string(element.get("id"), f"layout.elements[{index}].id", errors, 128)
        if element_id:
            folded = element_id.casefold()
            if not ELEMENT_ID_RE.fullmatch(element_id) or folded in element_ids:
                errors.append(f"Invalid or duplicate element id: {element_id}")
            element_ids.add(folded)
        element_type = element.get("type")
        if element_type not in SUPPORTED_ELEMENT_TYPES:
            errors.append(f"Unsupported element type for {element_id or index}: {element_type!r}")
        if not _finite_number(element.get("z")) or not -10_000 <= float(element.get("z", 0)) <= 10_000:
            errors.append(f"Element {element_id or index} has an invalid z value.")
        for coordinate in ("x", "y", "x2", "y2", "w", "h", "width", "height", "max_width", "max_height"):
            if coordinate in element and (not _finite_number(element[coordinate]) or abs(float(element[coordinate])) > 30_720):
                errors.append(f"Element {element_id or index} has invalid {coordinate}.")
        for opacity in ("opacity", "opacity_start", "opacity_end", "track_opacity", "stroke_opacity"):
            if opacity in element and (not _finite_number(element[opacity]) or not 0 <= float(element[opacity]) <= 1):
                errors.append(f"Element {element_id or index} has invalid {opacity}.")
        for color_key in ("color", "fill", "stroke", "track_color"):
            color = element.get(color_key)
            if color is not None and (not isinstance(color, str) or not COLOR_RE.fullmatch(color)):
                errors.append(f"Element {element_id or index} has invalid {color_key}.")
        if "missing_policy" in element and element["missing_policy"] not in MISSING_POLICIES:
            errors.append(f"Element {element_id or index} has an invalid missing_policy.")
        if element_type == "text":
            if "text" not in element and "bind" not in element:
                errors.append(f"Text element {element_id or index} requires text or bind.")
            font_id = element.get("font_id")
            if not isinstance(font_id, str) or font_id not in font_ids:
                errors.append(f"Text element {element_id or index} references an unknown font_id.")
            if not _finite_number(element.get("font_size")) or float(element.get("font_size", 0)) <= 0:
                errors.append(f"Text element {element_id or index} has an invalid font_size.")
        if element_type in {"bar_gauge", "vertical_gauge", "arc_gauge", "phase_list", "chart"} and "bind" not in element:
            errors.append(f"Dynamic element {element_id or index} requires bind.")

    used_bindings = sorted(_binding_values(layout))
    for binding in used_bindings:
        if not BINDING_RE.fullmatch(binding):
            errors.append(f"Layout contains an invalid binding: {binding}")
    for binding in binding_groups.get("required_bindings", []):
        if binding not in used_bindings:
            errors.append(f"Required binding is not used by the layout: {binding}")

    return errors, warnings, blocked, used_bindings


def _validate_images_and_fonts(
    archive: zipfile.ZipFile,
    members: Iterable[zipfile.ZipInfo],
    errors: list[str],
) -> None:
    for info in members:
        name = _normalized_member_name(info.filename)
        suffix = PurePosixPath(name).suffix.casefold()
        if suffix in IMAGE_SUFFIXES:
            try:
                raw = archive.read(info)
                with Image.open(io.BytesIO(raw)) as image:
                    width, height = image.size
                    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                        errors.append(f"Image dimensions exceed the safe limit: {name}")
                    image.verify()
            except (
                OSError,
                UnidentifiedImageError,
                ValueError,
                Image.DecompressionBombError,
            ) as exc:
                errors.append(f"Invalid image asset {name}: {exc}")
        elif suffix in FONT_SUFFIXES:
            try:
                with archive.open(info) as stream:
                    signature = stream.read(4)
                if signature not in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"}:
                    errors.append(f"Invalid font asset: {name}")
            except (OSError, zipfile.BadZipFile) as exc:
                errors.append(f"Invalid font asset {name}: {exc}")


def _member_hashes(
    archive: zipfile.ZipFile, members: Iterable[zipfile.ZipInfo]
) -> dict[str, str]:
    result: dict[str, str] = {}
    for info in members:
        digest = hashlib.sha256()
        with archive.open(info, "r") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result[_normalized_member_name(info.filename)] = digest.hexdigest()
    return result


def _validate_declared_font_hashes(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    member_names: set[str],
    file_hashes: dict[str, str],
    errors: list[str],
    warnings: list[str],
) -> None:
    for index, font in enumerate(manifest.get("fonts", [])):
        if not isinstance(font, dict) or not isinstance(font.get("file"), str):
            continue
        try:
            filename = _normalized_member_name(font["file"])
        except RotplValidationError:
            continue
        if filename not in member_names:
            continue
        expected = font.get("sha256")
        if expected is None:
            warnings.append(
                f"Declared font has no sha256 integrity value: {filename}"
            )
        elif not isinstance(expected, str) or not re.fullmatch(r"[0-9A-Fa-f]{64}", expected):
            errors.append(f"Invalid sha256 value for font: {filename}")
        elif file_hashes.get(filename) != expected.casefold():
            errors.append(f"Font sha256 mismatch: {filename}")
        expected_size = font.get("size_bytes")
        if expected_size is not None:
            if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 1:
                errors.append(f"Invalid size_bytes value for font: {filename}")
            else:
                try:
                    actual_size = archive.getinfo(filename).file_size
                except KeyError:
                    continue
                if actual_size != expected_size:
                    errors.append(f"Font size_bytes mismatch: {filename}")


def validate_rotpl(path: Path) -> ValidatedPackage:
    path = Path(path)
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        raise RotplValidationError(f"Unable to read template package: {exc}") from exc
    if not 1 <= size_bytes <= MAX_ARCHIVE_BYTES:
        raise RotplValidationError(
            f"The .rotpl package must be no larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB."
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if stream.read(4) != b"PK\x03\x04":
            raise RotplValidationError(
                "The .rotpl file must be a plain ZIP package, not a self-extracting or polyglot file."
            )
        stream.seek(0)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = _archive_members(archive)
            names = {_normalized_member_name(info.filename) for info in members}
            required = {"manifest.json", "layout.json"}
            missing = required - names
            if missing:
                raise RotplValidationError(
                    "The package root is missing: " + ", ".join(sorted(missing))
                )
            manifest = _strict_json_loads(archive.read("manifest.json"), "manifest.json")
            layout = _strict_json_loads(archive.read("layout.json"), "layout.json")
            errors, warnings, blocked, used_bindings = _validate_manifest_layout(
                manifest, layout, names
            )
            _validate_images_and_fonts(archive, members, errors)
            file_hashes = _member_hashes(archive, members)
            _validate_declared_font_hashes(
                archive, manifest, names, file_hashes, errors, warnings
            )
            bad_member = archive.testzip()
            if bad_member:
                errors.append(f"Archive CRC check failed: {bad_member}")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError) as exc:
        raise RotplValidationError(f"Invalid .rotpl archive: {exc}") from exc

    report = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "activatable": not errors and not blocked,
        "blocked_reasons": blocked,
    }
    if errors:
        raise RotplValidationError("Template validation failed.", report)
    return ValidatedPackage(
        manifest=manifest,
        layout=layout,
        members=members,
        report=report,
        sha256=digest.hexdigest(),
        size_bytes=size_bytes,
        used_bindings=tuple(used_bindings),
        file_sha256=file_hashes,
    )


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _version_sort_key(version: str) -> tuple[Any, ...]:
    match = VERSION_RE.fullmatch(version)
    if not match:
        return (0, 0, 0, -1, version)
    prerelease = match.group(4)
    parts: list[tuple[int, Any]] = []
    if prerelease:
        for item in prerelease.split("."):
            parts.append((0, int(item)) if item.isdigit() else (1, item.casefold()))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 1 if not prerelease else 0, tuple(parts))


class RotplRegistry:
    """Immutable package storage plus an atomic active-template pointer."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.packages_dir = self.root / "packages"
        self.staging_dir = self.root / "staging"
        self.state_path = self.root / "state.json"
        self._lock = threading.RLock()
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            _atomic_json(self.state_path, {"schema_version": 1, "active": None, "history": []})

    def _state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RotplError(f"Template registry state is unreadable: {exc}") from exc
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise RotplError("Template registry state has an unsupported format.")
        state.setdefault("active", None)
        state.setdefault("history", [])
        return state

    def _record_path(self, template_id: str, version: str) -> Path:
        self._validate_identity(template_id, version)
        return self.packages_dir / template_id / version / "record.json"

    @staticmethod
    def _validate_identity(template_id: str, version: str) -> None:
        if not TEMPLATE_ID_RE.fullmatch(template_id or ""):
            raise RotplValidationError("Invalid template id.")
        if not VERSION_RE.fullmatch(version or ""):
            raise RotplValidationError("Invalid template version.")

    def _read_record(self, path: Path) -> dict[str, Any]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RotplError(f"Installed template record is unreadable: {exc}") from exc
        if not isinstance(record, dict):
            raise RotplError("Installed template record is invalid.")
        return record

    def _extract(self, source: Path, validated: ValidatedPackage, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(source, "r") as archive:
            for info in validated.members:
                relative = PurePosixPath(_normalized_member_name(info.filename))
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info, "r") as input_stream, target.open("xb") as output_stream:
                    while True:
                        chunk = input_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size or written > MAX_MEMBER_BYTES:
                            raise RotplValidationError(f"Archive member expanded unexpectedly: {relative}")
                        output_stream.write(chunk)
                if written != info.file_size:
                    raise RotplValidationError(f"Archive member size mismatch: {relative}")

    def install_stream(self, stream: BinaryIO, filename: str) -> dict[str, Any]:
        if Path(filename or "").suffix.casefold() != ".rotpl":
            raise RotplValidationError("Template packages must use the .rotpl extension.")
        staging = self.staging_dir / f"upload-{uuid.uuid4().hex}.rotpl"
        written = 0
        try:
            with staging.open("xb") as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise RotplValidationError(
                            f"The .rotpl package must be no larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB."
                        )
                    output.write(chunk)
            return self.install_file(staging, original_filename=Path(filename).name)
        finally:
            staging.unlink(missing_ok=True)

    def install_file(self, source: Path, original_filename: str | None = None) -> dict[str, Any]:
        source = Path(source)
        validated = validate_rotpl(source)
        template_id = str(validated.manifest["id"])
        version = str(validated.manifest["template_version"])
        self._validate_identity(template_id, version)
        destination = self.packages_dir / template_id / version
        record_path = destination / "record.json"
        with self._lock:
            if record_path.exists():
                existing = self._read_record(record_path)
                if existing.get("sha256") == validated.sha256:
                    return self._with_status(existing)
                raise RotplConflictError(
                    f"Template {template_id} version {version} is immutable and already contains different bytes."
                )
            if destination.exists():
                raise RotplConflictError(f"Template destination already exists: {template_id} {version}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage_dir = destination.parent / f".{version}.{uuid.uuid4().hex}.stage"
            try:
                files_dir = stage_dir / "files"
                self._extract(source, validated, files_dir)
                shutil.copyfile(source, stage_dir / "package.rotpl")
                name = validated.manifest.get("name")
                description = validated.manifest.get("description", "")
                record = {
                    "id": template_id,
                    "version": version,
                    "name": name,
                    "description": description,
                    "package_filename": original_filename or source.name,
                    "sha256": validated.sha256,
                    "file_sha256": validated.file_sha256,
                    "size_bytes": validated.size_bytes,
                    "installed_at": _utc_now(),
                    "validation": validated.report,
                    "bindings": list(validated.used_bindings),
                    "bindings_count": len(validated.used_bindings),
                    "variables": validated.manifest.get("variables", {}),
                    "required_channels": validated.manifest.get("required_channels", []),
                    "optional_channels": validated.manifest.get("optional_channels", []),
                    "canvas": validated.manifest.get("canvas", {}),
                    "renderer": validated.manifest.get("engine", {}).get("renderer"),
                    "has_thumbnail": "preview/thumbnail.png" in {
                        _normalized_member_name(info.filename) for info in validated.members
                    },
                }
                _atomic_json(stage_dir / "record.json", record)
                os.replace(stage_dir, destination)
            except Exception:
                shutil.rmtree(stage_dir, ignore_errors=True)
                raise
            return self._with_status(record)

    def _with_status(self, record: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
        state = state or self._state()
        result = dict(record)
        active = state.get("active")
        result["status"] = (
            "active"
            if isinstance(active, dict)
            and active.get("id") == record.get("id")
            and active.get("version") == record.get("version")
            else "draft"
        )
        result["active"] = result["status"] == "active"
        result["installed_path"] = (
            f"packages/{record.get('id', '')}/{record.get('version', '')}"
        )
        return result

    def list(self) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            records = [
                self._with_status(self._read_record(path), state)
                for path in self.packages_dir.glob("*/*/record.json")
                if path.is_file()
            ]
            records.sort(
                key=lambda item: (item.get("id", ""), _version_sort_key(item.get("version", ""))),
                reverse=True,
            )
            return {"templates": records, "active": state.get("active")}

    def get(self, template_id: str, version: str | None = None) -> dict[str, Any]:
        self._validate_identity(template_id, version or "0.0.0")
        with self._lock:
            state = self._state()
            if version is None:
                active = state.get("active")
                if isinstance(active, dict) and active.get("id") == template_id:
                    version = str(active["version"])
                else:
                    candidates = [path.parent.name for path in (self.packages_dir / template_id).glob("*/record.json")]
                    if not candidates:
                        raise RotplNotFoundError(f"Template not found: {template_id}")
                    version = max(candidates, key=_version_sort_key)
            self._validate_identity(template_id, version)
            record_path = self._record_path(template_id, version)
            if not record_path.is_file():
                raise RotplNotFoundError(f"Template not found: {template_id} {version}")
            return self._with_status(self._read_record(record_path), state)

    def manifest_and_layout(self, template_id: str, version: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self.get(template_id, version)
        files = self.packages_dir / record["id"] / record["version"] / "files"
        try:
            manifest = _strict_json_loads((files / "manifest.json").read_bytes(), "manifest.json")
            layout = _strict_json_loads((files / "layout.json").read_bytes(), "layout.json")
        except OSError as exc:
            raise RotplError(f"Installed template files are unreadable: {exc}") from exc
        return manifest, layout

    @staticmethod
    def _sha256_path(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def resolve(
        self,
        template_id: str | None = None,
        version: str | None = None,
    ) -> ResolvedTemplate:
        """Resolve and integrity-check one exact renderer input.

        With no arguments this resolves the active pointer.  Explicit use must
        provide both id and version; silently selecting the newest draft would
        make an export impossible to reproduce later.
        """
        with self._lock:
            if template_id is None and version is None:
                active = self._state().get("active")
                if not isinstance(active, dict):
                    raise RotplNotFoundError("No template is currently active.")
                template_id = str(active.get("id", ""))
                version = str(active.get("version", ""))
            elif template_id is None or version is None:
                raise RotplValidationError(
                    "Both template id and version are required for an explicit selection."
                )
            record = self.get(template_id, version)
            root = self.packages_dir / template_id / version
            package_path = root / "package.rotpl"
            files_path = root / "files"
            try:
                if self._sha256_path(package_path) != record.get("sha256"):
                    raise RotplActivationError(
                        "Installed template package failed its integrity check."
                    )
                expected_files = record.get("file_sha256")
                if not isinstance(expected_files, dict) or not expected_files:
                    raise RotplActivationError(
                        "Installed template has no per-file integrity manifest."
                    )
                for relative, expected_hash in expected_files.items():
                    normalized = _normalized_member_name(str(relative))
                    target = files_path.joinpath(*PurePosixPath(normalized).parts)
                    if not target.is_file() or self._sha256_path(target) != expected_hash:
                        raise RotplActivationError(
                            f"Installed template file failed its integrity check: {normalized}"
                        )
            except OSError as exc:
                raise RotplActivationError(
                    f"Installed template files are unavailable: {exc}"
                ) from exc
            return ResolvedTemplate(
                id=template_id,
                version=version,
                sha256=str(record["sha256"]),
                files_path=files_path,
                package_path=package_path,
                manifest_path=files_path / "manifest.json",
                layout_path=files_path / "layout.json",
            )

    def activate(self, template_id: str, version: str) -> dict[str, Any]:
        with self._lock:
            record = self.get(template_id, version)
            validation = record.get("validation", {})
            if not validation.get("valid", False):
                raise RotplActivationError("The template did not pass validation.")
            if not validation.get("activatable", False):
                reasons = validation.get("blocked_reasons") or ["The package is release-blocked."]
                raise RotplActivationError("Template activation is blocked: " + "; ".join(str(item) for item in reasons))
            self.resolve(template_id, version)
            state = self._state()
            selected = {
                "id": template_id,
                "version": version,
                "sha256": record["sha256"],
                "activated_at": _utc_now(),
            }
            current = state.get("active")
            if isinstance(current, dict) and current.get("id") == template_id and current.get("version") == version:
                return current
            if current:
                state["history"].append(current)
                state["history"] = state["history"][-50:]
            state["active"] = selected
            _atomic_json(self.state_path, state)
            return selected

    def rollback(self) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            history = state.get("history", [])
            if not history:
                raise RotplActivationError("There is no previous active template to restore.")
            previous = history.pop()
            # Do not trust stale state blindly; immutable package files must
            # still exist and retain the recorded digest.
            record = self.get(str(previous.get("id", "")), str(previous.get("version", "")))
            if record.get("sha256") != previous.get("sha256"):
                raise RotplActivationError("The previous template package is unavailable or changed.")
            self.resolve(record["id"], record["version"])
            previous = dict(previous)
            previous["activated_at"] = _utc_now()
            state["active"] = previous
            state["history"] = history
            _atomic_json(self.state_path, state)
            return previous

    def asset_path(self, template_id: str, version: str, relative: str) -> Path:
        record = self.get(template_id, version)
        normalized = _normalized_member_name(relative)
        root = (self.packages_dir / record["id"] / record["version"] / "files").resolve()
        target = root.joinpath(*PurePosixPath(normalized).parts).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise RotplValidationError("Invalid template asset path.") from exc
        if not target.is_file():
            raise RotplNotFoundError("Template asset not found.")
        return target
