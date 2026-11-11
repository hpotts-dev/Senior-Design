# Three-camera beam diagnostics

An expandable Python framework for **Far Field, Dark Field, and Near Field Basler GigE cameras**, live display, raw-frame capture, centroid displacement, and a separate nominal two-axis PID process. The supplied configuration captures the **entire 1456 × 1088 sensor** of each dmA1440-73gm using **lossless packed Mono12p transmission**, decoded to the original 12-bit values in uint16 containers for processing and saving, with zero offsets and an image-centered reference. With the YuanLey YS2083GS-P switch, all camera links negotiate **1 Gb/s**. The supplied profile requests **12 fps for Far Field and Near Field**, and **6 fps for Dark Field**. This profile passed a one-minute live recording and display test on this setup; verify performance on another PC. The processing target remains under 50 ms after retrieval. Simulation works without cameras.

## Run

From this folder in PowerShell:

```powershell
.\run_simulation.cmd
```


You can also double-click the `.cmd` launchers; they work with PowerShell's existing execution policy. Install Basler's pylon Software Suite and GigE drivers before connecting real cameras. Qt Essentials provides the GUI; OpenCV's headless package supplies computation only, avoiding competing GUI frameworks.

Direct launch is also available:

```powershell
.\.venv\Scripts\python.exe diagnostic_app.py --simulate
```

In the window:

| Action | Shortcut | Behavior |
|---|---|---|
| Record / stop recording | R | Save incoming raw frames asynchronously |
| Save current frames | S | Queue the latest frame from each camera |
| Place a reference manually | Click a camera image | Move that camera's amber cross to the clicked pixel |
| Set an exact reference | Per-camera X/Y and Set | Apply fractional-pixel delivered-frame coordinates |
| Use a detected centroid | Per-camera button, or Z for all | Use each fresh valid centroid as its reference; reset PID state on the next source measurement |
| Center a reference | Per-camera Center reference | Restore the midpoint of that camera's delivered image |
| Adjust camera / detection settings | Per-camera Settings… | Exposure, gain, FPS, background, signal and clipping limits |
| Calibrate / tune feedback | Control feedback… | Far Field calibration matrix, target, analysis region, PID tuning, limits, pause/reset and profiles |
| Timed image interval | Main window field | Always-on snapshots every 60 seconds by default, independent of Record; Apply saves the interval and restarts capture |
| Capture now / restart timer | Main window button | Capture on the next frame of every camera and restart its countdown |
| Subtract background | Per-camera button | Cover camera, average 16 dark frames, save the per-pixel reference and enable subtraction |
| Edit every setting | Configuration… | Editable full TOML; Save & Apply drains the current session and restarts acquisition in a new capture folder |
| Quit | Q | Stop acquisition and PID, drain accepted writes, save summaries |

Far/Near Field measurements and PID outputs are logged throughout a session. Timed native and PNG snapshots start on the first frame and repeat even when continuous recording is off. Dark Field has no centroid calculation or reference controls. Green crosses mark measured centroids; amber crosses mark references. The controls display frame drops and write failures.

Each camera starts with **Auto contrast (preview only)** enabled so dim Mono12/Mono16 feeds remain visible. The adjacent range shows the native intensity mapped to screen white; the brightest 0.5% of pixels may clip in this preview. Uncheck it to view the full sensor range. Preview contrast changes only the live image, not camera exposure/gain, centroid/PID calculations, raw recordings or exported PNGs. It can also make sensor noise visible. Use **Settings…** to adjust actual exposure/gain, and use the native peak/clipping measurements to assess sensor saturation.

The latency optimization uses lossless packed transport, one owned SDK-buffer copy, SIMD first-moment reductions and faster preview resizing/contrast estimation. Live **image age** includes estimated camera transfer time when clock-latch measurements are available. See measured optimization results, switch speed verification for the earlier hardware tests and PC uplink-port requirements, and the Dark Field settings fix for the earlier rate profile. Camera transfer is separate from the host's 50 ms processing target.

### Move and set a reference

