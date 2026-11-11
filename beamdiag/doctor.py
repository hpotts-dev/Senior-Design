"""Readiness checks for a new Windows computer; no camera acquisition or system changes."""
from __future__ import annotations
import argparse
import importlib
import importlib.metadata
import json
from pathlib import Path
import platform
import socket
import struct
import sys
import sysconfig

from .config import load_config
from .windows_paths import default_config_path, ensure_config, resolve_recording_path


def inspect(root, config_path=None, discover=False):
    root = Path(root).resolve()
    info = {"os": platform.platform(), "python": sys.version, "python_executable": sys.executable,
            "architecture_bits": struct.calcsize("P")*8, "python_platform": sysconfig.get_platform(),
            "camera_discovery_checked": discover, "checks": {}}
    checks = info["checks"]
    checks["python"] = {"ok": (3, 11) <= sys.version_info[:2] < (3, 15) and info["architecture_bits"] == 64 and
                       (sys.platform != "win32" or info["python_platform"] == "win-amd64") and not sysconfig.get_config_var("Py_GIL_DISABLED")}
    for name, module in (("numpy", "numpy"), ("opencv-python-headless", "cv2"),
                         ("PySide6-Essentials", "PySide6.QtCore"), ("pypylon", "pypylon.pylon")):
        try:
            loaded = importlib.import_module(module)
            check = {"ok": True, "version": importlib.metadata.version(name)}
            if name == "PySide6-Essentials" and sys.platform == "win32":
                plugins = Path(loaded.QLibraryInfo.path(loaded.QLibraryInfo.LibraryPath.PluginsPath))
                check["windows_platform_plugin"] = str(plugins/"platforms"/"qwindows.dll")
                check["ok"] = (plugins/"platforms"/"qwindows.dll").exists()
            checks[name] = check
        except (ImportError, OSError, importlib.metadata.PackageNotFoundError) as exc:
            checks[name] = {"ok": False, "error": str(exc), "fix": "Run setup.cmd; for DLL failures install the Microsoft Visual C++ x64 runtime."}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
        checks["local_pid_socket"] = {"ok": True}
    except OSError as exc:
        checks["local_pid_socket"] = {"ok": False, "error": str(exc)}
    try:
        path = Path(config_path).resolve() if config_path else ensure_config(default_config_path(root), root)
        cfg = load_config(path)
        resolved = resolve_recording_path(cfg, path)
        checks["configuration_and_storage"] = {"ok": True, "config": str(path), "capture_parent": resolved.recording.output_dir}
        assigned = all(c.serial for c in cfg.cameras)
        checks["camera_assignment"] = {"ok": assigned, "serials": {c.role: c.serial for c in cfg.cameras},
                                       "fix": "Run configure_cameras.cmd to assign this computer's cameras."}
        if discover:
            try:
                from .cameras import discover_basler
                devices = discover_basler()
                present = {d["serial"] for d in devices}
                checks["camera_discovery"] = {"ok": assigned and all(c.serial in present for c in cfg.cameras), "devices": devices,
                    "fix": "Install pylon GigE drivers, close pylon Viewer, and check the camera adapter/subnet in NETWORK_SETUP.md."}
            except Exception as exc:
                checks["camera_discovery"] = {"ok": False, "error": str(exc), "fix": "Install/check pylon camera transport drivers."}
    except Exception as exc:
        checks["configuration_and_storage"] = {"ok": False, "error": str(exc)}
    core = ("python", "numpy", "opencv-python-headless", "PySide6-Essentials", "local_pid_socket", "configuration_and_storage")
    info["ready_for_simulation"] = all(checks.get(key, {}).get("ok", False) for key in core)
    info["ready_for_basler"] = info["ready_for_simulation"] and checks["pypylon"]["ok"] and checks.get("camera_assignment", {}).get("ok", False)
    if discover:
        info["ready_for_basler"] &= checks.get("camera_discovery", {}).get("ok", False)
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--cameras", action="store_true", help="Also check camera discovery; does not open or take over a camera")
    parser.add_argument("--save", type=Path, help="Optional JSON diagnostics output")
    args = parser.parse_args()
    result = inspect(Path(__file__).resolve().parents[1], args.config, args.cameras)
    text = json.dumps(result, indent=2)
    print(text)
    if args.save:
        args.save.write_text(text+"\n", encoding="utf-8")
    return 0 if result["ready_for_basler" if args.cameras else "ready_for_simulation"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
