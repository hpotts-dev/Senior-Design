"""Exercise actual feedback editors without cameras or a physical display."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import unittest

import tests.test_reference_ui as reference_ui
import tests.test_camera_settings_ui as camera_ui
from beamdiag.feedback_settings import pid_values, save_profile


@unittest.skipUnless(reference_ui.QT_AVAILABLE, "Install the GUI extra to test feedback editors")
class FeedbackUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = reference_ui.create_application()

    def setUp(self):
        self.fixture = camera_ui.CameraSettingsUITests()
        self.fixture.app = self.app
        self.fixture.setUp()
        self.engine, self.window, self.worker, self.source = (
            self.fixture.engine, self.fixture.window, self.fixture.worker, self.fixture.source)
        self.fixture.dialog.close()
        self.window._feedback_settings()
        self.dialog = self.window.feedback_dialog
        self.app.processEvents()

    def tearDown(self):
        self.fixture.tearDown()

    def test_calibration_target_and_region_apply_without_restarting_camera(self):
        dialog = self.dialog
        for key, value in (("m00", 2.5), ("m01", .1), ("m10", -.2), ("m11", 3),
                            ("reference_x", 30), ("reference_y", 31),
                            ("roi_x", 8), ("roi_y", 16), ("roi_w", 48), ("roi_h", 40)):
            dialog.fields[key].setValue(value)
        dialog.confirmed.setChecked(True)
        dialog.roi_enabled.setChecked(True)
        with patch.object(self.source, "apply_settings", wraps=self.source.apply_settings) as sdk:
            dialog.apply_feedback.click()
            self.assertFalse(self.worker.cfg.calibration_confirmed)
            self.assertFalse(dialog.apply_feedback.isEnabled())
            self.worker._apply_pending_settings(self.source)
            dialog.refresh()
            sdk.assert_not_called()
        self.assertTrue(self.worker.cfg.calibration_confirmed)
        self.assertEqual(self.worker.cfg.analysis_roi, (8, 16, 48, 40))
        self.assertEqual(self.worker.processor.reference_state(64, 64), (30, 31, 1))
        self.fixture.fixture.publish("far_field", reference_ui.spot())
        m = self.worker.latest.read().measurement
        self.assertTrue(m.angular_valid)
        self.assertAlmostEqual(m.error_x_urad, 2.5*m.error_x_px+.1*m.error_y_px)
        self.assertIn("confirmed", dialog.applied.text())
        self.assertFalse(self.engine.workers["near_field"].processor.calibrated)

    def test_singular_calibration_and_outside_region_are_visible_errors(self):
        self.dialog.fields["m11"].setValue(0)
        self.dialog.apply_feedback.click()
        self.assertIn("invertible", self.dialog.status.text())
        self.assertFalse(self.worker._settings_pending.is_set())
        self.dialog.reload()
        self.dialog.roi_enabled.setChecked(True)
        self.dialog.fields["roi_x"].setValue(63)
        self.dialog.apply_feedback.click()
        self.assertIn("ROI", self.dialog.status.text())
        self.assertFalse(self.worker._settings_pending.is_set())

    def test_typed_calibration_survives_live_refresh_and_requires_new_confirmation(self):
        QtCore, QtTest = reference_ui.QtCore, reference_ui.QtTest
        dialog = self.dialog
        dialog.confirmed.setChecked(True)
        field = dialog.fields["m00"]
        editor = field.lineEdit()
        editor.setFocus()
        field.selectAll()
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Backspace)
        QtTest.QTest.qWait(230)
        self.assertEqual(editor.text(), "")
        self.assertFalse(dialog.confirmed.isChecked())
        QtTest.QTest.keyClicks(editor, "2.25")
        QtTest.QTest.qWait(230)
        self.assertEqual(editor.text(), "2.25")
        self.assertTrue(dialog.feedback_dirty)
        self.assertFalse(self.worker._settings_pending.is_set())

    def test_optical_helper_is_a_draft_and_does_not_confirm_calibration(self):
        self.dialog.confirmed.setChecked(True)
        self.dialog.fields["magnification"].setValue(2)
        self.dialog.use_optical_estimate()
        self.assertEqual(self.dialog.fields["m00"].value(), 8.625)
        self.assertFalse(self.dialog.confirmed.isChecked())
        self.assertFalse(self.worker._settings_pending.is_set())

    def test_pid_shows_old_values_until_acknowledged_and_commits_typed_text(self):
        QtCore, QtTest = reference_ui.QtCore, reference_ui.QtTest
        self.dialog.tabs.setCurrentIndex(2)
        field = self.dialog.fields["kp_0"]
        editor = field.lineEdit()
        editor.setFocus()
        field.selectAll()
        QtTest.QTest.keyClicks(editor, "0.125")
        self.dialog.apply_pid.click()
        pending = self.engine.publisher._pending_settings
        self.assertIsNotNone(pending)
        self.assertEqual(self.engine.cfg.pid.kp[0], .02)
        self.assertTrue(self.engine.publisher.feedback_blocked)
        requested = replace(self.engine.cfg.pid, kp=(.125, .02))
        request_id = self.dialog.pending_pid
        self.engine.publisher.settings_state = {"request_id": request_id, "status": "applied",
            "message": "PID settings applied", "values": pid_values(requested)}
        self.engine.publisher._pending_settings = None
        self.engine.publisher.feedback_blocked = False
        self.dialog.refresh()
        self.assertEqual(self.engine.cfg.pid.kp[0], .125)
        self.assertIsNone(self.dialog.pending_pid)
        self.assertIn("applied", self.dialog.status.text())

    def test_pid_validation_and_unconfirmed_reason_are_clear(self):
        self.dialog.fields["watchdog_ms"].setValue(10)
        self.dialog.apply_pid.click()
        self.assertIn("watchdog", self.dialog.status.text())
        self.assertIsNone(self.engine.publisher._pending_settings)
        self.assertIn("calibration is not confirmed", self.engine.feedback_reason())

    def test_pause_preserves_gain_drafts_and_resume_uses_the_paused_baseline(self):
        import json
        self.dialog.fields["kp_0"].setValue(.125)
        self.dialog.pause()
        self.engine.publisher.settings_state = {"request_id": self.dialog.pending_pid, "status": "applied",
            "message": "Paused", "values": pid_values(replace(self.engine.cfg.pid, enabled=False))}
        self.engine.publisher._pending_settings = None
        self.engine.publisher.feedback_blocked = False
        self.dialog.refresh()
        self.assertEqual(self.dialog.fields["kp_0"].value(), .125)
        self.assertFalse(self.dialog.enabled.isChecked())
        self.dialog.enabled.setChecked(True)
        self.dialog.apply_pid.click()
        sent = json.loads(self.engine.publisher._pending_settings["data"][4:])
        self.assertTrue(sent["values"]["enabled"])
        self.assertEqual(sent["values"]["kp"][0], .125)

    def test_profile_load_is_a_draft_and_save_uses_applied_values(self):
        from PySide6 import QtWidgets
        path = Path(self.fixture.fixture.tmp.name)/"feedback.json"
        camera, pid = self.engine.feedback_snapshot()
        imported = replace(camera, urad_per_pixel=((2, 0), (0, 3)), calibration_confirmed=True)
        save_profile(path, imported, replace(pid, enabled=False, kp=(.1, .2)))
        with patch.object(QtWidgets.QFileDialog, "getOpenFileName", return_value=(str(path), "")):
            self.dialog.load_profile_dialog()
        self.assertEqual(self.dialog.fields["m00"].value(), 2)
        self.assertTrue(self.dialog.feedback_dirty)
        self.assertTrue(self.dialog.pid_dirty)
        self.dialog.refresh()
        self.assertEqual(self.dialog.fields["kp_0"].value(), .1)
        self.assertFalse(self.worker.cfg.calibration_confirmed)
        self.assertIsNone(self.engine.publisher._pending_settings)
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            self.dialog.save_profile_dialog()
        from beamdiag.feedback_settings import load_profile
        actual_camera, actual_pid = load_profile(path, camera, pid)
        self.assertFalse(actual_camera.calibration_confirmed)
        self.assertTrue(actual_pid.enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
