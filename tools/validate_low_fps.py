"""Regression test for empty Basler polling results at 10 fps and below."""
from __future__ import annotations

import os
for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(key, "1")
import argparse
import csv
from dataclasses import replace
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from time import perf_counter, perf_counter_ns
import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beamdiag.config import load_config
from beamdiag.frame_io import load_saved_frame
from beamdiag.runtime import DiagnosticEngine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware", action="store_true")
    args = parser.parse_args()
    cfg = load_config(ROOT/"config.toml")
    if args.hardware:
        from pypylon import pylon
        factory = pylon.TlFactory.GetInstance()
        devices = {device.GetSerialNumber(): device for device in factory.EnumerateDevices()}
        if any(camera.serial not in devices or not factory.IsDeviceAccessible(devices[camera.serial])
               for camera in cfg.cameras):
            raise RuntimeError("All configured cameras must be connected and free; no running application will be interrupted")
    cv2.setNumThreads(1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(dir=ROOT/"validation") as tmp:
        config = Path(tmp)/"config.toml"
        config.write_text((ROOT/"config.toml").read_text().replace("port = 49152", f"port = {port}"))
        cfg = load_config(config)
        cfg = replace(cfg, recording=replace(cfg.recording, start_recording=True, output_dir="validation/low_fps_runs"))
        engine = DiagnosticEngine(cfg, config, ROOT, simulated=not args.hardware)
        stages, failures = [], []
        worker = engine.workers["dark_field"]
        initial_fps = worker.cfg.fps

        def wait_for(predicate, timeout=6):
            deadline = perf_counter()+timeout
            while not predicate():
                engine.poll_control()
                if perf_counter() >= deadline:
                    raise TimeoutError("Timed out waiting for the camera worker")
                engine.stop.wait(.005)

        def apply_rate(fps):
            request_id = engine.queue_camera_settings("dark_field", {"fps": fps})
            wait_for(lambda: worker.controls.read().get("completed_request_id") == request_id)
            if worker.controls.read()["last_update_status"] != "applied":
                raise RuntimeError(worker.controls.read()["last_update_message"])

        def measure(fps, seconds):
            apply_rate(fps)
            start_ns = perf_counter_ns()
            before = worker.stats(False)
            deadline = perf_counter()+seconds
            while perf_counter() < deadline:
                engine.poll_control()
                engine.stop.wait(.005)
            elapsed = (perf_counter_ns()-start_ns)/1e9
            after = worker.stats(False)
            delivered = (after["frames"]-before["frames"])/elapsed
            stages.append({"requested_fps": fps, "readback_fps": worker.cfg.fps, "elapsed_s": elapsed,
                "frames": after["frames"]-before["frames"], "delivered_fps": delivered,
                "empty_polls": after["timeouts"]-before["timeouts"], "errors": after["errors"]-before["errors"]})
            if abs(delivered-fps) > max(.5, .1*fps) or after["errors"] != before["errors"]:
                failures.append(f"Dark Field did not remain healthy at {fps:g} fps")

        try:
            engine.start()
            wait_for(lambda: all(w.controls.read()["ready"] for w in engine.workers.values()), timeout=10)
            measure(10, 6)
            measure(5, 4)  # Force routine 100 ms polling timeouts between 200 ms frames.
            measure(initial_fps, 3)
        finally:
            if worker.thread.is_alive() and not engine.stop.is_set() and worker.cfg.fps != initial_fps:
                try:
                    apply_rate(initial_fps)
                except Exception as exc:
                    failures.append(f"Could not restore initial Dark Field FPS: {exc}")
            summary = engine.close()
        with (engine.session/"frame_index.csv").open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            image = load_saved_frame(engine.session, row)
            if image.shape != (int(row["height"]), int(row["width"])):
                failures.append("Saved frame shape mismatch")
        r = summary["recording"]
        if r["frames_dropped"] or r["telemetry_dropped"] or r["write_errors"] or not r["drain_complete"]:
            failures.append("Recording did not drain without errors or drops")
        if any(c["errors"] for c in summary["cameras"].values()):
            failures.append("A camera raised an acquisition error")
        for role in ("far_field", "near_field"):
            expected = next(camera.fps for camera in cfg.cameras if camera.role == role)
            if abs(summary["cameras"][role]["fps"]-expected) > .1*expected:
                failures.append(f"{role} did not retain its original frame rate")
        if args.hardware and not any(stage["requested_fps"] == 5 and stage["empty_polls"] > 0 for stage in stages):
            failures.append("The hardware test did not exercise empty polling results")
        if not summary["workers_stopped"] or summary["pid_process_exit_code"] != 0:
            failures.append("Shutdown did not complete cleanly")
        report = {"passed": not failures, "hardware": args.hardware, "failures": failures,
            "session": str(engine.session), "frames_read_back": len(rows), "stages": stages, "summary": summary}
        name = "low_fps_hardware.json" if args.hardware else "low_fps_simulation.json"
        (ROOT/"validation"/name).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("passed", "hardware", "failures", "frames_read_back", "stages")}, indent=2))
        return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
