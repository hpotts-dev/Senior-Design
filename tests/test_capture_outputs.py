"""Capture layout, readable exports, source joins and native image precision."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, sleep
from unittest.mock import patch
import csv
import hashlib
import json
import logging
import socket
import unittest
import cv2
import numpy as np

from beamdiag.capture_layout import capture_paths
from beamdiag.centroid import CentroidProcessor
from beamdiag.config import AppConfig, CameraConfig, PIDConfig, RecordingConfig, ROLES, load_config
from beamdiag.frame_io import load_saved_frame
from beamdiag.model import Frame, DiagnosticFrame
from beamdiag.recording import AsyncRecorder, LogEvent, create_session
from beamdiag.report import generate_report
from beamdiag.runtime import DiagnosticEngine
from beamdiag.transport import ControlPacket, ErrorPacket
from pid_controller import ControlLog

ROOT = Path(__file__).resolve().parents[1]


def table(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


class CaptureOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir=ROOT/"validation")
        self.root = Path(self.tmp.name)
        self.cfg = AppConfig(cameras=(CameraConfig("far_field", width=64, height=64, calibration_confirmed=True),
            CameraConfig("dark_field", width=64, height=64, pixel_format="Mono12"),
            CameraConfig("near_field", width=64, height=64, pixel_format="Mono16")),
            recording=RecordingConfig(output_dir=str(self.root), start_recording=True))

    def tearDown(self):
        self.tmp.cleanup()

    def populate(self):
        raw = create_session(self.cfg, self.root, simulated=False)
        capture, _, readable = capture_paths(raw)
        metadata = json.loads((raw/"session.json").read_text())
        base = metadata["started_host_ns"]
        recorder = AsyncRecorder(self.cfg, raw)
        recorder.start()
        snapshot = None
        for seq, seconds in enumerate((0, .2, .4, .6, 1.1, 1.2), 1):
            for cfg in self.cfg.cameras:
                image = np.zeros((64, 64), np.uint8 if cfg.pixel_format == "Mono8" else np.uint16)
                if cfg.role != "near_field":
                    image[20, 30] = 100 if image.dtype == np.uint8 else 3000
                image.flags.writeable = False
                frame = Frame(cfg.role, seq, base+int(seconds*1e9), seq, image)
                item = DiagnosticFrame(frame, CentroidProcessor(cfg).process(frame))
                recorder.submit(item)
                if cfg.role == "far_field" and seq == 3:
                    snapshot = item
        recorder.capture(snapshot, "snapshot")
        recorder.submit_event(LogEvent(base+1_000_000, "WARNING", "test", "Example warning <read safely>"))
        stats = recorder.close()
        self.assertTrue(stats["drain_complete"])
        control = ControlLog(raw, self.cfg.pid)
        control.thread.start()
        for seq, state in ((2, 1), (2, 4), (4, 1), (4, 5)):
            stamp = base+(200_000_000 if seq == 2 else 600_000_000)
            packet = ControlPacket(42, seq, stamp, stamp+1_000_000, .1 if state == 1 else 0, 0, .01, state)
            control.submit((packet, ErrorPacket(42, seq, stamp, stamp+1, 0, 1, 2, True)))
        control.close()
        (raw/"summary.json").write_text(json.dumps({"recording": stats, "cameras": {}, "final_configuration": self.cfg.to_dict()}))
        return capture, raw, readable

    def test_date_time_only_folder_collision_and_exactly_two_children(self):
        fixed = datetime(2026, 10, 21, 3, 4, 5, 123456, tzinfo=timezone.utc)
        with patch("beamdiag.recording.datetime") as clock:
            clock.now.return_value = fixed
            first = create_session(self.cfg, self.root, False)
            second = create_session(self.cfg, self.root, False)
        self.assertNotEqual(first.parent, second.parent)
        for raw in (first, second):
            self.assertRegex(raw.parent.name, r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.\d{6}$")
            self.assertEqual({p.name for p in raw.parent.iterdir()}, {"raw_data", "human_readable"})
            metadata = json.loads((raw/"session.json").read_text())
            self.assertEqual(metadata["started_local"], fixed.astimezone().isoformat())
            self.assertEqual(metadata["schema"], 3)

    def test_report_preserves_raw_data_and_includes_native_pngs_snapshots_and_last_frames(self):
        capture, raw, readable = self.populate()
        preserved = {p: hashlib.sha256(p.read_bytes()).digest() for p in raw.rglob("*") if p.is_file() and p.name not in ("summary.json", "readable_report_status.json")}
        result = generate_report(capture)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["saved_frames"], 22)
        self.assertEqual(result["png_frames"], 4)
        self.assertEqual({p.name for p in capture.iterdir()}, {"raw_data", "human_readable"})
        for path, digest in preserved.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)
        indexed = table(raw/"frame_index.csv")
        images = table(readable/"images_index.csv")
        for row in images:
            png = cv2.imread(str(readable/row["PNG path"]), cv2.IMREAD_UNCHANGED)
            source = next(original for original in indexed if original["role"].replace("_", " ").title() == row["Camera"] and
                          original["sequence"] == row["Frame"] and original["kind"] == row["Capture type"])
            np.testing.assert_array_equal(png, load_saved_frame(capture, source))
            self.assertEqual(str(png.dtype), row["Native dtype"])
            self.assertTrue((readable/row["Display preview path"]).exists())
        for camera in ("Far Field", "Dark Field", "Near Field"):
            self.assertTrue(any(row["Camera"] == camera and row["Frame"] == "1" for row in images))
        self.assertTrue(any(row["Capture type"] == "snapshot" for row in images))
        self.assertIn("No piezo", (readable/"START_HERE.html").read_text().replace("no piezo", "No piezo"))

    def test_human_tables_join_far_field_frames_and_leave_invalid_angles_blank(self):
        capture, raw, readable = self.populate()
        generate_report(raw)
        centroids = table(readable/"centroids.csv")
        self.assertEqual(len(centroids), 6)
        self.assertEqual(centroids[0]["FF_Cent_x"], "30.0")
        self.assertEqual(centroids[0]["FF_Cent_err_x"], "-1.5")
        self.assertEqual(centroids[0]["NF_Cent_x"], "")
        self.assertNotIn("Camera", centroids[0])
        pid = table(readable/"pid_feedback.csv")
        self.assertEqual([row["Far Field frame"] for row in pid], ["2", "2", "4", "4"])
        self.assertEqual(pid[1]["PID state"], "Watchdog")
        self.assertEqual(float(pid[1]["Nominal correction X (normalized)"]), 0.0)
        issues = table(readable/"errors_and_warnings.csv")
        self.assertTrue(any("Example warning" in row["Explanation"] for row in issues))
        self.assertIn("&lt;read safely&gt;", (readable/"START_HERE.html").read_text())
        self.assertIn("I_candidate", (readable/"PID_equation.txt").read_text())

    def test_zero_interval_exports_all_frames_and_invalid_interval_is_rejected(self):
        capture, _, _ = self.populate()
        result = generate_report(capture, image_interval=0)
        self.assertEqual(result["png_frames"], 22)
        with self.assertRaises(ValueError):
            generate_report(capture, image_interval=-1)

    def test_no_saved_frames_and_legacy_raw_reader(self):
        raw = create_session(self.cfg, self.root, False)
        recorder = AsyncRecorder(self.cfg, raw)
        recorder.start()
        recorder.close()
        result = generate_report(raw)
        self.assertEqual(result["png_frames"], 0)
        self.assertIn("No raw images were recorded", (raw.parent/"human_readable"/"README.txt").read_text())
        legacy = self.root/"legacy"
        legacy.mkdir()
        np.save(legacy/"frame.npy", np.array([[123]], np.uint16), allow_pickle=False)
        np.testing.assert_array_equal(load_saved_frame(legacy, {"path": "frame.npy"}), [[123]])
        with self.assertRaises(ValueError):
            load_saved_frame(raw.parent, {"path": "../legacy/frame.npy"})


class CaptureReportProcessTests(unittest.TestCase):
    def test_automatic_export_starts_after_capture_closes_and_all_outputs_finish(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with TemporaryDirectory(dir=ROOT/"validation") as tmp:
            folder = Path(tmp)
            config = folder/"config.toml"
            config.write_text((ROOT/"config.example.toml").read_text().replace("port = 49152", f"port = {port}"))
            cfg = load_config(config)
            cfg = replace(cfg, cameras=tuple(replace(c, width=64, height=64) for c in cfg.cameras),
                          recording=replace(cfg.recording, output_dir=str(folder/"captures"), start_recording=True))
            engine = DiagnosticEngine(cfg, config, ROOT, simulated=True)
            try:
                engine.start()
                deadline = perf_counter()+.4
                while perf_counter() < deadline:
                    engine.poll_control()
                    sleep(.005)
                self.assertIsNone(engine._reporter)
                self.assertTrue((engine.readable_dir/"START_HERE.html").exists())
                self.assertTrue(table(engine.readable_dir/"centroids.csv"))
                self.assertTrue(table(engine.readable_dir/"pid_feedback.csv"))
                engine.snapshot()
                logging.getLogger("capture-test").warning("Test warning saved asynchronously")
            finally:
                summary = engine.close()
            self.assertTrue(summary["recording"]["drain_complete"])
            self.assertIsNone(engine._reporter)
            self.assertTrue((engine.readable_dir/"START_HERE.html").exists())
            ff_count = sum(bool(r["Far Field Capture Time"]) for r in table(engine.readable_dir/"centroids.csv"))
            self.assertEqual(ff_count, summary["cameras"]["far_field"]["frames"])
            self.assertEqual(engine.workers["dark_field"].latest.read().measurement, None)
            self.assertTrue(any("Test warning" in row["Explanation"] for row in table(engine.readable_dir/"errors_and_warnings.csv")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
