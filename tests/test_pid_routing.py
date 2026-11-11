"""Verify Far Field is the exclusive PID input, including invalid/missing FF feedback."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter_ns
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
import numpy as np

from beamdiag.centroid import CentroidProcessor
from beamdiag.config import AppConfig, CameraConfig, PIDConfig, PID_SOURCE_ROLE, ROLES, validate
from beamdiag.model import Frame
from beamdiag.runtime import CameraWorker
from beamdiag.transport import ErrorPublisher
from beamdiag.camera_settings import live_values

ROOT = Path(__file__).resolve().parents[1]


class FarFieldPIDRoutingTests(unittest.TestCase):
    def measurement(self, role, blank=False):
        cfg = CameraConfig(role, width=8, height=8, background=0, threshold=0,
                           min_flux=1, calibration_confirmed=True)
        image = np.zeros((8, 8), np.uint8)
        if not blank:
            image[2, 1] = 100 if role == PID_SOURCE_ROLE else 200
        image.flags.writeable = False
        frame = Frame(role, 1, perf_counter_ns(), 0, image)
        return cfg, frame, CentroidProcessor(cfg).process(frame)

    def run_workers(self, far_field_mode="valid"):
        publisher, recorder = Mock(), Mock()
        with TemporaryDirectory(dir=ROOT/"validation") as tmp:
            self.assertTrue(Path(tmp).resolve().is_relative_to(ROOT.resolve()))
            for role in ROLES:
                cfg, frame, _ = self.measurement(role, blank=role == PID_SOURCE_ROLE and far_field_mode == "invalid")
                stop = Event()
                engine = SimpleNamespace(stop=stop, session=Path(tmp), simulated=False, publisher=publisher,
                                         recorder=recorder, cfg=AppConfig())
                source = Mock(info={"backend": "test"})
                source.control_state.return_value = {"values": live_values(cfg), "capabilities": {}, "resulting_fps": cfg.fps}

                def read(event, frame=frame, role=role):
                    event.set()  # one sample, then clean shutdown
                    return None if role == PID_SOURCE_ROLE and far_field_mode == "missing" else frame

                source.read.side_effect = read
                with patch("beamdiag.runtime.BaslerCamera", return_value=source):
                    CameraWorker(cfg, engine).run()
        return publisher, recorder

    def test_only_far_field_is_forwarded_when_all_cameras_are_valid(self):
        publisher, recorder = self.run_workers()
        self.assertEqual(recorder.submit.call_count, 3)
        publisher.send.assert_called_once()
        self.assertEqual(publisher.send.call_args.args[0].role, PID_SOURCE_ROLE)
        self.assertTrue(publisher.send.call_args.args[0].angular_valid)

    def test_invalid_far_field_is_forwarded_as_invalid_without_other_camera_fallback(self):
        publisher, recorder = self.run_workers("invalid")
        self.assertEqual(recorder.submit.call_count, 3)
        publisher.send.assert_called_once()
        self.assertEqual(publisher.send.call_args.args[0].role, PID_SOURCE_ROLE)
        self.assertFalse(publisher.send.call_args.args[0].angular_valid)

    def test_missing_far_field_does_not_use_other_cameras(self):
        publisher, recorder = self.run_workers("missing")
        self.assertEqual(recorder.submit.call_count, 2)
        publisher.send.assert_not_called()

    def test_configuration_rejects_other_pid_sources(self):
        for role in ("near_field", "dark_field"):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "must use far_field"):
                validate(replace(AppConfig(), pid=PIDConfig(source_role=role)))

    def test_publisher_rejects_diagnostic_centroids(self):
        with patch("beamdiag.transport.local_socket", return_value=Mock()):
            publisher = ErrorPublisher("127.0.0.1", 49152, 1)
        try:
            for role in ("near_field", "dark_field"):
                _, _, m = self.measurement(role)
                with self.subTest(role=role), self.assertRaisesRegex(ValueError, "Only Far Field"):
                    publisher.send(m)
            publisher.socket.sendto.assert_not_called()
            _, _, m = self.measurement(PID_SOURCE_ROLE)
            publisher.send(m)
            publisher.socket.sendto.assert_called_once()
        finally:
            publisher.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
