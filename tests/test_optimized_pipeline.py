"""Preserve full-resolution measurement accuracy, ownership, and all 12 bits while reducing latency."""
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from unittest.mock import Mock
import unittest
import cv2
import numpy as np
from beamdiag.cameras import BaslerCamera
from beamdiag.centroid import CentroidProcessor
from beamdiag.config import AppConfig, CameraConfig, bandwidth_report, validate
from beamdiag.display import resize_preview
from beamdiag.model import Frame


class OptimizedPipelineTests(unittest.TestCase):
    def test_full_sensor_first_moments_match_original_estimator_with_roi_and_fractional_background(self):
        rng = np.random.default_rng(117)
        image = rng.integers(0, 4096, (1088, 1456), dtype=np.uint16)
        for roi in ((), (113, 107, 700, 550)):
            for background in (0.0, 8.0, 13.25):
                with self.subTest(roi=roi, background=background):
                    cfg = CameraConfig('far_field', width=1456, height=1088, background=background,
                                       threshold=10.5, analysis_roi=roi, saturation_level=4000)
                    measurement = CentroidProcessor(cfg).process(Frame('far_field',1,0,0,image))
                    x,y,w,h = roi or (0,0,1456,1088)
                    signal = np.subtract(image[y:y+h,x:x+w], background, dtype=np.float32)
                    cv2.threshold(signal, cfg.threshold, 0, cv2.THRESH_TOZERO, dst=signal)
                    moments = cv2.moments(signal)
                    self.assertAlmostEqual(measurement.flux, moments['m00'], places=4)
                    self.assertAlmostEqual(measurement.centroid_x, x+moments['m10']/moments['m00'], places=9)
                    self.assertAlmostEqual(measurement.centroid_y, y+moments['m01']/moments['m00'], places=9)
                    self.assertAlmostEqual(measurement.saturated_fraction, np.count_nonzero(image[y:y+h,x:x+w]>=4000)/(w*h))

    def test_zero_copy_sdk_view_is_copied_before_the_grab_buffer_is_released(self):
        source = BaslerCamera(CameraConfig('far_field'))
        source._use_zero_copy = True
        source.info = {'offset_x':0, 'offset_y':0}
        borrowed = np.arange(16,dtype=np.uint16).reshape(4,4)
        expected = borrowed.copy()
        @contextmanager
        def view():
            yield borrowed
        result=Mock()
        result.IsValid.return_value=result.GrabSucceeded.return_value=True
        result.GetArrayZeroCopy.side_effect=view
        result.GetTimeStamp.return_value=123
        result.GetNumberOfSkippedImages.return_value=0
        result.Release.side_effect=lambda:borrowed.fill(0)
        source.camera=Mock()
        source.camera.RetrieveResult.return_value=result
        frame=source.read(Event())
        np.testing.assert_array_equal(frame.image,expected)
        self.assertFalse(frame.image.flags.writeable)
        self.assertFalse(np.shares_memory(frame.image,borrowed))

    def test_sdk_packed_unpacking_preserves_every_12bit_value(self):
        try:
            from pypylon import pylon
        except ImportError:
            self.skipTest('pypylon is required for the SDK conversion check')
        expected=np.tile(np.arange(4096,dtype=np.uint16),(2,1))
        raw=np.empty(expected.size*3//2,np.uint8)
        a,b=expected.ravel()[0::2],expected.ravel()[1::2]
        raw[0::3]=(a&255).astype(np.uint8)
        raw[1::3]=((a>>8)|((b&15)<<4)).astype(np.uint8)
        raw[2::3]=(b>>4).astype(np.uint8)
        image=pylon.PylonImage()
        image.AttachMemoryView(memoryview(raw),pylon.PixelType_Mono12p,4096,2,0)
        try:
            with image.GetArrayZeroCopy() as pixels:
                actual=np.array(pixels,copy=True)
                del pixels
            np.testing.assert_array_equal(actual,expected)
        finally:
            image.Release()

    def test_packed_budget_accounts_for_wire_bytes_while_native_buffers_stay_16bit(self):
        cfg=AppConfig()
        packed=replace(cfg,cameras=tuple(replace(c,packed_transport=True) for c in cfg.cameras))
        validate(packed)
        self.assertAlmostEqual(bandwidth_report(packed)['total_payload_MBps'], .75*bandwidth_report(cfg)['total_payload_MBps'])
        for invalid in (replace(cfg.cameras[0],packed_transport='yes'), replace(cfg.cameras[0],packed_transport=True,pixel_format='Mono8')):
            with self.assertRaises(ValueError):
                validate(replace(cfg,cameras=(invalid,*cfg.cameras[1:])))

    def test_fast_preview_resize_keeps_all_four_sensor_corners(self):
        image=np.zeros((1088,1456),np.uint16)
        image[:100,:100]=1000
        image[:100,-100:]=2000
        image[-100:,:100]=3000
        image[-100:,-100:]=4000
        resized=resize_preview(image,480,359)
        self.assertEqual(resized.shape,(359,480))
        self.assertEqual([int(resized[y,x]) for y,x in ((0,0),(0,-1),(-1,0),(-1,-1))],[1000,2000,3000,4000])


if __name__ == '__main__':
    unittest.main()
