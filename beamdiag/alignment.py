"""Two-plane pointing, dark-field monitoring and displacement spectra."""
from collections import deque
from dataclasses import replace
from math import atan2
from threading import Lock
import numpy as np


def plane_matrix(camera, simulated):
    if camera.plane_mm_per_pixel:
        return camera.plane_mm_per_pixel
    if simulated:
        return ((0.00345, 0.0), (0.0, 0.00345))
    return None


def plane_distance(cfg, simulated):
    return cfg.pid.plane_separation_mm or (200.0 if simulated else 0.0)


def position(matrix, x, y):
    (a, b), (c, d) = matrix
    return a*x+b*y, c*x+d*y


class ApproachAngle:
    """Match each frame once using comparable host retrieval times."""
    def __init__(self, cfg, simulated):
        self.matrices = {c.role: plane_matrix(c, simulated) for c in cfg.cameras}
        self.distance = plane_distance(cfg, simulated)
        self.pending = {}
        self.lock = Lock()
        self.revisions = None
        self.revision = 0
        self.reason = ("Waiting for Near and Far Field" if self.distance and self.matrices["near_field"]
                       and self.matrices["far_field"] else "Two-plane calibration is not confirmed")

    def publish(self, measurement, max_age_ms, send):
        with self.lock:
            self.pending[measurement.role] = measurement
            if len(self.pending) != 2:
                return
            near, far = self.pending["near_field"], self.pending["far_field"]
            oldest = min((near, far), key=lambda m: m.received_ns)
            if abs(near.received_ns-far.received_ns) > max_age_ms*1e6:
                del self.pending[oldest.role]
                self.reason = "Waiting for time-matched Near and Far Field"
                return
            self.pending.clear()
            revisions = near.reference_revision, far.reference_revision
            if self.revisions != revisions:
                self.revisions = revisions
                self.revision += 1
            valid = False
            x = y = 0.0
            self.reason = "Paired NF / FF approach angle"
            for m in (near, far):
                if not m.centroid_valid:
                    self.reason = f"{m.role.replace('_', ' ').title()}: {m.reason}"
                    break
                if not self.matrices[m.role] or not self.distance:
                    self.reason = "Set both plane transforms and their separation in Configuration"
                    break
            else:
                nx, ny = position(self.matrices[near.role], near.error_x_px, near.error_y_px)
                fx, fy = position(self.matrices[far.role], far.error_x_px, far.error_y_px)
                x, y = 1e6*atan2(fx-nx, self.distance), 1e6*atan2(fy-ny, self.distance)
                valid = True
            send(replace(far, received_ns=oldest.received_ns, computed_ns=max(near.computed_ns, far.computed_ns),
                         reference_revision=self.revision, angular_valid=valid, error_x_urad=x, error_y_urad=y))


class DarkFieldMonitor:
    def __init__(self):
        self.since = self.last = None
        self.bad = False
        self.episodes = 0
        self.snapshot = (None, "Waiting", 0)
        self.settings = None

    def update(self, stamp, mean, cfg):
        settings = cfg.df_mean_limit_dn, cfg.df_hold_s
        if (self.last is None or stamp-self.last > 2.5e9/cfg.fps
                or self.settings != settings or mean <= cfg.df_mean_limit_dn):
            self.since = None
            self.bad = False
        self.last, self.settings = stamp, settings
        if mean > cfg.df_mean_limit_dn:
            if self.since is None:
                self.since = stamp
            if not self.bad and stamp-self.since >= cfg.df_hold_s*1e9:
                self.bad = True
                self.episodes += 1
        self.snapshot = (stamp, "BAD" if self.bad else "OK", self.episodes)


class DisplacementSeries:
    """Single writer. Average Hann-windowed, one-sided FFTs on a uniform time grid."""
    def __init__(self):
        self.lock = Lock()
        self.history = deque(maxlen=4096)
        self.spectrum = None
        self.block = []
        self.total = None
        self.count = 0
        self.start = self.previous = self.epoch = None

    def snapshot(self):
        with self.lock:
            return tuple(self.history), self.spectrum

    def finish(self, end, emit):
        if not self.count and len(self.block) >= 4:
            self._transform(self.epoch[1])
        if self.count:
            emit(self.start, end, self.count, self.freq, self.total/self.count)
        self.start, self.total, self.count = end, None, 0
        self.block.clear()

    def add(self, m, fps, interval, emit):
        t = self.last_time = m.received_ns/1e9
        epoch = (m.reference_revision, fps, interval)
        with self.lock:
            if self.epoch != epoch:
                self.finish(t, emit)
                self.history.clear()
                self.spectrum = None
                self.previous = None
                self.epoch = epoch
            if t-self.start >= interval:
                boundary = self.start+interval
                self.finish(boundary, emit)
                self.start += int((t-self.start)//interval)*interval
                self.previous = None
            last = self.previous
            gap = last is not None and (m.sequence != last[0]+1 or t-last[1] > 2.5/fps)
            if gap or not m.centroid_valid:
                self.block.clear()
                self.spectrum = None
                self.history.append((t, np.nan, np.nan))
                self.previous = None
                if not m.centroid_valid:
                    return
                last = None
            xy = np.array([m.error_x_px, m.error_y_px])
            self.history.append((t, *xy))
            while self.history and self.history[0][0] < t-3:
                self.history.popleft()
            self.previous = (m.sequence, t, xy)
            if last is None:
                self.next_time = t
            size = max(4, min(256, int(fps*min(3, interval))))
            while self.next_time <= t:
                sample = xy if last is None or t <= last[1] else last[2]+(xy-last[2])*((self.next_time-last[1])/(t-last[1]))
                self.block.append(sample)
                self.next_time += 1/fps
                if len(self.block) == size:
                    self._transform(fps)

    def _transform(self, fps):
        size = len(self.block)
        values = np.array(self.block)
        self.block.clear()
        window = np.hanning(size)
        coeff = np.fft.rfft((values-values.mean(axis=0))*window[:, None], axis=0)/window.sum()
        coeff[1:(-1 if size % 2 == 0 else None)] *= 2
        self.freq = np.fft.rfftfreq(size, 1/fps)
        magnitude = np.abs(coeff)
        data = np.column_stack((coeff.real, coeff.imag, magnitude))
        self.total = data if self.total is None else self.total+data
        self.count += 1
        self.spectrum = (self.freq, self.total[:, 4:]/self.count)
