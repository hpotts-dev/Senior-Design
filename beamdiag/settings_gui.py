"""Nonmodal per-camera settings. All SDK work is queued to the acquisition worker."""
import cv2
from PySide6 import QtCore, QtWidgets

from .camera_settings import ACQUISITION_FIELDS, LIVE_FIELDS
from .config import PIXEL_BITS, PID_SOURCE_ROLE


class CameraSettingsDialog(QtWidgets.QDialog):
    def __init__(self, engine, role, parent=None, *, preview_control=None):
        super().__init__(parent)
        self.engine, self.role = engine, role
        self.worker = engine.workers[role]
        self.pending_id = None
        self.loaded = False
        self.dirty = False
        self.loaded_values = None
        self.loaded_field_values = {}
        self._brightness_frame = None
        self._raw_mean = self._raw_peak = 0.0
        self.active_fields = ACQUISITION_FIELDS if role == "dark_field" else LIVE_FIELDS
        self.setWindowTitle(role.replace("_", " ").title()+" camera settings")
        self.resize(550, 570)
        self.fields = {}
        adc_max = (1 << PIXEL_BITS[self.worker.cfg.pixel_format])-1
        layout = QtWidgets.QVBoxLayout(self)
        self.tabs = tabs = QtWidgets.QTabWidget()
        camera_page, detection_page = QtWidgets.QWidget(tabs), QtWidgets.QWidget(tabs)
        camera_form, detection_form = QtWidgets.QFormLayout(camera_page), QtWidgets.QFormLayout(detection_page)
        tabs.addTab(camera_page, "Camera")
        if role != "dark_field":
            tabs.addTab(detection_page, "Centroid / clipping")
        else:
            detection_page.hide()
            description = QtWidgets.QLabel("Dark Field displays and records images without calculating a centroid. "
                                          "Exposure, gain and frame rate control this camera's images.")
            description.setWordWrap(True)
            camera_form.addRow(description)
        layout.addWidget(tabs)
        rows = (
            (camera_form, "exposure_us", "Exposure (µs)", 0.001, 1e7, 3, "Integration time. Lower exposure reduces brightness and clipping."),
            (camera_form, "gain", "Gain (dB)", 0, 100, 3, "Sensor gain. The actual camera's dB or raw units are shown after connection."),
            (camera_form, "fps", "Requested frame rate (fps)", 0.001, 10000, 3, "Requested camera cadence, subject to readout, exposure and network limits."),
            (detection_form, "background", "Background (DN)", 0, adc_max, 3, "Constant background subtracted before finding the centroid."),
            (detection_form, "threshold", "Signal threshold (DN above background)", 0, adc_max, 3, "Keep only intensity above this threshold after background subtraction."),
            (detection_form, "min_flux", "Minimum integrated signal", 0, 1e12, 1, "Reject centroids whose summed signal is at or below this value."),
            (detection_form, "max_saturated_fraction", "Maximum clipped pixels (%)", 0, 100, 3, "Reject a frame if more than this percentage of the analysis ROI is clipped."),
        )
        for form, key, label, low, high, decimals, tooltip in rows:
            field = QtWidgets.QDoubleSpinBox()
            field.setDecimals(decimals)
            field.setRange(low, high)
            field.setKeyboardTracking(False)
            field.setGroupSeparatorShown(True)
            field.setToolTip(tooltip)
            self.fields[key] = field
            form.addRow(label, field)
        self.preview_auto_contrast = None
        if preview_control is not None:
            self.preview_auto_contrast = QtWidgets.QCheckBox("Auto contrast (preview only)")
            self.preview_auto_contrast.setChecked(preview_control.isChecked())
            self.preview_auto_contrast.setToolTip("Turn off to see exposure and gain change the displayed brightness. "
                "This does not change camera settings, saved images or measurements.")
            self.preview_note = QtWidgets.QLabel()
            self.preview_note.setWordWrap(True)
            camera_form.addRow(self.preview_auto_contrast)
            camera_form.addRow(self.preview_note)
            self.preview_auto_contrast.toggled.connect(preview_control.setChecked)
            preview_control.toggled.connect(self.preview_auto_contrast.setChecked)
            self.preview_auto_contrast.toggled.connect(self.update_preview_note)
            self.update_preview_note()
        clipping = QtWidgets.QSpinBox()
        clipping.setRange(0, adc_max)
        clipping.setSpecialValueText(f"ADC maximum ({adc_max})")
        clipping.setToolTip("0 uses the sensor's ADC maximum. Otherwise pixels at or above this DN count as clipped.")
        self.fields["saturation_level"] = clipping
        detection_form.addRow("Clipping level (DN; 0 = ADC maximum)", clipping)
        explanation = QtWidgets.QLabel("These cameras are monochrome. Reduce exposure or gain to reduce clipping. "
            "Changing the clipping limit only changes which frames the centroid accepts.")
        explanation.setWordWrap(True)
        detection_form.addRow(explanation)
        self.range_note = QtWidgets.QLabel("Camera-reported ranges appear after connection.")
        self.range_note.setWordWrap(True)
        camera_form.addRow(self.range_note)
        self.applied = QtWidgets.QLabel()
        self.applied.setWordWrap(True)
        layout.addWidget(self.applied)
        self.live = QtWidgets.QLabel()
        self.live.setWordWrap(True)
        layout.addWidget(self.live)
        self.timing_note = QtWidgets.QLabel()
        self.timing_note.setWordWrap(True)
        layout.addWidget(self.timing_note)
        note = QtWidgets.QLabel("Apply takes effect between frames. Exposure, gain or FPS changes briefly restart "
            "only this camera's stream to discard old buffers. Settings apply to the current run.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(45)
        layout.addWidget(self.status)
        buttons = QtWidgets.QDialogButtonBox()
        self.apply_button = buttons.addButton("Apply", QtWidgets.QDialogButtonBox.ButtonRole.ApplyRole)
        self.reload_button = buttons.addButton("Reload live values", QtWidgets.QDialogButtonBox.ButtonRole.ResetRole)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Close)
        self.apply_button.clicked.connect(self.apply)
        self.reload_button.clicked.connect(self.reload)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        for key in ("exposure_us", "fps"):
            self.fields[key].valueChanged.connect(self.update_timing_note)
        for field in self.fields.values():
            field.valueChanged.connect(self.mark_dirty)
            # With keyboard tracking off, valueChanged waits for Enter/focus loss.
            # Protect incomplete typed drafts (including an empty field) immediately.
            field.lineEdit().textEdited.connect(self.mark_dirty)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.setInterval(200)
        # Show configured values even while unplugged; actual camera capabilities
        # and live values replace them when the worker connects.
        self.load_values(self.worker.controls.read()["values"])
        self.refresh()

    def load_values(self, values):
        for key, field in self.fields.items():
            field.blockSignals(True)
            field.setValue(values[key]*100 if key == "max_saturated_fraction" else values[key])
            field.blockSignals(False)
        self.dirty = False
        self.loaded_values = dict(values)
        self.loaded_field_values = {key: field.value() for key, field in self.fields.items()}
        self.update_timing_note()

    def mark_dirty(self):
        self.dirty = True
        if self.pending_id is None:
            self.status.setText("Edits are not yet applied. Press Apply to send them to the camera worker.")
            self.status.setStyleSheet("color: #ffc46b")

    def update_preview_note(self):
        if self.preview_auto_contrast.isChecked():
            self.preview_note.setText("Auto contrast keeps the preview bright as exposure changes. "
                "Turn it off to see exposure and gain change image brightness. This switch takes effect immediately.")
        else:
            maximum = (1 << PIXEL_BITS[self.worker.cfg.pixel_format])-1
            self.preview_note.setText(f"Preview brightness uses a fixed 0–{maximum} DN scale. "
                "Exposure and gain changes now affect displayed brightness. Raw image values are unchanged.")

    def update_timing_note(self):
        fps = self.fields["fps"].value()
        exposure = self.fields["exposure_us"].value()
        period = 1000/fps if fps > 0 else float("inf")
        text = f"Requested interval: {period:.2f} ms ({fps:g} fps)."
        if exposure/1000 > period:
            text += " Exposure is longer than that interval, so delivered FPS may be lower."
        if self.role == PID_SOURCE_ROLE and period > self.engine.cfg.pid.watchdog_ms:
            text += f" This interval exceeds the Far Field PID watchdog ({self.engine.cfg.pid.watchdog_ms:g} ms)."
        self.timing_note.setText(text)

    def reload(self):
        self.load_values(self.worker.controls.read()["values"])
        self.status.setText("Live values reloaded; no unsent edits.")
        self.status.setStyleSheet("color: #71e0c0")

    def apply(self):
        # Apply can be clicked without moving focus away from the spin box first.
        # Commit its typed text before comparing values with the loaded baseline.
        for key in self.active_fields:
            self.fields[key].interpretText()
        # Compare displayed values with their displayed baseline: device gain readback can have
        # finer precision than the spin box. An untouched rounded gain must not restart a stream.
        patch = {key: self.fields[key].value() for key in self.active_fields
                 if self.fields[key].value() != self.loaded_field_values.get(key)}
        if "max_saturated_fraction" in patch:
            patch["max_saturated_fraction"] /= 100
        try:
            self.pending_id = self.engine.queue_camera_settings(self.role, patch)
        except ValueError as exc:
            self.status.setText(str(exc))
            self.status.setStyleSheet("color: #ffc46b")
            return
        self.status.setText("Queued; waiting for the camera worker…")
        self.refresh()

    def refresh(self):
        state = self.worker.controls.read()
        if self.pending_id is not None and state.get("completed_request_id") == self.pending_id:
            self.load_values(state["values"])
            self.pending_id = None
            self.status.setText(state["last_update_message"])
            self.status.setStyleSheet("color: #71e0c0" if state["last_update_status"] == "applied" else "color: #ffc46b")
        ready, pending = state["ready"], state["status"] == "queued" or self.pending_id is not None
        caps = state.get("capabilities", {})
        ranges = []
        for key in ("exposure_us", "gain", "fps"):
            cap = caps.get(key, {})
            field = self.fields[key]
            supported = cap.get("supported", False)
            if supported:
                maximum = cap["max"]
                if key == "fps":
                    maximum = min(maximum, state.get("delivery_budget_max_fps", maximum))
                # Do not truncate an existing, valid device value while displaying its range.
                maximum = max(maximum, state["values"][key])
                # Even setRange with unchanged limits interprets pending spin-box
                # text. Never let the polling timer commit or erase an active edit.
                if not field.lineEdit().hasFocus():
                    blocked = field.blockSignals(True)
                    if field.minimum() != cap["min"] or field.maximum() != maximum:
                        field.setRange(cap["min"], maximum)
                    step = cap.get("increment") or (1.0 if key == "exposure_us" else 0.1)
                    if field.singleStep() != step:
                        field.setSingleStep(step)
                    field.blockSignals(blocked)
                ranges.append(f"{key}: {cap['min']:g}–{maximum:g} {cap['unit']}")
                if key == "gain":
                    form = field.parentWidget().layout()
                    form.labelForField(field).setText(f"Gain ({cap['unit']})")
            field.setEnabled(ready and supported and not pending)
        self.range_note.setText("\n".join(ranges) or "Camera-reported controls are not available yet.")
        if ready and not self.loaded:
            self.load_values(state["values"])
            self.loaded = True
            if self.pending_id is None:
                self.status.setText(state["message"])
                self.status.setStyleSheet("color: #71e0c0")
        elif ready and not pending and not self.dirty and state["values"] != self.loaded_values:
            self.load_values(state["values"])
        if not ready:
            self.status.setText(state["message"])
        for key in ("background", "threshold", "min_flux", "saturation_level", "max_saturated_fraction"):
            self.fields[key].setEnabled(key in self.active_fields and ready and not pending)
        self.apply_button.setEnabled(ready and not pending)
        self.reload_button.setEnabled(not pending)
        v = state["values"]
        prefix = "Live" if ready else "Configured"
        result = (f"Camera resulting rate: {state.get('resulting_fps') or 0:.2f} fps" if ready else
                  "Connect the camera to view live values and apply changes.")
        self.applied.setText(f"{prefix}: exposure {v['exposure_us']:g} µs · gain {v['gain']:g} · requested {v['fps']:g} fps\n"
                            f"{result}")
        item = self.worker.latest.read()
        if item:
            m = item.measurement
            delivered_fps = self.worker.stats(False)["fps"]
            if m is None:
                height, width = item.frame.image.shape
                if self._brightness_frame != item.frame.received_ns:
                    self._raw_mean = cv2.mean(item.frame.image)[0]
                    self._raw_peak = cv2.minMaxLoc(item.frame.image)[1]
                    self._brightness_frame = item.frame.received_ns
                self.live.setText(f"Delivered: {delivered_fps:.1f} fps · {width} × {height}\n"
                                  f"Raw brightness: mean {self._raw_mean:.2f} DN · peak {self._raw_peak:g} DN\n"
                                  "Display and capture only; no centroid calculation.")
            else:
                self.live.setText(f"Peak: {m.peak:g} DN · clipped: {100*m.saturated_fraction:.3f}% · "
                                  f"delivered: {delivered_fps:.1f} fps\nCentroid status: {m.reason}")
        else:
            self.live.setText("Waiting for a frame")

    def showEvent(self, event):
        self.loaded = False
        self.refresh()
        self.timer.start()
        super().showEvent(event)

    def closeEvent(self, event):
        self.timer.stop()
        event.accept()
