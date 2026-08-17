import tempfile
import unittest
from pathlib import Path

from stellar_ops.app import app
import stellar_ops.control as control_module


class MediaControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = control_module.CONTROL_DB
        control_module.CONTROL_DB = Path(self.temp.name) / "control.db"
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        control_module.CONTROL_DB = self.original
        self.temp.cleanup()

    def test_media_console_and_independent_display_outputs(self):
        console = self.client.get("/media")
        self.assertEqual(console.status_code, 200)
        self.assertIn(b"DISPLAY, VIDEO & BROADCAST SYSTEM", console.data)
        state = self.client.get("/api/media/snapshot").get_json()
        self.assertGreaterEqual(len(state["graph_definitions"]), 3)
        self.assertGreaterEqual(len(state["display_pages"]), 3)
        self.assertGreaterEqual(len(state["broadcast_scenes"]), 5)
        self.assertEqual(self.client.get("/display/propulsion").status_code, 200)
        self.assertEqual(self.client.get("/display/not-real").status_code, 404)

    def test_multiple_graphs_and_unique_display_instances(self):
        first = self.client.post("/api/media/graph", json={"name": "Pressure Detail", "channels": ["motor.chamber_pressure"], "time_window": 30})
        second = self.client.post("/api/media/graph", json={"name": "Combined Detail", "channels": ["motor.chamber_pressure", "motor.thrust"], "time_window": 120})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        state = self.client.get("/api/media/snapshot").get_json()
        ids = {g["name"]: g["id"] for g in state["graph_definitions"]}
        page = self.client.post("/api/media/display-page", json={"name": "Propulsion Wall B", "layout": [
            {"instance_id": "pressure-a", "type": "graph", "ref_id": ids["Pressure Detail"], "x": 0, "y": 0, "w": 6, "h": 4},
            {"instance_id": "pressure-b", "type": "graph", "ref_id": ids["Combined Detail"], "x": 6, "y": 0, "w": 6, "h": 4},
        ]})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.client.get(page.get_json()["url"]).status_code, 200)
        invalid = self.client.post("/api/media/display-page", json={"name": "Bad", "layout": [
            {"instance_id": "same", "type": "graph", "x": 0, "y": 0, "w": 6, "h": 4},
            {"instance_id": "same", "type": "graph", "x": 6, "y": 0, "w": 6, "h": 4},
        ]})
        self.assertEqual(invalid.status_code, 400)

    def test_camera_live_mode_requires_rtsp_and_simulation_is_explicit(self):
        invalid = self.client.post("/api/media/camera", json={"device_id": "CAM-01", "mode": "LIVE", "main_url": "http://camera"})
        self.assertEqual(invalid.status_code, 400)
        valid = self.client.post("/api/media/camera", json={"device_id": "CAM-01", "mode": "LIVE", "main_url": "rtsp://10.0.20.11/main", "preview_url": "rtsp://10.0.20.11/sub"})
        self.assertEqual(valid.status_code, 200)
        profile = next(x for x in self.client.get("/api/media/snapshot").get_json()["camera_profiles"] if x["device_id"] == "CAM-01")
        self.assertEqual(profile["mode"], "LIVE")

    def test_broadcast_preview_program_emergency_and_stream_guard(self):
        state = self.client.get("/api/media/snapshot").get_json()
        target = next(x for x in state["broadcast_scenes"] if x["name"] == "Countdown")
        self.assertEqual(self.client.post("/api/media/broadcast", json={"action": "PREVIEW", "scene_id": target["id"]}).status_code, 200)
        self.assertEqual(self.client.post("/api/media/broadcast", json={"action": "TAKE"}).status_code, 200)
        state = self.client.get("/api/media/snapshot").get_json()
        self.assertEqual(state["broadcast"]["program_scene_id"], target["id"])
        blocked = self.client.post("/api/media/broadcast", json={"action": "START_STREAM"})
        self.assertEqual(blocked.status_code, 409)
        destination = self.client.post("/api/media/destination", json={"name": "YouTube Primary", "provider": "YOUTUBE", "ingest_url": "rtmps://a.rtmps.youtube.com/live2", "stream_key": "secret-key"})
        self.assertEqual(destination.status_code, 200)
        state = self.client.get("/api/media/snapshot").get_json()
        dest = state["stream_destinations"][0]
        self.assertNotIn("secret-key", str(dest))
        self.client.post(f"/api/media/destination/{dest['id']}/state", json={"enabled": True})
        self.assertEqual(self.client.post("/api/media/broadcast", json={"action": "START_STREAM"}).status_code, 200)
        self.client.post("/api/media/broadcast", json={"action": "EMERGENCY"})
        state = self.client.get("/api/media/snapshot").get_json()
        self.assertTrue(state["broadcast"]["emergency"])
        self.assertEqual(next(x for x in state["broadcast_scenes"] if x["id"] == state["broadcast"]["program_scene_id"])["scene_type"], "EMERGENCY")


if __name__ == "__main__":
    unittest.main()
