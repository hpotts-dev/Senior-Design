"""Build readable capture outputs after acquisition ends; raw data is never rewritten."""
from __future__ import annotations

import os
for _key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_key, "1")
import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

from .capture_layout import capture_paths
from .config import PIXEL_BITS, ROLES
from .frame_io import load_saved_frame
from .transport import STATES
from .image_io import write_png

CAMERA_NAMES = {role: role.replace("_", " ").title() for role in ROLES}
REASONS = {"ok": "Valid angular feedback", "uncalibrated": "Centroid detected; angular calibration unconfirmed",
           "low_flux": "Insufficient beam signal", "saturation": "Clipped pixels exceed the configured limit"}


def rows(path):
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as stream:
            yield from csv.DictReader(stream)


def json_lines(path):
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else ({} if default is None else default)


def write_json(path, data):
    temporary = path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    temporary.replace(path)


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else ""
    except (TypeError, ValueError):
        return ""


def yes(value):
    return value is True or str(value).lower() == "true"


class CaptureReport:
    def __init__(self, capture, image_interval=None):
        self.capture, self.raw, self.output = capture_paths(capture)
        self.output.mkdir(exist_ok=True)
        self.metadata = read_json(self.raw/"session.json")
        self.live = self.metadata.get("live_outputs", False)
        self.explicit_image_interval = image_interval
        self.summary = read_json(self.raw/"summary.json")
        self.cfg = self.metadata.get("configuration", {})
        self.started_ns = int(self.metadata.get("started_host_ns", 0))
        if not self.started_ns:  # Legacy captures have no monotonic-to-wall-clock anchor.
            first = next(rows(self.raw/"beam_data.csv"), {})
            self.started_ns = int(first.get("received_ns", 0))
        start = self.metadata.get("started_utc")
        self.started_utc = datetime.fromisoformat(start) if start else datetime.now(timezone.utc)
        local = self.metadata.get("started_local")
        self.started_local = datetime.fromisoformat(local) if local else self.started_utc.astimezone()
        self.image_interval = (self.cfg.get("recording", {}).get("readable_image_interval_s", 1.0)
                               if image_interval is None else image_interval)
        if not math.isfinite(self.image_interval) or self.image_interval < 0:
            raise ValueError("PNG interval must be finite and nonnegative (0 exports every saved frame)")
        self.cameras = {c["role"]: c for c in self.cfg.get("cameras", [])}
        self.camera_stats = {role: {"measurements": 0, "centroid_valid": 0, "angular_valid": 0,
                                  "reasons": Counter(), "dx_squared": 0.0, "dy_squared": 0.0}
                             for role in ROLES}
        self.control_states = Counter()
        self.issue_count = self.runtime_issues = 0
        self.issue_examples = []
        self.first_ns = self.last_ns = None
        self.image_count = self.saved_count = 0
        self.gallery = {}

    def time(self, value):
        if value in (None, ""):
            return "", ""
        stamp = int(value)
        seconds = (stamp-self.started_ns)/1e9
        local = (self.started_utc+timedelta(seconds=seconds)).astimezone(self.started_local.tzinfo)
        return round(seconds, 6), local.isoformat(timespec="milliseconds")

    def status(self, state, **extra):
        data = {"state": state, "capture": str(self.capture), "image_interval_s": self.image_interval, **extra}
        write_json(self.raw/"readable_report_status.json", data)

    def issue(self, writer, severity, source, message, start=None, end=None, count=1):
        elapsed, local = self.time(start)
        end_elapsed, _ = self.time(end)
        writer.writerow([local, elapsed, end_elapsed, severity, source, count, message])
        self.issue_count += 1
        if severity in ("Error", "Warning"):
            self.runtime_issues += 1
        if len(self.issue_examples) < 40:
            self.issue_examples.append((severity, source, message, count))

    def centroids(self, errors):
        states = {}

        def finish(role):
            segment = states.get(role)
            if segment and segment["reason"] != "ok":
                reason = segment["reason"]
                severity = "Notice" if reason == "uncalibrated" else "Detection warning"
                self.issue(errors, severity, CAMERA_NAMES[role], REASONS.get(reason, reason),
                           segment["start"], segment["end"], segment["count"])

        with (self.output/"centroids.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Local time (with UTC offset)", "Elapsed time (s)", "Camera", "Frame",
                "Centroid X (px)", "Centroid Y (px)", "Reference X (px)", "Reference Y (px)",
                "Displacement X (px)", "Displacement Y (px)", "Pointing error X (urad)", "Pointing error Y (urad)",
                "Centroid detected", "Angular feedback valid", "Status", "Peak intensity (DN)",
                "Clipped pixels (%)", "Integrated signal (DN)", "Processing time (ms)", "Feedback revision"])
            for row in rows(self.raw/"beam_data.csv"):
                role = row["role"]
                if role not in self.camera_stats:
                    continue
                stamp = int(row["received_ns"])
                self.first_ns = min(self.first_ns, stamp) if self.first_ns is not None else stamp
                self.last_ns = max(self.last_ns, stamp) if self.last_ns is not None else stamp
                elapsed, local = self.time(stamp)
                stats = self.camera_stats[role]
                stats["measurements"] += 1
                valid, angular = yes(row["centroid_valid"]), yes(row["angular_valid"])
                stats["centroid_valid"] += valid
                stats["angular_valid"] += angular
                reason = row["reason"]
                stats["reasons"][reason] += 1
                if valid:
                    stats["dx_squared"] += float(row["error_x_px"])**2
                    stats["dy_squared"] += float(row["error_y_px"])**2
                if role not in states or states[role]["reason"] != reason:
                    finish(role)
                    states[role] = {"reason": reason, "start": stamp, "end": stamp, "count": 0}
                states[role]["end"] = stamp
                states[role]["count"] += 1
                writer.writerow([local, elapsed, CAMERA_NAMES[role], row["sequence"],
                    *[number(row[k]) for k in ("centroid_x", "centroid_y", "reference_x", "reference_y", "error_x_px", "error_y_px")],
                    number(row["error_x_urad"]) if angular else "", number(row["error_y_urad"]) if angular else "",
                    "Yes" if valid else "No", "Yes" if angular else "No", REASONS.get(reason, reason),
                    number(row["peak"]), round(float(row["saturated_fraction"])*100, 6), number(row["flux"]),
                    number(row["host_processing_ms"]), row["reference_revision"]])
        for role in states:
            finish(role)

    def pid(self, errors):
        # Two sequential readers provide a bounded-memory join. Sequences are
        # monotonic per camera; watchdog/reset/stop rows may repeat a source frame.
        source = (row for row in rows(self.raw/"beam_data.csv") if row["role"] == "far_field")
        measurement = next(source, None)
        segment = None

        def finish():
            if segment and segment["state"] in ("invalid", "stale", "watchdog", "paused"):
                state = segment["state"]
                explanation = {"invalid": "Far Field angular feedback was invalid; nominal output was zero",
                    "stale": "Far Field sample was too old or had invalid timing; nominal output was zero",
                    "watchdog": "No valid Far Field feedback arrived before the watchdog expired; nominal output was zero",
                    "paused": "Nominal PID was intentionally paused"}[state]
                self.issue(errors, "Warning" if state in ("stale", "watchdog") else "Notice", "Far Field PID",
                           explanation, segment["start"], segment["end"], segment["count"])
        with (self.output/"pid_feedback.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Local time (with UTC offset)", "Elapsed time (s)", "Far Field frame", "PID state",
                "Nominal correction X (normalized)", "Nominal correction Y (normalized)",
                "Pointing error X (urad)", "Pointing error Y (urad)", "Centroid X (px)", "Centroid Y (px)",
                "Displacement X (px)", "Displacement Y (px)", "Far Field detection status",
                "Frame retrieval to PID reply (ms)", "PID calculation time (ms)"])
            for row in rows(self.raw/"control.csv"):
                sequence = int(row["sequence"])
                while measurement is not None and int(measurement["sequence"]) < sequence:
                    measurement = next(source, None)
                match = measurement if measurement is not None and int(measurement["sequence"]) == sequence else None
                state = STATES.get(int(row["state"]), "unknown")
                self.control_states[state] += 1
                if segment is None or segment["state"] != state:
                    finish()
                    segment = {"state": state, "start": int(row["received_ns"]), "end": int(row["received_ns"]), "count": 0}
                segment["end"] = int(row["received_ns"])
                segment["count"] += 1
                elapsed, local = self.time(row["received_ns"])
                values = [number(match[k]) for k in ("error_x_urad", "error_y_urad", "centroid_x", "centroid_y",
                                                    "error_x_px", "error_y_px")] if match else [""]*6
                writer.writerow([local, elapsed, sequence, state.title(), number(row["x"]), number(row["y"]),
                    *values, REASONS.get(match["reason"], match["reason"]) if match else "Measurement not in this capture",
                    number(row["host_to_pid_ms"]), number(row["compute_ms"])])
        finish()

    def settings(self, errors):
        events = list(json_lines(self.raw/"settings_changes.jsonl"))
        pid_events = list(json_lines(self.raw/"pid_settings_changes.jsonl"))
        with (self.output/"settings_history.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Local time (with UTC offset)", "Elapsed time (s)", "Area", "Camera", "Outcome", "Requested change", "Applied values", "Message"])
            for camera in self.cfg.get("cameras", []):
                writer.writerow([self.started_local.isoformat(), 0, "Initial requested camera configuration",
                    CAMERA_NAMES.get(camera["role"], camera["role"]), "Requested at startup", "", json.dumps(camera),
                    "See raw_data/camera_<role>.json for device readback"])
            combined = [(e["host_ns"], "PID", e) for e in pid_events]
            combined += [(e["host_ns"], e.get("category", "camera"), e) for e in events]
            for stamp, area, event in sorted(combined, key=lambda item: item[0]):
                elapsed, local = self.time(stamp)
                success = event.get("success", False)
                role = event.get("role", "far_field")
                writer.writerow([local, elapsed, area, CAMERA_NAMES.get(role, role), "Applied" if success else "Failed",
                    json.dumps(event.get("requested", {})), json.dumps(event.get("actual", event.get("values", {}))), event.get("message", "")])
                if not success:
                    self.issue(errors, "Error", f"{CAMERA_NAMES.get(role, role)} {area} settings", event.get("message", "Update failed"), stamp)
        initial = self.cfg.get("pid", {})
        with (self.output/"pid_settings.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            scalar_keys = ("output_limit", "integral_limit", "slew_per_s", "deadband_urad", "derivative_tau_s", "max_sample_age_ms", "watchdog_ms")
            writer.writerow(["Local time (with UTC offset)", "Elapsed time (s)", "Enabled", "Outcome",
                "Kp X", "Kp Y", "Ki X", "Ki Y", "Kd X", "Kd Y", "Output amplitude limit (normalized)",
                "Integral contribution limit (normalized)", "Slew limit (normalized/s)", "Pointing deadband (urad)",
                "Derivative filter time (s)", "Max sample age (ms)", "Watchdog (ms)"])
            configs = [(self.started_ns, initial, "Startup configuration")]
            configs += [(e["host_ns"], e["values"], "Applied" if e["success"] else "Rejected; values unchanged") for e in pid_events]
            for stamp, values, outcome in configs:
                elapsed, local = self.time(stamp)
                gains = [v for key in ("kp", "ki", "kd") for v in values.get(key, ("", ""))]
                writer.writerow([local, elapsed, "Yes" if values.get("enabled", True) else "No", outcome,
                                 *gains, *[values.get(key, "") for key in scalar_keys]])
        final = self.summary.get("final_configuration", {}).get("pid", initial)
        for event in pid_events:
            if event.get("success"):
                final = {**final, **event["values"]}
        equation = """FAR FIELD NOMINAL PID EQUATION

Only Far Field supplies the PID error. Commands are normalized; no voltage or piezo drive is produced.

For each axis, on a fresh, valid sample with confirmed angular calibration:
  displacement_px = centroid_px - reference_px
  [displacement_X_urad, displacement_Y_urad] = calibration_2x2 * displacement_px
  e = -displacement_urad  (e = 0 inside the configured angular deadband)
  dt = actual time between host frame retrievals, in seconds
  alpha = dt / (derivative_filter_time_s + dt)
  D_filtered = D_previous + alpha * ((e - e_previous)/dt - D_previous)
  I_candidate = clamp(I_previous + Ki*e*dt, -integral_limit, +integral_limit)
  u_candidate = Kp*e + I_candidate + Kd*D_filtered
  low  = max(-output_limit, previous_output - slew_per_s*dt)
  high = min(+output_limit, previous_output + slew_per_s*dt)
  u = clamp(Kp*e + I + Kd*D_filtered, low, high)

Anti-windup: do not accept I_candidate when it would push further beyond low/high.
The first sample after a reset uses the nominal camera interval and zero derivative.
Invalid, stale, uncalibrated, paused and lost-feedback states produce zero nominal correction.
Calibration/reference and applied PID changes reset integral/derivative history.

GAINS AND LIMITS
Initial configuration:
"""+json.dumps(initial, indent=2)+"\n\nFinal applied configuration:\n"+json.dumps(final, indent=2)+"\n\nSee pid_settings.csv for every acknowledged tuning change and pid_feedback.csv for actual emitted outputs.\n"
        (self.output/"PID_equation.txt").write_text(equation, encoding="utf-8")

    def runtime_errors(self, errors):
        for event in json_lines(self.raw/"events.jsonl"):
            level = event.get("level", "WARNING")
            self.issue(errors, "Error" if level in ("ERROR", "CRITICAL") else "Warning",
                       event.get("component", "Application"), event.get("message", ""), event.get("host_ns"))
        for role, stats in self.summary.get("cameras", {}).items():
            if stats.get("errors", 0):
                self.issue(errors, "Error", CAMERA_NAMES.get(role, role),
                    f"Acquisition failures: {stats['errors']}. Last error: {stats.get('last_error') or 'recovered; see application.log'}")
        for filename, label in (("recording_summary.json", "Recording"), ("control_summary.json", "PID logging")):
            stats = read_json(self.raw/filename)
            for key in ("frames_dropped", "telemetry_dropped", "write_errors", "log_drops"):
                if stats.get(key, 0):
                    self.issue(errors, "Error", label, f"{key.replace('_', ' ').title()}: {stats[key]}")
            message = stats.get("last_error") or stats.get("log_error")
            if message:
                self.issue(errors, "Error", label, message)
            if stats and not stats.get("drain_complete", True):
                self.issue(errors, "Error", label, "Output queue did not finish draining")
        if not (self.raw/"control.csv").exists():
            self.issue(errors, "Notice", "PID", "No local PID output log. If using standalone PID, its control.csv is in that process's output folder.")

    def images(self):
        if self.live and self.explicit_image_interval is None:
            self.saved_count = sum(1 for _ in rows(self.raw/"frame_index.csv"))
            for row in rows(self.output/"images_index.csv"):
                self.image_count += 1
                role = next((r for r in ROLES if CAMERA_NAMES[r] == row["Camera"]), None)
                if role:
                    self.gallery[role] = row["Display preview path"]
            return
        images = self.output/"images"
        images.mkdir(exist_ok=True)
        last_sample_ns, last_rows = {}, {}
        interval_ns = self.image_interval*1e9
        with (self.output/"images_index.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Local time (with UTC offset)", "Elapsed time (s)", "Camera", "Frame", "Capture type",
                "PNG path", "Display preview path", "Native dtype", "Display scale", "Raw frame path", "Raw byte offset"])

            def export(index, row):
                role = row["role"]
                if role not in ROLES:
                    raise ValueError("Unknown camera role in frame index")
                folder = images/role
                folder.mkdir(exist_ok=True)
                elapsed, local = self.time(row["received_ns"])
                kind = "record" if row["kind"] == "record" else "snapshot"
                name = f"frame_{int(row['sequence']):010d}_{kind}_{index:08d}.png"
                target = folder/name
                image = load_saved_frame(self.raw, row)
                if row.get("background_reference"):
                    reference = (self.raw/row["background_reference"]).resolve()
                    if not reference.is_relative_to(self.raw.resolve()):
                        raise ValueError("Background reference escapes raw capture folder")
                    with np.load(reference, allow_pickle=False) as saved:
                        image = cv2.subtract(image, saved["image"])
                if not write_png(target, image):
                    raise OSError(f"Cannot write {target}")
                preview, scale = target, "Native 8-bit values"
                if image.dtype == np.uint16:
                    bits = PIXEL_BITS.get(self.cameras.get(role, {}).get("pixel_format", "Mono16"), 16)
                    preview = folder/(target.stem+"_display.png")
                    display = cv2.convertScaleAbs(image, alpha=255/((1 << bits)-1))
                    if not write_png(preview, display):
                        raise OSError(f"Cannot write {preview}")
                    scale = f"Display only: 0..{(1 << bits)-1} DN mapped to 0..255; native PNG preserves uint16"
                relative, display_relative = target.relative_to(self.output).as_posix(), preview.relative_to(self.output).as_posix()
                raw_relative = Path(os.path.relpath(self.raw/row["path"], self.output)).as_posix()
                writer.writerow([local, elapsed, CAMERA_NAMES.get(role, role), row["sequence"], row["kind"], relative,
                                 display_relative, str(image.dtype), scale, raw_relative, row.get("byte_offset", 0)])
                self.image_count += 1
                self.gallery[role] = display_relative
                if self.image_count % 100 == 0:
                    self.status("generating PNG images", png_frames=self.image_count)

            for index, row in enumerate(rows(self.raw/"frame_index.csv")):
                role, stamp = row["role"], int(row["received_ns"])
                selected = (row["kind"] != "record" or role not in last_sample_ns or
                            stamp-last_sample_ns[role] >= interval_ns)
                self.saved_count += 1
                if selected:
                    export(index, row)
                    if row["kind"] == "record":
                        last_sample_ns[role] = stamp
                if role not in last_rows or stamp >= int(last_rows[role][1]["received_ns"]):
                    last_rows[role] = (index, row, selected)
            for index, row, selected in last_rows.values():
                if not selected:
                    export(index, row)

    def overview(self):
        duration = (self.last_ns-self.first_ns)/1e9 if self.first_ns is not None else 0
        measurements = sum(s["measurements"] for s in self.camera_stats.values())
        recording = self.summary.get("recording", read_json(self.raw/"recording_summary.json"))
        coverage = ("every saved frame" if self.image_interval == 0 else
                    f"one recorded frame per camera every {self.image_interval:g} second(s), plus the last frame and every manual snapshot")
        if self.live and self.explicit_image_interval is None:
            coverage = "timed and manual snapshots, written live independently of continuous recording"
        mode = "SIMULATION — angular scales are demonstration values" if self.metadata.get("simulated") else "REAL CAMERAS"
        text = (f"CAPTURE SUMMARY\n\nStarted: {self.started_local.isoformat(timespec='seconds')}\nMode: {mode}\n"
            f"Measured frame span: {duration:.3f} s\nCentroid measurement rows: {measurements}\nRaw saved frames: {self.saved_count}\n"
            f"PNG frames exported: {self.image_count}\nPNG coverage: {coverage}\n"
            f"Dropped raw saves: {recording.get('frames_dropped', 0)}\nRecording write errors: {recording.get('write_errors', 0)}\n"
            f"PID states: {dict(self.control_states)}\n\n"
            "START_HERE.html: open this in a browser for the summary and image gallery.\n"
            "centroids.csv: Far/Near Field capture times, pixel centroids and measured-minus-desired pixel errors.\n"
            "pid_feedback.csv: actual Far Field nominal commands, source centroids and latency.\n"
            "PID_equation.txt: actual controller equation, sign convention and initial/final gains.\n"
            "pid_settings.csv / settings_history.csv: applied settings and rejected updates.\n"
            "errors_and_warnings.csv: application failures, settings failures and grouped detection notices.\n"
            "images_index.csv: PNG files and links back to the exact raw source frame.\n"
            "../raw_data/: complete native frames, full precision measurement/control logs and configuration.\n\n"
            "IMPORTANT INTERPRETATION\n"
            "Only Far Field supplies PID feedback. Near Field has diagnostic centroids; Dark Field is display/capture only.\n"
            "FF/NF cameras are asynchronous. CSV rows retain each camera's actual capture time; missing/invalid values are blank.\n"
            "Commands are normalized nominal calculations; no piezo voltage was output.\n"
            "Blank angle cells mean angular feedback was unavailable; they are not zero error.\n"
            "Unconfirmed calibration is a notice, not a camera connection failure.\n"
            "Polling timeouts between slow frames are normal; acquisition errors are reported separately.\n"
            "Raw files preserve native pixel values. PNGs apply the archived background reference when active; uint16 also has an 8-bit display preview.\n"
            "No centroid/reference markers are burned into native PNGs or raw frames.\n"
            "References and settings may change during capture; use feedback revision/settings history when comparing intervals.\n"
            "Raw data includes every saved frame. Preview sampling does not drop raw recordings.\n"
            "Telemetry is captured from application start even while raw-frame recording is off.\n"
            "Host latency starts after frame retrieval; exposure, network and actuator settling are excluded.\n")
        if not self.saved_count:
            text += "\nNo raw images were recorded. Enable Record frames or Save current frames during acquisition to obtain PNG images.\n"
        if not self.metadata.get("started_host_ns"):
            text += "\nLegacy timestamps: elapsed times use the first measurement as zero; local wall times are approximate.\n"
        (self.output/"README.txt").write_text(text, encoding="utf-8")
        camera_rows = []
        for role, stats in self.camera_stats.items():
            n = stats["centroid_valid"]
            rms = (f"{math.sqrt(stats['dx_squared']/n):.4f}, {math.sqrt(stats['dy_squared']/n):.4f}" if n else "Unavailable")
            camera_rows.append("<tr>"+"".join(f"<td>{escape(str(v))}</td>" for v in (
                CAMERA_NAMES[role], stats["measurements"], stats["centroid_valid"], stats["angular_valid"], rms,
                "; ".join(f"{REASONS.get(k, k)}: {v}" for k, v in stats["reasons"].items())))+"</tr>")
        issues = "".join("<tr>"+"".join(f"<td>{escape(str(v))}</td>" for v in entry)+"</tr>" for entry in self.issue_examples)
        if not issues:
            issues = '<tr><td colspan="4">No errors, warnings or detection notices were logged.</td></tr>'
        gallery = "".join(f'<figure><img src="{escape(path)}" alt="{CAMERA_NAMES[role]} last exported frame">'
                          f'<figcaption>{CAMERA_NAMES[role]} — last exported frame</figcaption></figure>' for role, path in self.gallery.items())
        links = "".join(f'<a href="{name}">{label}</a>' for name, label in (
            ("centroids.csv", "Centroids and pointing error"), ("pid_feedback.csv", "Nominal PID outputs"),
            ("PID_equation.txt", "PID equation"), ("pid_settings.csv", "PID gains and limits"),
            ("errors_and_warnings.csv", "Errors and notices"), ("settings_history.csv", "Settings history"),
            ("images_index.csv", "PNG image index"), ("../raw_data/", "Complete raw data")))
        html = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beam capture — {escape(self.capture.name)}</title><style>