Click inside a Far Field or Near Field camera image to move its amber cross immediately, or enter X and Y under that view and press **Set**. Coordinates use the delivered image's pixels, with (0, 0) at the top left; scaling and padding in the display are accounted for. Manual placement works even on a dark or saturated frame. Clicks in the padding around the image are ignored.

**Z** means **use detected centroids**, rather than placing a point manually. Click the application window first so it receives the key. It updates only fresh, valid detections; each camera now shows a persistent success message or a reason for skipping it. The displayed Reference coordinates show the applied target. In simulation, the initial beam is within a fraction of a pixel of the center, so Z may produce little visible crosshair movement even though the pixel displacement is zeroed. The beam's image and measured green centroid do not move when the reference changes.

References apply to the current run and are recorded with each subsequent measurement in `beam_data.csv`. To choose an initial reference for future runs, set `reference_px = [x, y]` in that camera's `config.toml` section or use Configuration… → Save & Apply while the app is running. Setting a reference does not confirm angular calibration; real-camera angular errors and nominal PID corrections remain disabled until the optical scale is confirmed.

### Adjust exposure, gain and clipping

Click **Settings…** beneath the relevant camera. In the **Camera** tab, change exposure (µs), sensor gain (dB, or raw units on older cameras), or requested frame rate. The controls use the connected camera's reported limits and the stream's bandwidth budget. Press **Apply** and wait for confirmation; the **Live** line shows actual readback, including any adjustment made by the camera. **Reload live values** discards unsent edits. Twenty fps gives a nominal 50 ms update interval; long exposures or slower requested rates can reduce delivery cadence. The dialog identifies exposure/interval conflicts and a Far Field cadence longer than the PID watchdog.

If changing exposure leaves the beam looking equally bright, turn off **Auto contrast (preview only)** in this dialog or beneath that camera's image. Auto contrast rescales every frame and can hide actual brightness changes. With it off, the preview uses the fixed sensor range, so exposure/gain changes affect visible brightness. The two switches stay synchronized and take effect immediately without Apply. Dark Field also shows the raw frame's mean and peak brightness, independently of preview scaling or background subtraction. See the hardware exposure check.

Far/Near Field's **Centroid / clipping** tab offers background subtraction, the signal threshold above background, minimum integrated signal, a clipping level in native intensity units (DN), and the maximum percentage of clipped pixels allowed in the analysis ROI. A clipping level of **0** uses the pixel format's ADC maximum (255 for Mono8, 4095 for unpacked Mono12, 65535 for Mono16). Live peak intensity and clipped-pixel percentage also appear under those images. Dark Field shows the **Camera** tab with exposure, gain and FPS, plus delivered rate and dimensions. While disconnected, the dialog shows configured values; live editing becomes available after connection.

These cameras are monochrome: exposure and gain control actual image brightness and can reduce sensor clipping. Increasing the allowed clipped percentage only relaxes centroid rejection; it does not recover clipped beam information or change the recorded raw pixels.

At low frame rates, a retrieval poll can finish before the next image arrives. Empty Basler grab results are counted as normal polling timeouts, not camera failures; the worker continues waiting. The supplied configuration requests 12 fps for Far/Near Field (83.3 ms per frame) and 6 fps for Dark Field (166.7 ms per frame). See the low-rate hardware regression.

The acquisition worker owns every SDK call. Exposure, gain and FPS changes briefly stop and restart only that camera's stream to discard old buffers. Detection-only changes apply between frames without restarting acquisition. Updates are logged asynchronously in `settings_changes.jsonl`, including requested and actual values, outcome, the next frame sequence and feedback revision. A settings change increments the measurement's `reference_revision` so the Far Field PID resets integral/derivative history while retaining its reference position. Near Field and Dark Field changes remain diagnostic only.

Live edits apply to the current run. Use **Configuration…** to edit, save and apply all current settings without closing the app. A full configuration application briefly restarts acquisition and creates a new timestamped session. The per-camera Settings dialog does not modify optical calibration, camera serials, pixel format or hardware ROI geometry. Optical calibration and PID operation are available in **Control feedback…**.

### Calibrate and operate Far Field feedback

Click **Control feedback…** in the main window. The source is fixed to **Far Field**.

