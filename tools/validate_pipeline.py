"""Reproducible simulated soak test. --gui uses Qt offscreen unless a display platform is set."""
from __future__ import annotations

import os
for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(key, "1")
import argparse
import csv
from dataclasses import replace
from pathlib import Path
import sys
from time import perf_counter, perf_counter_ns
import json
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beamdiag.config import load_config
from beamdiag.runtime import DiagnosticEngine
from beamdiag.transport import STATES
from beamdiag.frame_io import load_saved_frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()
    if args.duration < 3:
        raise ValueError("Use at least three seconds to exercise control and recording")
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
    cfg = load_config(ROOT/"config.toml")
    cfg = replace(cfg, recording=replace(cfg.recording, start_recording=True, output_dir="validation/runs"))
    engine = DiagnosticEngine(cfg, ROOT/"config.toml", ROOT, simulated=True)
    actions = {}
    try:
        engine.start()
        if args.gui:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6 import QtCore, QtWidgets
            from beamdiag.gui import DiagnosticWindow, create_application
            app = create_application()
            window = DiagnosticWindow(engine)
            window.show()
            QtCore.QTimer.singleShot(1000, lambda: actions.update(references=engine.set_references()))
            QtCore.QTimer.singleShot(1500, lambda: actions.update(snapshots=engine.snapshot()))
            QtCore.QTimer.singleShot(2000, lambda: window.grab().save(str(ROOT/"validation"/"gui_preview.png")))
            QtCore.QTimer.singleShot(round(args.duration*1000), window.close)
            app.exec()
        else:
            started = perf_counter()
            while perf_counter()-started < args.duration:
                engine.poll_control()
                engine.stop.wait(0.005)
    finally:
        summary = engine.close()

    with (engine.session/"control.csv").open(newline="", encoding="utf-8") as f:
        control = list(csv.DictReader(f))
    with (engine.session/"beam_data.csv").open(newline="", encoding="utf-8") as f:
        measurements = list(csv.DictReader(f))
    with (engine.session/"frame_index.csv").open(newline="", encoding="utf-8") as f:
        saved = list(csv.DictReader(f))
    active = [r for r in control if int(r["state"]) == 1]
    control_delays = [float(r["host_to_pid_ms"]) for r in active]
    from beamdiag.timing import percentiles
    # CSV covers every emitted PID sample; GUI summary intentionally holds only replies it polled.
    summary["all_active_control_latency"] = percentiles(control_delays)
    expected_count = sum(s["frames"] for s in summary["cameras"].values())
    checks = {
        "all_cameras_10_hz": all(9 <= s["fps"] <= 11 for s in summary["cameras"].values()),
        "no_camera_failures": all(s["errors"] == 0 for s in summary["cameras"].values()),
        "all_measurements_logged": len(measurements) == sum(s["frames"] for r, s in summary["cameras"].items() if r != "dark_field"),
        "all_requested_frames_saved": len(saved) == expected_count+3+actions.get("snapshots", 0),
        "no_recording_drops": summary["recording"]["frames_dropped"] == 0 and summary["recording"]["telemetry_dropped"] == 0,
        "no_recording_errors": summary["recording"]["write_errors"] == 0,
        "pid_active": len(active) >= args.duration*8,
        "pid_process_stopped": summary["pid_process_exit_code"] == 0,
        "pid_output_bounded": all(abs(float(r[k])) <= cfg.pid.output_limit for r in control for k in ("x", "y")),
        "pid_stopped_at_zero": bool(control) and int(control[-1]["state"]) == 5 and float(control[-1]["x"]) == 0 and float(control[-1]["y"]) == 0,
        "all_workers_stopped": summary["workers_stopped"],
        "writer_drained": summary["recording"]["drain_complete"],
        "pid_p95_under_50ms": bool(control_delays) and summary["all_active_control_latency"]["p95_ms"] < 50,
        "save_p95_under_50ms": summary["recording"]["host_to_saved"]["p95_ms"] < 50,
    }
    if args.gui:
        checks["display_p95_under_50ms"] = summary["timing"]["host_to_display_submission"]["p95_ms"] < 50
        checks["gui_reference_and_snapshot_actions"] = actions == {"references": 2, "snapshots": 3}
        checks["gui_preview_created"] = (ROOT/"validation"/"gui_preview.png").exists()
    # Inspect raw output: full precision, correct dimensions, no display overlays, no pickles.
    examples = []
    for role in engine.workers:
        row = next(r for r in saved if r["role"] == role)
        image = load_saved_frame(engine.session, row)
        checks[f"{role}_raw_shape_dtype"] = bool(image.shape == (512, 512) and image.dtype == np.uint16 and 255 < image.max() <= 4095)
        examples.append({"role": role, "shape": list(image.shape), "dtype": str(image.dtype),
                         "peak": int(image.max()), "path": row["path"]})
    report = {"session": str(engine.session), "duration_s": args.duration, "gui_offscreen": args.gui,
              "checks": checks, "passed": all(checks.values()), "summary": summary,
              "raw_examples": examples, "measurement_rows": len(measurements), "control_rows": len(control)}
    report_path = ROOT/"validation"/("gui_validation.json" if args.gui else "headless_validation.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "passed": report["passed"], "checks": checks,
                      "pid_latency": summary["all_active_control_latency"],
                      "save_latency": summary["recording"]["host_to_saved"],
                      "display_latency": summary["timing"]["host_to_display_submission"]}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
