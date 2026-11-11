"""Feedback validation, epoch consistency, and reusable profiles."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
import unittest
import numpy as np

from beamdiag.config import CameraConfig, PIDConfig
from beamdiag.centroid import CentroidProcessor
from beamdiag.feedback_settings import (with_feedback_settings, with_pid_settings, optical_scale,
                                       save_profile, load_profile)
from beamdiag.model import Frame
from beamdiag.pid import NominalController
from beamdiag.transport import ErrorPacket

ROOT = Path(__file__).resolve().parents[1]


class FeedbackSettingsTests(unittest.TestCase):
    def test_calibration_and_regions_are_validated_and_source_is_locked(self):
        cfg = CameraConfig("far_field", width=64, height=64)
        for patch in ({"urad_per_pixel": [[1, 2], [2, 4]]},
                      {"urad_per_pixel": [[True, 0], [0, 1]]},
                      {"urad_per_pixel": [[float("nan"), 0], [0, 1]]},
                      {"calibration_confirmed": 1}, {"analysis_roi": [60, 0, 10, 10]},
                      {"reference_px": [64, 1]}, {"serial": "other"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                with_feedback_settings(cfg, patch)
        with self.assertRaisesRegex(ValueError, "Far Field"):
            with_feedback_settings(CameraConfig("near_field"), {})

    def test_pid_settings_reject_invalid_limits_nonfinite_and_routing_changes(self):
        for patch in ({"enabled": 1}, {"output_limit": 1.1}, {"output_limit": 0},
                      {"kp": [1]}, {"ki": [True, 0]}, {"kd": [float("inf"), 0]},
                      {"watchdog_ms": 10}, {"source_role": "near_field"}, {"port": 12345}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                with_pid_settings(PIDConfig(), patch)
        actual = with_pid_settings(PIDConfig(), {"enabled": False, "kp": [0.1, -0.2]})
        self.assertFalse(actual.enabled)
        self.assertEqual(actual.kp, (0.1, -0.2))

    def test_calibration_changes_angular_error_and_revokes_confirmation(self):
        cfg = CameraConfig("far_field", width=8, height=8, background=0, threshold=0, min_flux=1)
        processor = CentroidProcessor(cfg)
        image = np.zeros((8, 8), np.uint8)
        image[2, 1] = 100
        frame = Frame("far_field", 1, perf_counter_ns(), 0, image)
        self.assertEqual(processor.process(frame).reason, "uncalibrated")
        cfg = with_feedback_settings(cfg, {"urad_per_pixel": [[2, .5], [-.25, 3]],
                                          "calibration_confirmed": True, "reference_px": [0, 0]})
        processor.update_settings(cfg, explicit_calibration=True, update_reference=True)
        actual = processor.process(frame)
        self.assertEqual((actual.error_x_urad, actual.error_y_urad), (3, 5.75))
        self.assertEqual(actual.reference_revision, 1)
        processor.update_settings(replace(cfg, calibration_confirmed=False), explicit_calibration=True)
        self.assertFalse(processor.process(frame).angular_valid)
        self.assertEqual(processor.reference_state(8, 8), (0, 0, 2))

    def test_unconfirming_calibration_in_simulation_overrides_the_demo_scale(self):
        cfg = CameraConfig("far_field")
        processor = CentroidProcessor(cfg, simulated=True)
        self.assertTrue(processor.calibrated)
        processor.update_settings(cfg, explicit_calibration=True)
        self.assertFalse(processor.calibrated)
        processor.update_settings(replace(cfg, exposure_us=500))
        self.assertFalse(processor.calibrated)

    def test_reconfiguration_clears_pid_history_but_keeps_sequence_guard(self):
        cfg = PIDConfig(kp=(0, 0), ki=(1, 1), slew_per_s=1000)
        controller = NominalController(cfg, .05)
        now = perf_counter_ns()
        first = ErrorPacket(1, 1, now, now, 0, 2, 3, True)
        self.assertNotEqual(controller.update(first, now).x, 0)
        controller.reconfigure(replace(cfg, enabled=False))
        self.assertEqual(controller.axes[0].integral, 0)
        self.assertIsNone(controller.update(first, now))
        self.assertEqual(controller.update(replace(first, sequence=2), now).state, 6)
        controller.reconfigure(replace(cfg, kp=(.1, .1), ki=(0, 0)))
        self.assertAlmostEqual(controller.update(replace(first, sequence=3), now).x, -.2)

    def test_profiles_round_trip_only_feedback_values_and_optical_estimate(self):
        self.assertAlmostEqual(optical_scale(3.45, 200, 1), 17.25)
        self.assertAlmostEqual(optical_scale(3.45, 200, 2), 8.625)
        with self.assertRaises(ValueError):
            optical_scale(3.45, 0, 1)
        cfg = with_feedback_settings(CameraConfig("far_field"), {
            "urad_per_pixel": [[2, 0], [0, -3]], "calibration_confirmed": True,
            "analysis_roi": [1, 2, 100, 100], "reference_px": [20.5, 30.5]})
        pid = with_pid_settings(PIDConfig(), {"enabled": False, "kp": [.1, .2]})
        with TemporaryDirectory(dir=ROOT/"validation") as tmp:
            path = Path(tmp)/"profile.json"
            save_profile(path, cfg, pid)
            camera, actual_pid = load_profile(path, replace(cfg, exposure_us=1000), PIDConfig())
            self.assertEqual(camera.urad_per_pixel, cfg.urad_per_pixel)
            self.assertTrue(camera.calibration_confirmed)
            self.assertEqual(camera.reference_px, cfg.reference_px)
            self.assertEqual(camera.exposure_us, 1000)
            self.assertEqual(actual_pid, pid)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_profile(path, cfg, pid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
