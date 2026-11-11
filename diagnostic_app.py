"""Start three-camera live diagnostics. Use --simulate to run without Basler hardware."""
from __future__ import annotations

import os
# Prevent hidden BLAS/OpenMP thread pools from competing with the three camera workers.
for _key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_key, "1")

import argparse
from dataclasses import replace
from pathlib import Path
from time import perf_counter
import json
import logging
import math
import signal
import sys
import cv2

from beamdiag.config import load_config, bandwidth_report, validate
from beamdiag.cameras import discover_basler
from beamdiag.runtime import DiagnosticEngine
from beamdiag.windows_paths import default_config_path, ensure_config, resolve_recording_path

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("diagnostic")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Configuration file; defaults to this application or your writable user settings")
    parser.add_argument("--configure-cameras", action="store_true", help="Discover cameras and save their Far/Dark/Near Field assignments")
    parser.add_argument("--simulate", action="store_true", help="Three synthetic beams; demo angular calibration")
    parser.add_argument("--list-cameras", action="store_true", help="List serial numbers and IP addresses, then exit")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration and estimate bandwidth")
    parser.add_argument("--headless", action="store_true", help="Acquisition, analysis, recording and PID without GUI")
    parser.add_argument("--duration", type=float, default=0, help="Automatically stop after this many seconds")
    parser.add_argument("--record", action="store_true", help="Record raw frames immediately")
    parser.add_argument("--output", type=Path, help="Override the captures parent folder")
    parser.add_argument("--no-pid", action="store_true", help="Do not launch PID; measurements still go to standalone PID")
    parser.add_argument("--wait-report", action="store_true", help="Compatibility option; readable files are now written live and always drained on exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
    try:
        if args.list_cameras:
            print(json.dumps(discover_basler(), indent=2))
            return 0
        config_path = args.config.expanduser().resolve() if args.config else ensure_config(default_config_path(ROOT), ROOT)
        cfg = load_config(config_path)
        if args.configure_cameras:
            from beamdiag.camera_setup_gui import configure_cameras
            return 0 if configure_cameras(cfg, config_path) else 1
        if not math.isfinite(args.duration) or args.duration < 0:
            raise ValueError("--duration must be finite and nonnegative")
        if args.record or args.output:
            cfg = replace(cfg, recording=replace(cfg.recording,
                start_recording=args.record or cfg.recording.start_recording,
                output_dir=str(args.output) if args.output else cfg.recording.output_dir))
        validate(cfg)
        report = bandwidth_report(cfg)
        if args.check_config:
            print(json.dumps(report, indent=2))
            return 0
        if not report["within_budget"]:
            raise ValueError("Configured image payload exceeds shared network budget; reduce ROI, FPS, or bit depth")
        if not args.simulate and any(not c.serial for c in cfg.cameras):
            if args.headless:
                raise ValueError("Assign all three camera serials with configure_cameras.cmd before headless acquisition")
            from beamdiag.camera_setup_gui import configure_cameras
            configured = configure_cameras(cfg, config_path)
            if configured is None:
                return 1
            config_path, cfg = configured, load_config(configured)
        if not args.headless:
            # Diagnose a missing GUI dependency before starting acquisition or making a session folder.
            from beamdiag.gui import run_gui
        if args.record:
            cfg = replace(cfg, recording=replace(cfg.recording, start_recording=True))
        cfg = resolve_recording_path(cfg, config_path, args.output)
        engine = DiagnosticEngine(cfg, config_path, ROOT, args.simulate, not args.no_pid)
        try:
            engine.start()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: engine.stop.set())
            if args.headless:
                started = perf_counter()
                while not engine.stop.wait(0.01):
                    engine.poll_control()
                    if args.duration and perf_counter()-started >= args.duration:
                        break
            else:
                run_gui(engine, args.duration)
        finally:
            summary = engine.close()
            if args.wait_report:
                engine.wait_for_readable_report()
                if (engine.session/"summary.json").exists():
                    summary = json.loads((engine.session/"summary.json").read_text(encoding="utf-8"))
            print(json.dumps({"session": str(engine.capture_dir), "raw_data": str(engine.session), **summary}, indent=2))
        healthy = all(w._frames for w in engine.workers.values())
        if not args.no_pid:
            healthy = healthy and summary.get("pid_process_exit_code") == 0 and summary.get("udp_control_replies", 0) > 0
        return 0 if healthy else 2
    except (ImportError, TypeError, ValueError, OSError, RuntimeError, TimeoutError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
