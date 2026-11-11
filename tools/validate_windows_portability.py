"""Install the transfer ZIP into a fresh environment at a different Windows path and run it."""
from __future__ import annotations
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.build_windows_package import build_package
from beamdiag.config import load_config
from beamdiag.windows_paths import save_config


def main():
    if os.name != "nt":
        raise RuntimeError("This validator exercises Windows batch launchers")
    archive = build_package(ROOT, ROOT/"dist"/"BeamDiagnostics_Windows.zip")
    install_log = ROOT/"validation"/"windows_portability_install.log"
    base = str(getattr(sys, "_base_executable", sys.executable))
    with TemporaryDirectory(prefix="pc_", dir=ROOT/"validation") as tmp:
        workspace = Path(tmp)
        destination = workspace/"Team & optics (移動)"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)
        app = destination/"Control_Sys_Diagnostic_Code"
        # Simulate a copied installation with a stale interpreter reference.
        old = app/".venv"
        old.mkdir()
        (old/"pyvenv.cfg").write_text("home = Z:\\A different Windows PC\\Python\n", encoding="utf-8")
        env = os.environ.copy()
        env["BEAMDIAG_PYTHON"] = base
        env["QT_QPA_PLATFORM"] = "offscreen"
        env.pop("BEAMDIAG_CONFIG", None)
        with install_log.open("w", encoding="utf-8") as log:
            result = subprocess.run([base, str(app/"bootstrap.py"), "--mode", "setup"], cwd=workspace,
                                    env=env, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f"Fresh setup failed. See {install_log}")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = workspace/"Profiles (新しい)"/"my cameras.toml"
        cfg = load_config(app/"config.toml")
        save_config(replace(cfg, pid=replace(cfg.pid, port=port)), config)
        def launch(script, *args):
            # All file names are controlled workspace paths; list2cmdline quotes
            # spaces/Unicode, and the outer quotes satisfy cmd /s /c batch rules.
            command = subprocess.list2cmdline([str(app/script), *map(str, args)])
            return subprocess.run('cmd.exe /d /s /c "'+command+'"', cwd=workspace,
                                  env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=60)
        readiness = launch("check_windows.cmd", "--config", config)
        if readiness.returncode:
            raise RuntimeError(readiness.stdout+readiness.stderr)
        doctor = json.loads(readiness.stdout)
        result = launch("run_simulation.cmd", "--config", config, "--duration", "3",
                        "--record", "--wait-report", "--output", app/"Captured beams (測定)")
        if result.returncode:
            raise RuntimeError(result.stdout+result.stderr)
        summary = json.loads(result.stdout)
        capture = Path(summary["session"])
        status = json.loads((capture/"raw_data"/"readable_report_status.json").read_text())
        checks = {"fresh_environment_created": (app/".venv"/"Scripts"/"python.exe").exists(),
            "stale_environment_preserved": len(list(app.glob(".venv.backup_*"))) == 1,
            "new_path_spaces_unicode_and_ampersand": doctor["ready_for_simulation"] and result.returncode == 0,
            "no_machine_serials_in_transfer_defaults": all(not camera.serial for camera in cfg.cameras),
            "three_camera_streams": all(c["frames"] >= 25 and not c["errors"] for c in summary["cameras"].values()),
            "qt_interface_rendered_frames": summary["timing"]["host_to_display_submission"]["count"] > 0,
            "nominal_pid_subprocess_healthy": summary["pid_process_exit_code"] == 0 and summary["udp_control_replies"] > 0,
            "raw_recording_healthy": summary["recording"]["frames_saved"] > 0 and not summary["recording"]["frames_dropped"] and not summary["recording"]["write_errors"],
            "readable_export_healthy": status["state"] == "ready" and (capture/"human_readable"/"START_HERE.html").exists(),
            "two_output_folders": {p.name for p in capture.iterdir()} == {"raw_data", "human_readable"}}
        report = {"passed": all(checks.values()), "checks": checks, "readiness": doctor, "capture_summary": summary,
                  "temporary_test_installation": "Removed after validation; package remains in dist/BeamDiagnostics_Windows.zip"}
        (ROOT/"validation"/"windows_portability_validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "checks": checks}, indent=2))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
