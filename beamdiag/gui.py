"""Qt GUI: the main thread only reads latest frames; acquisition never waits for repaint."""
from __future__ import annotations

from time import perf_counter_ns
from pathlib import Path
import os
import cv2
from PySide6 import QtCore, QtGui, QtWidgets

from .config import PID_SOURCE_ROLE
from .display import preview_u8, resize_preview
from .runtime import DiagnosticEngine


class ReferenceImage(QtWidgets.QLabel):
    reference_selected = QtCore.Signal(float, float)

    def __init__(self):
        super().__init__("Waiting for camera…")
        self.frame_size: tuple[int, int] | None = None
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.setToolTip("Click the image to place the amber reference. Z uses detected centroids.")

    def frame_position(self, position: QtCore.QPointF) -> tuple[float, float] | None:
        pixmap = self.pixmap()
        if not self.frame_size or pixmap is None or pixmap.isNull():
            return None
        # QLabel centers the pixmap inside its contents; ignore clicks in the surrounding padding.
        logical_size = pixmap.deviceIndependentSize().toSize()
        bounds = QtWidgets.QStyle.alignedRect(self.layoutDirection(), self.alignment(), logical_size,
                                             self.contentsRect())
        x, y = position.x()-bounds.x(), position.y()-bounds.y()
        if not (0 <= x < bounds.width() and 0 <= y < bounds.height()):
            return None
        width, height = self.frame_size
        # Invert OpenCV's pixel-center resize mapping, retaining fractional-pixel coordinates.
        fx = (x+0.5)*width/bounds.width()-0.5
        fy = (y+0.5)*height/bounds.height()-0.5
        return max(0.0, min(width-1.0, fx)), max(0.0, min(height-1.0, fy))

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            position = self.frame_position(event.position())
            if position is not None:
                self.reference_selected.emit(*position)
                event.accept()
                return
        super().mousePressEvent(event)


class CameraPanel(QtWidgets.QGroupBox):
    def __init__(self, role: str):
        suffix = " (PID source)" if role == PID_SOURCE_ROLE else " (diagnostic)"
        super().__init__(role.replace("_", " ").title()+suffix)
        self.image = ReferenceImage()
        self.image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(240, 200)
        self.image.setStyleSheet("background: #080c12; color: #9eb2c8")
        self.image.setScaledContents(False)
        self.image.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
        self.metrics = QtWidgets.QLabel()
        self.metrics.setMinimumHeight(130)
        self.metrics.setWordWrap(True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.image, 1)
        contrast = QtWidgets.QHBoxLayout()
        self.auto_contrast = QtWidgets.QCheckBox("Auto contrast (preview only)")
        self.auto_contrast.setChecked(True)
        self.auto_contrast.setToolTip("Brighten dim images without changing exposure, recordings or centroid/PID values. "
                                     "Sensor noise may also become visible. Uncheck for the full sensor range.")
        contrast.addWidget(self.auto_contrast)
        self.display_range = QtWidgets.QLabel()
        self.display_range.setToolTip("Native intensity mapped to black and white on screen; brighter preview pixels may clip.")
        contrast.addWidget(self.display_range)
        contrast.addStretch()
        layout.addLayout(contrast)
        layout.addWidget(self.metrics)
        editor = QtWidgets.QHBoxLayout()
        self.reference_x = QtWidgets.QDoubleSpinBox()
        self.reference_y = QtWidgets.QDoubleSpinBox()
        for field in (self.reference_x, self.reference_y):
            field.setDecimals(3)
            field.setRange(0, 0)
            field.setKeyboardTracking(False)
            field.setMinimumWidth(85)
            field.setToolTip("Delivered-frame pixel coordinates; origin is the top-left pixel")
        editor.addWidget(QtWidgets.QLabel("X"))
        editor.addWidget(self.reference_x)
        editor.addWidget(QtWidgets.QLabel("Y"))
        editor.addWidget(self.reference_y)
        self.apply_reference = QtWidgets.QPushButton("Set")
        editor.addWidget(self.apply_reference)
        layout.addLayout(editor)
        actions = QtWidgets.QHBoxLayout()
        self.use_centroid = QtWidgets.QPushButton("Use detected centroid")
        self.center = QtWidgets.QPushButton("Center reference")
        self.camera_settings = QtWidgets.QPushButton("Settings…")
        self.camera_settings.setToolTip("Exposure, gain, frame rate, centroid thresholds and clipping limits")
        actions.addWidget(self.use_centroid)
        actions.addWidget(self.center)
        actions.addWidget(self.camera_settings)
        layout.addLayout(actions)
        self.background_button = QtWidgets.QPushButton("Subtract background…")
        self.background_button.setToolTip("Cover this camera first, then press to average and save a per-pixel dark reference. Uncover after the saved message.")
        layout.addWidget(self.background_button)
        self.background_notice = QtWidgets.QLabel("Cover camera before capturing a background.")
        self.background_notice.setWordWrap(True)
        layout.addWidget(self.background_notice)
        if role == "dark_field":
            for widget in (self.reference_x, self.reference_y, self.apply_reference, self.use_centroid, self.center):
                widget.setEnabled(False)
            for index in range(editor.count()):
                editor.itemAt(index).widget().hide()
            self.use_centroid.hide()
            self.center.hide()
            self.image.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.reference_notice = QtWidgets.QLabel("Click the image, or enter X/Y and press Set.")
        if role == "dark_field":
            self.reference_notice.setText("Dark Field is displayed and captured without centroid processing.")
        self.reference_notice.setWordWrap(True)
        self.reference_notice.setMinimumHeight(34)
        layout.addWidget(self.reference_notice)
        self.last_sequence = -1
        self.display_dirty = False
        self.editor_revision: int | None = None

    def sync_reference(self, x: float, y: float, revision: int, width: int, height: int):
        if self.editor_revision != revision or self.reference_x.maximum() != width-1 or self.reference_y.maximum() != height-1:
            for field, value, limit in ((self.reference_x, x, width-1), (self.reference_y, y, height-1)):
                field.setMaximum(limit)
                field.setValue(value)
            self.editor_revision = revision


