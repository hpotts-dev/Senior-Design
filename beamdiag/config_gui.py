"""Full TOML editor, with an orderly asynchronous acquisition restart for any setting."""
from PySide6 import QtCore, QtGui, QtWidgets
from .config import parse_config, bandwidth_report
from .windows_paths import config_text


class ApplyConfigThread(QtCore.QThread):
    completed = QtCore.Signal(str)

    def __init__(self, engine, cfg, parent=None):
        super().__init__(parent)
        self.engine, self.cfg = engine, cfg

    def run(self):
        try:
            self.engine.reconfigure(self.cfg)
            self.completed.emit("")
        except Exception as exc:
            self.completed.emit(str(exc))


class ConfigDialog(QtWidgets.QDialog):
    applying = QtCore.Signal(bool)
    applied = QtCore.Signal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.task = None
        self.setWindowTitle("Edit all configuration settings")
        self.resize(920, 760)
        layout = QtWidgets.QVBoxLayout(self)
        label = QtWidgets.QLabel(f"{engine.config_path}\nSave & Apply validates all settings, finishes the current capture, and starts a new capture folder. "
            "Camera feeds pause briefly; the application stays open. Exposure/FPS controls and snapshot timing can also be changed directly without this restart.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.editor = QtWidgets.QPlainTextEdit()
        self.editor.setFont(QtGui.QFont("Consolas", 10))
        self.editor.setPlainText(config_text(engine.current_config()))
        layout.addWidget(self.editor)
        self.notice = QtWidgets.QLabel("All values below reflect the running configuration. Errors are checked before saving.")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        row = QtWidgets.QHBoxLayout()
        self.validate_button = QtWidgets.QPushButton("Validate")
        self.validate_button.clicked.connect(self.validate_text)
        row.addWidget(self.validate_button)
        self.reload_button = QtWidgets.QPushButton("Reload running values")
        self.reload_button.clicked.connect(lambda: self.editor.setPlainText(config_text(engine.current_config())))
        row.addWidget(self.reload_button)
        self.apply_button = QtWidgets.QPushButton("Save & Apply")
        self.apply_button.clicked.connect(self.apply_text)
        row.addWidget(self.apply_button)
        layout.addLayout(row)

    def validate_text(self):
        try:
            cfg = parse_config(self.editor.toPlainText())
            if not bandwidth_report(cfg)["within_budget"]:
                raise ValueError("Image payload exceeds the shared network budget")
            self.notice.setText("Configuration is valid.")
            return cfg
        except (ValueError, TypeError) as exc:
            self.notice.setText(f"Cannot apply: {exc}")
            return None

    def apply_text(self):
        cfg = self.validate_text()
        if cfg is None or self.task is not None:
            return
        self.applying.emit(True)
        self.editor.setReadOnly(True)
        for button in (self.validate_button, self.reload_button, self.apply_button):
            button.setEnabled(False)
        self.notice.setText("Draining saved data and applying configuration…")
        self.task = ApplyConfigThread(self.engine, cfg, self)
        self.task.completed.connect(self._completed)
        self.task.start()

    def _completed(self, error):
        # finished may follow the completed signal; wait only for the now-returning run().
        self.task.wait()
        self.task.deleteLater()
        self.task = None
        self.editor.setReadOnly(False)
        for button in (self.validate_button, self.reload_button, self.apply_button):
            button.setEnabled(True)
        self.notice.setText(f"Apply failed: {error}" if error else "Configuration saved and applied. A new capture is running.")
        self.applied.emit()
        self.applying.emit(False)

    def closeEvent(self, event):
        if self.task is not None:
            event.ignore()
        else:
            event.accept()
