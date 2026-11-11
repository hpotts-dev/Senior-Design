"""Validate the new capture bundle and background human-readable report end to end."""
from __future__ import annotations
import os
for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(key, "1")
import csv
from dataclasses import replace
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beamdiag.config import load_config
from beamdiag.runtime import DiagnosticEngine
from beamdiag.frame_io import load_saved_frame


def read_rows(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    cv2.setNumThreads(1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(dir=ROOT/"validation") as tmp:
        config = Path(tmp)/"config.toml"
        config.write_text((ROOT/"config.toml").read_text().replace("port = 49152", f"port = {port}"))
        cfg = load_config(config)
        cfg = replace(cfg, recording=replace(cfg.recording, output_dir="validation/capture_layout_runs", start_recording=True))
        engine = DiagnosticEngine(cfg, config, ROOT, simulated=True)
        snapshot_count = 0
        try:
            engine.start()
            started = perf_counter()
            while perf_counter()-started < 8:
                engine.poll_control()
                if not snapshot_count and perf_counter()-started > 3:
                    snapshot_count = engine.snapshot()
                if engine._reporter is not None:
                    raise RuntimeError("Readable conversion started during acquisition")
                engine.stop.wait(.005)
        finally:
            summary = engine.close()
        code = engine.wait_for_readable_report(timeout=45)
        status = json.loads((engine.session/"readable_report_status.json").read_text())
        raw_rows = read_rows(engine.session/"frame_index.csv")
        measurements = read_rows(engine.session/"beam_data.csv")
        centroids = read_rows(engine.readable_dir/"centroids.csv")
        pid_raw = read_rows(engine.session/"control.csv")
        pid_readable = read_rows(engine.readable_dir/"pid_feedback.csv")
        images = read_rows(engine.readable_dir/"images_index.csv")
        for row in raw_rows:
            image = load_saved_frame(engine.capture_dir, row)
            assert image.shape == (int(row["height"]), int(row["width"]))
        checks = {
            "exactly_two_capture_subfolders": {p.name for p in engine.capture_dir.iterdir()} == {"raw_data", "human_readable"},
            "live_output_finished": code is None and status["state"] == "ready",
            "all_measurements_have_readable_cells": len(measurements) == sum(bool(r[k]) for r in centroids for k in ("Far Field Capture Time", "Near Field Capture Time")),
            "all_pid_results_have_readable_rows": len(pid_raw) == len(pid_readable),
            "all_raw_frames_read_back": len(raw_rows) == summary["recording"]["frames_saved"],
            "all_snapshots_exported_to_png": sum(row["Capture type"] == "snapshot" for row in images) == snapshot_count,
            "all_png_files_exist": all((engine.readable_dir/row["PNG path"]).exists() for row in images),
            "no_capture_or_save_errors": all(not c["errors"] for c in summary["cameras"].values()) and not summary["recording"]["write_errors"],
            "no_dropped_saves_or_telemetry": not summary["recording"]["frames_dropped"] and not summary["recording"]["telemetry_dropped"],
            "save_p95_under_50ms": summary["recording"]["host_to_saved"]["p95_ms"] < 50,
            "pid_p95_under_50ms": summary["timing"]["host_to_pid"]["p95_ms"] < 50,
        }
        report = {"passed": all(checks.values()), "checks": checks, "capture": str(engine.capture_dir),
                  "raw_frames": len(raw_rows), "centroid_rows": len(centroids), "pid_rows": len(pid_readable),
                  "png_frames": len(images), "report_status": status, "summary": summary}
        (ROOT/"validation"/"capture_outputs_validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: report[k] for k in ("passed", "checks", "capture", "raw_frames", "centroid_rows", "pid_rows", "png_frames", "report_status")}, indent=2))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