class DiagnosticWindow(QtWidgets.QMainWindow):
    def __init__(self, engine: DiagnosticEngine):
        super().__init__()
        self.engine = engine
        self.settings_dialogs = {}
        self.feedback_dialog = None
        self.config_dialog = None
        self._config_applying = False
        self.setWindowTitle("Three-camera beam diagnostics"+(" — SIMULATION" if engine.simulated else " — BASLER"))
        self.resize(1450, 960)
        central = QtWidgets.QWidget()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(central)
        self.setCentralWidget(scroll)
        layout = QtWidgets.QVBoxLayout(central)
        heading = QtWidgets.QLabel("BEAM DIAGNOSTICS  /  "+("SIMULATED CAMERAS · DEMO CALIBRATION" if engine.simulated else "LIVE BASLER CAMERAS"))
        heading.setStyleSheet("font-size: 20px; font-weight: 600; padding: 8px")
        layout.addWidget(heading)
        row = QtWidgets.QHBoxLayout()
        self.panels = {r: CameraPanel(r) for r in engine.workers}
        for role, panel in self.panels.items():
            row.addWidget(panel)
            panel.image.reference_selected.connect(lambda x, y, r=role: self._manual_reference(r, x, y))
            panel.apply_reference.clicked.connect(lambda checked=False, r=role: self._numeric_reference(r))
            panel.use_centroid.clicked.connect(lambda checked=False, r=role: self._show_reference_changes(
                (self.engine.use_centroid_reference(r),)))
            panel.center.clicked.connect(lambda checked=False, r=role: self._show_reference_changes(
                (self.engine.center_reference(r),)))
            panel.camera_settings.clicked.connect(lambda checked=False, r=role: self._camera_settings(r))
            panel.background_button.clicked.connect(lambda checked=False, r=role: self._background(r))
            panel.auto_contrast.toggled.connect(lambda checked=False, r=role: self._display_contrast(r))
        layout.addLayout(row, 1)
        controls = QtWidgets.QHBoxLayout()
        self.record = QtWidgets.QPushButton("Record frames (R)")
        self.record.setCheckable(True)
        self.record.setChecked(engine.recorder.recording.is_set())
        self.record.toggled.connect(self._record)
        controls.addWidget(self.record)
        snapshot = QtWidgets.QPushButton("Save current frames (S)")
        snapshot.clicked.connect(self._snapshot)
        controls.addWidget(snapshot)
        reference = QtWidgets.QPushButton("Use detected centroids (Z)")
        reference.clicked.connect(self._references)
        controls.addWidget(reference)
        feedback = QtWidgets.QPushButton("Control feedback…")
        feedback.clicked.connect(self._feedback_settings)
        controls.addWidget(feedback)
        self.config_button = QtWidgets.QPushButton("Configuration…")
        self.config_button.clicked.connect(self._configuration)
        controls.addWidget(self.config_button)
        controls.addStretch()
        stop = QtWidgets.QPushButton("Stop")
        stop.clicked.connect(self.close)
        controls.addWidget(stop)
        layout.addLayout(controls)
        timed = QtWidgets.QHBoxLayout()
        timed.addWidget(QtWidgets.QLabel("Always-on image capture interval (seconds):"))
        self.snapshot_interval = QtWidgets.QDoubleSpinBox()
        self.snapshot_interval.setRange(.1, 86400)
        self.snapshot_interval.setDecimals(2)
        self.snapshot_interval.setKeyboardTracking(False)
        self.snapshot_interval.setValue(engine.cfg.recording.snapshot_interval_s)
        timed.addWidget(self.snapshot_interval)
        self.apply_interval = QtWidgets.QPushButton("Apply interval and capture")
        self.apply_interval.clicked.connect(self._apply_snapshot_interval)
        timed.addWidget(self.apply_interval)
        self.restart_timer = QtWidgets.QPushButton("Capture now / restart timer")
        self.restart_timer.clicked.connect(lambda: self.engine.recorder.restart_snapshot_timer())
        timed.addWidget(self.restart_timer)
        timed.addStretch()
        layout.addLayout(timed)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        location = self.location = QtWidgets.QLabel(f"Capture folder: {engine.capture_dir}\nRaw data, readable CSVs and timed PNGs are written live. Timed capture stays active while Record is off.")
        location.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(location)
        self.shortcuts = []
        for key, callback in [("R", self.record.toggle), ("S", self._snapshot), ("Z", self._references), ("Q", self.close)]:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), self)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        self._tick_count = 0
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(max(1, round(1000/engine.cfg.display_fps)))

    def _record(self, enabled: bool):
        if enabled:
            self.engine.recorder.recording.set()
        else:
            self.engine.recorder.recording.clear()
        self.record.setText("Stop recording (R)" if enabled else "Record frames (R)")

    def _snapshot(self):
        self.statusBar().showMessage(f"Queued {self.engine.snapshot()} snapshots", 3000)

    def _apply_snapshot_interval(self):
        self.snapshot_interval.interpretText()
        try:
            self.engine.set_snapshot_interval(self.snapshot_interval.value())
            self.statusBar().showMessage("Interval saved. Timed capture will run on the next frame, then repeat at this interval.", 6000)
        except (ValueError, OSError) as exc:
            self.statusBar().showMessage(f"Cannot save interval: {exc}", 8000)

    def _background(self, role):
        try:
            self.engine.workers[role].request_background()
            self.panels[role].background_notice.setText(self.engine.workers[role].background_status)
        except ValueError as exc:
            self.panels[role].background_notice.setText(str(exc))

    def _configuration(self):
        if self.config_dialog is None:
            from .config_gui import ConfigDialog
            self.config_dialog = ConfigDialog(self.engine, self)
            self.config_dialog.applying.connect(self._configuration_applying)
            self.config_dialog.applied.connect(self._configuration_applied)
        self.config_dialog.show()
        self.config_dialog.raise_()
        self.config_dialog.activateWindow()

    def _configuration_applying(self, active):
        self._config_applying = active
        self.centralWidget().setEnabled(not active)
        if active:
            self.timer.stop()
            for dialog in self.settings_dialogs.values():
                dialog.close()
                dialog.deleteLater()
            self.settings_dialogs.clear()
            if self.feedback_dialog is not None:
                self.feedback_dialog.close()
                self.feedback_dialog.deleteLater()
                self.feedback_dialog = None
        else:
            self.timer.start(max(1, round(1000/self.engine.cfg.display_fps)))

    def _configuration_applied(self):
        for panel in self.panels.values():
            panel.last_sequence = -1
            panel.editor_revision = None
        self.snapshot_interval.setValue(self.engine.cfg.recording.snapshot_interval_s)
        self.location.setText(f"Capture folder: {self.engine.capture_dir}\nFiles are written live. Timed capture stays active while Record is off.")

    def _references(self):
        self._show_reference_changes(self.engine.set_references_detailed())

    def _camera_settings(self, role):
        if role not in self.settings_dialogs:
            from .settings_gui import CameraSettingsDialog
            self.settings_dialogs[role] = CameraSettingsDialog(self.engine, role, self,
                preview_control=self.panels[role].auto_contrast)
        dialog = self.settings_dialogs[role]
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _feedback_settings(self):
        if self.feedback_dialog is None:
            from .feedback_gui import FeedbackSettingsDialog
            self.feedback_dialog = FeedbackSettingsDialog(self.engine, self)
        self.feedback_dialog.show()
        self.feedback_dialog.raise_()
        self.feedback_dialog.activateWindow()

    def _manual_reference(self, role: str, x: float, y: float):
        if role == "dark_field":
            return
        self._show_reference_changes((self.engine.set_reference(role, x, y),))

    def _numeric_reference(self, role: str):
        panel = self.panels[role]
        self._manual_reference(role, panel.reference_x.value(), panel.reference_y.value())

    def _show_reference_changes(self, changes):
        for change in changes:
            panel = self.panels[change.role]
            if change.applied:
                panel.reference_notice.setText(f"Reference set to ({change.x:.3f}, {change.y:.3f}) px.")
                panel.reference_notice.setStyleSheet("color: #71e0c0")
                panel.display_dirty = True
            else:
                panel.reference_notice.setText(f"Reference unchanged: {change.reason}.")
                panel.reference_notice.setStyleSheet("color: #ffc46b")
        count = sum(change.applied for change in changes)
        self.statusBar().showMessage(f"Set {count} of {len(changes)} references. See each camera's message.", 6000)
        # Refresh even if the camera has not delivered another frame: the target is a UI action.
        self.refresh()

    def _display_contrast(self, role: str):
        self.panels[role].display_dirty = True
        self.refresh()

    def refresh(self):
        if self._config_applying:
            return
        for role, worker in self.engine.workers.items():
            panel = self.panels[role]
            item = worker.latest.read()
            new_frame = item is not None and item.frame.sequence != panel.last_sequence
            if item is None or (not new_frame and not panel.display_dirty):
                continue
            frame, m = item.frame, item.measurement
            image = item.corrected_image if item.corrected_image is not None else frame.image
            scale = min(1.0, min(self.engine.cfg.display_width, max(1, panel.image.width()))/image.shape[1],
                        max(1, panel.image.height())/image.shape[0])
            if scale < 1:
                image = resize_preview(image, max(1, round(image.shape[1]*scale)), max(1, round(image.shape[0]*scale)))
            image, white = preview_u8(image, worker.cfg.pixel_format, panel.auto_contrast.isChecked())
            panel.display_range.setText(f"0–{white:.0f} DN")
            # Display overlay lives in a small display-only copy; raw recorded pixels are untouched.
            display = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
            height, width = frame.image.shape
            rx, ry, revision = worker.processor.reference_state(width, height)
            panel.sync_reference(rx, ry, revision, width, height)
            sx, sy = display.shape[1]/width, display.shape[0]/height
            if worker.cfg.analysis_roi:
                x, y, w, h = worker.cfg.analysis_roi
                cv2.rectangle(display, (round(x*sx), round(y*sy)),
                              (round((x+w-1)*sx), round((y+h-1)*sy)), (100, 160, 255), 1)
            if m is not None and m.centroid_valid:
                cv2.drawMarker(display, (round((m.centroid_x+0.5)*sx-0.5), round((m.centroid_y+0.5)*sy-0.5)),
                               (0, 230, 180), cv2.MARKER_CROSS, 16, 1)
            if role != "dark_field":
                cv2.drawMarker(display, (round((rx+0.5)*sx-0.5), round((ry+0.5)*sy-0.5)),
                               (255, 180, 40), cv2.MARKER_TILTED_CROSS, 12, 1)
            qimage = QtGui.QImage(display.data, display.shape[1], display.shape[0], display.strides[0],
                                 QtGui.QImage.Format.Format_RGB888)
            panel.image.setPixmap(QtGui.QPixmap.fromImage(qimage.copy()))
            panel.image.frame_size = (width, height)
            panel.last_sequence = frame.sequence
            panel.display_dirty = False
            if new_frame:
                self.engine.note_display(frame.received_ns, frame.camera_to_host_ms)
        self._tick_count += 1
        # Stats and text refresh at ~5 Hz, independent of image refresh rate.
        if self._tick_count % max(1, round(self.engine.cfg.display_fps/5)):
            self.engine.poll_control()
            return
        stats = self.engine.stats(include_timing=False)
        for role, worker in self.engine.workers.items():
            s, panel = stats["cameras"][role], self.panels[role]
            panel.background_notice.setText(worker.background_status)
            panel.background_button.setEnabled(s["state"] == "running")
            item = worker.latest.read()
            if not item:
                panel.metrics.setText(f"{s['state']}\n{s['last_error']}")
                continue
            m = item.measurement
            image_age = (perf_counter_ns()-item.frame.received_ns)/1e6+(item.frame.camera_to_host_ms or 0)
            if m is None:
                panel.metrics.setText(f"{s['state']} · {s['fps']:.1f} fps · frame {item.frame.sequence}\n"
                    f"Dark Field: display and capture only\n{worker.cfg.pixel_format} · {item.frame.image.shape[1]} × {item.frame.image.shape[0]}\n"
                    f"Image age {image_age:.1f} ms\n"
                    f"SDK skipped {s['sdk_skipped']} · failures {s['errors']} · {s['last_error']}")
                continue
            height, width = item.frame.image.shape
            rx, ry, _ = worker.processor.reference_state(width, height)
            angular = f"({m.error_x_urad:+.3f}, {m.error_y_urad:+.3f}) µrad" if m.angular_valid else m.reason.upper()
            stale = " · STALE" if s["age_ms"] > self.engine.cfg.pid.watchdog_ms else ""
            panel.metrics.setText(f"{s['state']}{stale} · {s['fps']:.1f} fps · frame {m.sequence}\n"
                f"Centroid ({m.centroid_x:.3f}, {m.centroid_y:.3f}) px\n"
                f"Reference ({rx:.3f}, {ry:.3f}) px\n"
                f"Peak {m.peak:g} DN · clipped {100*m.saturated_fraction:.2f}%\n"
                f"Displacement ({m.error_x_px:+.3f}, {m.error_y_px:+.3f}) px\nError {angular}\n"
                f"Processing {m.host_processing_ms:.2f} ms · image age {image_age:.1f} ms\n"
                f"SDK skipped {s['sdk_skipped']} · failures {s['errors']} · {s['last_error']}")
        r, p = stats["recording"], stats["control"]
        elapsed = p["host_to_pid_ms"]
        timing = f"{elapsed:.2f} ms" if elapsed is not None else "waiting"
        self.status.setText(f"Nominal PID · Far Field only [{p['state']}]: X {p['x']:+.5f}, Y {p['y']:+.5f} normalized · "
            f"host retrieval → PID {timing}\n{p['reason']}\nSaved {r['frames_saved']} · queue {r['frame_queue']} · "
            f"frame drops {r['frames_dropped']} · telemetry drops {r['telemetry_dropped']} · "
            f"write failures {r['write_errors']} {r['last_error']}")
        self.record.blockSignals(True)
        self.record.setChecked(r["recording"])
        self.record.setText("Stop recording (R)" if r["recording"] else "Record frames (R)")
        self.record.blockSignals(False)

    def closeEvent(self, event):
        if self._config_applying:
            self.statusBar().showMessage("Finishing the configuration update before closing…", 4000)
            event.ignore()
            return
        self.timer.stop()
        for dialog in self.settings_dialogs.values():
            dialog.close()
        if self.feedback_dialog is not None:
            self.feedback_dialog.close()
        if self.config_dialog is not None:
            self.config_dialog.close()
        # Engine shutdown is owned by the CLI finally block after Qt exits.
        event.accept()


