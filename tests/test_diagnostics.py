"""Numerical, ownership, overload and control-state tests; no cameras or Qt needed."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter_ns
from unittest.mock import Mock, patch
import json
import csv
import math
import struct
import types
import unittest
import cv2
import numpy as np

from beamdiag.cameras import BaslerCamera, SimulatedCamera
from beamdiag.centroid import CentroidProcessor
from beamdiag.config import AppConfig, CameraConfig, PIDConfig, RecordingConfig, bandwidth_report, load_config, validate
from beamdiag.model import DiagnosticFrame, Frame, LatestValue
from beamdiag.frame_io import load_saved_frame
from beamdiag.pid import NominalController, PIDAxis
from beamdiag.recording import AsyncRecorder, create_session
from beamdiag.transport import ErrorPacket, ControlPacket

ROOT = Path(__file__).resolve().parents[1]


def frame(image, sequence=1):
    return Frame("far_field", sequence, perf_counter_ns(), 123, image)


def gaussian(cx=31.2, cy=30.7, dtype=np.uint8):
    y, x = np.mgrid[:64, :64]
    peak = 200 if dtype == np.uint8 else 3000
    return (8+peak*np.exp(-((x-cx)**2+(y-cy)**2)/(2*4**2))).astype(dtype)


def sample(seq=1, stamp=1_000_000_000, x=2.0, y=-3.0, valid=True, revision=0):
    return ErrorPacket(42, seq, stamp, stamp+1000, revision, x, y, valid)


class CentroidTests(unittest.TestCase):
    def setUp(self):
        self.cfg = CameraConfig("far_field", width=64, height=64, calibration_confirmed=True,
                                pixel_format="Mono8", reference_px=(30, 30), urad_per_pixel=((2, 1), (-1, 3)))

    def test_subpixel_centroid_and_rotation(self):
        m = CentroidProcessor(self.cfg).process(frame(gaussian()))
        self.assertTrue(m.angular_valid)
        self.assertAlmostEqual(m.centroid_x, 31.2, delta=0.03)
        self.assertAlmostEqual(m.centroid_y, 30.7, delta=0.03)
        self.assertAlmostEqual(m.error_x_urad, 3.1, delta=0.1)
        self.assertAlmostEqual(m.error_y_urad, 0.9, delta=0.1)

    def test_roi_offsets_are_in_delivered_coordinates(self):
        m = CentroidProcessor(replace(self.cfg, analysis_roi=(12, 10, 40, 40))).process(frame(gaussian()))
        self.assertAlmostEqual(m.centroid_x, 31.2, delta=0.03)
        self.assertAlmostEqual(m.centroid_y, 30.7, delta=0.03)

    def test_black_and_saturated_reject_control(self):
        p = CentroidProcessor(self.cfg)
        for image, reason in [(np.zeros((64, 64), np.uint8), "low_flux"),
                              (np.full((64, 64), 255, np.uint8), "saturation")]:
            m = p.process(frame(image))
            self.assertFalse(m.angular_valid)
            self.assertEqual(m.reason, reason)

    def test_unconfirmed_calibration_retains_pixels(self):
        m = CentroidProcessor(replace(self.cfg, calibration_confirmed=False)).process(frame(gaussian()))
        self.assertTrue(m.centroid_valid)
        self.assertFalse(m.angular_valid)
        self.assertTrue(math.isnan(m.error_x_urad))
        self.assertEqual(m.reason, "uncalibrated")

    def test_reference_update_is_versioned(self):
        p = CentroidProcessor(self.cfg)
        first = p.process(frame(gaussian()))
        p.set_reference(first.centroid_x, first.centroid_y)
        second = p.process(frame(gaussian(), 2))
        self.assertEqual(second.reference_revision, 1)
        self.assertEqual(second.error_x_px, 0)
        self.assertEqual(second.error_y_px, 0)

    def test_mono12_and_16_are_not_downconverted(self):
        image = gaussian(dtype=np.uint16)
        original = image.copy()
        for fmt in ("Mono12", "Mono16"):
            m = CentroidProcessor(replace(self.cfg, pixel_format=fmt)).process(frame(image))
            self.assertTrue(m.angular_valid)
            self.assertAlmostEqual(m.centroid_x, 31.2, delta=0.01)
        np.testing.assert_array_equal(image, original)

    def test_invalid_analysis_roi_fails_loudly(self):
        with self.assertRaises(ValueError):
            CentroidProcessor(replace(self.cfg, analysis_roi=(63, 0, 5, 5))).process(frame(gaussian()))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = PIDConfig(kp=(0.1, 0.1), ki=(0.1, 0.1), kd=(0.0, 0.0), slew_per_s=100)

    def test_correction_sign(self):
        c = NominalController(self.cfg, 0.05)
        result = c.update(sample(), 1_001_000_000)
        self.assertLess(result.x, 0)
        self.assertGreater(result.y, 0)

    def test_amplitude_and_anti_windup(self):
        axis = PIDAxis(self.cfg, 0)
        for _ in range(500):
            self.assertEqual(axis.step(10000, 0.05), -1)
        self.assertEqual(axis.integral, 0)
        self.assertGreater(axis.step(-1, 0.05), 0)

    def test_slew_limit(self):
        axis = PIDAxis(replace(self.cfg, slew_per_s=0.2), 0)
        self.assertAlmostEqual(axis.step(100, 0.05), -0.01)
        self.assertEqual(axis.integral, 0)

    def test_stale_invalid_and_future_samples_zero(self):
        for s, now, state in [(sample(), 1_051_000_001, 3), (sample(valid=False), 1_001_000_000, 2),
                              (sample(x=math.nan), 1_001_000_000, 2), (sample(), 999_000_000, 3),
                              (replace(sample(), computed_ns=2_000_000_000), 1_001_000_000, 3)]:
            result = NominalController(self.cfg, 0.05).update(s, now)
            self.assertEqual((result.x, result.y, result.state), (0, 0, state))

    def test_duplicate_and_reordered_samples_are_ignored(self):
        c = NominalController(self.cfg, 0.05)
        c.update(sample(seq=2), 1_001_000_000)
        self.assertIsNone(c.update(sample(seq=2), 1_002_000_000))
        self.assertIsNone(c.update(sample(seq=1), 1_002_000_000))

    def test_watchdog_resets_once(self):
        c = NominalController(self.cfg, 0.05)
        c.update(sample(), 1_001_000_000)
        result = c.watchdog(1_151_000_000)
        self.assertEqual((result.x, result.y, result.state), (0, 0, 4))
        self.assertIsNone(c.watchdog(1_161_000_000))

    def test_reference_change_resets_integral_and_derivative(self):
        c = NominalController(replace(self.cfg, kd=(0.5, 0.5)), 0.05)
        c.update(sample(x=10), 1_001_000_000)
        result = c.update(sample(seq=2, stamp=1_050_000_000, x=0, y=0, revision=1), 1_051_000_000)
        self.assertEqual((result.x, result.y), (0, 0))

    def test_time_reversal_does_not_integrate(self):
        c = NominalController(self.cfg, 0.05)
        c.update(sample(stamp=1_010_000_000), 1_011_000_000)
        result = c.update(sample(seq=2), 1_012_000_000)
        self.assertEqual(result.state, 3)


class DataFlowTests(unittest.TestCase):
    def test_wire_round_trip_and_reject_corruption(self):
        s = sample()
        self.assertEqual(ErrorPacket.unpack(s.pack()), s)
        c = ControlPacket(42, 1, 100, 200, 0.1, -0.2, 0.01, 1)
        self.assertEqual(ControlPacket.unpack(c.pack()), c)
        with self.assertRaises(ValueError):
            ErrorPacket.unpack(b"BAD!"+s.pack()[4:])
        with self.assertRaises(struct.error):
            ErrorPacket.unpack(b"short")

    def test_latest_slot_replaces_instead_of_queuing(self):
        slot = LatestValue()
        for i in range(100):
            slot.publish(i)
        self.assertEqual(slot.read(), 99)

    def test_basler_buffer_is_copied_before_release(self):
        cfg = CameraConfig("far_field")
        source = BaslerCamera(cfg)
        borrowed = np.full((4, 4), 100, np.uint8)
        result = Mock()
        result.IsValid.return_value = True
        result.Array = borrowed
        result.GrabSucceeded.return_value = True
        result.GetTimeStamp.return_value = 123
        result.GetNumberOfSkippedImages.return_value = 2
        result.Release.side_effect = lambda: borrowed.fill(0)
        source.camera = Mock()
        source.camera.RetrieveResult.return_value = result
        source.info = {"offset_x": 4, "offset_y": 8}
        fake = types.ModuleType("pypylon")
        fake.pylon = types.SimpleNamespace(TimeoutHandling_Return=1)
        with patch.dict("sys.modules", {"pypylon": fake}):
            owned = source.read(Event())
        np.testing.assert_array_equal(owned.image, 100)
        self.assertFalse(owned.image.flags.writeable)
        self.assertEqual(owned.skipped, 2)
        self.assertEqual(owned.sensor_offset_y, 8)
        result.Release.assert_called_once()

    def test_basler_empty_timeout_result_is_not_dereferenced_or_released(self):
        source = BaslerCamera(CameraConfig("dark_field", fps=10, grab_timeout_ms=100))
        result = Mock()
        result.IsValid.return_value = False
        result.GrabSucceeded.side_effect = RuntimeError("Cannot access NULL pointer")
        source.camera = Mock()
        source.camera.RetrieveResult.return_value = result
        fake = types.ModuleType("pypylon")
        fake.pylon = types.SimpleNamespace(TimeoutHandling_Return=1)
        with patch.dict("sys.modules", {"pypylon": fake}):
            self.assertIsNone(source.read(Event()))
        result.IsValid.assert_called_once()
        result.GrabSucceeded.assert_not_called()
        result.Release.assert_not_called()
        self.assertEqual(source.sequence, 0)

    def test_real_pylon_empty_grab_result_is_a_normal_timeout(self):
        try:
            from pypylon import pylon
        except ImportError:
            self.skipTest("Install pypylon to test its actual empty smart pointer")
        source = BaslerCamera(CameraConfig("dark_field", fps=10))
        empty = pylon.GrabResult()
        self.assertIsNotNone(empty)
        self.assertFalse(empty.IsValid())
        source.camera = Mock()
        source.camera.RetrieveResult.return_value = empty
        self.assertIsNone(source.read(Event()))
        self.assertEqual(source.sequence, 0)

    def test_basler_none_timeout_and_real_grab_failure_remain_distinct(self):
        source = BaslerCamera(CameraConfig("dark_field", fps=10))
        result = Mock()
        result.IsValid.return_value = True
        result.GrabSucceeded.return_value = False
        result.GetErrorCode.return_value = 123
        result.GetErrorDescription.return_value = "incomplete frame"
        source.camera = Mock()
        source.camera.RetrieveResult.side_effect = [None, result]
        fake = types.ModuleType("pypylon")
        fake.pylon = types.SimpleNamespace(TimeoutHandling_Return=1)
        with patch.dict("sys.modules", {"pypylon": fake}):
            self.assertIsNone(source.read(Event()))
            with self.assertRaisesRegex(RuntimeError, "Grab failed: 123 incomplete frame"):
                source.read(Event())
        result.Release.assert_called_once()

    def test_recording_overload_is_bounded_and_nonblocking(self):
        cfg = AppConfig(recording=RecordingConfig(frame_queue_size=1, telemetry_queue_size=1, start_recording=True))
        with TemporaryDirectory(dir=ROOT/"validation") as tmp:
            self.assertTrue(Path(tmp).resolve().is_relative_to(ROOT.resolve()))
            recorder = AsyncRecorder(cfg, Path(tmp))
            item = DiagnosticFrame(frame(gaussian()), CentroidProcessor(CameraConfig("far_field")).process(frame(gaussian())))
            for _ in range(10):
                recorder.submit(item)
            s = recorder.stats()
            self.assertEqual(s["frame_queue"], 1)
            self.assertEqual(s["frames_dropped"], 10)
            self.assertEqual(s["telemetry_dropped"], 9)

    def test_raw_round_trip_and_writer_drain(self):
        cfg = AppConfig(recording=RecordingConfig(start_recording=True))
        with TemporaryDirectory(dir=ROOT/"validation") as tmp:
            self.assertTrue(Path(tmp).resolve().is_relative_to(ROOT.resolve()))
            session = create_session(cfg, Path(tmp), True)
            recorder = AsyncRecorder(cfg, session)
            recorder.start()
            image = gaussian(dtype=np.uint16)
            item_frame = frame(image)
            m = CentroidProcessor(CameraConfig("far_field", pixel_format="Mono16"), True).process(item_frame)
            recorder.submit(DiagnosticFrame(item_frame, m))
            result = recorder.close()
            self.assertTrue(result["drain_complete"])
            self.assertEqual(result["frames_saved"], 2)
            with (session/"frame_index.csv").open(newline="") as f:
                saved = next(csv.DictReader(f))
            np.testing.assert_array_equal(load_saved_frame(session, saved), image)
            self.assertEqual(len((session/"beam_data.csv").read_text().splitlines()), 2)

    def test_stream_offsets_and_rollover(self):
        cfg = AppConfig(recording=RecordingConfig(start_recording=True, stream_rollover_MB=1))
        with TemporaryDirectory(dir=ROOT/"validation") as tmp:
            self.assertTrue(Path(tmp).resolve().is_relative_to(ROOT.resolve()))
            session = create_session(cfg, Path(tmp), True)
            recorder = AsyncRecorder(cfg, session)
            recorder.start()
            p = CentroidProcessor(CameraConfig("far_field"), True)
            for seq in range(1, 9):
                image_frame = frame(np.full((512, 512), seq, np.uint8), seq)
                recorder.submit(DiagnosticFrame(image_frame, p.process(image_frame)))
            self.assertTrue(recorder.close()["drain_complete"])
            with (session/"frame_index.csv").open(newline="") as f:
                rows = [r for r in csv.DictReader(f) if r["kind"] == "record"]
            self.assertEqual(len(rows), 8)
            self.assertEqual(len(set(r["path"] for r in rows)), 2)
            self.assertGreater(int(rows[1]["byte_offset"]), 0)
            for seq, row in enumerate(rows, 1):
                np.testing.assert_array_equal(load_saved_frame(session, row), seq)

    def test_individual_npy_and_png_preserve_uint16(self):
        for fmt in ("npy", "png"):
            with self.subTest(format=fmt), TemporaryDirectory(dir=ROOT/"validation") as tmp:
                self.assertTrue(Path(tmp).resolve().is_relative_to(ROOT.resolve()))
                cfg = AppConfig(recording=RecordingConfig(start_recording=True, format=fmt))
                session = create_session(cfg, Path(tmp), True)
                recorder = AsyncRecorder(cfg, session)
                recorder.start()
                image_frame = frame(gaussian(dtype=np.uint16))
                m = CentroidProcessor(CameraConfig("far_field", pixel_format="Mono16"), True).process(image_frame)
                recorder.submit(DiagnosticFrame(image_frame, m))
                self.assertTrue(recorder.close()["drain_complete"])
                with (session/"frame_index.csv").open(newline="") as f:
                    row = next(csv.DictReader(f))
                np.testing.assert_array_equal(load_saved_frame(session, row), image_frame.image)

    def test_simulator_produces_owned_subpixel_beams(self):
        source = SimulatedCamera(CameraConfig("far_field", width=64, height=64))
        source.open()
        image_frame = source.read(Event())
        self.assertFalse(image_frame.image.flags.writeable)
        self.assertEqual(image_frame.image.dtype, np.uint16)
        self.assertLessEqual(image_frame.image.max(), 4095)


class ConfigurationTests(unittest.TestCase):
    def test_default_file_and_bandwidth(self):
        cfg = load_config(ROOT/"config.example.toml")
        self.assertTrue(bandwidth_report(cfg)["within_budget"])
        # The saved operating rates are user-configurable; keep the arithmetic
        # regression independent of those physical-camera settings.
        self.assertAlmostEqual(bandwidth_report(AppConfig())["total_payload_MBps"], 15.72864)
        self.assertTrue(all(not c.calibration_confirmed for c in cfg.cameras))

    def test_invalid_config_is_rejected(self):
        cases = [replace(AppConfig(), pid=PIDConfig(host="0.0.0.0")),
                 replace(AppConfig(), recording=RecordingConfig(frame_queue_size=100000)),
                 replace(AppConfig(), display_fps=math.nan),
                 replace(AppConfig(), cameras=(CameraConfig("far_field"),)),
                 replace(AppConfig(), pid=PIDConfig(watchdog_ms=10)),
                 replace(AppConfig(), recording=RecordingConfig(every_n_frames=1.5)),
                 replace(AppConfig(), pid=PIDConfig(ki=(math.nan, 0))),
                 replace(AppConfig(), cameras=tuple(CameraConfig(r, serial="same") for r in ("far_field", "dark_field", "near_field")))]
        for cfg in cases:
            with self.subTest(cfg=cfg):
                with self.assertRaises(ValueError):
                    validate(cfg)


if __name__ == "__main__":
    cv2.setNumThreads(1)
    unittest.main(verbosity=2)
