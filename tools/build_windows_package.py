"""Create a source distribution with generic settings, excluding environments and recorded data."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build_package(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = "Control_Sys_Diagnostic_Code/"
    top_names = {"diagnostic_app.py", "pid_controller.py", "bootstrap.py", "pyproject.toml", ".gitignore"}
    selected = [p for p in root.iterdir() if p.is_file() and p.name != "README.md" and
                (p.name in top_names or p.suffix in (".cmd", ".ps1", ".md") or p.name.startswith("requirements"))]
    for folder in ("beamdiag", "tools", "tests"):
        selected += list((root/folder).rglob("*.py"))
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(selected):
            archive.write(path, prefix+path.relative_to(root).as_posix())
        # Never ship this machine's serials, optical confirmation, output paths or environment.
        template = (root/"config.example.toml").read_bytes()
        archive.writestr(prefix+"config.example.toml", template)
        archive.writestr(prefix+"config.toml", template)
        archive.writestr(prefix+"validation/", b"")
        archive.writestr(prefix+"README.md", """# Beam diagnostics for Windows

Start with [WINDOWS_SETUP.md](WINDOWS_SETUP.md). This package contains source code and automated setup, with generic camera settings.

1. Install standard x64 Python 3.11–3.14 (3.12 recommended) on Windows 10/11 x64.
2. Fully extract this folder, then run **setup.cmd**.
3. Try **run_simulation.cmd** without cameras.
4. Install Basler pylon GigE drivers and follow [NETWORK_SETUP.md](NETWORK_SETUP.md).
5. Run **configure_cameras.cmd**, assign each physical camera to Far/Dark/Near Field, save, and run **run_basler.cmd**.

**check_windows.cmd** checks dependencies and storage. Add `--cameras` to check camera visibility without taking camera ownership.

In the app, **Settings…** adjusts exposure, gain and FPS for every camera, plus detection/clipping for Far/Near Field. Dark Field has a Camera tab and no centroid controls; disconnected cameras show configured values. **Control feedback…** sets the Far Field calibration, analysis region, reference, PID gains, limits, pause/reset and profiles. Enter and verify the real angular scale before confirming calibration. Only Far Field drives nominal PID calculations; no piezo voltage output is implemented.

**R** records/stops raw frames, **S** saves snapshots, **Z** sets references to valid detected centroids, and clicking an image places a reference manually. **Q/Stop** closes acquisition.

The supplied configuration uses full 1456×1088 Mono12 images (12-bit values in uint16), zero offsets and image-centered default references. Lossless Mono12p transmission retains all 12 bits while reducing wire data by 25%; native files remain uint16. Far/Near Field request 12 fps and Dark Field requests 6 fps, requiring 1 Gb/s camera links. This profile passed a one-minute live recording test on the source PC; verify performance on the destination PC. The source setup uses the YuanLey YS2083GS-P in Default/Standard mode, with the PC on uplink port 9. The watchdog is 200 ms. Verify dimensions and link speeds for your cameras. Live Auto contrast affects only the preview. Each run creates `captures/LOCAL_DATE_TIME/` with exactly `raw_data` and `human_readable`. Open `human_readable/START_HERE.html` during capture: CSVs and PNGs are written live. Timed native/PNG snapshots default to every 60 seconds even with Record off; the main-window interval field changes this. R additionally records every native frame. FF/NF centroids appear side by side with their own capture times and pixel errors; only FF drives PID. Dark Field is display/capture only.

Use **Configuration…** for every setting; Save & Apply finishes the current session and restarts acquisition in a new folder while the app stays open. For **Subtract background…**, cover that camera, press its button, wait for the averaged reference to be saved, then uncover it. References are saved beside config.toml and archived with the capture. See [LIVE_CAPTURE_GUIDE.md](LIVE_CAPTURE_GUIDE.md) for details.

Rebuild readable outputs with `python -m beamdiag.report CAPTURE_FOLDER`. Export all native frames with `python tools/export_frames.py CAPTURE_FOLDER OUTPUT_FOLDER --format png`, using this folder's `.venv/Scripts/python.exe`.

The existing source computer's environment, captures, serial assignments and calibration confirmation are not included. Timing and optical calibration must be verified on the destination system.
""")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT/"dist"/"BeamDiagnostics_Windows.zip")
    args = parser.parse_args()
    print(build_package(ROOT, args.output))


if __name__ == "__main__":
    main()
