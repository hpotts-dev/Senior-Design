"""Regressions for dim 12-bit camera feeds appearing black in the preview."""
import unittest

import numpy as np

from beamdiag.display import preview_u8


class PreviewTests(unittest.TestCase):
    def test_dim_mono12_frame_has_visible_detail_without_changing_native_data(self):
        image = np.tile(np.arange(57, dtype=np.uint16), (100, 1))
        original = image.copy()
        image.flags.writeable = False
        fixed, fixed_white = preview_u8(image, "Mono12", False)
        auto, white = preview_u8(image, "Mono12")
        self.assertEqual(fixed_white, 4095)
        self.assertLessEqual(int(fixed.max()), 4)
        self.assertLessEqual(white, 56)
        self.assertGreater(int(auto[:, 28].min()), 100)
        self.assertEqual(int(auto.max()), 255)
        np.testing.assert_array_equal(image, original)

    def test_hot_pixel_does_not_keep_dim_scene_black(self):
        image = np.tile(np.arange(57, dtype=np.uint16), (100, 1))
        image[0, 0] = 4095
        auto, white = preview_u8(image, "Mono12")
        self.assertLessEqual(white, 56)
        self.assertGreater(int(auto[20, 28]), 100)
        self.assertEqual(int(auto[0, 0]), 255)

    def test_black_frames_stay_black_and_sparse_spots_remain_visible(self):
        for dtype, fmt in ((np.uint8, "Mono8"), (np.uint16, "Mono12"), (np.uint16, "Mono16")):
            with self.subTest(fmt=fmt):
                image = np.zeros((100, 100), dtype)
                auto, white = preview_u8(image, fmt)
                self.assertEqual(int(auto.max()), 0)
                self.assertGreater(white, 0)
                image[10, 10] = 40
                auto, white = preview_u8(image, fmt)
                self.assertEqual(white, 40)
                self.assertEqual(int(auto[10, 10]), 255)
                self.assertEqual(int(auto[50, 50]), 0)

    def test_full_sensor_range_for_each_supported_format(self):
        for dtype, fmt, maximum in ((np.uint8, "Mono8", 255), (np.uint16, "Mono12", 4095),
                                    (np.uint16, "Mono16", 65535)):
            with self.subTest(fmt=fmt):
                image = np.array([[0, maximum//2, maximum]], dtype)
                fixed, white = preview_u8(image, fmt, False)
                self.assertEqual(white, maximum)
                self.assertEqual(fixed.dtype, np.uint8)
                self.assertEqual(int(fixed[0, 0]), 0)
                self.assertAlmostEqual(int(fixed[0, 1]), 127, delta=1)
                self.assertEqual(int(fixed[0, 2]), 255)

    def test_auto_contrast_can_hide_exposure_changes_while_fixed_scale_shows_them(self):
        low = np.tile(np.arange(100, dtype=np.uint16), (100, 1))
        high = low*5
        auto_low, white_low = preview_u8(low, 'Mono12', True)
        auto_high, white_high = preview_u8(high, 'Mono12', True)
        np.testing.assert_array_equal(auto_low, auto_high)
        self.assertAlmostEqual(white_high, white_low*5)
        fixed_low, _ = preview_u8(low, 'Mono12', False)
        fixed_high, _ = preview_u8(high, 'Mono12', False)
        self.assertGreater(float(fixed_high.mean()), 4*float(fixed_low.mean()))


if __name__ == "__main__":
    unittest.main()
