# Three-camera beam diagnostics

Beam Diagnostics shows live Far Field, Dark Field and Near Field images from Basler GigE cameras. It measures the beam position in Far and Near Field, saves raw frames and readable reports, and calculates nominal feedback from Far Field. The feedback calculation has no hardware output.

## Download for Windows x64

1. [Download BeamDiagnostics-Windows-x64.zip](https://github.com/hpotts-dev/Senior-Design/releases/latest/download/BeamDiagnostics-Windows-x64.zip) on Windows 10 or 11 x64.
2. Right-click the ZIP, choose **Extract All**, and open the extracted **BeamDiagnostics** folder. Keep its files and folders together.
3. Double-click **run_simulation.cmd** to try the app with three simulated beams. The ZIP includes Python and the app libraries.
4. Press **R** to start or stop recording, **S** for a snapshot, and **Q** to exit. Look in **captures** for the session and open **human_readable/START_HERE.html** to view its reports.

## Connect real cameras

Install [Basler pylon with GigE support](https://www.baslerweb.com/en/software/pylon/) and your Ethernet adapter driver. If Windows reports a missing Visual C++ runtime, install Microsoft's [x64 redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist).

The **YuanLey YS2083GS-P** switch powers and connects the three GigE cameras and carries their images over one shared uplink to the PC. Set the switch to **Default/Standard**. Connect **Near Field to port 6**, **Far Field to port 7**, **Dark Field to port 8**, and **the PC Ethernet adapter to uplink port 9**. Confirm that each link negotiates 1 Gb/s and that all cameras appear in pylon Viewer, then close Viewer. The supplied full-sensor profile uses lossless packed Mono12 and requests 12 fps for Far and Near Field and 6 fps for Dark Field. [Network setup](NETWORK_SETUP.md)

Run **configure_cameras.cmd**, assign the serial number for each role, and save. Then open **run_basler.cmd**. Verify the optical calibration before using nominal feedback.

Use **Auto contrast (preview only)** beneath a camera image or in its **Settings → Camera** tab. Turn it on to brighten dim previews automatically. Turn it off to see how exposure and gain change the preview brightness. Both controls update immediately and stay synchronized. Raw recordings retain the sensor values. [Capture and settings guide](LIVE_CAPTURE_GUIDE.md)

## Run or build from source

On Windows x64, install standard x64 Python 3.12, then run these commands from the repository folder in PowerShell.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-release.txt
.\run_simulation.cmd
.\.venv\Scripts\python.exe tools\build_portable_windows.py
```

The build writes **dist/BeamDiagnostics-Windows-x64.zip**. It packages the standard Python runtime and pinned binary libraries in an isolated folder, with fresh camera settings. [Full user guide](USER_GUIDE.md) · [Third-party software](THIRD_PARTY.md)
