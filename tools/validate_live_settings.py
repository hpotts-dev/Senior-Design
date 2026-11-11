"""Exercise live UI controls while recording. Simulation is the default; --hardware uses available real cameras."""
from __future__ import annotations

import os
for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(key, "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import argparse
import csv
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import json
import re
import socket
import sys
import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beamdiag.camera_settings import live_values
from beamdiag.config import load_config
from beamdiag.gui import DiagnosticWindow, create_application
from beamdiag.runtime import DiagnosticEngine
from beamdiag.frame_io import load_saved_frame
from PySide6 import QtCore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware", action="store_true")
    args = parser.parse_args()
    cfg = load_config(ROOT/"config.toml")
    if args.hardware:
        from pypylon import pylon
        factory = pylon.TlFactory.GetInstance()
        devices = {d.GetSerialNumber(): d for d in factory.EnumerateDevices()}
        if any(c.serial not in devices or not factory.IsDeviceAccessible(devices[c.serial]) for c in cfg.cameras):
            raise RuntimeError("All configured cameras must be connected and free; this test will not interrupt another application")
    cv2.setNumThreads(1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(dir=ROOT/"validation") as tmp:
        assert Path(tmp).resolve().is_relative_to(ROOT.resolve())
        config_path = Path(tmp)/"config.toml"
        config_path.write_text(re.sub(r"(?m)^port\s*=\s*\d+", f"port = {port}", (ROOT/"config.toml").read_text()))
        cfg = load_config(config_path)
        cfg = replace(cfg, recording=replace(cfg.recording, start_recording=True, output_dir="validation/live_settings_runs"))
        engine = DiagnosticEngine(cfg, config_path, ROOT, simulated=not args.hardware)
        initial = {c.role: live_values(c) for c in cfg.cameras}
        stages, errors = {}, []
        app = create_application()
        window = None
        try:
            engine.start()
            window = DiagnosticWindow(engine)
            window.show()

            def guarded(action):
                def run():
                    try:
                        action()
                    except Exception as exc:
                        errors.append(str(exc))
                        window.close()
                return run

            def change():
                if not all(w.controls.read()["ready"] for w in engine.workers.values()):
                    raise RuntimeError("Cameras did not become ready")
                for role, worker in engine.workers.items():
                    initial[role] = worker.controls.read()["values"].copy()
                window._camera_settings("far_field")
                dialog = window.settings_dialogs["far_field"]
                dialog.fields["exposure_us"].setValue(500)
                dialog.fields["gain"].setValue(1)
                dialog.fields["fps"].setValue(15)
                dialog.fields["saturation_level"].setValue(3500)
                dialog.fields["max_saturated_fraction"].setValue(2.5)
                dialog.apply_button.click()
                if dialog.pending_id is None:
                    raise RuntimeError("UI request was rejected: "+dialog.status.text())
                for role in ("near_field", "dark_field"):
                    engine.queue_camera_settings(role, {"exposure_us": 500, "gain": 1})

            def capture_stage(name):
                current = {r: w.controls.read() for r, w in engine.workers.items()}
                if any(s["status"] not in ("applied", "ready") for s in current.values()):
                    raise RuntimeError(f"Settings did not apply at {name}: {current}")
                stages[name] = current

            def detection():
                engine.queue_camera_settings("far_field", {"threshold": 15, "background": 4})

            def restore():
                for role, values in initial.items():
                    engine.queue_camera_settings(role, values)

            QtCore.QTimer.singleShot(2000, guarded(change))
            QtCore.QTimer.singleShot(4000, guarded(lambda: capture_stage("changed")))
            QtCore.QTimer.singleShot(5000, guarded(detection))
            QtCore.QTimer.singleShot(7000, guarded(lambda: capture_stage("detection_changed")))
            QtCore.QTimer.singleShot(8000, guarded(restore))
            QtCore.QTimer.singleShot(10000, guarded(lambda: capture_stage("restored")))
            QtCore.QTimer.singleShot(12000, window.close)
            app.exec()
        finally:
            # Best-effort restoration also covers a rejected request or a failed intermediate stage.
            for role, values in initial.items():
                worker = engine.workers[role]
                if worker.controls.read()["ready"] and live_values(worker.cfg) != values:
                    deadline = perf_counter()+2
                    while worker._applying_settings and perf_counter() < deadline:
                        engine.stop.wait(0.01)
                    try:
                        engine.queue_camera_settings(role, values)
                    except ValueError as exc:
                        errors.append("Restore: "+str(exc))
            deadline = perf_counter()+3
            while any(w._settings_pending.is_set() or w._applying_settings for w in engine.workers.values()) and perf_counter() < deadline:
                engine.stop.wait(0.01)
            summary = engine.close()
        preview = ROOT/"validation"/("live_settings_hardware_preview.png" if args.hardware else "live_settings_simulation_preview.png")
        if window:
            # Snapshot only after acquisition stops, to keep PNG encoding out of the measurements.
            dialog = window.settings_dialogs.get("far_field")
            if dialog:
                dialog.refresh()
                dialog.reload()
                dialog.grab().save(str(preview))
            window.deleteLater()
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
        events = [json.loads(line) for line in (engine.session/"settings_changes.jsonl").read_text().splitlines()]
        def rows(name):
            with (engine.session/name).open(newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        measurements, frames, controls = rows("beam_data.csv"), rows("frame_index.csv"), rows("control.csv")
        examples = []
        for role in engine.workers:
            indexed = [r for r in frames if r["role"] == role]
            for row in (indexed[0], indexed[-1]):
                image = load_saved_frame(engine.session, row)
                examples.append(image.shape == (512, 512) and str(image.dtype) == row["dtype"])
        checks = {
            "all_stages_completed": len(stages) == 3 and not errors,
            "camera_and_detection_requests_applied": len(events) >= 7 and all(e["success"] for e in events),
            "exposure_read_back": bool(stages) and abs(stages["changed"]["far_field"]["values"]["exposure_us"]-500) < 1,
            "fps_read_back": bool(stages) and abs(stages["changed"]["far_field"]["values"]["fps"]-15) < 0.01,
            "detection_values_read_back": "detection_changed" in stages and stages["detection_changed"]["far_field"]["values"]["threshold"] == 15,
            "all_original_values_restored": all(live_values(w.cfg) == initial[r] for r,w in engine.workers.items()),
            "all_frames_and_measurements_recorded": len(frames) == summary["recording"]["frames_saved"] and
                len(measurements) == sum(v["frames"] for r, v in summary["cameras"].items() if r != "dark_field"),
            "no_capture_errors_or_drops": all(w["errors"] == 0 for w in summary["cameras"].values()) and all(summary["recording"][k] == 0 for k in ("frames_dropped", "telemetry_dropped", "write_errors")),
            "raw_examples_readable": all(examples),
            "clean_shutdown": summary["workers_stopped"] and summary["recording"]["drain_complete"] and summary["pid_process_exit_code"] == 0,
        }
        if args.hardware:
            checks["uncalibrated_pid_stays_zero"] = all(float(r["x"]) == float(r["y"]) == 0 for r in controls)
        report = {"passed": all(checks.values()), "hardware": args.hardware, "checks": checks, "errors": errors,
                  "session": str(engine.session), "stages": stages, "events": events, "summary": summary}
        path = ROOT/"validation"/("live_settings_hardware.json" if args.hardware else "live_settings_simulation.json")
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(path), "passed": report["passed"], "checks": checks, "errors": errors,
            "frames_saved": len(frames), "display_latency": summary["timing"]["host_to_display_submission"]}, indent=2))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
