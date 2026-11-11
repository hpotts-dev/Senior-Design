# Live capture controls

The supplied configuration captures the **full 1456 × 1088 sensor in Mono12**, at zero offsets, with the default reference at the image center (727.5, 543.5). Far Field and Near Field request **12 fps**, while Dark Field requests **6 fps**. This profile passed a one-minute live recording and display test on this setup. All camera links negotiated 1 Gb/s in the hardware test. The sensor's 12-bit values (0–4095) are retained in 16-bit native files and PNGs. Separate 8-bit display PNGs make these images easy to view without changing the measurement data. Lossless Mono12p packing is used only during transmission, then decoded to the original uint16 pixel values. Live **Auto contrast (preview only)** improves visibility without changing image geometry or measurement data.

## Images and continuous recording

- The first frame from each camera is saved automatically. Another frame is saved every **60 seconds**, even when **Record** is off.
- Change **Always-on image capture interval (seconds)** and press **Apply interval and capture**. This saves the interval in the configuration and starts a fresh timed capture.
- **Capture now / restart timer** captures each camera's next frame and restarts its countdown.
- **Save current frames (S)** saves the latest available frames immediately without changing the timer.
- **Record (R)** additionally saves every arriving native frame. Turning it off leaves timed snapshots and centroid/PID logging active.

Timed and manual snapshots each save a native raw frame, a native PNG, and (for 12/16-bit images) an 8-bit viewing PNG. Continuous recording saves native frames without creating a PNG for every frame. The interval is per camera and follows actual frame arrivals; unavailable cameras cannot provide a snapshot. Queue drops and disk failures are counted in the interface and logs.

## Centroid and PID data

Only **Far Field and Near Field** calculate centroids. Only **Far Field** supplies the nominal PID calculation. Dark Field remains available for display, camera settings, background subtraction and image capture.

Press **Settings…** beneath Dark Field to adjust exposure, gain and requested FPS in its **Camera** tab. Delivered FPS and image dimensions appear when frames arrive. Centroid controls appear only for Far/Near Field. While unplugged, the dialog shows configured values and enables live editing after connection.

**Auto contrast (preview only)** is also available beside the exposure controls. It keeps the preview bright by rescaling each frame, which can make different exposures look similar. Turn it off to see exposure/gain change displayed brightness; it immediately updates the matching switch beneath the image. Dark Field's **Raw brightness** mean/peak readings show actual sensor intensity regardless of preview contrast or background subtraction. Native recordings remain unchanged by the contrast switch.

`human_readable/centroids.csv` contains exactly:

```text
Far Field Capture Time | FF_Cent_x | FF_Cent_y | FF_Cent_err_x | FF_Cent_err_y | Near Field Capture Time | NF_Cent_x | NF_Cent_y | NF_Cent_err_x | NF_Cent_err_y
```

Centroid coordinates and errors are in delivered-frame pixels. Error is **measured position minus desired reference**. Each camera has its own local host capture time, including UTC offset. The streams are asynchronous; adjacent arrivals within 0.5 seconds are paired once. Missing and invalid measurements have blank cells. These rows do not imply synchronized exposures. Complete validity, signal, calibration and timing information is retained in `raw_data/beam_data.csv`.

The requested intervals are **83.3 ms** at 12 fps for Far/Near Field and **166.7 ms** at 6 fps for Dark Field. The **50 ms processing target** measures work after retrieval, independently of frame cadence. The configured 200 ms watchdog accommodates Far Field's 83.3 ms interval. Real angular feedback still needs measured, confirmed calibration.

## Subtract background

1. Cover the selected camera manually so it receives no beam or ambient light.
2. Press its **Subtract background…** button.
3. Keep it covered while the requested frames are averaged; the default is **16 frames**, about 1.33 seconds at 12 fps or 2.67 seconds at 6 fps.
4. Wait for **Background saved and active — uncover camera**, then remove the cover.

Each pixel's averaged dark brightness is subtracted with clipping at zero. The reference applies to the live display, centroid calculation and readable PNGs. Original raw pixels are preserved. Saturation rejection uses the original sensor pixels so subtraction cannot hide clipping.

The reference is saved in `backgrounds/` beside the active configuration; `background_file` records its path. Each capture archives its own copy. The map is tied to the camera serial, dimensions, offsets, pixel format, exposure and gain. Changing those settings requires a new dark capture. Real-camera and simulated references are kept incompatible. `background_frames` controls averaging. Clear `background_file` with the configuration editor to disable a reference. Fixed-pattern offsets can be removed; random noise cannot be fully removed this way.

## Edit every setting

Press **Configuration…** for the complete editable TOML configuration, populated from the current running settings. **Validate** checks edits without changing files. **Save & Apply** atomically saves valid settings, drains the current capture, restarts acquisition/PID, and opens a new timestamped capture folder while the app stays open. Geometry, camera assignments, transport limits, storage paths, queue sizes and PID ports are therefore all editable. Feeds pause briefly during this operation. Use the per-camera and feedback dialogs for adjustments that can be applied between frames or by restarting just one stream.

Unsupported camera features are reported by the camera worker; validation cannot prove that disconnected hardware supports a requested setting. Camera role order and Far Field PID routing remain enforced.

## Live files and crash recovery

Each session has exactly `raw_data/` and `human_readable/` beneath its date/time folder. Open `human_readable/START_HERE.html` while acquisition runs. Centroid/PID tables, settings, errors, PID equations and snapshot images are written during capture. CSV rows and frame indexes are flushed after each write, even when queues remain busy. Storage and PNG encoding run separately from acquisition/PID calculations.

A crash can lose pending queued items or the write currently in progress. Previously completed indexed frames and flushed rows remain usable without a shutdown report. OS-cache writes do not guarantee power-loss durability. Keep sufficient disk space and check the visible drop/error counters during long runs.

`readable_image_interval_s`, `generate_readable` and `--wait-report` remain for older configurations/tools; they do not disable live tables or timed captures. Optional offline report rebuilding preserves live tables. Use `--image-interval` only when deliberately regenerating an offline image selection.

## Another Windows PC

Use the updated `dist/BeamDiagnostics_Windows.zip` and follow [WINDOWS_SETUP.md](WINDOWS_SETUP.md). Supported targets are Windows 10/11 x64 with standard x64 Python 3.11–3.14, plus Basler/adapter drivers for physical acquisition. Run `setup.cmd`, test `run_simulation.cmd`, assign cameras with `configure_cameras.cmd`, then use `run_basler.cmd`. No developer username, copied Python environment, or PowerShell policy change is required. Verify performance and calibration on each PC.
