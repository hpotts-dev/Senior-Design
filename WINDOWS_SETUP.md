# Run on another Windows computer

The supported application target is **Windows 10/11 x64** with standard **64-bit x64 Python 3.11–3.14**. Python 3.12 is recommended. Windows 7/8, 32-bit Python, native ARM Python and experimental free-threaded builds are not supported by this deployment. Camera drivers must support the destination PC and Windows version. This is a source distribution with automated setup, not a standalone executable containing Python.

## Transfer and setup

1. Copy/extract `dist/BeamDiagnostics_Windows.zip` into a normal folder such as Documents or `C:\BeamDiagnostics`. Fully extract it before launching. The ZIP contains generic, unassigned camera settings; it excludes this computer's Python environment, recordings, calibration confirmation and personal output paths. You can also copy the source folder, but omit `.venv`, `.venv.backup_*`, `captures` and validation recordings.
2. Install standard x64 Python from [python.org](https://www.python.org/downloads/windows/). Install the Python launcher or add Python to PATH. Python 3.12 is the preferred version; setup rejects unsupported versions and architectures.
3. Run **setup.cmd**. It creates the destination computer's environment and installs binary dependencies. It checks an existing environment and preserves an unusable copied environment as a timestamped backup before rebuilding. Internet access is needed for initial dependency installation unless using an offline wheel folder. You do not need Codex, an IDE, or the original user's directories.
4. Run **run_simulation.cmd** to verify display, centroid calculations, raw recording, the separate nominal PID process and readable exports without cameras.
5. For physical cameras, install [Basler pylon](https://www.baslerweb.com/en/software/pylon/) with the GigE transport/driver components and the USB Ethernet adapter's correct driver. Follow [NETWORK_SETUP.md](NETWORK_SETUP.md), verify the cameras in pylon Viewer, then close Viewer.
6. Run **configure_cameras.cmd**, choose the serial for Far Field, Dark Field and Near Field, and save. Assignments are explicit; discovery order never chooses the PID source. Replacing a camera clears its angular calibration confirmation, software ROI, reference and saved dark reference. Run **run_basler.cmd** afterward. With unassigned roles, ordinary GUI startup opens the assignment dialog automatically.

The original working `config.toml` is retained on this development computer. The transfer ZIP uses `config.example.toml` as its fresh `config.toml`, with blank serials and unconfirmed optical calibration. A different Windows PC using the same physical cameras may retain their serials, but should still verify its network, timing and optical setup.

## Location and permissions

All program/resource paths derive from the extracted application location. Launchers can be run from another working directory and handle ordinary spaces and Unicode in paths. No PowerShell execution-policy change is needed: use `.cmd`. Existing `.ps1` files delegate to the same launchers if your policy permits them.

Default configuration is `config.toml` beside the application. `--config "path\to\settings.toml"` or the `BEAMDIAG_CONFIG` environment variable selects another file. If the application folder is read-only, setup uses `%LOCALAPPDATA%\BeamDiagnostics\environments\<application-id>`, and camera assignment saves configuration under `%LOCALAPPDATA%\BeamDiagnostics`. This does not require writing to Program Files. A standard Python installation is still needed to run the bootstrap in that case.

Relative capture paths resolve beside the selected configuration file. If the default `captures` directory is not writable, captures fall back to `%LOCALAPPDATA%\BeamDiagnostics\captures`. An explicitly requested unwritable output fails with an explanation instead of silently moving data. Use `--output "D:\BeamCaptures"` to choose another writable destination. Calibration profiles default beside the selected configuration file. Capture folder timestamps use the destination computer's local timezone.

Python imports run in the local environment; launchers isolate inherited Python/Qt search-path overrides that can otherwise load another installation's libraries. They preserve PATH for vendor camera drivers. `BEAMDIAG_PYTHON` may point to a particular supported `python.exe` if it is not registered with `py` or PATH.

## Check a new PC

```powershell
.\check_windows.cmd
.\check_windows.cmd --cameras
.\run_simulation.cmd --headless --duration 5 --record --wait-report
```

The readiness report checks Python architecture/version, dependency imports, the Windows Qt platform plugin, local PID socket access, configuration and writable storage. `--cameras` additionally performs discovery without opening/taking over a camera. Without that option, Basler readiness means dependencies and role assignments are present; network/camera connectivity has not been verified. Use `--save readiness.json` to save the report.

For DLL-load errors, install Microsoft's [Visual C++ x64 runtime](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist). For blocked camera streaming, check the camera interface, subnet and approved firewall rules. This application does not modify network adapters, firewall rules or system security policies.

## Offline setup and packaging

On a Windows x64 PC with the same Python minor version as the destination, collect wheels:

```powershell
py -3.12 -m pip download --only-binary=:all: -r requirements.txt -d wheelhouse
.\setup.cmd --wheelhouse "C:\path\to\wheelhouse"
```

Build a clean transfer ZIP from the source folder:

```powershell
.\.venv\Scripts\python.exe tools\build_windows_package.py
```

The package does not redistribute Python, Microsoft runtimes or camera/adapter drivers; install those using their official installers. Dependency ranges allow compatible wheels for each supported Python. `requirements-tested.txt` records the development machine's exact versions. PySide is capped below 6.13 to retain the Qt release line supporting Windows 10.

The supplied configuration uses the full 1456×1088 sensor in Mono12, with zero offsets, an image-centered default reference, always-on snapshots every 60 seconds and live-readable outputs. Lossless Mono12p transport preserves all 12 bits and reduces wire payload by 25%; native recordings remain uint16. Far/Near Field are set to 12 fps and Dark Field to 6 fps, requiring 1 Gb/s camera links. This profile passed a one-minute live recording test on the source PC; verify performance on the destination PC. The PC connects to port 9 of the YuanLey YS2083GS-P in Default/Standard mode. Verify sensor dimensions and link speeds on a different setup. The Far Field watchdog is 200 ms. The full configuration editor and snapshot controls work without closing the app. See [LIVE_CAPTURE_GUIDE.md](LIVE_CAPTURE_GUIDE.md). The 50 ms target applies to processing after retrieval, independently of frame rate.

Hardware throughput and the 50 ms processing target still need qualification on each destination PC. A successful portability check is not a hard real-time guarantee or a measured angular calibration.

Vendor compatibility references: [Basler pypylon](https://github.com/basler/pypylon), [pypylon environment setup](https://docs.baslerweb.com/pypylon-installation-and-environment-setup), [Qt supported platforms](https://doc.qt.io/qtforpython-6/overviews/qtdoc-supported-platforms.html).