1. In **Calibration**, enter the measured 2×2 matrix in µrad/pixel. For aligned axes, enter the signed X and Y scales on the diagonal and zero off-diagonal terms. The optional optical helper fills an estimate using pixel pitch, effective focal length and additional magnification; it does not confirm the estimate. After verifying the values and axis signs, tick **I have verified this Far Field angular calibration** and press **Apply calibration / target**. Editing matrix values clears that confirmation.
2. In **Beam region / target**, set the reference coordinates or use a valid detected centroid. Optionally restrict the software analysis region to isolate the beam. These edits use the same **Apply calibration / target** button. A blue rectangle marks the analysis region; raw saved frames remain unchanged. The main window's click/reference controls still work.
3. In **PID tuning**, edit X/Y Kp, Ki and Kd. In **Limits / timing**, set the normalized output and integral limits, slew limit, pointing deadband, derivative filter time, maximum sample age and watchdog. Press **Apply PID**. Actual applied tuning is shown only after the separate PID process acknowledges the update. Output is held at zero while awaiting acknowledgment; a timeout is shown after 3 seconds and requires a retry.
4. **Pause PID** holds nominal output at zero while acquisition and recording continue. To resume, tick **Enable nominal PID calculations** and press **Apply PID**. **Reset PID history** clears integral/derivative history using the current applied tuning. Calibration, target and tuning changes also reset history automatically.

**Save applied profile…** exports the current Far Field calibration, analysis region, reference and acknowledged PID settings to JSON. **Load profile…** fills drafts; Apply each section to activate the imported values. Profiles do not change startup defaults automatically. **Reload live values** discards unsent edits. Calibration and PID changes are recorded in `settings_changes.jsonl` and `pid_settings_changes.jsonl`, respectively.

The main status and feedback dialog explain why feedback is unavailable, including unconfirmed calibration, excessive Far Field clipping, low signal, stale data, paused PID or an unacknowledged settings update. All output remains **nominal only**; piezo voltage conversion and hardware output are still future integration work. See feedback controls and verification.

The working configuration on this development PC contains these verified serials. The transfer ZIP instead starts with unassigned serials; use **configure_cameras.cmd** on a new setup:

| Camera | Basler serial |
|---|---|
| Far Field | 40680007 |
| Dark Field | 40680017 |
| Near Field | 40680016 |

Verify discovery, check the configuration, then run:

```powershell
.\run_basler.cmd --list-cameras
.\run_basler.cmd --check-config
.\run_basler.cmd --record
```

Roles are assigned **only by serial number**, never discovery order. The supplied configuration requests the full 1456×1088 image at zero offsets, rather than a corner crop; `reference_px = []` places the default target at its center, (727.5, 543.5). The camera-specific applied settings and dimensions are recorded in the session folder. If replacing a camera with a different sensor, match width/height to its maximum resolution before use. Unsupported required features fail visibly and retry instead of silently using different acquisition settings.

Useful alternatives:

```powershell
# Full processing and saving, without a display; stop after 30 seconds.
.\run_simulation.cmd --headless --record --duration 30

# Put captured data on a separate SSD.
.\run_basler.cmd --record --output D:\BeamCaptures

# Standalone PID: start this first in one terminal, then acquisition in another.
.\.venv\Scripts\python.exe pid_controller.py --session captures\standalone_pid
.\run_basler.cmd --no-pid

# Numerical and data-flow tests, then a full simulated display/recording benchmark.
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tools\validate_pipeline.py --gui --duration 15

# Real-camera connectivity/timing and complete raw-frame readback (no optical calibration required).
.\.venv\Scripts\python.exe tools\validate_hardware.py --gui --duration 60

# Exercise live controls while recording; hardware mode requires all cameras to be free.
.\.venv\Scripts\python.exe tools\validate_live_settings.py
.\.venv\Scripts\python.exe tools\validate_live_settings.py --hardware
```

Only one PID listener can own the configured port. Standalone PID locks to its first application's stream; restart it between acquisition sessions. With automatic launch, the application generates a unique stream token and stops its child on exit. The child also monitors parent liveness.

## Data

