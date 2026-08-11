import shutil
import subprocess
import unittest
from pathlib import Path


class PreviewRasterQueueTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser queue tests")
    def test_slow_preview_queue_does_not_starve_or_show_stale_frames(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", str(root / "tests" / "preview_raster_queue.test.mjs")],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        details = "\n".join(part for part in (result.stdout, result.stderr) if part)
        self.assertEqual(result.returncode, 0, details)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
    def test_template_package_frontend_contract(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", str(root / "tests" / "template_package_frontend.test.mjs")],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        details = "\n".join(part for part in (result.stdout, result.stderr) if part)
        self.assertEqual(result.returncode, 0, details)


if __name__ == "__main__":
    unittest.main()
