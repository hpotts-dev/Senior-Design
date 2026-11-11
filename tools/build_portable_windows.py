"""Build an extract-and-run ZIP from a Windows x64 Python 3.12 environment."""
from importlib.metadata import version
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = {
    "run_simulation": "diagnostic_app.py --simulate",
    "run_basler": "diagnostic_app.py",
    "configure_cameras": "diagnostic_app.py --configure-cameras",
    "check_windows": "-m beamdiag.doctor",
}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "codex-*.pem")


def main():
    if sysconfig.get_platform() != "win-amd64" or sys.version_info[:2] != (3, 12):
        raise SystemExit("Build with standard Windows x64 Python 3.12.")
    requirements = ROOT / "requirements-release.txt"
    for pin in requirements.read_text(encoding="utf-8").splitlines():
        name, expected = pin.split("==")
        if version(name) != expected:
            raise SystemExit(f"Install {pin} in the build environment.")

    destination = ROOT / "dist" / "BeamDiagnostics-Windows-x64.zip"
    destination.parent.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as work:
        stage = Path(work) / "BeamDiagnostics"
        runtime = stage / ".runtime"
        runtime.mkdir(parents=True)
        base = Path(sys.base_prefix)
        for name in ("python.exe", "python3.dll", "python312.dll",
                     "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
            shutil.copy2(base / name, runtime)
        shutil.copytree(base / "DLLs", runtime / "DLLs", ignore=IGNORE)
        shutil.copytree(base / "Lib", runtime / "Lib", ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "site-packages", "ensurepip", "test", "idle_test", "codex-*.pem"))
        packages = runtime / "Lib" / "site-packages"
        subprocess.run([sys.executable, "-m", "pip", "install", "--only-binary=:all:",
                        "--no-compile", "--target", str(packages), "-r", str(requirements)],
                       check=True)
        (runtime / "python312._pth").write_text(
            "Lib\nDLLs\nLib/site-packages\n..\n", encoding="ascii")
        shutil.copytree(ROOT / "beamdiag", stage / "beamdiag", ignore=IGNORE)
        for name in ("diagnostic_app.py", "pid_controller.py", "config.example.toml",
                     "README.md", "NETWORK_SETUP.md", "LIVE_CAPTURE_GUIDE.md",
                     "USER_GUIDE.md", "THIRD_PARTY.md", "requirements-release.txt"):
            shutil.copy2(ROOT / name, stage)
        shutil.copytree(ROOT / "licenses", stage / "licenses")
        shutil.copy2(ROOT / "config.example.toml", stage / "config.toml")
        for name, args in LAUNCHERS.items():
            if args.startswith("diagnostic_app.py"):
                args = args.replace("diagnostic_app.py", '"%~dp0diagnostic_app.py"', 1)
            (stage / f"{name}.cmd").write_text(
                f'@echo off\nsetlocal\n"%~dp0.runtime\\python.exe" -X utf8 {args} %*\n',
                encoding="ascii")
        subprocess.run([str(runtime / "python.exe"), "-m", "compileall", "-q", "-f",
                        "--invalidation-mode", "checked-hash", str(stage)], check=True)
        archive_path = Path(work) / destination.name
        with ZipFile(archive_path, "w", ZIP_DEFLATED, compresslevel=3) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage.parent).as_posix())
        archive_path.replace(destination)
    print(destination)


if __name__ == "__main__":
    main()


