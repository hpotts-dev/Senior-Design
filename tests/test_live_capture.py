"""12-bit precision, independent timed snapshots, live files and saved dark references."""
from dataclasses import replace
import csv
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter, perf_counter_ns, sleep
import unittest
from unittest.mock import patch
import numpy as np

from beamdiag.background import load_background, save_background, subtract_background
from beamdiag.centroid import CentroidProcessor
from beamdiag.config import AppConfig, CameraConfig, RecordingConfig, ROLES, load_config, parse_config
from beamdiag.frame_io import load_saved_frame
from beamdiag.image_io import read_image
from beamdiag.live_output import CaptureClock, CentroidTable, CENTROID_HEADER
from beamdiag.model import Frame, DiagnosticFrame
from beamdiag.recording import AsyncRecorder, create_session
from beamdiag.runtime import DiagnosticEngine
from beamdiag.windows_paths import config_text, save_config

ROOT = Path(__file__).resolve().parents[1]


def table(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def wait_for(check, seconds=5):
    deadline = perf_counter()+seconds
    while not check():
        if perf_counter() >= deadline:
            raise AssertionError("Timed out waiting for live data")
        sleep(.01)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LiveCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir=ROOT/"validation")
        self.folder = Path(self.tmp.name)
        self.cfg = AppConfig(cameras=tuple(CameraConfig(r, width=32, height=32, background_frames=3) for r in ROLES),
            recording=RecordingConfig(output_dir=str(self.folder/"captures")))
        self.cfg = replace(self.cfg, pid=replace(self.cfg.pid, port=free_port()))
        self.config = self.folder/"config.toml"
        save_config(self.cfg, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, raw, seq, seconds=0, role="far_field"):
        origin = json.loads((raw/"session.json").read_text())["started_host_ns"]
        cfg = next(c for c in self.cfg.cameras if c.role == role)
        image = np.zeros((32, 32), np.uint16)
        image[10, 15] = 4095
        image.flags.writeable = False
        frame = Frame(role, seq, origin+round(seconds*1e9), seq, image)
        measurement = CentroidProcessor(cfg, True).process(frame) if role != "dark_field" else None
        return DiagnosticFrame(frame, measurement)

    def test_defaults_and_full_configuration_round_trip(self):
        self.assertTrue(all(c.pixel_format == "Mono12" and c.fps == 10 for c in AppConfig().cameras))
        self.assertEqual(AppConfig().recording.snapshot_interval_s, 60)
        self.assertEqual(parse_config(config_text(self.cfg)), self.cfg)
        with self.assertRaises(ValueError):
            parse_config(config_text(self.cfg).replace("snapshot_interval_s = 60.0", "snapshot_interval_s = 0"))

    def test_timed_images_while_recording_off_preserve_all_12_bits(self):
        raw = create_session(self.cfg, ROOT, True)
        recorder = AsyncRecorder(self.cfg, raw)
        recorder.start()
        try:
            for seq, seconds in enumerate((0, 59.9, 60, 119.9, 120), 1):
                for role in ROLES:
                    recorder.submit(self.item(raw, seq, seconds, role))
            wait_for(lambda: recorder.stats()["frames_saved"] == 9)
            wait_for(lambda: len(table(raw.parent/"human_readable"/"images_index.csv")) == 9)
            self.assertFalse(recorder.recording.is_set())
            self.assertFalse((raw/"recording_summary.json").exists())
            index = table(raw/"frame_index.csv")
            self.assertEqual({r["sequence"] for r in index}, {"1", "3", "5"})
            for row in index:
                image = load_saved_frame(raw, row)
                self.assertEqual(image.dtype, np.uint16)
                self.assertEqual(image.max(), 4095)
            for row in table(raw.parent/"human_readable"/"images_index.csv"):
                image = read_image(raw.parent/"human_readable"/row["PNG path"])
                self.assertEqual(image.dtype, np.uint16)
                self.assertEqual(image.max(), 4095)
            self.assertEqual({r["role"] for r in table(raw/"beam_data.csv")}, {"far_field", "near_field"})
        finally:
            self.assertTrue(recorder.close()["drain_complete"])

    def test_manual_capture_and_timer_restart_do_not_depend_on_record(self):
        raw = create_session(self.cfg, ROOT, True)
        recorder = AsyncRecorder(self.cfg, raw)
        recorder.start()
        try:
            recorder.submit(self.item(raw, 1))
            recorder.capture(self.item(raw, 2, 1))
            recorder.set_snapshot_interval(2)
            recorder.submit(self.item(raw, 3, 2))
            recorder.submit(self.item(raw, 4, 3))
            recorder.restart_snapshot_timer()
            recorder.submit(self.item(raw, 5, 3.1))
        finally:
            result = recorder.close()
        self.assertEqual(result["frames_saved"], 4)
        self.assertEqual([r["kind"] for r in table(raw/"frame_index.csv")], ["timed", "snapshot", "timed", "timed"])

    def test_centroid_columns_errors_and_missing_camera_cells(self):
        raw = create_session(self.cfg, ROOT, True)
        stream = io.StringIO()
        table_writer = CentroidTable(stream, CaptureClock(raw))
        ff = self.item(raw, 1).measurement
        nf = self.item(raw, 1, .01, "near_field").measurement
        table_writer.add(ff)
        table_writer.add(nf)
        table_writer.add(replace(ff, sequence=2, received_ns=ff.received_ns+1_000_000_000))
        table_writer.expire(0, final=True)
        rows = list(csv.DictReader(io.StringIO(stream.getvalue())))
        self.assertEqual(list(rows[0]), CENTROID_HEADER)
        self.assertEqual(float(rows[0]["FF_Cent_x"]), 15)
        self.assertEqual(float(rows[0]["FF_Cent_err_x"]), -.5)
        self.assertNotEqual(rows[0][CENTROID_HEADER[0]], rows[0][CENTROID_HEADER[5]])
        self.assertEqual(rows[1]["NF_Cent_x"], "")

    def test_background_is_per_pixel_saturating_and_signature_checked(self):
        cfg = self.cfg.cameras[0]
        image = np.full((32, 32), 100, np.uint16)
        image[0, 0] = 300
        reference = np.full_like(image, 150)
        corrected = subtract_background(image, reference)
        self.assertEqual(corrected[0, 0], 150)
        self.assertEqual(corrected[1, 1], 0)
        self.assertEqual(image[1, 1], 100)
        self.assertFalse(corrected.flags.writeable)
        path = self.folder/"暗い.npz"
        save_background(path, reference, cfg)
        cfg = replace(cfg, background_file=path.name)
        np.testing.assert_array_equal(load_background(cfg, self.config), reference)
        with self.assertRaisesRegex(ValueError, "Simulation"):
            load_background(cfg, self.config, simulated=True)
        for changes in ({"exposure_us": 4000}, {"gain": 1}, {"pixel_format": "Mono8"}, {"offset_x": 1}, {"serial": "another"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                load_background(replace(cfg, **changes), self.config)

    def test_clipping_rejection_uses_native_image_after_background_subtraction(self):
        cfg = replace(self.cfg.cameras[0], background=0)
        native = np.full((32, 32), 4095, np.uint16)
        corrected = np.full_like(native, 300)
        frame = Frame("far_field", 1, perf_counter_ns(), 0, corrected)
        self.assertEqual(CentroidProcessor(cfg, True).process(frame, native_image=native).reason, "saturation")

    def test_live_worker_background_save_reload_and_setting_invalidation(self):
        engine = DiagnosticEngine(self.cfg, self.config, ROOT, True, launch_pid=False)
        try:
            engine.start()
            worker = engine.workers["far_field"]
            wait_for(lambda: worker.latest.read() is not None)
            for camera_worker in engine.workers.values():
                camera_worker.request_background()
            wait_for(lambda: all("saved and active" in w.background_status for w in engine.workers.values()))
            self.assertTrue(all(c.background_file for c in load_config(self.config).cameras))
            cfg = load_config(self.config).cameras[0]
            self.assertTrue(cfg.background_file)
            self.assertEqual(cfg.background, 0)
            self.assertIsNotNone(load_background(cfg, self.config))
            self.assertTrue((engine.session/worker._background_archive).exists())
            self.assertIsNone(engine.workers["dark_field"].latest.read().measurement)
            engine.queue_camera_settings("far_field", {"background": 2})
            wait_for(lambda: worker.cfg.background == 2 and not worker._applying_settings)
            self.assertEqual(engine.current_config().cameras[0].background, 2)
            engine.queue_camera_settings("far_field", {"exposure_us": 1000})
            wait_for(lambda: worker.cfg.exposure_us == 1000)
            self.assertIsNone(worker._background)
        finally:
            self.assertTrue(engine.close()["recording"]["drain_complete"])

    def test_every_setting_can_be_applied_with_a_drained_session_restart(self):
        engine = DiagnosticEngine(self.cfg, self.config, ROOT, True, launch_pid=False)
        try:
            engine.start()
            wait_for(lambda: all(w.latest.read() for w in engine.workers.values()))
            old = engine.session
            cfg = replace(self.cfg, display_fps=30, cameras=tuple(replace(c, width=40, height=36, pixel_format="Mono8", fps=12) for c in self.cfg.cameras),
                recording=replace(self.cfg.recording, frame_queue_size=12, snapshot_interval_s=30))
            engine.reconfigure(cfg)
            wait_for(lambda: all(w.latest.read() for w in engine.workers.values()))
            self.assertNotEqual(old, engine.session)
            self.assertTrue(json.loads((old/"recording_summary.json").read_text())["drain_complete"])
            self.assertEqual(engine.recorder.frames.maxsize, 12)
            for worker in engine.workers.values():
                self.assertEqual(worker.latest.read().frame.image.shape, (36, 40))
                self.assertEqual(worker.latest.read().frame.image.dtype, np.uint8)
        finally:
            engine.close()

    def test_process_crash_keeps_live_outputs_without_shutdown_export(self):
        cfg = replace(self.cfg, recording=replace(self.cfg.recording, start_recording=True, snapshot_interval_s=.2))
        save_config(cfg, self.config)
        log = (self.folder/"crash_test.log").open("w", encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(ROOT/"diagnostic_app.py"), "--simulate", "--headless", "--config", str(self.config)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            wait_for(lambda: list((self.folder/"captures").glob("*/raw_data/frame_index.csv")), 10)
            raw = next((self.folder/"captures").glob("*/raw_data"))
            readable = raw.parent/"human_readable"
            wait_for(lambda: len(table(raw/"frame_index.csv")) > 15)
            wait_for(lambda: len(table(readable/"centroids.csv")) > 3 and len(table(readable/"pid_feedback.csv")) > 3)
            process.kill()
            process.wait(timeout=5)
            # Child PID watches the parent process; allow its log to drain before temp cleanup.
            wait_for(lambda: (raw/"control_summary.json").exists())
            self.assertFalse((raw/"summary.json").exists())
            self.assertGreater(len(table(readable/"images_index.csv")), 3)
            for row in table(raw/"frame_index.csv"):
                self.assertEqual(load_saved_frame(raw, row).dtype, np.uint16)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            log.close()