Each run creates one folder named only with its **local start date/time**, including fractional seconds to avoid collisions. On this workstation, local time is Mountain Time. Capture starts when the application begins collecting telemetry; raw-frame recording can be toggled during the same run. There are exactly two folders inside it:

```text
captures/YYYY-MM-DD_HH-MM-SS.ffffff/
  raw_data/
    session.json               configuration, local/UTC start time, monotonic clock anchor
    camera_<role>.json         camera identity, network and applied device settings
    frames/<role>/*.npyseq     every recorded native frame; snapshots are individual NPY files
    frame_index.csv            exact raw paths, offsets, lengths and frame/save timestamps
    beam_data.csv              complete full-precision measurement telemetry
    control.csv                every nominal PID result emitted by the attached PID process
    settings_changes.jsonl     camera/calibration/reference update history
    pid_settings_changes.jsonl acknowledged PID updates and rejections
    events.jsonl               timestamped application warnings/errors, including exceptions
    application.log            text version of warnings/errors
    pid_process.log            PID subprocess output and startup failures
    *_summary.json             capture, recorder and PID integrity/timing summaries
    readable_report_status.json live/complete output status
  human_readable/
    START_HERE.html            easy-to-read summary, links and image gallery
    README.txt                 plain-language interpretation and file guide
    centroids.csv              side-by-side FF/NF times, pixel centroids and pixel errors
    pid_feedback.csv           nominal PID outputs joined to their Far Field source frames
    PID_equation.txt           equation, sign, filtering, anti-windup and actual gains/limits
    pid_settings.csv           tuning history with descriptive column names
    settings_history.csv       requested/applied settings and failures
    errors_and_warnings.csv    application failures and grouped detection/PID notices
    images_index.csv           PNG filenames and exact raw-frame references
    images/<role>/*.png        selected lossless native images and any display previews
```

Open **human_readable/START_HERE.html** during capture. Native frames, centroid/PID CSVs, camera settings history, errors, PID equations and snapshot PNGs are written live by separate storage workers. Rows and frame indexes are flushed after every write, including under sustained load; shutdown only drains pending work and writes final summaries. A crash can lose pending queues and an unfinished write, but completed indexed frames and flushed CSV rows remain usable. This does not guarantee power-loss durability.

The default **snapshot_interval_s = 60.0** saves one native frame plus one PNG per camera at the first frame and every minute afterward, independently of the Record button. Use the main-window interval field and **Apply interval and capture**, **Capture now / restart timer**, or **Save current frames (S)**. **R** additionally records every arriving native frame. No extra PNG is created for every continuous-recording frame.

`centroids.csv` has exactly these columns:

```text
Far Field Capture Time | FF_Cent_x | FF_Cent_y | FF_Cent_err_x | FF_Cent_err_y | Near Field Capture Time | NF_Cent_x | NF_Cent_y | NF_Cent_err_x | NF_Cent_err_y
```

Coordinates and errors are in pixels; error = measured centroid minus desired reference. Each side retains its own local host capture time with UTC offset. The cameras are asynchronous: nearby arrivals are paired within 0.5 seconds; unmatched or invalid measurements have blank cells. Dark Field is omitted. Complete validity, angular errors and processing data remain in raw_data/beam_data.csv.

**Subtract background…**: cover the selected camera, press its button, keep it covered while 16 frames are averaged (background_frames is configurable), then uncover when the saved/active message appears. The per-pixel dark map is saved under backgrounds/ beside config.toml, its path is written atomically to the configuration, and an independent copy is archived in the session. Subtraction applies to live display, centroid analysis and PNG snapshots. Original raw frames are retained for reprocessing. Exposure, gain, format, ROI, serial or simulation/backend mismatches invalidate the reference and require recapture. Saturation rejection still uses the original sensor pixels. A dark map removes fixed offsets; it cannot remove random shot/read noise.

An optional offline report rebuild updates the summary/gallery from files already saved; live tables are preserved. Legacy readable_image_interval_s affects only optional offline image export; generate_readable and --wait-report are compatibility options and do not disable live output.

```powershell
.\.venv\Scripts\python.exe tools\export_frames.py captures\SESSION exported_frames --format png
# Rebuild the readable bundle; 0 includes every saved frame as a PNG.
.\.venv\Scripts\python.exe -m beamdiag.report captures\SESSION --image-interval 0
```

