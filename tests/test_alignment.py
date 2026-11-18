from dataclasses import replace
from math import atan2
from unittest import TestCase
import numpy as np

from beamdiag.alignment import ApproachAngle, DarkFieldMonitor, DisplacementSeries
from beamdiag.config import AppConfig, CameraConfig
from beamdiag.model import Measurement


def sample(role, sequence, t, x, y):
    stamp = round(t*1e9)
    return Measurement(role, sequence, stamp, stamp, x, y, 0, 0, x, y, 0, 0, 100, 1, 0,
                       True, False, "uncalibrated", 0, 0)


class AlignmentTests(TestCase):
    def test_geometry_applies_signed_transforms_and_rejects_stale_pair(self):
        cfg = AppConfig()
        cameras = tuple(replace(c, plane_mm_per_pixel=((0, -.01), (.02, 0))) for c in cfg.cameras)
        cfg = replace(cfg, pid=replace(cfg.pid, plane_separation_mm=200), cameras=cameras)
        angle = ApproachAngle(cfg, False)
        result = []
        angle.publish(sample("near_field", 1, 0, 1, 1), 50, result.append)
        angle.publish(sample("far_field", 2, 1, 3, 4), 50, result.append)
        self.assertFalse(result)
        angle.publish(sample("near_field", 2, 1.01, 1, 1), 50, result.append)
        self.assertAlmostEqual(result[0].error_x_urad, 1e6*atan2(-.03, 200))
        self.assertAlmostEqual(result[0].error_y_urad, 1e6*atan2(.04, 200))
        self.assertEqual(result[0].received_ns, 1_000_000_000)
        for role in ("near_field", "far_field"):
            angle.publish(replace(sample(role, 3, 1.1, 0, 0), centroid_valid=role == "far_field"), 50, result.append)
        self.assertFalse(result[1].angular_valid)

    def test_separate_frequency_peaks_and_one_minute_flush(self):
        series, saved = DisplacementSeries(), []
        for i in range(64*61):
            t = i/64
            series.add(sample("near_field", i, t, 2*np.sin(2*np.pi*4*t), .5*np.cos(2*np.pi*8*t)),
                       64, 60, lambda *args: saved.append(args))
        self.assertEqual(len(saved), 1)
        start, end, windows, hz, coeff = saved[0]
        self.assertEqual((start, end, windows), (0, 60, 20))
        self.assertEqual(hz[np.argmax(coeff[:, 4])], 4)
        self.assertEqual(hz[np.argmax(coeff[:, 5])], 8)
        self.assertAlmostEqual(coeff[:, 4].max(), 2, places=3)
        self.assertAlmostEqual(coeff[:, 5].max(), .5, places=3)
        np.testing.assert_allclose(coeff[np.argmax(coeff[:, 4]), 2], -2, atol=.001)

    def test_dark_field_hold_gap_recovery_and_rearm(self):
        cfg = CameraConfig("dark_field", fps=10, df_mean_limit_dn=25, df_hold_s=.2)
        monitor = DarkFieldMonitor()
        for stamp, mean, state, count in [
            (0, 10, "OK", 0), (.1, 30, "OK", 0), (.2, 10, "OK", 0), (.3, 30, "OK", 0),
            (1, 30, "OK", 0), (1.1, 30, "OK", 0), (1.2, 30, "BAD", 1), (1.3, 30, "BAD", 1),
            (1.4, 10, "OK", 1), (1.5, 30, "OK", 1), (1.7, 30, "BAD", 2),
        ]:
            ns = round(stamp*1e9)
            monitor.update(ns, mean, cfg)
            self.assertEqual(monitor.snapshot, (ns, state, count))
