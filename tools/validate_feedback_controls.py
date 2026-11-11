"""Exercise feedback UI, real PID subprocess and continuous simulated recording."""
from __future__ import annotations

import os
for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(key, "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import json
import socket
import sys
import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beamdiag.config import load_config
from beamdiag.gui import DiagnosticWindow, create_application
from beamdiag.runtime import DiagnosticEngine
from beamdiag.frame_io import load_saved_frame
from PySide6 import QtCore


def main():
    cv2.setNumThreads(1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(dir=ROOT/"validation") as tmp:
        config = Path(tmp)/"config.toml"
        config.write_text((ROOT/"config.toml").read_text().replace("port = 49152", f"port = {port}"))
        cfg = load_config(config)
        # Operator dark references belong to physical cameras, not this simulation.
        cfg = replace(cfg, cameras=tuple(replace(c, background_file="") for c in cfg.cameras),
                      recording=replace(cfg.recording, start_recording=True,
                                             output_dir="validation/feedback_runs"))
        app = create_application()
        engine = DiagnosticEngine(cfg, config, ROOT, simulated=True)
        stages, failures = {}, []
        window = None
        try:
            engine.start()
            window = DiagnosticWindow(engine)
            window.show()
            window._feedback_settings()
            dialog = window.feedback_dialog

            def guarded(action):
                def run():
                    try:
                        action()
                    except Exception as exc:
                        failures.append(str(exc))
                        window.close()
                return run

            def capture(name, expected):
                dialog.refresh()
                control = engine.poll_control()
                m = engine.workers["far_field"].latest.read().measurement
                stages[name] = {"control": control, "calibration_confirmed": engine.workers["far_field"].cfg.calibration_confirmed,
                    "measurement_reason": m.reason, "angular_valid": m.angular_valid,
                    "error_px": [m.error_x_px, m.error_y_px],
                    "error_urad": [m.error_x_urad, m.error_y_urad] if m.angular_valid else None,
                    "kp": engine.cfg.pid.kp, "enabled": engine.cfg.pid.enabled}
                if control["state"] != expected:
                    raise RuntimeError(f"{name}: expected {expected}, got {control}")

            def unconfirm():
                dialog.confirmed.setChecked(False)
                dialog.apply_feedback.click()

            def calibrate():
                dialog.fields["m00"].setValue(2)
                dialog.fields["m11"].setValue(2)
                dialog.confirmed.setChecked(True)
                dialog.roi_enabled.setChecked(True)
                camera = engine.workers["far_field"].cfg
                width, height = min(384, camera.width), min(384, camera.height)
                # Keep the synthetic beam inside the test region for full-sensor
                # configurations as well as the original 512-pixel test frames.
                for key, value in (("roi_x", (camera.width-width)//2), ("roi_y", (camera.height-height)//2),
                                   ("roi_w", width), ("roi_h", height)):
                    dialog.fields[key].setValue(value)
                dialog.apply_feedback.click()

            def tune():
                dialog.fields["kp_0"].setValue(.04)
                dialog.fields["kp_1"].setValue(.05)
                dialog.fields["ki_0"].setValue(.001)
                dialog.fields["ki_1"].setValue(.002)
                dialog.fields["output_limit"].setValue(.2)
                dialog.fields["deadband_urad"].setValue(.01)
                dialog.apply_pid.click()

            def resume():
                dialog.enabled.setChecked(True)
                dialog.apply_pid.click()

            for ms, action in ((1000, unconfirm), (2000, lambda: capture("uncalibrated", "invalid")),
                (3000, calibrate), (4000, lambda: capture("calibrated", "active")),
                (5000, tune), (6000, lambda: capture("tuned", "active")),
                (7000, dialog.pause), (8000, lambda: capture("paused", "paused")),
                (9000, resume), (10000, lambda: capture("resumed", "active")),
                (10500, dialog.reset), (11000, unconfirm),
                (12000, lambda: capture("confirmation_revoked", "invalid")), (13000, window.close)):
                QtCore.QTimer.singleShot(ms, guarded(action))
            app.exec()
            # Stop acquisition before grabbing screenshots: encoding an image on
            # the GUI thread must not affect the latency measurement window.
            engine.stop.set()
            for worker in engine.workers.values():
                worker.thread.join(timeout=3)
            dialog.show()
            dialog.reload()
            for index, name in enumerate(("calibration", "region", "pid", "limits")):
                dialog.tabs.setCurrentIndex(index)
                app.processEvents()
                dialog.grab().save(str(ROOT/"validation"/f"feedback_{name}_preview.png"))
            dialog.close()
        finally:
            summary = engine.close()
            if window is not None:
                window.close()
                window.deleteLater()
                app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                app.processEvents()
        rows = list(csv.DictReader((engine.session/"frame_index.csv").open(newline="", encoding="utf-8")))
        for row in rows:
            pixels = load_saved_frame(engine.session, row)
            if pixels.shape != (int(row["height"]), int(row["width"])):
                failures.append("Saved frame shape mismatch")
        feedback_events = [json.loads(line) for line in (engine.session/"settings_changes.jsonl").read_text().splitlines()]
        pid_events = [json.loads(line) for line in (engine.session/"pid_settings_changes.jsonl").read_text().splitlines()]
        if len(feedback_events) != 3 or not all(e["success"] for e in feedback_events):
            failures.append("Expected three successful calibration/region updates")
        if len(pid_events) != 4 or not all(e["success"] for e in pid_events):
            failures.append("Expected four acknowledged PID updates")
        for name in ("calibrated", "tuned", "resumed"):
            stage = stages.get(name, {})
            if stage.get("error_urad") is None or any(abs(a-2*b) > 1e-9 for a, b in zip(stage["error_urad"], stage["error_px"])):
                failures.append(f"{name}: calibration matrix not reflected in angular error")
        for name in ("uncalibrated", "paused", "confirmation_revoked"):
            control = stages.get(name, {}).get("control", {})
            if (control.get("x"), control.get("y")) != (0, 0):
                failures.append(f"{name}: expected zero nominal command")
        r = summary["recording"]
        if r["frames_dropped"] or r["write_errors"] or r["telemetry_dropped"] or not r["drain_complete"]:
            failures.append("Recording did not drain cleanly without dropped data")
        if any(c["errors"] or not c["frames"] for c in summary["cameras"].values()):
            failures.append("A simulated camera did not remain healthy")
        if not summary["workers_stopped"] or summary["pid_process_exit_code"] != 0:
            failures.append("Shutdown was not clean")
        report = {"passed": not failures, "failures": failures, "session": str(engine.session),
            "frames_read_back": len(rows), "stages": stages, "feedback_events": feedback_events,
            "pid_events": pid_events, "summary": summary}
        (ROOT/"validation"/"feedback_validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"passed": not failures, "failures": failures, "frames_read_back": len(rows),
                          "timing": summary["timing"]}, indent=2))
        return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