def create_application():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # Qt's offscreen plugin may have an empty font database on Windows.
    if not QtGui.QFontDatabase.families():
        font = Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or ".")/"Fonts"/"segoeui.ttf"
        if font.is_file():
            QtGui.QFontDatabase.addApplicationFont(str(font))
    app.setFont(QtGui.QFont("Segoe UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet("QWidget { background: #141c27; color: #dbe6f2; font-size: 13px; } "
        "QGroupBox { border: 1px solid #34445b; border-radius: 6px; margin-top: 14px; padding-top: 14px; } "
        "QGroupBox::title { subcontrol-origin: margin; left: 12px; } "
        "QPushButton { background: #26384e; padding: 10px; border-radius: 4px; } "
        "QPushButton:checked { background: #14654c; } QStatusBar { color: #71e0c0; }")
    return app


def run_gui(engine: DiagnosticEngine, duration_s: float = 0.0) -> int:
    app = create_application()
    window = DiagnosticWindow(engine)
    window.show()
    stop_timer = QtCore.QTimer()
    stop_timer.timeout.connect(lambda: window.close() if engine.stop.is_set() else None)
    stop_timer.start(100)
    if duration_s > 0:
        QtCore.QTimer.singleShot(round(duration_s*1000), window.close)
    return app.exec()
