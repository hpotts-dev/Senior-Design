"""Test actual cameras, frame delivery, recording integrity, display and guarded nominal PID timing."""
from __future__ import annotations

import os
for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(key, "1")
import argparse
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import math
import sys
from time import perf_counter
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beamdiag.cameras import discover_basler
from beamdiag.config import load_config
from beamdiag.runtime import DiagnosticEngine
from beamdiag.timing import percentiles


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--gui", action="store_true", help="Exercise the actual GUI with offscreen Qt")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration < 5:
        raise ValueError("Use at least five seconds for hardware validation")
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
    cfg = load_config(ROOT/"config.toml")
    devices = discover_basler()
    present = {d["serial"] for d in devices}
    missing = [c.role for c in cfg.cameras if c.serial not in present]
    if missing:
        raise RuntimeError(f"Configured cameras not visible: {missing}")
    cfg = replace(cfg, recording=replace(cfg.recording, output_dir="validation/hardware_runs", start_recording=True))
    engine = DiagnosticEngine(cfg, ROOT/"config.toml", ROOT, simulated=False)
    try:
        engine.start()
        if args.gui:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6 import QtCore
            from beamdiag.gui import DiagnosticWindow, create_application
            app = create_application()
            window = DiagnosticWindow(engine)
            window.show()
            QtCore.QTimer.singleShot(round(args.duration*1000), window.close)
            app.exec()
        else:
            started = perf_counter()
            while perf_counter()-started < args.duration:
                engine.poll_control()
                engine.stop.wait(0.005)
    finally:
        summary = engine.close()
    if args.gui:
        # Screenshot encoding can hold the Python interpreter and perturb camera delivery.
        # Capture the frozen widgets only AFTER acquisition has stopped and files have drained.
        window.grab().save(str(ROOT/"validation"/"hardware_preview.png"))
    samples = read_csv(engine.session/"beam_data.csv")
    frames = read_csv(engine.session/"frame_index.csv")
    controls = read_csv(engine.session/"control.csv")
    live_controls = [r for r in controls if int(r["state"]) in (1, 2, 3)]
    pid_latency = percentiles(float(r["host_to_pid_ms"]) for r in live_controls)
    camera_reports = {}
    for c in cfg.cameras:
        rows = [r for r in samples if r["role"] == c.role]
        saved_rows = [r for r in frames if r["role"] == c.role]
        recorded = [r for r in saved_rows if r["kind"] == "record"]
        arrived = [int(r["received_ns"]) for r in recorded]
        device_times = [int(r["camera_timestamp_ticks"]) for r in recorded]
        info = json.loads((engine.session/f"camera_{c.role}.json").read_text(encoding="utf-8"))
        tick_hz = info.get("timestamp_tick_frequency_hz")
        camera_reports[c.role] = {"serial": c.serial, "model": info["model"], "ip": info["ip"],
            "link_speed_bps": info.get("device_link_speed_bps"), "applied": info["applied"],
            "frames": len(recorded), "frames_saved": len(saved_rows),
            "delivered_fps": (len(recorded)-1)*1e9/(arrived[-1]-arrived[0]) if len(recorded)>1 else 0,
            "host_interarrival": percentiles((b-a)/1e6 for a,b in zip(arrived, arrived[1:])),
            "device_frame_interval": percentiles((b-a)*1000/tick_hz for a,b in zip(device_times, device_times[1:])) if tick_hz else None,
            "measurement_reasons": dict(Counter(r["reason"] for r in rows)),
            "stream_statistics": info.get("final_stream_statistics", {}),
            "host_processing": summary["cameras"][c.role]["host_processing"]}
    # Read every indexed record and verify byte offsets, lengths, dimensions, precision and count.
    by_path = defaultdict(list)
    for row in frames:
        by_path[row["path"]].append(row)
    raw_checks = {"readable": True, "offsets_contiguous": True, "file_sizes_match": True,
                  "shape_dtype_match": True, "records_checked": 0}
    for relative, indexed in by_path.items():
        target = (engine.session/relative).resolve()
        if not target.is_relative_to(engine.session.resolve()):
            raise ValueError("Frame path is outside its session")
        with target.open("rb") as handle:
            for row in sorted(indexed, key=lambda r: int(r["byte_offset"])):
                offset = int(row["byte_offset"])
                raw_checks["offsets_contiguous"] &= handle.tell() == offset
                handle.seek(offset)
                try:
                    image = np.load(handle, allow_pickle=False)
                except (OSError, ValueError, EOFError):
                    raw_checks["readable"] = False
                    break
                raw_checks["shape_dtype_match"] &= image.shape == (int(row["height"]), int(row["width"])) and str(image.dtype) == row["dtype"]
                raw_checks["file_sizes_match"] &= handle.tell() == offset+int(row["record_bytes"])
                raw_checks["records_checked"] += 1
            raw_checks["file_sizes_match"] &= handle.tell() == target.stat().st_size
    recording = summary["recording"]
    checks = {
        "three_configured_cameras_discovered": not missing,
        "all_cameras_near_requested_fps": all(abs(camera_reports[c.role]["delivered_fps"]-c.fps) <= 0.02*c.fps for c in cfg.cameras),
        "enough_frames_for_requested_duration": all(camera_reports[c.role]["frames"] >= args.duration*c.fps*0.9 for c in cfg.cameras),
        "no_camera_failures": all(v["errors"] == 0 for v in summary["cameras"].values()),
        "no_sdk_skipped_images": all(v["sdk_skipped"] == 0 for v in summary["cameras"].values()),
        "every_measurement_and_frame_saved": len(samples) == sum(v["frames"] for r, v in summary["cameras"].items() if r != "dark_field") and
            sum(r["kind"] == "record" for r in frames) == sum(v["frames"] for v in summary["cameras"].values()),
        "no_recorder_drops_or_errors": all(recording[k] == 0 for k in ("frames_dropped", "telemetry_dropped", "write_errors")),
        "all_saved_records_valid": all(raw_checks[k] for k in ("readable", "offsets_contiguous", "file_sizes_match", "shape_dtype_match")) and raw_checks["records_checked"] == len(frames),
        "processing_p95_under_50ms": all(v["host_processing"]["p95_ms"] is not None and v["host_processing"]["p95_ms"] < 50 for r, v in summary["cameras"].items() if r != "dark_field"),
        "saving_p95_under_50ms": recording["host_to_saved"]["p95_ms"] is not None and recording["host_to_saved"]["p95_ms"] < 50,
        "pid_responses_p95_under_50ms": pid_latency["p95_ms"] is not None and pid_latency["p95_ms"] < 50,
        "clean_shutdown_and_drain": summary["workers_stopped"] and recording["drain_complete"] and summary["pid_process_exit_code"] == 0,
    }
    if args.gui:
        latency = summary["timing"]["host_to_display_submission"]
        checks["display_p95_under_50ms"] = latency["p95_ms"] is not None and latency["p95_ms"] < 50
    control_source = next(c for c in cfg.cameras if c.role == cfg.pid.source_role)
    if not control_source.calibration_confirmed:
        checks["uncalibrated_pid_stays_zero"] = bool(live_controls) and all(int(r["state"]) == 2 and float(r["x"]) == float(r["y"]) == 0 for r in live_controls)
    report = {"tested_utc": datetime.now(timezone.utc).isoformat(), "session": str(engine.session),
        "duration_requested_s": args.duration, "simulated": False, "gui_offscreen": args.gui,
        "scope": "Connectivity, delivery timing, raw-data integrity and zero-output calibration guard; no angular or actuator calibration validation.",
        "devices": devices, "checks": checks, "passed": all(checks.values()), "cameras": camera_reports,
        "timing_qualification": "Passing p95 host-processing checks does not imply every camera delivery interval is under 50 ms; inspect host_interarrival maxima separately.",
        "pid_response_latency": pid_latency, "control_states": dict(Counter(r["state"] for r in controls)),
        "raw_data_checks": raw_checks, "summary": summary}
    report_path = ROOT/"validation"/"hardware_validation.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "passed": report["passed"], "checks": checks,
        "cameras": {r: {k:v for k,v in d.items() if k not in ("applied", "measurement_reasons")} for r,d in camera_reports.items()},
        "pid_response_latency": pid_latency, "recording": recording,
        "display_latency": summary["timing"]["host_to_display_submission"], "raw_data_checks": raw_checks}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
