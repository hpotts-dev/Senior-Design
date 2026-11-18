"""Streaming capture with one copy of each measurement and native PNG."""
import csv
from queue import Empty
from time import perf_counter_ns
from .recording import AsyncRecorder
from .image_io import write_png


class CompactRecorder(AsyncRecorder):
    compact = True

    def submit_event(self, event):
        if isinstance(event, tuple):
            super().submit_event(event)

    def _frame_loop(self):
        try:
            while not self._stop.is_set() or not self.frames.empty():
                try:
                    item, kind, queued = self.frames.get(timeout=.05)
                except Empty:
                    continue
                try:
                    frame = item.frame
                    folder = self.session/"images"/frame.role
                    folder.mkdir(parents=True, exist_ok=True)
                    elapsed = self.clock.time(frame.received_ns)[1]
                    path = folder/f"{elapsed:.6f}s_{frame.sequence}_{kind}_{queued}.png"
                    write_png(path, frame.image)
                    self._count("frames_saved")
                    self._count("png_frames_saved")
                    with self._lock:
                        self._save_latencies.append((perf_counter_ns()-frame.received_ns)/1e6)
                except Exception as exc:
                    self._error(exc)
                finally:
                    self.frames.task_done()
        except Exception as exc:
            self._error(exc)

    def _telemetry_loop(self):
        try:
            with (self.session/"centroids.csv").open("w", newline="", encoding="utf-8") as centroids, \
                    self.spectrum_output(self.session) as process:
                rows = csv.writer(centroids)
                rows.writerow(["time_s", "camera", "frame", "centroid_x_px", "centroid_y_px",
                               "delta_x_px", "delta_y_px", "status"])
                centroids.flush()
                while not self._stop.is_set() or not self.telemetry.empty():
                    try:
                        sample = self.telemetry.get(timeout=.05)
                    except Empty:
                        continue
                    try:
                        m = process(sample)
                        xy = (m.centroid_x, m.centroid_y, m.error_x_px, m.error_y_px)
                        rows.writerow([self.clock.time(m.received_ns)[1], m.role, m.sequence,
                                       *(tuple(round(v, 7) for v in xy) if m.centroid_valid else ("",)*4),
                                       "ok" if m.centroid_valid else m.reason])
                        centroids.flush()
                    finally:
                        self.telemetry.task_done()
        except Exception as exc:
            self._error(exc)
