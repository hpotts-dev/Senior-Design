"""Assign physical cameras to roles before acquisition; never guess by discovery order."""
from pathlib import Path
from PySide6 import QtCore, QtWidgets

from .cameras import discover_basler
from .config import ROLES, load_config
from .windows_paths import assign_cameras, save_config, user_data_directory, writable_directory


class Discovery(QtCore.QThread):
    finished_scan = QtCore.Signal(object, str)

    def run(self):
        try:
            self.finished_scan.emit(discover_basler(), "")
        except Exception as exc:
            self.finished_scan.emit([], str(exc))


class CameraAssignmentDialog(QtWidgets.QDialog):
    def __init__(self, cfg, config_path, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.config_path = Path(config_path)
        if not writable_directory(self.config_path.parent):
            self.config_path = user_data_directory()/"config.toml"
        self.scan = None
        self.setWindowTitle("Connect cameras to this PC")
        self.resize(660, 350)
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel("Select the physical camera for each view. Serial numbers identify cameras on every computer. "
            "The PID uses Far Field exclusively. This dialog does not change adapter IPs or firewall settings.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QtWidgets.QFormLayout()
        self.selectors = {}
        for role in ROLES:
            combo = QtWidgets.QComboBox()
            self.selectors[role] = combo
            form.addRow(role.replace("_", " ").title(), combo)
        layout.addLayout(form)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.path_note = QtWidgets.QLabel(f"Settings file: {self.config_path}")
        self.path_note.setWordWrap(True)
        layout.addWidget(self.path_note)
        buttons = QtWidgets.QDialogButtonBox()
        self.refresh_button = buttons.addButton("Refresh cameras", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.save_button = buttons.addButton("Save camera assignment", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.refresh_button.clicked.connect(self.refresh_cameras)
        self.save_button.clicked.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_cameras()

    def refresh_cameras(self):
        if self.scan is not None and self.scan.isRunning():
            return
        self.saved_choices = {role: combo.currentData() for role, combo in self.selectors.items()}
        self.refresh_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status.setText("Searching for cameras… Close pylon Viewer before starting acquisition.")
        self.scan = Discovery(self)
        self.scan.finished_scan.connect(self.show_cameras)
        self.scan.start()

    def show_cameras(self, devices, error):
        for camera in self.cfg.cameras:
            combo = self.selectors[camera.role]
            selected = self.saved_choices.get(camera.role) or camera.serial
            combo.clear()
            combo.addItem("Choose a camera…", "")
            for device in devices:
                combo.addItem(f"{device['serial']} — {device['model']} — {device.get('ip') or device['interface']}", device["serial"])
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else 0)
        self.refresh_button.setEnabled(True)
        self.save_button.setEnabled(len(devices) >= 3)
        self.status.setText(f"Found {len(devices)} camera(s). Select the correct role for each serial." if not error else
                            "Camera discovery failed: "+error+". Install pylon's GigE drivers and check NETWORK_SETUP.md.")

    def save(self):
        try:
            updated = assign_cameras(self.cfg, {role: combo.currentData() for role, combo in self.selectors.items()})
            save_config(updated, self.config_path)
            self.accept()
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def done(self, result):
        if self.scan is not None and self.scan.isRunning():
            self.status.setText("Finishing camera discovery; please wait before closing.")
            return
        super().done(result)


def configure_cameras(cfg, config_path):
    from .gui import create_application
    app = create_application()
    dialog = CameraAssignmentDialog(cfg, config_path)
    try:
        accepted = dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        return dialog.config_path if accepted else None
    finally:
        dialog.deleteLater()
        app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
