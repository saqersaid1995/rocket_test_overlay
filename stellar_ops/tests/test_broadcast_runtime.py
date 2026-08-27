import http.server
import io
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from stellar_ops import broadcast_runtime


class BroadcastRuntimeTests(unittest.TestCase):
    def tearDown(self):
        broadcast_runtime._outputs.clear()
        broadcast_runtime._program_recording = None

    @patch("stellar_ops.broadcast_runtime._ffmpeg_executable", return_value="ffmpeg")
    def test_encoder_consumes_composited_program_bus_not_camera_rtsp(self, _ffmpeg):
        cameras = [{"device_id": "CAM-01", "username": "operator"}]
        scene = {"name": "Test Stand", "sources": [{"kind": "camera", "source": "CAM-01"}]}
        command = broadcast_runtime._program_command(
            cameras, scene, "rtmps://example/live/key",
            source_url="http://127.0.0.1:5001/api/media/bus/program/stream.mjpg",
        )
        joined = " ".join(command)
        self.assertIn("-f mpjpeg", joined)
        self.assertIn("/api/media/bus/program/stream.mjpg", joined)
        self.assertNotIn("rtsp://", joined)
        self.assertNotIn("operator", joined)
        self.assertIn("-progress pipe:2", joined)
        self.assertNotIn(" -an", joined)
        self.assertIn("-c:a aac", joined)
        self.assertIn("anullsrc=r=48000:cl=stereo", joined)

    def test_configured_audio_source_is_mapped_to_program(self):
        with patch.dict("os.environ", {"STELLAR_BROADCAST_AUDIO_SOURCE": "rtsp://audio.local/live"}):
            with patch("stellar_ops.broadcast_runtime._ffmpeg_executable", return_value="ffmpeg"):
                command = broadcast_runtime._program_command([], {}, "output.mkv")
        joined = " ".join(command)
        self.assertIn("-rtsp_transport tcp -i rtsp://audio.local/live", joined)
        self.assertIn("-map 1:a:0", joined)

    def test_program_bus_url_uses_local_application_port(self):
        with patch.dict("os.environ", {"PORT": "5111", "STELLAR_PROGRAM_BUS_URL": ""}):
            self.assertEqual(
                broadcast_runtime.program_bus_url(),
                "http://127.0.0.1:5111/api/media/bus/program/stream.mjpg",
            )

    def test_encoder_progress_is_exposed_as_real_runtime_metrics(self):
        class Progress:
            def __init__(self):
                self.lines = iter(["frame=90\n", "fps=29.9\n", "bitrate=4480kbits/s\n",
                                   "drop_frames=2\n", "speed=1.00x\n"])

            def readline(self):
                return next(self.lines, "")

        class Process:
            stderr = Progress()

            @staticmethod
            def wait():
                return 0

        broadcast_runtime._outputs[7] = {
            "state": "STREAMING", "started_at": 1, "reconnects": 0,
            "device_ids": ["CAM-01"],
        }
        self.assertEqual(broadcast_runtime._capture_progress(7, Process()), 0)
        metrics = broadcast_runtime.output_metrics(7)
        self.assertEqual(metrics["frame"], "90")
        self.assertEqual(metrics["fps"], "29.9")
        self.assertEqual(metrics["drop_frames"], "2")
        self.assertEqual(metrics["failovers"], 0)

    def test_ffmpeg_records_decodable_video_from_program_bus(self):
        output = io.BytesIO()
        Image.new("RGB", (320, 180), (20, 150, 220)).save(output, "JPEG")
        jpeg = output.getvalue()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    for _ in range(35):
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                        )
                        self.wfile.flush()
                        time.sleep(0.01)
                except BrokenPipeError:
                    pass

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "program.mkv"
                source = f"http://127.0.0.1:{server.server_port}/program.mjpg"
                completed = subprocess.run(
                    broadcast_runtime._program_command([], {}, str(target), source_url=source),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=True, timeout=15,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
                self.assertGreater(target.stat().st_size, 3_000)
                import cv2
                capture = cv2.VideoCapture(str(target))
                ok, frame = capture.read()
                capture.release()
                self.assertTrue(ok)
                self.assertEqual(frame.shape[:2], (720, 1280))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
