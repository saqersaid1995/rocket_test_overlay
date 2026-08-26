import io
import unittest

from PIL import Image

from stellar_ops.scene_compositor import (
    SceneCompositorError,
    compose_scene_jpeg,
    extract_mjpeg_jpeg,
    mjpeg_part,
    slate_jpeg,
)


def encoded(image: Image.Image, kind: str) -> bytes:
    output = io.BytesIO()
    image.save(output, format=kind)
    return output.getvalue()


class SceneCompositorTests(unittest.TestCase):
    def test_rotpl_pixels_are_burned_into_the_camera_frame(self):
        camera_jpeg = encoded(Image.new("RGB", (80, 40), "red"), "JPEG")
        camera_part = mjpeg_part(camera_jpeg)
        overlay = Image.new("RGBA", (80, 40), (0, 0, 255, 0))
        for x in range(40, 80):
            for y in range(40):
                overlay.putpixel((x, y), (0, 0, 255, 255))

        result = Image.open(io.BytesIO(compose_scene_jpeg(
            camera_part, encoded(overlay, "PNG"), quality=95
        ))).convert("RGB")

        left = result.getpixel((10, 20))
        right = result.getpixel((70, 20))
        self.assertGreater(left[0], left[2])
        self.assertGreater(right[2], right[0])

    def test_invalid_browser_placeholder_is_not_accepted_as_video(self):
        with self.assertRaises(SceneCompositorError):
            extract_mjpeg_jpeg(b"not-a-camera-frame")

    def test_controlled_slate_is_a_real_encoded_video_frame(self):
        image = Image.open(io.BytesIO(slate_jpeg("TECHNICAL HOLD", "SAFE OUTPUT")))
        self.assertEqual(image.size, (960, 540))
        self.assertEqual(image.format, "JPEG")


if __name__ == "__main__":
    unittest.main()
