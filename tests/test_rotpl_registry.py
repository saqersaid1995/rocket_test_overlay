import io
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app import app
from rotpl_registry import (
    RotplActivationError,
    RotplConflictError,
    RotplRegistry,
    RotplValidationError,
    validate_rotpl,
)


def package_bytes(
    version="1.0.0",
    *,
    include_font=True,
    release_blocked_reason=None,
    extra_members=None,
    title="Reference",
    font_hash=True,
):
    font_bytes = b"\x00\x01\x00\x00test-font"
    font_declaration = {
        "font_id": "display-bold",
        "file": "fonts/Display-Bold.ttf",
        "family": "Display",
        "weight": 700,
    }
    if font_hash:
        font_declaration.update({
            "sha256": hashlib.sha256(font_bytes).hexdigest(),
            "size_bytes": len(font_bytes),
        })
    manifest = {
        "schema": "rocket-overlay-template",
        "schema_version": "1.0.0",
        "id": "test.stellar.template",
        "template_version": version,
        "name": {"ar": "قالب اختبار", "en": "Test Template"},
        "description": "A safe declarative test package",
        "engine": {
            "renderer": "opencv-declarative-v1",
            "minimum_version": "2.0.0",
        },
        "canvas": {
            "width": 1920,
            "height": 1080,
            "aspect_ratio": "16:9",
            "units": "px",
            "color_space": "sRGB",
            "alpha_mode": "straight",
        },
        "entry": "layout.json",
        "fonts": [font_declaration],
        "font_fallback": "none",
        "variables": {
            "test.title": {"type": "text", "required": True, "default": title}
        },
        "required_bindings": ["test.title"],
        "optional_bindings": [],
        "required_channels": ["pressure"],
        "optional_channels": ["thrust"],
        "missing_data": {"thrust": "show_na"},
    }
    if release_blocked_reason:
        manifest["release_blocked_reason"] = release_blocked_reason
    layout = {
        "schema": "rocket-overlay-layout",
        "canvas": {"width": 1920, "height": 1080},
        "layers": {"video": 0, "dynamic": 50},
        "elements": [{
            "id": "mission_title",
            "type": "text",
            "z": 50,
            "bind": "test.title",
            "x": 100,
            "y": 100,
            "font_id": "display-bold",
            "font_size": 42,
            "color": "#FFFFFF",
        }],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("layout.json", json.dumps(layout))
        if include_font:
            archive.writestr("fonts/Display-Bold.ttf", font_bytes)
        for name, value in extra_members or []:
            if isinstance(value, zipfile.ZipInfo):
                archive.writestr(value, b"link")
            else:
                archive.writestr(name, value)
    return output.getvalue()


class RotplRegistryTests(unittest.TestCase):
    def install(self, registry, raw, name="template.rotpl"):
        return registry.install_stream(io.BytesIO(raw), name)

    def test_install_is_immutable_and_activation_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = RotplRegistry(Path(directory))
            first = self.install(registry, package_bytes("1.0.0"))
            self.assertEqual(first["status"], "draft")
            self.assertTrue(first["validation"]["activatable"])
            self.assertEqual(first["bindings"], ["test.title"])

            active_first = registry.activate(first["id"], first["version"])
            self.assertEqual(active_first["version"], "1.0.0")
            resolved = registry.resolve()
            self.assertEqual(resolved.sha256, first["sha256"])
            self.assertTrue(resolved.layout_path.is_file())
            second = self.install(registry, package_bytes("1.1.0"))
            registry.activate(second["id"], second["version"])
            self.assertEqual(registry.list()["active"]["version"], "1.1.0")

            restored = registry.rollback()
            self.assertEqual(restored["version"], "1.0.0")
            self.assertEqual(registry.list()["active"]["version"], "1.0.0")

            same = self.install(registry, package_bytes("1.0.0"))
            self.assertEqual(same["sha256"], first["sha256"])
            with self.assertRaises(RotplConflictError):
                self.install(
                    registry,
                    package_bytes("1.0.0", title="Changed immutable package"),
                )

            resolved.layout_path.write_text("tampered", encoding="utf-8")
            with self.assertRaises(RotplActivationError):
                registry.resolve(first["id"], first["version"])

    def test_missing_declared_font_installs_as_blocked_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = RotplRegistry(Path(directory))
            record = self.install(
                registry,
                package_bytes(
                    include_font=False,
                    release_blocked_reason="Golden reference is provisional.",
                ),
            )
            self.assertTrue(record["validation"]["valid"])
            self.assertFalse(record["validation"]["activatable"])
            self.assertTrue(record["validation"]["warnings"])
            with self.assertRaises(RotplActivationError):
                registry.activate(record["id"], record["version"])

    def test_rejects_traversal_code_symlinks_and_wrong_extension(self):
        cases = [
            package_bytes(extra_members=[("../escape.txt", b"escape")]),
            package_bytes(extra_members=[("assets/execute.py", b"print('bad')")]),
        ]
        link = zipfile.ZipInfo("assets/link.txt")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        cases.append(package_bytes(extra_members=[("ignored", link)]))
        with tempfile.TemporaryDirectory() as directory:
            registry = RotplRegistry(Path(directory))
            for raw in cases:
                with self.subTest(index=cases.index(raw)):
                    with self.assertRaises(RotplValidationError):
                        self.install(registry, raw)
            with self.assertRaises(RotplValidationError):
                self.install(registry, package_bytes(), "template.zip")
            self.assertFalse(any((Path(directory) / "packages").glob("*/*/record.json")))

    def test_duplicate_json_keys_fail_strict_validation(self):
        raw = package_bytes()
        input_archive = zipfile.ZipFile(io.BytesIO(raw), "r")
        malformed = io.BytesIO()
        with zipfile.ZipFile(malformed, "w") as output:
            output.writestr(
                "manifest.json",
                '{"schema":"rocket-overlay-template","schema":"duplicate"}',
            )
            output.writestr("layout.json", input_archive.read("layout.json"))
        input_archive.close()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.rotpl"
            path.write_bytes(malformed.getvalue())
            with self.assertRaises(RotplValidationError):
                validate_rotpl(path)

    def test_declared_font_hash_is_enforced(self):
        raw = package_bytes()
        source = zipfile.ZipFile(io.BytesIO(raw), "r")
        manifest = json.loads(source.read("manifest.json"))
        manifest["fonts"][0]["sha256"] = "0" * 64
        changed = io.BytesIO()
        with zipfile.ZipFile(changed, "w") as output:
            for info in source.infolist():
                value = source.read(info)
                if info.filename == "manifest.json":
                    value = json.dumps(manifest).encode()
                output.writestr(info.filename, value)
        source.close()
        with tempfile.TemporaryDirectory() as directory:
            registry = RotplRegistry(Path(directory))
            with self.assertRaises(RotplValidationError) as caught:
                self.install(registry, changed.getvalue())
            self.assertTrue(
                any("sha256 mismatch" in message for message in caught.exception.report["errors"])
            )


class RotplApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = RotplRegistry(Path(self.temp.name))
        self.patch = mock.patch("app.template_registry", self.registry)
        self.patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def upload(self, raw, name="stellar.rotpl"):
        return self.client.post(
            "/api/templates",
            data={"template": (io.BytesIO(raw), name)},
            content_type="multipart/form-data",
        )

    def test_upload_list_details_and_activate_contract(self):
        response = self.upload(package_bytes())
        self.assertEqual(response.status_code, 201)
        record = response.get_json()["template"]
        self.assertEqual(record["id"], "test.stellar.template")
        self.assertEqual(record["status"], "draft")
        self.assertIn("validation", record)

        listing = self.client.get("/api/templates")
        self.assertEqual(listing.status_code, 200)
        self.assertIsNone(listing.get_json()["active"])
        self.assertEqual(len(listing.get_json()["templates"]), 1)

        details = self.client.get(
            "/api/templates/test.stellar.template?version=1.0.0"
        )
        self.assertEqual(details.status_code, 200)
        self.assertEqual(details.get_json()["layout"]["schema"], "rocket-overlay-layout")

        activated = self.client.post(
            "/api/templates/test.stellar.template/activate",
            json={"version": "1.0.0"},
        )
        self.assertEqual(activated.status_code, 200)
        self.assertEqual(activated.get_json()["active"]["version"], "1.0.0")

    def test_invalid_upload_returns_structured_validation_report(self):
        response = self.upload(
            package_bytes(extra_members=[("unsafe.js", b"alert(1)")])
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertIn("error", payload)
        self.assertFalse(payload["validation"]["valid"])


if __name__ == "__main__":
    unittest.main()
