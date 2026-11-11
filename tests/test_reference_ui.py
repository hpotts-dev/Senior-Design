"""Exercise reference controls with Qt offscreen; no cameras, hardware PID or physical display."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
import math
import unittest
import numpy as np

try:
    from PySide6 import QtCore, QtGui, QtTest
    from beamdiag.gui import ReferenceImage, DiagnosticWindow, create_application
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

from beamdiag.config import AppConfig, CameraConfig, RecordingConfig, ROLES
from beamdiag.model import Frame, DiagnosticFrame
from beamdiag.runtime import DiagnosticEngine

ROOT = Path(__file__).resolve().parents[1]


def spot(width=64, height=64):
    y, x = np.mgrid[:height, :width]
    return (8+200*np.exp(-((x-20.2)**2+(y-41.7)**2)/32)).astype(np.uint8)


@unittest.skipUnless(QT_AVAILABLE, "Install the GUI extra to test reference widgets")
class ReferenceImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application()

    def make_image(self, dpr=1):
        label = ReferenceImage()
        label.resize(300, 200)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        pixmap = QtGui.QPixmap(round(100*dpr), round(100*dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(QtCore.Qt.GlobalColor.black)
        label.setPixmap(pixmap)
        label.frame_size = (512, 512)
        return label

    def test_downsampled_coordinates_include_centered_padding(self):
        label = self.make_image()
        try:
            self.assertEqual(label.frame_position(QtCore.QPointF(149.5, 99.5)), (255.5, 255.5))
            self.assertEqual(label.frame_position(QtCore.QPointF(124.5, 74.5)), (127.5, 127.5))
            self.assertIsNone(label.frame_position(QtCore.QPointF(99, 99)))
            self.assertIsNone(label.frame_position(QtCore.QPointF(200, 99)))
        finally:
            label.close()
            label.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_high_dpi_pixmap_uses_logical_mouse_coordinates(self):
        label = self.make_image(dpr=2)
        try:
            self.assertEqual(label.frame_position(QtCore.QPointF(149.5, 99.5)), (255.5, 255.5))
        finally:
            label.close()
            label.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@unittest.skipUnless(QT_AVAILABLE, "Install the GUI extra to test reference widgets")
class ReferenceControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application()

    def setUp(self):
        self.tmp = TemporaryDirectory(dir=ROOT/"validation")
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to(ROOT.resolve()))
        cfg = AppConfig(cameras=tuple(CameraConfig(r, width=64, height=64) for r in ROLES),
                        recording=RecordingConfig(output_dir=self.tmp.name))
        self.engine = DiagnosticEngine(cfg, ROOT/"config.toml", ROOT, simulated=False, launch_pid=False)
        for role in ROLES:
            self.publish(role, spot())
        self.window = DiagnosticWindow(self.engine)
        self.window.timer.stop()
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()
        self.window.refresh()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.engine.close()
        self.tmp.cleanup()

    def publish(self, role, image, age_ns=0):
        worker = self.engine.workers[role]
        image.flags.writeable = False
        old = worker.latest.read()
        seq = old.frame.sequence+1 if old else 1
        f = Frame(role, seq, perf_counter_ns()-age_ns, 0, image)
        worker.latest.publish(DiagnosticFrame(f, worker.processor.process(f)))

    def test_z_key_sets_detected_centroid_and_explains_invalid_cameras(self):
        self.publish("dark_field", np.full((64, 64), 255, np.uint8))
        self.publish("near_field", np.zeros((64, 64), np.uint8))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Z)
        self.app.processEvents()
        p = self.engine.workers["far_field"].processor
        x, y, revision = p.reference_state(64, 64)
        self.assertAlmostEqual(x, 20.2, delta=0.03)
        self.assertAlmostEqual(y, 41.7, delta=0.03)
        self.assertEqual(revision, 1)
        self.assertFalse(self.window.panels["dark_field"].use_centroid.isEnabled())
        self.assertIn("No beam detected", self.window.panels["near_field"].reference_notice.text())
        self.assertIn("Set 1 of 2", self.window.statusBar().currentMessage())

    def test_click_selects_only_that_camera_and_does_not_enable_calibration(self):
        label = self.window.panels["near_field"].image
        # Delivered image is 64x64 with no enlargement, centered in the label.
        bounds = label.contentsRect()
        x0 = bounds.x()+(bounds.width()-64)//2
        y0 = bounds.y()+(bounds.height()-64)//2
        QtTest.QTest.mouseClick(label, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(x0+12, y0+10))
        self.app.processEvents()
        self.assertEqual(self.engine.workers["near_field"].processor.reference_state(64, 64), (12, 10, 1))
        self.assertEqual(self.engine.workers["far_field"].processor.reference_state(64, 64), (31.5, 31.5, 0))
        self.assertFalse(self.engine.workers["near_field"].processor.calibrated)
        self.assertIn("(12.000, 10.000)", self.window.panels["near_field"].reference_notice.text())

    def test_numeric_set_redraws_target_without_new_frame_or_fake_display_sample(self):
        panel = self.window.panels["far_field"]
        original = self.engine.workers["far_field"].latest.read().frame.image.copy()
        samples_before = len(self.engine._display_ages)
        panel.reference_x.setValue(12.125)
        panel.reference_y.setValue(10.25)
        QtTest.QTest.mouseClick(panel.apply_reference, QtCore.Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(self.engine.workers["far_field"].processor.reference_state(64, 64), (12.125, 10.25, 1))
        pixel = panel.image.pixmap().toImage().pixelColor(12, 10)
        self.assertEqual((pixel.red(), pixel.green(), pixel.blue()), (255, 180, 40))
        self.assertEqual(len(self.engine._display_ages), samples_before)
        np.testing.assert_array_equal(self.engine.workers["far_field"].latest.read().frame.image, original)
        self.publish("far_field", spot())
        m = self.engine.workers["far_field"].latest.read().measurement
        self.assertEqual((m.reference_x, m.reference_y, m.reference_revision), (12.125, 10.25, 1))
        self.assertAlmostEqual(m.error_x_px, m.centroid_x-12.125)

    def test_preview_contrast_toggle_redraws_dim_frame_without_changing_measurements(self):
        # Dark Field has no overlays, so inspect an actual rendered camera pixel.
        image = np.tile(np.arange(64, dtype=np.uint16), (64, 1))
        original = image.copy()
        self.publish("dark_field", image)
        self.window.refresh()
        panel = self.window.panels["dark_field"]
        published = self.engine.workers["dark_field"].latest.read()
        samples_before = len(self.engine._display_ages)
        auto_pixel = panel.image.pixmap().toImage().pixelColor(32, 10).red()
        self.assertTrue(panel.auto_contrast.isChecked())
        self.assertGreater(auto_pixel, 100)
        panel.auto_contrast.setChecked(False)
        fixed_pixel = panel.image.pixmap().toImage().pixelColor(32, 10).red()
        self.assertLessEqual(fixed_pixel, 4)
        self.assertIn("4095", panel.display_range.text())
        panel.auto_contrast.setChecked(True)
        self.assertEqual(panel.image.pixmap().toImage().pixelColor(32, 10).red(), auto_pixel)
        self.assertEqual(len(self.engine._display_ages), samples_before)
        self.assertIs(self.engine.workers["dark_field"].latest.read(), published)
        np.testing.assert_array_equal(published.frame.image, original)
        self.assertEqual(self.engine.workers["far_field"].processor.reference_state(64, 64), (31.5, 31.5, 0))

    def test_draft_numeric_values_survive_new_camera_frames(self):
        panel = self.window.panels["far_field"]
        panel.reference_x.setValue(5.25)
        self.publish("far_field", spot())
        self.window.refresh()
        self.assertEqual(panel.reference_x.value(), 5.25)
        self.assertEqual(self.engine.workers["far_field"].processor.reference_state(64, 64), (31.5, 31.5, 0))

    def test_manual_target_works_on_dark_image_and_center_restores_midpoint(self):
        self.publish("near_field", np.zeros((64, 64), np.uint8))
        self.assertFalse(self.engine.use_centroid_reference("near_field").applied)
        result = self.engine.set_reference("near_field", 4, 7)
        self.assertTrue(result.applied)
        panel = self.window.panels["near_field"]
        QtTest.QTest.mouseClick(panel.center, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(self.engine.workers["near_field"].processor.reference_state(64, 64), (31.5, 31.5, 2))

    def test_bad_coordinates_and_stale_centroid_leave_reference_unchanged(self):
        for x, y in ((-1, 0), (64, 2), (math.nan, 4), (1, math.inf)):
            self.assertFalse(self.engine.set_reference("far_field", x, y).applied)
        self.publish("far_field", spot(), age_ns=2_000_000_000)
        change = self.engine.use_centroid_reference("far_field")
        self.assertFalse(change.applied)
        self.assertIn("stale", change.reason)
        self.assertEqual(self.engine.workers["far_field"].processor.reference_state(64, 64), (31.5, 31.5, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
