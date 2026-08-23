import os
import unittest
from unittest.mock import patch

from stellar_ops import camera_runtime


class CameraRuntimeTests(unittest.TestCase):
    def setUp(self):
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


if __name__ == "__main__":
    unittest.main()