You can also seek to `byte_offset` in a `.npyseq` file and call `numpy.load(file_handle, allow_pickle=False)`; calling `numpy.load` on the stream path alone reads only its **first** record. Set `format = "npy"` for individual raw files, or `format = "png"` for directly viewable lossless files at a higher CPU/filesystem cost. Native image formats are Mono8/Mono12/Mono16. With `pixel_format = "Mono12"` and `packed_transport = true`, the camera sends lossless Mono12p at **1.5 bytes per pixel on the wire**; pypylon unpacks it to the original 0–4095 uint16 values for analysis and recording. Disabling packed transport uses two wire bytes per pixel. Packing never reduces sensor dimensions or bit depth. A crash can leave an unindexed partial tail record; indexed records before it remain readable.

Readers and exporters accept the capture root, its `raw_data` folder, and older flat session folders. Existing captures are left in their original locations. Internally, `engine.session` is the raw directory and `engine.capture_dir` is the enclosing capture folder. Frame numbers are application sequences per camera, continuing across reconnects. `camera_timestamp` is the original device tick count, whose frequency is camera-dependent. Host nanosecond columns use `perf_counter_ns`; local/UTC start times and a clock anchor are supplied in session metadata. Device ticks are not compared to the host clock. The cameras free-run independently; these three streams are **not exposure-synchronized**.

## Centroid and angular calibration

For each monochrome analysis ROI, the processor subtracts a configured background, discards values at or below the intensity threshold, and uses compiled image moments:

```text
w(x,y) = I(x,y) - background, if I(x,y) - background > threshold; otherwise 0
cx = sum(x*w) / sum(w)              cy = sum(y*w) / sum(w)
displacement_px = [cx - reference_x, cy - reference_y]
error_urad = calibration_2x2 @ displacement_px
```

Coordinates are in the **delivered camera frame**, with software-ROI offsets restored. Add the saved sensor ROI offsets if full-sensor coordinates are required. Image y increases downward. The 2×2 matrix can encode rotation, handedness and cross-axis calibration. Low signal and excessive saturated-pixel fraction invalidate feedback. This is a bright-spot intensity centroid, so multiple bright spots, clipping at ROI edges, background gradients, hot pixels, and scattering need application-specific quality checks.

The sample scale, **17.25 µrad/pixel**, assumes 3.45 µm pixels, 200 mm effective focal length, and unit magnification:

```text
small-angle scale = 1e6 * pixel_pitch_m / (effective_focal_length_m * magnification)
10 urad / 17.25 urad_per_pixel = 0.580 pixels
```

Those optical values are placeholders. Hardware angular output is disabled until `calibration_confirmed = true` is set for that camera using measured values. Pixel displacement remains available. Simulation explicitly enables demo calibration, creating roughly ±6 µrad horizontal and ±4.8 µrad vertical jitter. It does not simulate a closed-loop plant.

The PID source is exclusively **Far Field**. Configuration validation rejects any other `source_role`, and the feedback publisher rejects Near Field and Dark Field measurements. Near Field centroids and references are diagnostic only; Dark Field is display/capture only: their centroid values, reference changes and detection failures cannot alter the PID error or reset its history. There is no averaging, fusion or fallback to another camera. Invalid or missing Far Field feedback produces zero nominal correction or the watchdog response even when the other cameras have valid detections. A Far Field reference change resets the PID history on its next measurement. The GUI labels Far Field as the PID source and the other views as diagnostic.

## Nominal PID and future piezo integration

`pid_controller.py` receives fixed-size versioned UDP packets over loopback. Each carries a stream token, sequence, host timestamps, reference revision, angular error and validity. It drains queued packets to the newest sample instead of accumulating control delay. It rejects duplicates, reordered samples, invalid centroids, unconfirmed calibration, nonfinite values, and measurements older than 50 ms. The supplied configuration resets to zero nominal correction after 200 ms without valid data, allowing for the Far Field camera's 83.3 ms frame interval. A reference change resets integral/derivative history. The GUI labels missing replies as offline and displays zero nominal output.

