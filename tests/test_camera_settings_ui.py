"""Confirm the settings UI queues changes, shows readback, and converts clipping percentages."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from dataclasses import replace
from unittest.mock import patch

import tests.test_reference_ui as reference_ui
from beamdiag.cameras import SimulatedCamera


@unittest.skipUnless(reference_ui.QT_AVAILABLE, "Install the GUI extra to test camera settings")
class CameraSettingsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = reference_ui.create_application()

    def setUp(self):
        self.fixture = reference_ui.ReferenceControlTests()
        self.fixture.app = self.app
        self.fixture.setUp()
        self.engine, self.window = self.fixture.engine, self.fixture.window
        self.worker = self.engine.workers["far_field"]
        self.source = SimulatedCamera(self.worker.cfg)
        self.source.open()
        self.worker.info = self.source.info
        self.worker._publish_controls(self.source)
        self.engine.recorder.start()
        self.window._camera_settings("far_field")
        self.dialog = self.window.settings_dialogs["far_field"]
        self.app.processEvents()

    def tearDown(self):
        self.fixture.tearDown()

    def test_apply_is_queued_then_read_back_and_percentage_is_converted(self):
        self.dialog.fields["exposure_us"].setValue(500)
        self.dialog.fields["gain"].setValue(6)
        self.dialog.fields["fps"].setValue(25)
        self.dialog.fields["max_saturated_fraction"].setValue(2.5)
        self.dialog.fields["saturation_level"].setValue(200)
        with patch.object(self.source, "apply_settings", wraps=self.source.apply_settings) as apply_call:
            self.dialog.apply_button.click()
            apply_call.assert_not_called()
            self.assertEqual(self.worker.cfg.exposure_us, 2000)
            self.assertFalse(self.dialog.apply_button.isEnabled())
            self.worker._apply_pending_settings(self.source)
            apply_call.assert_called_once()
        self.dialog.refresh()
        self.assertEqual(self.worker.cfg.exposure_us, 500)
        self.assertEqual(self.worker.cfg.max_saturated_fraction, 0.025)
        self.assertEqual(self.worker.cfg.saturation_level, 200)
        self.assertTrue(self.dialog.apply_button.isEnabled())
        self.assertTrue(self.dialog.fields["exposure_us"].isEnabled())
        self.assertIn("Applied", self.dialog.status.text())
        self.assertIn("exposure 500", self.dialog.applied.text())

    def test_initial_ranges_and_reload_do_not_report_unsent_edits(self):
        self.assertFalse(self.dialog.dirty)
        self.assertNotIn("not yet applied", self.dialog.status.text())
        self.dialog.fields["exposure_us"].setValue(800)
        self.assertTrue(self.dialog.dirty)
        self.dialog.reload_button.click()
        self.assertFalse(self.dialog.dirty)
        self.assertEqual(self.dialog.fields["exposure_us"].value(), 2000)
        self.assertIn("no unsent edits", self.dialog.status.text())

    def test_typing_exposure_survives_refresh_and_apply_commits_the_text(self):
        QtCore, QtTest = reference_ui.QtCore, reference_ui.QtTest
        # Use the real camera's minimum: a first digit of 5 is intermediate until
        # the rest of 500.5 is typed. Refresh must preserve even an empty draft.
        state = self.worker.controls.read()
        caps = {**state["capabilities"], "exposure_us": {
            **state["capabilities"]["exposure_us"], "min": 18.0}}
        self.worker.controls.publish({**state, "capabilities": caps})
        self.dialog.refresh()
        field = self.dialog.fields["exposure_us"]
        editor = field.lineEdit()
        editor.setFocus()
        field.selectAll()
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Backspace)
        self.dialog.refresh()
        self.assertEqual(editor.text(), "")
        self.assertTrue(self.dialog.dirty)
        for char in "500.5":
            QtTest.QTest.keyClicks(editor, char)
            self.dialog.refresh()
        self.assertEqual(editor.text(), "500.5")
        # Invoke Apply while the text is still being edited; no Enter or focus
        # change should be required before reading the requested exposure.
        self.dialog.apply_button.click()
        self.worker._apply_pending_settings(self.source)
        self.dialog.refresh()
        self.assertEqual(self.worker.cfg.exposure_us, 500.5)
        self.assertEqual(field.value(), 500.5)

    def test_all_numeric_fields_allow_replacement_across_live_timer_ticks(self):
        from PySide6 import QtWidgets
        QtCore, QtTest = reference_ui.QtCore, reference_ui.QtTest
        tabs = self.dialog.findChild(QtWidgets.QTabWidget)
        for key, text in (("exposure_us", "750.5"), ("gain", "6.5"), ("fps", "19.5"),
                          ("background", "4.5"), ("threshold", "12.5"), ("min_flux", "600.5"),
                          ("max_saturated_fraction", "2.5"), ("saturation_level", "220")):
            with self.subTest(field=key):
                tabs.setCurrentIndex(0 if key in ("exposure_us", "gain", "fps") else 1)
                self.dialog.reload()
                field = self.dialog.fields[key]
                editor = field.lineEdit()
                editor.setFocus()
                field.selectAll()
                QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Backspace)
                QtTest.QTest.qWait(230)  # Exercise the real 200 ms refresh timer.
                self.assertEqual(editor.text(), "")
                self.assertTrue(self.dialog.dirty)
                QtTest.QTest.keyClicks(editor, text)
                QtTest.QTest.qWait(230)
                self.assertEqual(editor.text(), text)
                self.assertEqual(editor.cursorPosition(), len(text))
                self.assertFalse(self.worker._settings_pending.is_set())

    def test_external_readback_does_not_overwrite_an_uncommitted_typed_draft(self):
        QtCore, QtTest = reference_ui.QtCore, reference_ui.QtTest
        field = self.dialog.fields["exposure_us"]
        editor = field.lineEdit()
        editor.setFocus()
        field.selectAll()
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Backspace)
        QtTest.QTest.keyClicks(editor, "750")
        # keyboardTracking is disabled: value() still holds the last committed
        # value, but the typed draft must already be protected from live readback.
        self.assertEqual(field.value(), 2000)
        self.engine.queue_camera_settings("far_field", {"exposure_us": 900})
        self.worker._apply_pending_settings(self.source)
        self.dialog.refresh()
        self.assertEqual(editor.text(), "750")
        self.assertIn("exposure 900", self.dialog.applied.text())
        self.dialog.apply_button.click()
        self.worker._apply_pending_settings(self.source)
        self.dialog.refresh()
        self.assertEqual(self.worker.cfg.exposure_us, 750)

    def test_unsupported_controls_are_disabled_and_fields_use_device_bounds(self):
        state = self.worker.controls.read()
        caps = {**state["capabilities"], "gain": {"supported": False}}
        self.worker.controls.publish({**state, "capabilities": caps})
        self.dialog.refresh()
        self.assertFalse(self.dialog.fields["gain"].isEnabled())
        self.assertTrue(self.dialog.fields["exposure_us"].isEnabled())
        self.assertEqual(self.dialog.fields["exposure_us"].maximum(), 1000000)
        self.assertIn("exposure_us:", self.dialog.range_note.text())

    def test_shared_link_rejection_is_visible_without_changing_camera(self):
        state = self.worker.controls.read()
        self.worker.controls.publish({**state, "delivery_budget_max_fps": 21.0})
        self.dialog.fields["fps"].setValue(22)
        self.dialog.apply_button.click()
        self.assertIn("paced link", self.dialog.status.text())
        self.assertEqual(self.worker.cfg.fps, 10)
        self.assertFalse(self.worker._settings_pending.is_set())

    def test_ack_survives_a_reconnection_state_refresh(self):
        self.dialog.fields["threshold"].setValue(15)
        self.dialog.apply_button.click()
        self.worker._apply_pending_settings(self.source)
        self.worker._publish_controls(self.source)  # emulate reconnect readback before the GUI's next tick
        self.dialog.refresh()
        self.assertIsNone(self.dialog.pending_id)
        self.assertTrue(self.dialog.apply_button.isEnabled())
        self.assertEqual(self.dialog.fields["threshold"].value(), 15)

    def test_external_readback_updates_clean_form_but_preserves_unsaved_edits(self):
        self.engine.queue_camera_settings("far_field", {"exposure_us": 750})
        self.worker._apply_pending_settings(self.source)
        self.dialog.refresh()
        self.assertEqual(self.dialog.fields["exposure_us"].value(), 750)
        self.dialog.fields["exposure_us"].setValue(800)
        self.engine.queue_camera_settings("far_field", {"exposure_us": 900})
        self.worker._apply_pending_settings(self.source)
        self.dialog.refresh()
        self.assertEqual(self.dialog.fields["exposure_us"].value(), 800)
        self.assertIn("exposure 900", self.dialog.applied.text())
        self.assertIn("not yet applied", self.dialog.status.text())

    def test_rounded_gain_readback_does_not_restart_camera_for_detection_only_edit(self):
        self.worker.cfg = self.source.cfg = replace(self.worker.cfg, gain=1.00004708119)
        self.worker._publish_controls(self.source)
        self.dialog.reload()
        self.assertEqual(self.dialog.fields["gain"].value(), 1.0)
        self.dialog.fields["threshold"].setValue(15)
        with patch.object(self.source, "apply_settings", wraps=self.source.apply_settings) as hardware_apply:
            self.dialog.apply_button.click()
            self.worker._apply_pending_settings(self.source)
            hardware_apply.assert_not_called()
        self.assertEqual(self.worker.cfg.gain, 1.00004708119)
        self.assertEqual(self.worker.cfg.threshold, 15)


@unittest.skipUnless(reference_ui.QT_AVAILABLE, "Install the GUI extra to test Dark Field settings")
class DarkFieldSettingsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = reference_ui.create_application()

    def setUp(self):
        from beamdiag.model import DiagnosticFrame
        self.fixture = reference_ui.ReferenceControlTests()
        self.fixture.app = self.app
        self.fixture.setUp()
        self.engine, self.window = self.fixture.engine, self.fixture.window
        self.worker = self.engine.workers["dark_field"]
        frame = self.worker.latest.read().frame
        # Real Dark Field frames deliberately have no centroid measurement.
        self.worker.latest.publish(DiagnosticFrame(frame, None))
        self.source = SimulatedCamera(self.worker.cfg)
        self.source.open()
        self.worker.info = self.source.info
        self.worker._publish_controls(self.source)
        self.engine.recorder.start()

    def tearDown(self):
        self.fixture.tearDown()

    def open_dialog(self):
        self.window.panels["dark_field"].camera_settings.click()
        self.app.processEvents()
        return self.window.settings_dialogs["dark_field"]

    def test_dark_settings_opens_with_a_frame_and_no_centroid(self):
        dialog = self.open_dialog()
        dialog.refresh()
        self.assertTrue(dialog.isVisible())
        self.assertIsNone(self.worker.latest.read().measurement)
        self.assertEqual(dialog.tabs.count(), 1)
        self.assertEqual(dialog.tabs.tabText(0), "Camera")
        self.assertIn("no centroid", dialog.live.text())
        for key in ("exposure_us", "gain", "fps"):
            self.assertTrue(dialog.fields[key].isEnabled())

    def test_dark_settings_apply_exposure_gain_and_fps_without_centroid_processing(self):
        dialog = self.open_dialog()
        dialog.fields["exposure_us"].setValue(750)
        dialog.fields["gain"].setValue(2.5)
        dialog.fields["fps"].setValue(8)
        original_threshold = self.worker.cfg.threshold
        # Hidden centroid-only fields must never be sent for a display-only camera.
        dialog.fields["threshold"].setValue(30)
        dialog.apply_button.click()
        self.worker._apply_pending_settings(self.source)
        dialog.refresh()
        self.assertEqual(self.worker.cfg.exposure_us, 750)
        self.assertEqual(self.worker.cfg.gain, 2.5)
        self.assertEqual(self.worker.cfg.fps, 8)
        self.assertEqual(self.worker.cfg.threshold, original_threshold)
        self.assertIsNone(self.worker.latest.read().measurement)
        self.assertTrue(dialog.apply_button.isEnabled())
        self.assertIn("requested 8 fps", dialog.applied.text())
        self.assertIn("Applied", dialog.status.text())
        self.assertEqual(self.engine.workers["far_field"].cfg.fps, 10)
        self.assertEqual(self.engine.workers["near_field"].cfg.fps, 10)

    def test_dark_settings_show_configured_values_while_disconnected(self):
        state = self.worker.controls.read()
        self.worker.controls.publish({**state, "ready": False, "capabilities": {}, "message": "Camera disconnected"})
        dialog = self.open_dialog()
        self.assertTrue(dialog.isVisible())
        self.assertIn("Configured:", dialog.applied.text())
        for key in ("exposure_us", "gain", "fps"):
            self.assertEqual(dialog.fields[key].value(), state["values"][key])
            self.assertFalse(dialog.fields[key].isEnabled())
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertIn("disconnected", dialog.status.text())

    def test_preview_contrast_control_tracks_the_main_view_without_camera_changes(self):
        dialog = self.open_dialog()
        panel = self.window.panels['dark_field']
        original = self.worker.cfg
        self.assertTrue(dialog.preview_auto_contrast.isChecked())
        dialog.preview_auto_contrast.click()
        self.assertFalse(panel.auto_contrast.isChecked())
        self.assertIn('fixed', dialog.preview_note.text())
        self.assertFalse(dialog.dirty)
        self.assertFalse(self.worker._settings_pending.is_set())
        self.assertEqual(self.worker.cfg, original)
        panel.auto_contrast.click()
        self.assertTrue(dialog.preview_auto_contrast.isChecked())
        self.assertIn('keeps the preview bright', dialog.preview_note.text())

    def test_dark_raw_brightness_reports_sensor_pixels_not_background_corrected_pixels(self):
        import numpy as np
        from beamdiag.model import DiagnosticFrame
        frame = self.worker.latest.read().frame
        image = np.full(frame.image.shape, 100, dtype=np.uint16)
        frame = replace(frame, image=image)
        self.worker.latest.publish(DiagnosticFrame(frame, None, np.zeros_like(image)))
        dialog = self.open_dialog()
        self.assertIn('mean 100.00 DN', dialog.live.text())
        self.assertIn('peak 100 DN', dialog.live.text())
        bright = replace(frame, image=image*5, received_ns=frame.received_ns+1)
        self.worker.latest.publish(DiagnosticFrame(bright, None, np.zeros_like(image)))
        dialog.refresh()
        self.assertIn('mean 500.00 DN', dialog.live.text())
        self.assertIn('peak 500 DN', dialog.live.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