body{{font:16px system-ui,sans-serif;color:#182b3a;background:#f3f6f8;margin:0}}main{{max-width:1200px;margin:auto;padding:32px}}
h1{{font-size:30px}}h2{{margin-top:32px}}.cards,.gallery{{display:flex;flex-wrap:wrap;gap:16px}}.card,figure{{background:white;border:1px solid #d4dfe5;border-radius:8px;padding:18px;margin:0}}
.card strong{{display:block;font-size:26px}}nav{{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}}a{{color:#146c96}}nav a{{padding:10px;background:white;border:1px solid #d4dfe5;border-radius:6px}}
table{{border-collapse:collapse;width:100%;background:white}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #d4dfe5}}th{{background:#e3edf3}}
.scroll{{overflow:auto}}figure img{{width:300px;max-width:100%;image-rendering:auto}}figcaption{{padding-top:10px}}pre{{white-space:pre-wrap;background:white;padding:20px;border:1px solid #d4dfe5}}.note{{color:#425a6b}}</style>
<main><h1>Beam capture</h1><p>{escape(self.started_local.isoformat(timespec='seconds'))} · {escape(mode)}</p>
<div class="cards"><div class="card"><strong>{self.saved_count:,}</strong>raw frames saved</div>
<div class="card"><strong>{measurements:,}</strong>centroid measurements</div><div class="card"><strong>{self.image_count:,}</strong>PNG frames</div>
<div class="card"><strong>{recording.get('frames_dropped', 0):,}</strong>dropped raw saves</div></div>
<nav>{links}</nav><h2>Camera measurements</h2><p class="note">RMS X/Y is displacement from the active target, in pixels, across all valid centroids. Settings and targets may change during this capture.</p>
<div class="scroll"><table><tr><th>Camera</th><th>Measurements</th><th>Detected centroids</th><th>Valid angular samples</th><th>RMS X/Y (px)</th><th>Detection status</th></tr>{''.join(camera_rows)}</table></div>
<h2>Nominal PID</h2><p>Far Field only. States: {escape(str(dict(self.control_states)))}. Commands are normalized; no piezo drive was produced.</p>
<p>Use PID equation for the sign convention, anti-windup, filtering, gains and limits. Use Nominal PID outputs for actual emitted commands and source-frame measurements.</p>
<h2>Images</h2><p>{escape(coverage.capitalize())}. Native PNGs preserve precision. For 16-bit images, the gallery uses a separately labelled 8-bit display preview.</p>
<div class="gallery">{gallery or '<p>No raw frames were recorded.</p>'}</div>
<h2>Errors and detection notices</h2><p>{self.issue_count} grouped entries. Detection notices are separate from acquisition failures; see the CSV for every entry.</p>
<div class="scroll"><table><tr><th>Severity</th><th>Source</th><th>Message</th><th>Affected samples/events</th></tr>{issues}</table></div>
<h2>How to read these files</h2><pre>{escape(text)}</pre></main></html>"""
        (self.output/"START_HERE.html").write_text(html, encoding="utf-8")

    def build(self):
        started = perf_counter()
        if self.live:
            # Live files are already complete, including after an interrupted run.
            # An optional summary rebuild never overwrites those tables.
            for row in rows(self.raw/"beam_data.csv"):
                role = row["role"]
                if role not in self.camera_stats:
                    continue
                s = self.camera_stats[role]
                s["measurements"] += 1
                valid = yes(row["centroid_valid"])
                s["centroid_valid"] += valid
                s["angular_valid"] += yes(row["angular_valid"])
                s["reasons"][row["reason"]] += 1
                if valid:
                    s["dx_squared"] += float(row["error_x_px"])**2
                    s["dy_squared"] += float(row["error_y_px"])**2
                stamp = int(row["received_ns"])
                self.first_ns = min(self.first_ns, stamp) if self.first_ns else stamp
                self.last_ns = max(self.last_ns, stamp) if self.last_ns else stamp
            for row in rows(self.raw/"control.csv"):
                self.control_states[STATES.get(int(row["state"]), "unknown")] += 1
            for row in rows(self.output/"errors_and_warnings.csv"):
                self.issue_count += 1
                if len(self.issue_examples) < 40:
                    self.issue_examples.append((row["Severity"], row["Source"], row["Explanation"], row["Affected samples/events"]))
            self.images()
            self.overview()
            result = {"state": "ready", "path": str(self.output/"START_HERE.html"),
                "saved_frames": self.saved_count, "png_frames": self.image_count,
                "image_interval_s": self.image_interval, "duration_s": round(perf_counter()-started, 3)}
            self.status("ready", **{k: v for k, v in result.items() if k != "state"})
            return result
        self.status("generating tables")
        with (self.output/"errors_and_warnings.csv").open("w", newline="", encoding="utf-8-sig") as f:
            errors = csv.writer(f)
            errors.writerow(["Local start time (with UTC offset)", "Start elapsed time (s)", "End elapsed time (s)",
                             "Severity", "Source", "Affected samples/events", "Explanation"])
            self.centroids(errors)
            self.pid(errors)
            self.settings(errors)
            self.runtime_errors(errors)
        self.status("generating PNG images")
        self.images()
        self.overview()
        result = {"state": "ready", "path": str(self.output/"START_HERE.html"),
                  "saved_frames": self.saved_count, "png_frames": self.image_count,
                  "image_interval_s": self.image_interval, "duration_s": round(perf_counter()-started, 3)}
        self.status("ready", **{k: v for k, v in result.items() if k != "state"})
        if (self.raw/"summary.json").exists():
            self.summary["readable_report"] = result
            write_json(self.raw/"summary.json", self.summary)
        return result


def generate_report(capture, image_interval=None):
    cv2.setNumThreads(1)
    report = CaptureReport(capture, image_interval)
    try:
        return report.build()
    except Exception as exc:
        report.status("failed", error=str(exc))
        if (report.raw/"summary.json").exists():
            report.summary["readable_report"] = {"state": "failed", "error": str(exc)}
            write_json(report.raw/"summary.json", report.summary)
        (report.output/"README.txt").write_text("Readable report generation failed: "+str(exc)+
            "\nThe complete raw recording is preserved in ../raw_data/.\nSee raw_data/readable_report.log or retry the report tool.\n", encoding="utf-8")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="Capture root, raw_data folder, or legacy session")
    parser.add_argument("--image-interval", type=float, help="Seconds between PNGs per camera; 0 exports every saved frame")
    args = parser.parse_args()
    print(json.dumps(generate_report(args.capture, args.image_interval), indent=2))


if __name__ == "__main__":
    main()
