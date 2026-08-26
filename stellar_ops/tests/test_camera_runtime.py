import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from stellar_ops import camera_runtime


class CameraRuntimeTests(unittest.TestCase):
    def setUp(self):
        camera_runtime.shutdown_shared_ingests()
        camera_runtime._statuses.clear()
        camera_runtime._secret_presence.clear()

    def test_credentials_are_injected_without_exposing_reserved_characters(self):
        value = camera_runtime._credential_url(
            "rtsp://192.168.1.64:554/Streaming/Channels/102",
            "camera user",
            "p@ss:/word",
        )
        self.assertEqual(
            value,
            "rtsp://camera%20user:p%40ss%3A%2Fword@192.168.1.64:554/Streaming/Channels/102",
        )

    def test_environment_secret_fallback(self):
        with patch.dict(os.environ, {"STELLAR_CAMERA_CAM_01_PASSWORD": "secret"}, clear=False):
            self.assertEqual(camera_runtime.load_password("CAM-01"), "secret")

    @patch("stellar_ops.camera_runtime._probe_frame", return_value=(True, "decoded", 12.5))
    @patch(
        "stellar_ops.camera_runtime._onvif_discover",
        return_value=(
            "rtsp://192.168.1.64/Streaming/Channels/101",
            "rtsp://192.168.1.64/Streaming/Channels/102",
            "Hikvision",
            "TEST-CAMERA",
        ),
    )
    @patch("stellar_ops.camera_runtime.load_password", return_value="secret")
    def test_authenticated_onvif_probe_promotes_camera_to_streaming(self, *_mocks):
        result = camera_runtime.test_camera("CAM-01", "ONVIF", "http://192.168.1.64", "smtcscamera")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "STREAMING")
        self.assertEqual(camera_runtime.camera_status("CAM-01")["status"], "STREAMING")
        self.assertEqual(result.preview_url, "rtsp://192.168.1.64/Streaming/Channels/102")

    @patch("stellar_ops.camera_runtime.load_password", return_value=None)
    def test_missing_secret_is_not_reported_as_reachable(self, _mock):
        result = camera_runtime.test_camera("CAM-01", "ONVIF", "http://192.168.1.64", "smtcscamera")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "MISSING_CREDENTIALS")

    def test_native_camera_recording_is_finalized(self):
        class FakeProcess:
            def __init__(self, command, **_kwargs):
                self.output = Path(command[-1])
                self.return_code = None

            def poll(self):
                return self.return_code

            def send_signal(self, _signal):
                self.output.write_bytes(b"matroska-video")
                self.return_code = 0

            def wait(self, timeout=None):
                return self.return_code

        with tempfile.TemporaryDirectory() as directory, \
                patch("stellar_ops.camera_runtime.load_password", return_value="secret"), \
                patch("stellar_ops.camera_runtime.camera_status", return_value={"main_url": "rtsp://camera/main"}), \
                patch("stellar_ops.camera_runtime._ffmpeg_executable", return_value="ffmpeg"), \
                patch("stellar_ops.camera_runtime.subprocess.Popen", side_effect=FakeProcess), \
                patch("stellar_ops.camera_runtime.time.sleep"):
            started = camera_runtime.start_camera_recordings([
                {"device_id": "CAM-01", "adapter": "ONVIF", "endpoint": "http://camera", "username": "operator"}
            ], Path(directory), 42)
            self.assertEqual(started[0]["state"], "RECORDING")
            stopped = camera_runtime.stop_camera_recordings(42)
            self.assertEqual(stopped[0]["state"], "RECORDED")
            self.assertEqual(Path(stopped[0]["file"]).read_bytes(), b"matroska-video")

    def test_browser_consumers_share_one_camera_ingest_session(self):
        source_started = threading.Event()
        release_source = threading.Event()
        sessions = []

        def fake_source(*_args):
            sessions.append("opened")
            source_started.set()
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\nshared\r\n"
            release_source.wait(timeout=2)

        with patch("stellar_ops.camera_runtime._camera_source_frames", side_effect=fake_source):
            first = camera_runtime.mjpeg_frames(
                "CAM-01", "ONVIF", "http://camera", "operator", "preview"
            )
            self.assertEqual(next(first)[-8:], b"shared\r\n")
            self.assertTrue(source_started.wait(timeout=1))

            second = camera_runtime.mjpeg_frames(
                "CAM-01", "ONVIF", "http://camera", "operator", "preview"
            )
            self.assertEqual(next(second)[-8:], b"shared\r\n")

            status = camera_runtime.shared_ingest_status()
            self.assertEqual(len(status), 1)
            self.assertEqual(status[0]["subscribers"], 2)
            self.assertEqual(status[0]["source_sessions"], 1)
            self.assertEqual(len(sessions), 1)

            first.close()
            second.close()
            release_source.set()


if __name__ == "__main__":
    unittest.main()