The two axes use actual sample intervals, a filtered derivative, bounded integral, conditional anti-windup for amplitude **and slew** limiting, and normalized outputs in ±1. The correcting sign is opposite `centroid - reference`. Gains are examples only. `control.csv` state values are 1=active, 2=invalid, 3=stale, 4=watchdog, 5=stopped, 6=paused, 7=reset (until the next sample).

There is **no actuator driver or voltage output**. A hardware implementation must map these normalized corrections to the actual nominal bias, units, calibrated piezo/mirror response matrix, voltage limits, polarity, and device-specific loss-of-feedback behavior. Reflected beam angle can be twice mirror rotation; do not assume a one-to-one beam/piezo scale. The natural adapter point is the result emitted by `NominalController.update()` in the standalone PID process; keep blocking device I/O out of that loop. Sub-10 µrad measurement and suppression depend on optics, SNR, actuator dynamics and jitter frequency. A 20 Hz sample rate has a 10 Hz Nyquist limit and does not imply 10 Hz closed-loop suppression bandwidth.

## Efficiency and expansion

```text
3 Basler GigE cameras → YuanLey switch → one shared Belkin uplink
    each camera worker: retrieve → one owned buffer copy → centroid → error packet to PID
                                                        ├→ latest display slot → Qt
                                                        ├→ bounded frame queue → disk writer
                                                        └→ bounded measurement queue → CSV writer
    separate PID process: newest valid angular error → PID → nominal command CSV + GUI reply
```

Each camera has one worker and one reusable float32 analysis buffer. OpenCV moments/thresholds run in compiled code. Frames are copied once out of the SDK buffer and then shared read-only with display and recording. SDK results are always released. Image/CSV writes, GUI rendering, and PID logging stay off the acquisition path. OpenCV threads are limited to one per call, and default numerical-library thread counts are limited to avoid competing thread pools.

Latest-image acquisition can skip older SDK frames. Display holds only the latest result. The raw-frame queue defaults to 48 entries; the supplied full-sensor profile uses 160 entries (about 483 MiB of native pixels, plus corrected images when background subtraction is active) to absorb brief disk pauses. Standard NPY records are written directly from contiguous frame memory without NumPy's per-record `tofile` path. When full, new save requests are dropped and counted. Telemetry has its own queue so image encoding cannot stall measurement logging. This is intentionally a low-latency framework with **observable drops**, rather than a lossless recorder under arbitrary load. Saved files use the operating system's normal write cache; write completion is not a guarantee of power-loss durability. Queue drain is bounded at shutdown and recorded in summaries.

The GUI polls at 60 Hz with a precise timer and redraws only new frames. Status text updates at about 5 Hz. Acquisition and PID continue if rendering lags. Latency windows retain the last 4096 samples; all measurement and control rows are separately logged. Infrequent PID settings messages use a separate, token-checked JSON management message on the same local socket, with acknowledgment/retry and idempotent request IDs. Per-frame measurement and command packets remain fixed-size binary messages.

Extension points are deliberately small:

| Module | Responsibility / extension |
|---|---|
| `beamdiag/cameras.py` | `CameraSource` interface; Basler and simulation adapters |
| `beamdiag/centroid.py` | Replaceable per-camera measurement algorithm |
| `beamdiag/model.py` | Immutable frame/measurement contracts and latest-value mailbox |
| `beamdiag/transport.py` | Versioned scalar control protocol |
| `beamdiag/pid.py` | Independent two-axis nominal control logic |
| `pid_controller.py` | Separate control process and nominal output integration point |
| `beamdiag/recording.py` | Alternative writers, chunked raw formats, archival policy |
| `beamdiag/runtime.py` | Worker orchestration, reconnection, timing |
| `beamdiag/gui.py` | Display and operator controls |

For higher resolution or rates, measure first. Reduce the **camera hardware ROI** to cut both network traffic and CPU load; a software analysis ROI only reduces CPU work. The default stream writer avoids per-frame file creation; future preallocated/chunked storage can use the same queue contract. GPU transfer, neural detection and compression are deliberately absent from the feedback path.


