"""Native PNG transparency survives the image importer and CPU compositor."""
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from infofilm.image_animation_assets import inspect_asset
from infofilm.image_animation_renderer import prepare_assets


class PaletteTransparencyTests(unittest.TestCase):
    def prepare_palette_part(self, root, *, transparent=True, opaque=False):
        image = Image.new("P", (128, 96), 1 if opaque else 0)
        image.putpalette([0, 0, 0, 240, 120, 40] + [0] * (768 - 6))
        ImageDraw.Draw(image).rectangle((80, 40, 91, 51), fill=1)
        source = root / "part.png"
        image.save(source, **({"transparency": 0} if transparent else {}))
        scene = {"canvas": {"width": 128, "height": 96},
                 "assets": {"part": {"path": source.name}},
                 "controller": {"type": "orbit_path", "bindings": {"orbiter": "part"}}}
        return source, scene

    def test_importer_accepted_palette_alpha_is_preserved_by_renderer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, scene = self.prepare_palette_part(root)
            observed = inspect_asset(source, {"width": 128, "height": 96,
                                               "transparent_required": True})
            self.assertTrue(observed["alpha"]["channel_present"])
            sprite = prepare_assets(scene, root / "scene.json", root / "prepared")["part"]
            self.assertEqual(sprite["alpha_crop_xyxy"], [80, 40, 92, 52])
            self.assertEqual(sprite["size_px"], [12, 12])
            self.assertEqual(sprite["centre_px"], [86, 46])
            with Image.open(sprite["path"]) as cropped:
                self.assertEqual(cropped.mode, "RGBA")
                self.assertEqual(cropped.getpixel((0, 0)), (240, 120, 40, 255))

    def test_palette_transparency_metadata_does_not_approve_opaque_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, scene = self.prepare_palette_part(root, opaque=True)
            with self.assertRaisesRegex(ValueError, "genuine transparent RGBA"):
                prepare_assets(scene, root / "scene.json", root / "prepared")

    def test_palette_without_transparency_is_rejected_for_moving_part(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, scene = self.prepare_palette_part(root, transparent=False)
            with self.assertRaisesRegex(ValueError, "genuine transparent RGBA"):
                prepare_assets(scene, root / "scene.json", root / "prepared")


if __name__ == "__main__":
    unittest.main()
