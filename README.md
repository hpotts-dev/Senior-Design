# Three-camera beam diagnostics

The Beam Alignment dashboard shows Near Field (NF) and Far Field (FF) position, displacement and spectra. A separate Dark Field (DF) view monitors incoming light. The nominal PID reports a normalized X/Y correction from the measured angle between the NF and FF planes.

## Download for Windows x64

[![Download Beam Diagnostics for Windows x64](https://img.shields.io/badge/Download-Beam%20Diagnostics%20for%20Windows%20x64-0969da?style=for-the-badge&logo=windows)](https://github.com/hpotts-dev/Senior-Design/releases/latest/download/BeamDiagnostics-Windows-x64.zip)

1. Download the ZIP and use **Extract All** to unpack it into a folder you can write to, such as **Documents**.
2. Open the extracted **BeamDiagnostics** folder and double-click **run_simulation.cmd**. The ZIP includes Python and the libraries needed to run the dashboard.
3. To use real cameras, follow the [camera setup](#connect-real-cameras) before opening **run_basler.cmd**.

[Latest release and notes](https://github.com/hpotts-dev/Senior-Design/releases/latest)

### Automatic updates

The portable launchers check GitHub for a newer stable release at startup. They install updates before opening the dashboard and keep your settings, background references, and captures. Any release published during a capture is picked up the next time you launch the app. If GitHub is unavailable, the installed version opens as usual.

If you are using a release older than **v1.1.0**, download and extract the current ZIP once. Copy your existing **config.toml** and any **backgrounds** folder into the new **BeamDiagnostics** folder to reuse your settings. Your previous captures are in the original folder.

## Use the dashboard

- Click an NF or FF image to set its reference. The red X and blue Y traces show centroid minus reference over the last three seconds. Displacement uses inches when the plane scale is known and pixels otherwise. RMS is the root mean square radial displacement, PV is its peak-to-valley range, and Δ is the current radius.
- Open **PID → Settings** for gains, limits and timing. For hardware, open **Capture → Configuration** and set each NF/FF camera’s `plane_mm_per_pixel` to its measured 2×2 transform into the same transverse coordinate system. Set `pid.plane_separation_mm` to the positive axial distance between the imaged planes. The per-axis angle in µrad is `1e6 × atan2(FF displacement − NF displacement, plane separation)`, with lengths in mm. Each input uses its own reference. The software pairs each sample once, checks the age of the older sample, and holds output at zero on invalid or expired feedback. Simulation uses 0.00345 mm/px and 200 mm unless overridden.
- **Show DF** opens the live image and its settings. A mean native frame brightness above the configured limit for the hold time changes **OK** to **BAD** and opens one alert per episode. Defaults are 100 DN and 0.5 s. Set the limit against the normal dark-field signal at your operating exposure and gain. Live camera edits last for the session. Use **Capture → Configuration → Save & Apply** to retain them.
- **Output Settings** controls the timed image cadence for all three cameras. New installations start at five seconds. Press **S** for an extra snapshot. **Capture → Record all frames** explicitly enables additional images at the configured recording stride.

## Capture files

**View Output Data** opens the current timestamped folder. Compact capture starts with the first measurement.

| File | Contents |
| --- | --- |
| `centroids.csv` | Elapsed seconds, camera and frame ID, centroid X/Y, ΔX/ΔY in pixels, and validity. Invalid coordinates are blank. |
| `average_fft.csv` | NF and FF frequency bins in Hz, window count, and mean real, imaginary and magnitude coefficients for X and Y in pixels. |
| `pid.csv` | Input time, FF frame ID, controller state, paired approach angles in µrad and normalized X/Y outputs from the PID process. |
| `images/<camera>/` | One lossless native-depth PNG per selected exposure, with elapsed time and frame ID in the name. |
| `session.json` | Capture start time, simulation flag, spatial transforms and plane separation. |

The FFT uses acquisition timestamps to interpolate each uninterrupted run of valid samples onto the camera sampling grid. Complete, mean-subtracted Hann windows span up to three seconds or 256 samples. The graphs show the running average of one-sided magnitudes, which preserves peaks even when phase varies between windows. With 12 fps, the highest measurable frequency is 6 Hz. A gap or reference change starts a fresh segment. The default CSV interval is 60 seconds. Change it in **NF, FF or DF Settings → NF + FF spectrum average → Save FFT interval**. Closing capture flushes the last interval. At least four contiguous valid samples are needed for a spectrum.

Set `recording.compact_output = false` only when you need the previous raw-stream and report layout. The legacy capture guides describe that format.

## Connect real cameras

Install [Basler pylon with GigE support](https://www.baslerweb.com/en/software/pylon/) and your Ethernet adapter driver. If Windows reports a missing Visual C++ runtime, install Microsoft's [x64 redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist).

The **YuanLey YS2083GS-P** switch powers and connects the three GigE cameras and carries their images over one shared uplink to the PC. Set the switch to **Default/Standard**. Connect **Near Field to port 6**, **Far Field to port 7**, **Dark Field to port 8**, and **the PC Ethernet adapter to uplink port 9**. Confirm that each link negotiates 1 Gb/s and that all cameras appear in pylon Viewer, then close Viewer. The supplied full-sensor profile uses lossless packed Mono12 and requests 12 fps for Far and Near Field and 6 fps for Dark Field. [Network setup](NETWORK_SETUP.md)

Run **configure_cameras.cmd**, assign the serial number for each role, and save. Then open **run_basler.cmd**. Verify both spatial transforms and the plane separation before using nominal feedback.

Use **Auto contrast (preview only)** in a camera’s **Settings → Camera** tab. NF and FF use a light-background preview. Turn it on to brighten dim previews automatically. Turn it off to see how exposure and gain change the preview brightness. Both controls update immediately and stay synchronized. Raw recordings retain the sensor values. [Capture and settings guide](LIVE_CAPTURE_GUIDE.md)

## Run or build from source

On Windows x64, install standard x64 Python 3.12, then run these commands from the repository folder in PowerShell.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-release.txt
.\run_simulation.cmd
.\.venv\Scripts\python.exe tools\build_portable_windows.py
```

The build writes **dist/BeamDiagnostics-Windows-x64.zip** and its SHA-256 checksum. The ZIP includes the Python runtime, pinned libraries, and fresh camera settings. [Full user guide](USER_GUIDE.md) · [Third-party software](THIRD_PARTY.md)

## Publish a portable release

Increase the stable version in **VERSION** and push the release commit to **main**. The **Windows x64 release** workflow builds and tests the portable ZIP before publishing it on GitHub. If the workflow fails, rerun the original workflow for the same commit. If a draft already exists and you need to publish a corrected commit, increase **VERSION** again. [Release workflow](https://github.com/hpotts-dev/Senior-Design/actions/workflows/windows-release.yml)
