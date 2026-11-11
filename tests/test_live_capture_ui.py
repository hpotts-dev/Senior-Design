"""Exercise the new controls, including asynchronous full configuration application."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, sleep
import unittest
from PySide6 import QtCore, QtTest
from beamdiag.config import AppConfig, CameraConfig, RecordingConfig, ROLES, load_config
from beamdiag.gui import DiagnosticWindow, create_application
from beamdiag.runtime import DiagnosticEngine
from beamdiag.windows_paths import save_config

ROOT = Path(__file__).resolve().parents[1]


class LiveCaptureUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application()

    def setUp(self):
        self.tmp = TemporaryDirectory(dir=ROOT/"validation")
        folder = Path(self.tmp.name)
        self.config = folder/"config.toml"
        self.cfg = AppConfig(cameras=tuple(CameraConfig(r, width=32, height=32) for r in ROLES),
            recording=RecordingConfig(output_dir=str(folder/"captures")))
        save_config(self.cfg, self.config)
        self.engine = DiagnosticEngine(self.cfg, self.config, ROOT, True, launch_pid=False)
        self.engine.start()
        self.window = DiagnosticWindow(self.engine)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.engine.close()
        self.tmp.cleanup()

    def wait_for(self, check):
        end = perf_counter()+8
        while not check():
            self.app.processEvents()
            if perf_counter() > end:
                raise AssertionError("UI operation timed out")
            sleep(.005)

    def test_interval_accepts_direct_typing_saves_and_restarts_timed_capture(self):
        field = self.window.snapshot_interval
        field.setFocus()
        field.lineEdit().selectAll()
        QtTest.QTest.keyClicks(field.lineEdit(), "12.5")
        QtTest.QTest.mouseClick(self.window.apply_interval, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(self.engine.cfg.recording.snapshot_interval_s, 12.5)
        self.assertEqual(load_config(self.config).recording.snapshot_interval_s, 12.5)
        self.assertFalse(self.engine.recorder.recording.is_set())

    def test_configuration_validation_preserves_file_and_typed_draft(self):
        before = self.config.read_bytes()
        self.window.config_button.click()
        dialog = self.window.config_dialog
        self.assertIn("background_frames", dialog.editor.toPlainText())
        dialog.editor.setPlainText("this is invalid TOML")
        dialog.apply_button.click()
        self.assertIn("Cannot apply", dialog.notice.text())
        self.assertIsNone(dialog.task)
        self.assertEqual(self.config.read_bytes(), before)

    def test_full_config_apply_restarts_workers_and_keeps_window_usable(self):
        old = self.engine.session
        self.window.config_button.click()
        dialog = self.window.config_dialog
        text = dialog.editor.toPlainText().replace("width = 32", "width = 40").replace("display_fps = 60.0", "display_fps = 30.0")
        dialog.editor.setPlainText(text)
        dialog.apply_button.click()
        self.wait_for(lambda: dialog.task is None)
        self.assertIn("saved and applied", dialog.notice.text())
        self.wait_for(lambda: all(w.latest.read() for w in self.engine.workers.values()))
        self.assertNotEqual(self.engine.session, old)
        self.assertTrue(self.window.timer.isActive())
        self.window.refresh()
        self.window.restart_timer.click()
        self.window._snapshot()
        self.assertIn(str(self.engine.capture_dir), self.window.location.text())
        self.assertEqual(self.engine.workers["near_field"].latest.read().frame.image.shape, (32, 40))
