"""Standard-library Windows bootstrap; no application dependencies or personal paths required."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import sysconfig
import hashlib
from beamdiag.windows_paths import user_data_directory, writable_directory

ROOT = Path(__file__).resolve().parent
PROBE = ("import json,sys,struct,sysconfig; print(json.dumps({'version':list(sys.version_info[:2]),"
         "'bits':struct.calcsize('P')*8,'platform':sysconfig.get_platform(),"
         "'free_threaded':bool(sysconfig.get_config_var('Py_GIL_DISABLED'))}))")


def isolated_environment():
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
    for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env.setdefault(key, "1")
    return env


def supported(info):
    return ((3, 11) <= tuple(info.get("version", (0, 0))) < (3, 15) and info.get("bits") == 64 and not info.get("free_threaded", False) and
            (not str(info.get("platform", "")).startswith("win") or info["platform"] == "win-amd64"))


def runtime_info(executable):
    try:
        result = subprocess.run([str(executable), "-I", "-c", PROBE], capture_output=True, text=True,
                                encoding="utf-8", timeout=15, env=isolated_environment())
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def environment_python(root=ROOT):
    root = Path(root).resolve()
    folder = root/".venv"
    if not writable_directory(root):
        key = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:12]
        folder = user_data_directory()/"environments"/key
    return folder/("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepare_environment(root=ROOT):
    """Preserve broken copied environments before building one for this computer."""
    root = Path(root).resolve()
    executable = environment_python(root)
    info = runtime_info(executable) if executable.exists() else None
    if info and supported(info):
        return executable
    base = Path(getattr(sys, "_base_executable", sys.executable))
    if not supported(runtime_info(base) or {}):
        raise RuntimeError("Install standard x64 Python 3.11–3.14 (3.12 recommended), then run setup.cmd. "
                           "32-bit and native ARM Python builds are not supported.")
    folder = executable.parent.parent
    if folder.exists():
        if folder.is_symlink() or folder.resolve().parent != folder.parent.resolve():
            raise RuntimeError("The .venv directory points outside this application. Use a normal writable application folder.")
        backup = folder.with_name(folder.name+".backup_"+datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
        print(f"Preserving the unusable copied environment as {backup.name}", flush=True)
        folder.rename(backup)
    print("Creating this computer's application environment...", flush=True)
    subprocess.run([str(base), "-I", "-m", "venv", str(folder)], check=True, env=isolated_environment())
    return executable


def dependency_check(executable, gui=True, basler=True):
    modules = ["numpy", "cv2"]
    if gui:
        modules += ["PySide6.QtCore", "PySide6.QtWidgets"]
    if basler:
        modules += ["pypylon.pylon"]
    result = subprocess.run([str(executable), "-I", "-c", "; ".join(f"import {m}" for m in modules)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=isolated_environment(), timeout=30)
    return result.returncode == 0, result.stderr.strip()


def install_dependencies(executable, root=ROOT, wheelhouse=None):
    command = [str(executable), "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:"]
    if wheelhouse:
        command += ["--no-index", "--find-links", str(Path(wheelhouse).resolve())]
    command += ["-r", str(Path(root)/"requirements.txt")]
    subprocess.run(command, cwd=root, check=True, env=isolated_environment())
    subprocess.run([str(executable), "-m", "pip", "check"], check=True, cwd=root, env=isolated_environment())
    okay, detail = dependency_check(executable)
    if not okay:
        raise RuntimeError("Dependencies could not load. Install the Microsoft Visual C++ 2015–2022 x64 runtime "
                           "if DLL loading failed.\n"+detail)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("setup", "simulate", "basler", "cameras", "doctor"), default="simulate")
    parser.add_argument("--wheelhouse", type=Path, help="Folder of Windows x64 wheels for offline setup")
    args, forwarded = parser.parse_known_args(argv)
    try:
        if not supported({"version": list(sys.version_info[:2]), "bits": struct.calcsize("P")*8,
                          "platform": sysconfig.get_platform(), "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED"))}):
            raise RuntimeError("Use standard x64 Python 3.11–3.14; Python 3.12 is recommended.")
        executable = environment_python()
        missing = not executable.exists() or not supported(runtime_info(executable) or {})
        if args.mode == "setup" or missing:
            executable = prepare_environment()
            install_dependencies(executable, wheelhouse=args.wheelhouse)
        if args.mode == "setup":
            if forwarded:
                parser.error("Unexpected setup arguments: "+" ".join(forwarded))
            print("Ready. Run run_simulation.cmd, or configure_cameras.cmd then run_basler.cmd.")
            return 0
        if args.mode == "doctor":
            return subprocess.call([str(executable), "-m", "beamdiag.doctor", *forwarded], cwd=ROOT, env=isolated_environment())
        gui = "--headless" not in forwarded and not any(v in forwarded for v in ("--check-config", "--list-cameras"))
        basler = args.mode in ("basler", "cameras") and "--check-config" not in forwarded
        okay, detail = dependency_check(executable, gui=gui, basler=basler)
        if not okay:
            raise RuntimeError("The environment is incomplete or DLLs could not load. Run setup.cmd on this PC.\n"+detail)
        switches = ["--simulate"] if args.mode == "simulate" else (["--configure-cameras"] if args.mode == "cameras" else [])
        return subprocess.call([str(executable), str(ROOT/"diagnostic_app.py"), *switches, *forwarded],
                               cwd=ROOT, env=isolated_environment())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Setup/launch failed: {exc}\nSee WINDOWS_SETUP.md for installation and transfer instructions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
