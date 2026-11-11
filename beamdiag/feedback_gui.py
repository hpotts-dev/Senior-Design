"""Far Field calibration and acknowledged, live nominal PID configuration."""
from functools import partial
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from .config import PID_SOURCE_ROLE
from .feedback_settings import (feedback_values, pid_values, optical_scale, save_profile, load_profile)


class FeedbackSettingsDialog(QtWidgets.QDialog):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.worker = engine.workers[PID_SOURCE_ROLE]
        self.pending_feedback = self.pending_pid = None
        self.pid_action = "apply"
        self.feedback_dirty = self.pid_dirty = False
        self.fields = {}
        self.setWindowTitle("Far Field control feedback")
        self.resize(740, 700)
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel("Far Field is the only PID source. Outputs are nominal, normalized commands; "
                                "this application does not drive piezo hardware.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs, 1)

        calibration, region, tuning, limits = (QtWidgets.QWidget() for _ in range(4))
        for page, label in ((calibration, "Calibration"), (region, "Beam region / target"),
                            (tuning, "PID tuning"), (limits, "Limits / timing")):
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, label)
        cal = QtWidgets.QVBoxLayout(calibration)
        equation = QtWidgets.QLabel("Angular error [X, Y] = calibration matrix × (centroid − reference), in µrad.\n"
                                    "Enter measured µrad/pixel. Off-diagonal terms account for axis coupling or rotation.")
        equation.setWordWrap(True)
        cal.addWidget(equation)
        matrix = QtWidgets.QGridLayout()
        matrix.addWidget(QtWidgets.QLabel("From image X"), 0, 1)
        matrix.addWidget(QtWidgets.QLabel("From image Y"), 0, 2)
        for row in range(2):
            matrix.addWidget(QtWidgets.QLabel(f"Angle {'X' if row == 0 else 'Y'} (µrad/px)"), row+1, 0)
            for col in range(2):
                field = self.number(f"m{row}{col}", -1e6, 1e6, 8, "feedback")
                field.valueChanged.connect(self.invalidate_calibration)
                field.lineEdit().textEdited.connect(self.invalidate_calibration)
                matrix.addWidget(field, row+1, col+1)
        cal.addLayout(matrix)
        self.confirmed = QtWidgets.QCheckBox("I have verified this Far Field angular calibration")
        self.confirmed.toggled.connect(partial(self.mark_dirty, "feedback"))
        cal.addWidget(self.confirmed)
        helper = QtWidgets.QGroupBox("Optional optical estimate — verify before confirming")
        helper_form = QtWidgets.QFormLayout(helper)
        for key, label, value in (("pitch", "Pixel pitch (µm)", 3.45),
                                  ("focal", "Effective focal length (mm)", 200),
                                  ("magnification", "Additional image magnification", 1)):
            field = self.number(key, 0.000001, 1e7, 6, None)
            field.setValue(value)
            helper_form.addRow(label, field)
        estimate = QtWidgets.QPushButton("Fill diagonal matrix from optics")
        estimate.clicked.connect(self.use_optical_estimate)
        helper_form.addRow(estimate)
        helper_note = QtWidgets.QLabel("Scale = 1000 × pitch / (effective focal length × magnification). "
            "Use magnification 1 if it is already included in the effective focal length. Image Y increases downward; "
            "use signed matrix values for your angle convention. This estimates beam angle, not mirror rotation.")
        helper_note.setWordWrap(True)
        helper_form.addRow(helper_note)
        cal.addWidget(helper)
        cal.addStretch()

        region_form = QtWidgets.QFormLayout(region)
        region_note = QtWidgets.QLabel("Coordinates refer to the delivered camera image, with origin at the top left. "
            "The analysis region selects the beam used for the centroid; saved frames retain all original pixels.")
        region_note.setWordWrap(True)
        region_form.addRow(region_note)
        for key, label, maximum in (("reference_x", "Reference X (px)", self.worker.cfg.width-1),
                                    ("reference_y", "Reference Y (px)", self.worker.cfg.height-1)):
            region_form.addRow(label, self.number(key, 0, maximum, 3, "feedback"))
        target_buttons = QtWidgets.QHBoxLayout()
        self.use_centroid = QtWidgets.QPushButton("Use detected centroid")
        self.use_centroid.clicked.connect(self.use_detected_centroid)
        self.center_target = QtWidgets.QPushButton("Center target")
        self.center_target.clicked.connect(self.center_reference)
        target_buttons.addWidget(self.use_centroid)
        target_buttons.addWidget(self.center_target)
        region_form.addRow(target_buttons)
        self.roi_enabled = QtWidgets.QCheckBox("Restrict centroid analysis to a region")
        self.roi_enabled.toggled.connect(partial(self.mark_dirty, "feedback"))
        self.roi_enabled.toggled.connect(self.update_roi_enabled)
        region_form.addRow(self.roi_enabled)
        for key, label, maximum, minimum in (("roi_x", "Region left X (px)", self.worker.cfg.width-1, 0),
                                             ("roi_y", "Region top Y (px)", self.worker.cfg.height-1, 0),
                                             ("roi_w", "Region width (px)", self.worker.cfg.width, 1),
                                             ("roi_h", "Region height (px)", self.worker.cfg.height, 1)):
            region_form.addRow(label, self.number(key, minimum, maximum, 0, "feedback", integer=True))

        tune = QtWidgets.QVBoxLayout(tuning)
        self.enabled = QtWidgets.QCheckBox("Enable nominal PID calculations")
        self.enabled.toggled.connect(partial(self.mark_dirty, "pid"))
        tune.addWidget(self.enabled)
        gains = QtWidgets.QGridLayout()
        for col, label in enumerate(("Gain", "X", "Y")):
            gains.addWidget(QtWidgets.QLabel(label), 0, col)
        for row, (key, label) in enumerate((("kp", "Proportional Kp (command/µrad)"),
                                            ("ki", "Integral Ki (command/µrad/s)"),
                                            ("kd", "Derivative Kd (command·s/µrad)")), 1):
            gains.addWidget(QtWidgets.QLabel(label), row, 0)
            for axis in range(2):
                gains.addWidget(self.number(f"{key}_{axis}", -1e6, 1e6, 8, "pid"), row, axis+1)
        tune.addLayout(gains)
        gain_note = QtWidgets.QLabel("The correcting sign is opposite centroid − reference. "
            "Every applied calibration, target or PID change resets the integral and derivative history. "
            "Pause holds nominal output at zero while capture, centroid analysis and recording continue.")
        gain_note.setWordWrap(True)
        tune.addWidget(gain_note)
        tune.addStretch()

        limit_form = QtWidgets.QFormLayout(limits)
        for key, label, low, high, decimals in (
            ("output_limit", "Output amplitude limit (normalized ±)", 0.000001, 1, 6),
            ("integral_limit", "Integral contribution limit (normalized ±)", 0, 1e6, 6),
            ("slew_per_s", "Output slew limit (normalized units/s)", 0.000001, 1e6, 6),
            ("deadband_urad", "Pointing deadband (µrad)", 0, 1e6, 6),
            ("derivative_tau_s", "Derivative filter time (ms)", 0, 60000, 3),
            ("max_sample_age_ms", "Maximum sample age (ms)", 0.001, 60000, 3),
            ("watchdog_ms", "Loss-of-feedback watchdog (ms)", 0.001, 60000, 3)):
            limit_form.addRow(label, self.number(key, low, high, decimals, "pid"))
        limits_note = QtWidgets.QLabel("Output is limited to ±1 or less. The watchdog must be at least the maximum "
            "sample age. Frame rate and exposure are adjusted in the camera's Settings dialog.")
        limits_note.setWordWrap(True)
        limit_form.addRow(limits_note)
        for page in (calibration, region, tuning, limits):
            page.layout().setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        self.applied = QtWidgets.QLabel()
        self.applied.setWordWrap(True)
        layout.addWidget(self.applied)
        self.live = QtWidgets.QLabel()
        self.live.setWordWrap(True)
        layout.addWidget(self.live)
        self.status = QtWidgets.QLabel("Changes are drafts until the corresponding Apply button is pressed.")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(40)
        layout.addWidget(self.status)
        actions = QtWidgets.QHBoxLayout()
        self.apply_feedback = QtWidgets.QPushButton("Apply calibration / target")
        self.apply_pid = QtWidgets.QPushButton("Apply PID")
        self.pause_pid = QtWidgets.QPushButton("Pause PID")
        self.reset_pid = QtWidgets.QPushButton("Reset PID history")
        for button, callback in ((self.apply_feedback, self.send_feedback), (self.apply_pid, self.send_pid),
                                  (self.pause_pid, self.pause), (self.reset_pid, self.reset)):
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        buttons = QtWidgets.QDialogButtonBox()
        self.load_profile_button = buttons.addButton("Load profile…", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.save_profile_button = buttons.addButton("Save applied profile…", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.reload_button = buttons.addButton("Reload live values", QtWidgets.QDialogButtonBox.ButtonRole.ResetRole)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Close)
        self.load_profile_button.clicked.connect(self.load_profile_dialog)
        self.save_profile_button.clicked.connect(self.save_profile_dialog)
        self.reload_button.clicked.connect(self.reload)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        # Enter inside a number field commits its text; explicit buttons apply changes.
        for button in self.findChildren(QtWidgets.QPushButton):
            button.setAutoDefault(False)
        self.reload()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def number(self, key, low, high, decimals, section, integer=False):
        field = QtWidgets.QSpinBox() if integer else QtWidgets.QDoubleSpinBox()
        if not integer:
            field.setDecimals(decimals)
        field.setRange(low, high)
        field.setKeyboardTracking(False)
        field.setMinimumHeight(28)
        field.setMinimumWidth(125)
        self.fields[key] = field
        if section:
            field.valueChanged.connect(partial(self.mark_dirty, section))
            field.lineEdit().textEdited.connect(partial(self.mark_dirty, section))
        return field

    def mark_dirty(self, section, *_):
        setattr(self, f"{section}_dirty", True)
        self.message("Unsent edits. Press Apply calibration / target or Apply PID for the edited section.")

    def invalidate_calibration(self, *_):
        self.confirmed.setChecked(False)

    def message(self, text, success=False):
        self.status.setText(text)
        self.status.setStyleSheet("color: #71e0c0" if success else "color: #ffc46b")

    @staticmethod
    def set_value(field, value):
        blocked = field.blockSignals(True)
        if isinstance(field, QtWidgets.QCheckBox):
            field.setChecked(value)
        else:
            field.setValue(value)
        field.blockSignals(blocked)

    def load_feedback(self, camera):
        for row in range(2):
            for col in range(2):
                self.set_value(self.fields[f"m{row}{col}"], camera.urad_per_pixel[row][col])
        self.set_value(self.confirmed, camera.calibration_confirmed)
        x, y = camera.reference_px or ((camera.width-1)/2, (camera.height-1)/2)
        self.set_value(self.fields["reference_x"], x)
        self.set_value(self.fields["reference_y"], y)
        roi = camera.analysis_roi or (0, 0, camera.width, camera.height)
        for key, value in zip(("roi_x", "roi_y", "roi_w", "roi_h"), roi):
            self.set_value(self.fields[key], value)
        self.set_value(self.roi_enabled, bool(camera.analysis_roi))
        self.feedback_baseline = self.feedback_form_values()
        self.loaded_feedback = feedback_values(camera)
        self.feedback_dirty = False
        self.update_roi_enabled()

    def load_pid(self, pid):
        self.set_value(self.enabled, pid.enabled)
        for key, value in pid_values(pid).items():
            if key in ("kp", "ki", "kd"):
                for axis in range(2):
                    self.set_value(self.fields[f"{key}_{axis}"], value[axis])
            elif key != "enabled":
                self.set_value(self.fields[key], value*1000 if key == "derivative_tau_s" else value)
        self.pid_baseline = self.pid_form_values()
        self.loaded_pid = pid_values(pid)
        self.pid_dirty = False

    def reload(self):
        camera, pid = self.engine.feedback_snapshot()
        self.load_feedback(camera)
        self.load_pid(pid)
        self.message("Live values loaded. No unsent edits.", True)

    def feedback_form_values(self):
        return {"urad_per_pixel": tuple(tuple(self.fields[f"m{r}{c}"].value() for c in range(2)) for r in range(2)),
                "calibration_confirmed": self.confirmed.isChecked(),
                "reference_px": (self.fields["reference_x"].value(), self.fields["reference_y"].value()),
                "analysis_roi": tuple(self.fields[k].value() for k in ("roi_x", "roi_y", "roi_w", "roi_h"))
                                if self.roi_enabled.isChecked() else ()}

    def pid_form_values(self):
        result = {"enabled": self.enabled.isChecked()}
        for key in self.loaded_pid if hasattr(self, "loaded_pid") else pid_values(self.engine.cfg.pid):
            if key in ("kp", "ki", "kd"):
                result[key] = tuple(self.fields[f"{key}_{axis}"].value() for axis in range(2))
            elif key != "enabled":
                result[key] = self.fields[key].value()/(1000 if key == "derivative_tau_s" else 1)
        return result

    def commit_text(self):
        for field in self.fields.values():
            field.interpretText()

    def send_feedback(self):
        self.commit_text()
        values = self.feedback_form_values()
        patch = {key: value for key, value in values.items() if value != self.feedback_baseline[key]}
        try:
            self.pending_feedback = self.engine.queue_feedback_settings(patch)
            self.message("Queued calibration / target update for the Far Field worker.")
        except ValueError as exc:
            self.message(str(exc))
        self.refresh()

    def send_pid(self):
        self.commit_text()
        values = self.pid_form_values()
        patch = {key: value for key, value in values.items() if value != self.pid_baseline[key]}
        self.request_pid(patch, "apply")

    def request_pid(self, patch, action):
        try:
            self.pending_pid = self.engine.queue_pid_settings(patch, reset=action == "reset")
            self.pid_action = action
            self.message("Waiting for PID acknowledgment. Nominal output is zero while the update is pending.")
        except ValueError as exc:
            self.message(str(exc))
        self.refresh()

    def pause(self):
        self.request_pid({"enabled": False}, "pause")

    def reset(self):
        self.request_pid({}, "reset")

    def update_roi_enabled(self, *_):
        enabled = self.roi_enabled.isChecked() and self.pending_feedback is None
        for key in ("roi_x", "roi_y", "roi_w", "roi_h"):
            self.fields[key].setEnabled(enabled)

    def use_optical_estimate(self):
        self.commit_text()
        try:
            scale = optical_scale(*(self.fields[k].value() for k in ("pitch", "focal", "magnification")))
            for key, value in (("m00", scale), ("m01", 0), ("m10", 0), ("m11", scale)):
                self.fields[key].setValue(value)
            self.confirmed.setChecked(False)
            self.mark_dirty("feedback")
            self.message(f"Optical estimate: {scale:g} µrad/pixel. Verify the scale and axis signs before confirming.")
        except ValueError as exc:
            self.message(str(exc))

    def use_detected_centroid(self):
        item = self.worker.latest.read()
        if item is None or not item.measurement.centroid_valid:
            self.message("No valid Far Field centroid. Adjust exposure, clipping limits or signal threshold first.")
            return
        if self.worker.stats(False)["age_ms"] >= 1000:
            self.message("Far Field image is stale; wait for a live centroid.")
            return
        self.fields["reference_x"].setValue(item.measurement.centroid_x)
        self.fields["reference_y"].setValue(item.measurement.centroid_y)

    def center_reference(self):
        self.fields["reference_x"].setValue((self.worker.cfg.width-1)/2)
        self.fields["reference_y"].setValue((self.worker.cfg.height-1)/2)

    def save_profile_dialog(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save applied feedback profile",
            str(self.engine.config_path.parent/"feedback_profile.json"), "Feedback profile (*.json)")
        if path:
            try:
                save_profile(Path(path), *self.engine.feedback_snapshot())
                self.message("Applied feedback profile saved. Load it in a future run, then Apply each section.", True)
            except (OSError, ValueError) as exc:
                self.message(str(exc))

    def load_profile_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load feedback profile",
            str(self.engine.config_path.parent), "Feedback profile (*.json)")
        if path:
            try:
                camera, pid = load_profile(Path(path), *self.engine.feedback_snapshot())
                # Compare the imported values against the live display baseline.
                live_camera, live_pid = self.engine.feedback_snapshot()
                self.load_feedback(live_camera)
                self.load_pid(live_pid)
                live_feedback_baseline, live_pid_baseline = self.feedback_baseline, self.pid_baseline
                self.load_feedback(camera)
                self.load_pid(pid)
                self.feedback_baseline, self.pid_baseline = live_feedback_baseline, live_pid_baseline
                self.feedback_dirty = self.feedback_form_values() != self.feedback_baseline
                self.pid_dirty = self.pid_form_values() != self.pid_baseline
                self.message("Profile loaded into editors. Press Apply calibration / target and Apply PID to use it.")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.message(f"Cannot load profile: {exc}")

    def refresh(self):
        control = self.engine.poll_control()
        camera, pid = self.engine.feedback_snapshot()
        state = self.worker.controls.read()
        if self.pending_feedback is not None and state.get("completed_request_id") == self.pending_feedback:
            if state["last_update_status"] == "applied":
                self.load_feedback(camera)
            self.pending_feedback = None
            self.message("Far Field: "+state["last_update_message"], state["last_update_status"] == "applied")
        settings = self.engine.publisher.settings_state
        if self.pending_pid is not None and settings["request_id"] == self.pending_pid and settings["status"] != "queued":
            if settings["status"] == "applied":
                if self.pid_action == "apply" or not self.pid_dirty:
                    self.load_pid(pid)
                elif self.pid_action == "pause":
                    self.enabled.setChecked(False)
                    # Preserve gain drafts, but resume must compare against the
                    # acknowledged paused state rather than the old enabled state.
                    self.pid_baseline["enabled"] = pid.enabled
                    self.loaded_pid = pid_values(pid)
            self.pending_pid = None
            self.message(settings["message"], settings["status"] == "applied")
        if not self.feedback_dirty and self.pending_feedback is None and feedback_values(camera) != self.loaded_feedback:
            self.load_feedback(camera)
        if not self.pid_dirty and self.pending_pid is None and pid_values(pid) != self.loaded_pid:
            self.load_pid(pid)
        busy_feedback = self.pending_feedback is not None or state["status"] == "queued"
        busy_pid = settings["status"] == "queued"
        self.apply_feedback.setEnabled(state["ready"] and not busy_feedback)
        self.apply_pid.setEnabled(not busy_pid)
        self.pause_pid.setEnabled(not busy_pid)
        self.reset_pid.setEnabled(not busy_pid)
        self.reload_button.setEnabled(not (busy_feedback or busy_pid))
        self.load_profile_button.setEnabled(not (busy_feedback or busy_pid))
        self.save_profile_button.setEnabled(not (busy_feedback or busy_pid or self.engine.publisher.feedback_blocked))
        for page in range(4):
            self.tabs.widget(page).setEnabled(not (busy_feedback if page < 2 else busy_pid))
        self.update_roi_enabled()
        matrix = camera.urad_per_pixel
        label = "confirmed" if camera.calibration_confirmed else "unconfirmed"
        if self.engine.simulated and self.worker.processor.calibrated and not camera.calibration_confirmed:
            label = "simulation demo scale"
        self.applied.setText(f"Applied Far Field calibration: {label} · [{matrix[0][0]:g}, {matrix[0][1]:g}; "
            f"{matrix[1][0]:g}, {matrix[1][1]:g}] µrad/px\n"
            f"Applied PID: {'enabled' if pid.enabled else 'paused'} · Kp {pid.kp} · Ki {pid.ki} · Kd {pid.kd}")
        self.live.setText(f"Feedback [{control['state']}]: {control['reason']}\n"
            f"Far Field interval {1000/self.worker.cfg.fps:.2f} ms · max sample age {pid.max_sample_age_ms:g} ms · "
            f"watchdog {pid.watchdog_ms:g} ms · output ±{pid.output_limit:g}")

    def showEvent(self, event):
        self.refresh()
        self.timer.start()
        super().showEvent(event)

    def closeEvent(self, event):
        self.timer.stop()
        event.accept()
