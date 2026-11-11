"""Small, streaming readable outputs. All disk work runs on storage threads."""
from collections import deque
import csv
from datetime import datetime, timedelta
from time import perf_counter_ns
from html import escape
import json
import math

CENTROID_HEADER = ["Far Field Capture Time", "FF_Cent_x", "FF_Cent_y", "FF_Cent_err_x", "FF_Cent_err_y",
                   "Near Field Capture Time", "NF_Cent_x", "NF_Cent_y", "NF_Cent_err_x", "NF_Cent_err_y"]
IMAGE_HEADER = ["Local time (with UTC offset)", "Elapsed time (s)", "Camera", "Frame", "Capture type",
                "PNG path", "Display preview path", "Native dtype", "Display scale", "Raw frame path", "Raw byte offset",
                "Background reference"]
ERROR_HEADER = ["Local start time (with UTC offset)", "Start elapsed time (s)", "End elapsed time (s)",
                "Severity", "Source", "Affected samples/events", "Explanation"]


class CaptureClock:
    def __init__(self, raw):
        if (raw/"session.json").exists():
            metadata = json.loads((raw/"session.json").read_text(encoding="utf-8"))
            self.origin = metadata["started_host_ns"]
            self.start = datetime.fromisoformat(metadata["started_local"])
        else:
            self.origin, self.start = perf_counter_ns(), datetime.now().astimezone()

    def time(self, ns):
        seconds = (ns-self.origin)/1e9
        return (self.start+timedelta(seconds=seconds)).isoformat(timespec="milliseconds"), round(seconds, 6)


def finite(value):
    return value if math.isfinite(value) else ""


class CentroidTable:
    """Pair asynchronous arrivals once; preserve each camera's actual host timestamp.

    Large gaps and missing cameras produce blank cells, never repeated/stale coordinates.
    Pending memory is bounded, even if a camera stops or rates differ.
    """
    def __init__(self, stream, clock, max_gap_s=.5):
        self.stream, self.clock = stream, clock
        self.writer = csv.writer(stream)
        self.writer.writerow(CENTROID_HEADER)
        stream.flush()
        self.pending = {r: deque() for r in ("far_field", "near_field")}
        self.max_gap = max_gap_s*1e9

    def values(self, m):
        if m is None:
            return [""]*5
        values = [finite(v) for v in (m.centroid_x, m.centroid_y, m.error_x_px, m.error_y_px)] if m.centroid_valid else [""]*4
        return [self.clock.time(m.received_ns)[0], *values]

    def row(self, ff=None, nf=None):
        self.writer.writerow(self.values(ff)+self.values(nf))
        self.stream.flush()

    def add(self, m):
        if m.role not in self.pending:
            return
        self.pending[m.role].append(m)
        ff, nf = self.pending.values()
        while ff and nf:
            if abs(ff[0].received_ns-nf[0].received_ns) <= self.max_gap:
                self.row(ff.popleft(), nf.popleft())
            elif ff[0].received_ns < nf[0].received_ns:
                self.row(ff=ff.popleft())
            else:
                self.row(nf=nf.popleft())
        self.expire(m.received_ns)

    def expire(self, now, final=False):
        for role, queue in self.pending.items():
            while queue and (final or len(queue) > 64 or now-queue[0].received_ns > self.max_gap):
                m = queue.popleft()
                self.row(ff=m if role == "far_field" else None, nf=m if role == "near_field" else None)


def write_pid_equation(output, values):
    text = """FAR FIELD NOMINAL PID EQUATION

Only Far Field drives the PID. Commands are normalized; no piezo output is connected.
displacement_px = measured_centroid_px - desired_centroid_px
displacement_urad = calibration_2x2 * displacement_px
e = -displacement_urad; e = 0 inside the deadband
dt = actual interval between fresh host frame retrievals (seconds)
alpha = dt / (derivative_tau_s + dt)
D_filtered = previous_D + alpha * ((e - previous_e)/dt - previous_D)
I_candidate = clamp(previous_I + Ki*e*dt, +/- integral_limit)
u_candidate = Kp*e + I_candidate + Kd*D_filtered
Conditional integration rejects I_candidate when it increases output saturation.
low = max(-output_limit, previous_output - slew_per_s*dt)
high = min(output_limit, previous_output + slew_per_s*dt)
u = clamp(Kp*e + I + Kd*D_filtered, low, high)
Invalid/unconfirmed/stale feedback, watchdog, pause and stop produce zero output.
Reference/calibration changes reset controller history. See pid_settings.csv for changes.

Current applied values:
"""+json.dumps(values, indent=2)+"\n"
    target = output/"PID_equation.txt"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)


def initialize_readable(raw, cfg):
    output = raw.parent/"human_readable"
    text = """Capture is running. CSV tables, raw frames and timed PNGs are written live.
centroids.csv: FF/NF pixel coordinates and measured-minus-desired pixel errors.
Each side has its own host capture time. Cameras are asynchronous, not hardware synchronized.
Rows pair arrivals within 0.5 seconds; missing/invalid measurements have blank cells.
Dark Field is displayed and saved but has no centroid calculation. Only FF drives PID.
Timed snapshots start at the first frame and continue independently of the Record button.
Native raw frames are unmodified. PNGs use saved per-pixel background subtraction when active.
12-bit values (0..4095) are stored losslessly in 16-bit containers, with separate display previews.
Files are flushed after each row/frame. A crash can lose pending queues or an unfinished write;
OS-cache flushing does not guarantee survival of a power failure.
"""
    (output/"README.txt").write_text(text, encoding="utf-8")
    links = "".join(f'<li><a href="{name}">{label}</a></li>' for name, label in (
        ("centroids.csv", "Far/Near Field centroids and pixel errors"), ("pid_feedback.csv", "Nominal PID commands"),
        ("PID_equation.txt", "PID equation"), ("pid_settings.csv", "PID settings"),
        ("settings_history.csv", "Camera/settings history"), ("errors_and_warnings.csv", "Errors and notices"),
        ("images_index.csv", "Saved PNG image index"), ("images/", "PNG images"), ("../raw_data/", "Native raw data")))
    (output/"START_HERE.html").write_text('<!doctype html><meta charset="utf-8"><title>Live beam capture</title>'
        '<style>body{font:17px system-ui;max-width:950px;margin:40px auto;padding:20px}li{margin:12px}pre{white-space:pre-wrap}</style>'
        '<h1>Live beam capture</h1><p>Files update during acquisition. Reopen a CSV to see new rows.</p><ul>'+links+
        '</ul><pre>'+escape(text)+'</pre>', encoding="utf-8")
    write_pid_equation(output, cfg.to_dict()["pid"])
