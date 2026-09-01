import hashlib
import tempfile
import unittest
from pathlib import Path

from stellar_ops.evidence import video_evidence


class CameraEvidenceTests(unittest.TestCase):
    def test_video_files_receive_immutable_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera-cam-01-session-000042.mkv"
            payload = b"test-video-evidence"
            path.write_bytes(payload)
            result = video_evidence(Path(directory))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["file"], path.name)
            self.assertEqual(result[0]["bytes"], len(payload))
            self.assertEqual(result[0]["file_sha256"], hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()
