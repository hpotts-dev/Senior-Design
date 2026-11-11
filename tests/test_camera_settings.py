"""Live-control validation, SDK restart/rollback, worker ownership and settings audit records."""
from dataclasses import replace
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from time import perf_counter, perf_counter_ns, sleep
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
import unittest
import numpy as np

from beamdiag.camera_settings import live_values, with_live_settings
from beamdiag.cameras import BaslerCamera, SimulatedCamera
from beamdiag.centroid import CentroidProcessor
from beamdiag.config import AppConfig, CameraConfig, PIDConfig, RecordingConfig, ROLES
from beamdiag.model import Frame
from beamdiag.runtime import DiagnosticEngine

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=3):
    deadline = perf_counter()+timeout
    while not predicate():
        if perf_counter() > deadline:
            raise AssertionError("Timed out waiting for the camera worker")
        sleep(0.005)


class LiveSettingValidationTests(unittest.TestCase):
    def test_allowlist_blocks_geometry_and_calibration_changes(self):
        cfg = CameraConfig("far_field")
        for patch_values in ({"width": 100}, {"calibration_confirmed": True}, {"role": "near_field"},
                             {"exposure_us": float("nan")}, {"fps": 0}, {"gain": -1},
                             {"saturation_level": 4096}, {"saturation_level": 1.5},
                             {"max_saturated_fraction": 1.01}):
            with self.subTest(patch=patch_values), self.assertRaises(ValueError):
                with_live_settings(cfg, patch_values)

    def test_clipping_level_and_fraction_change_rejection_without_touching_pixels(self):
        cfg = CameraConfig("far_field", width=10, height=10, background=0, threshold=0,
                           min_flux=1, calibration_confirmed=True)
        image = np.full((10, 10), 20, np.uint8)
        image[0, :2] = 250
        original = image.copy()
        frame = Frame("far_field", 1, perf_counter_ns(), 0, image)
        p = CentroidProcessor(cfg)
        self.assertTrue(p.process(frame).centroid_valid)
        p.set_reference(4, 5)
        p.update_settings(with_live_settings(cfg, {"saturation_level": 200}))
        result = p.process(frame)
        self.assertEqual(result.reason, "saturation")
        self.assertAlmostEqual(result.saturated_fraction, 0.02)
        self.assertEqual((result.reference_x, result.reference_y), (4, 5))
        self.assertEqual(result.reference_revision, 2)
        p.update_settings(with_live_settings(p.cfg, {"max_saturated_fraction": 0.03}))
        self.assertTrue(p.process(frame).centroid_valid)
        np.testing.assert_array_equal(image, original)

    def test_simulated_exposure_and_gain_clip_instead_of_wrapping(self):
        source = SimulatedCamera(CameraConfig("far_field", width=64, height=64, pixel_format="Mono8"))
        source.open()
        source.apply_settings({"exposure_us": 500})
        dim = source.read(Event()).image
        source.apply_settings({"exposure_us": 10000})
        bright = source.read(Event()).image
        self.assertLess(int(dim.max()), 70)
        self.assertEqual(int(bright.max()), 255)
        self.assertFalse(bright.flags.writeable)

    def test_low_fps_simulator_returns_to_worker_within_grab_timeout(self):
        source = SimulatedCamera(CameraConfig("far_field", width=64, height=64, fps=1, grab_timeout_ms=1))
        source.open()
        self.assertIsNotNone(source.read(Event()))
        started = perf_counter()
        self.assertIsNone(source.read(Event()))
        self.assertLess(perf_counter()-started, 0.1)


class NumericNode:
    def __init__(self, value, low=0, high=1000000, increment=1):
        self.value, self.low, self.high, self.increment = value, low, high, increment
        self.fail_next = False

    def GetValue(self): return self.value
    def GetMin(self): return self.low
    def GetMax(self): return self.high
    def GetInc(self): return self.increment

    def SetValue(self, value):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("Injected camera write error")
        self.value = value


class SDKSettingsTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.nodes = {"ExposureTime": NumericNode(2000.0, 10, 10000), "Gain": NumericNode(0.0, 0, 24, 0.1),
                      "AcquisitionFrameRate": NumericNode(20.0, 1, 100, 0.1),
                      "ResultingFrameRate": NumericNode(20), "ExposureAuto": NumericNode("Off"),
                      "GainAuto": NumericNode("Off"), "AcquisitionFrameRateEnable": NumericNode(True)}
        self.source = BaslerCamera(CameraConfig("far_field", fps=20))
        class Camera:
            grabbing = True
            def IsGrabbing(camera): return camera.grabbing
            def StopGrabbing(camera):
                camera.grabbing = False
                self.events.append("stop")
            def StartGrabbing(camera, strategy):
                camera.grabbing = True
                self.events.append("start")
            def GetNodeMap(camera): return SimpleNamespace(GetNode=lambda name: self.nodes.get(name))
        self.source.camera = Camera()
        fake = ModuleType("pypylon")
        fake.pylon = SimpleNamespace(GrabStrategy_LatestImageOnly=1)
        fake.genicam = SimpleNamespace(IsReadable=lambda n: True, IsWritable=lambda n: True)
        self.module_patch = patch.dict("sys.modules", {"pypylon": fake})
        self.module_patch.start()

    def tearDown(self):
        self.module_patch.stop()

    def test_sdk_stops_and_restarts_only_its_stream_and_reports_clamped_values(self):
        cfg = self.source.apply_settings({"exposure_us": 20000, "gain": 30, "fps": 75})
        self.assertEqual((cfg.exposure_us, cfg.gain, cfg.fps), (10000, 24, 75))
        self.assertEqual(self.events, ["stop", "start"])
        self.assertTrue(self.source.camera.IsGrabbing())
        self.assertEqual(self.source.control_state()["capabilities"]["gain"]["unit"], "dB")

    def test_failed_sdk_write_rolls_back_then_restarts(self):
        self.nodes["AcquisitionFrameRate"].fail_next = True
        with self.assertRaisesRegex(RuntimeError, "Injected"):
            self.source.apply_settings({"exposure_us": 1000, "gain": 6, "fps": 40})
        self.assertEqual(self.events, ["stop", "start"])
        self.assertEqual(self.nodes["ExposureTime"].value, 2000)
        self.assertEqual(self.nodes["Gain"].value, 0)
        self.assertEqual(self.nodes["AcquisitionFrameRate"].value, 20)
        self.assertEqual(self.source.cfg.exposure_us, 2000)

    def test_legacy_raw_gain_is_aligned_to_device_increment(self):
        del self.nodes["Gain"]
        self.nodes["GainRaw"] = NumericNode(0, 0, 24, 4)
        cfg = self.source.apply_settings({"gain": 7})
        self.assertEqual(cfg.gain, 4)
        self.assertEqual(self.source.control_state()["capabilities"]["gain"]["unit"], "raw")


class TrackedCamera(SimulatedCamera):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.apply_threads = []

    def apply_settings(self, patch_values):
        self.apply_threads.append(get_ident())
        return super().apply_settings(patch_values)


class WorkerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir=ROOT/"validation")
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to(ROOT.resolve()))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cfg = AppConfig(cameras=tuple(CameraConfig(role, width=64, height=64) for role in ROLES),
                        pid=PIDConfig(port=port), recording=RecordingConfig(output_dir=self.tmp.name))
        self.engine = DiagnosticEngine(cfg, ROOT/"config.toml", ROOT, simulated=False, launch_pid=False, generate_reports=False)
        self.sources = {}
        def make_source(cfg):
            source = TrackedCamera(cfg)
            self.sources[cfg.role] = source
            return source
        self.camera_patch = patch("beamdiag.runtime.BaslerCamera", side_effect=make_source)
        self.camera_patch.start()
        self.engine.start()
        wait_for(lambda: all(w.controls.read()["ready"] and w.latest.read() for w in self.engine.workers.values()))

    def tearDown(self):
        self.engine.close()
        self.camera_patch.stop()
        self.tmp.cleanup()

    def apply_and_wait(self, role, values):
        request_id = self.engine.queue_camera_settings(role, values)
        worker = self.engine.workers[role]
        wait_for(lambda: worker.controls.read()["request_id"] == request_id and worker.controls.read()["status"] in ("applied", "failed"))
        return worker.controls.read()

    def test_hardware_updates_run_on_owner_worker_and_are_logged(self):
        source = self.sources["far_field"]
        state = self.apply_and_wait("far_field", {"exposure_us": 500, "gain": 6, "fps": 25, "threshold": 12})
        self.assertEqual(state["status"], "applied")
        self.assertEqual((state["values"]["exposure_us"], state["values"]["gain"], state["values"]["fps"]), (500, 6, 25))
        self.assertEqual(source.apply_threads, [self.engine.workers["far_field"].thread.ident])
        self.assertNotEqual(source.apply_threads[0], get_ident())
        self.assertFalse(self.engine.workers["far_field"].processor.calibrated)
        self.engine.close()
        events = [json.loads(line) for line in (self.engine.session/"settings_changes.jsonl").read_text().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["success"])
        self.assertEqual(events[0]["actual"]["threshold"], 12)
        self.assertEqual(events[0]["feedback_revision"], 1)

    def test_detection_only_change_does_not_restart_camera_or_affect_far_field(self):
        ff = self.engine.workers["far_field"].processor.reference_state(64, 64)
        state = self.apply_and_wait("dark_field", {"saturation_level": 200, "max_saturated_fraction": 0.025})
        self.assertEqual(state["status"], "applied")
        self.assertEqual(self.sources["dark_field"].apply_threads, [])
        self.assertEqual(self.engine.workers["far_field"].processor.reference_state(64, 64), ff)

    def test_paced_network_limit_rejects_excessive_rate_before_queuing(self):
        worker = self.engine.workers["far_field"]
        worker.controls.publish({**worker.controls.read(), "delivery_budget_max_fps": 21.0})
        with self.assertRaisesRegex(ValueError, "paced link"):
            self.engine.queue_camera_settings("far_field", {"fps": 22})
        self.assertFalse(worker._settings_pending.is_set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
