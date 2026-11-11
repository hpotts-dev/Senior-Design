"""Bounded, nonblocking producer queues; append-only NPY streams avoid per-frame file creation."""
from __future__ import annotations

from dataclasses import dataclass, fields
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone, timedelta
from pathlib import Path
from queue import Queue, Empty, Full
from threading import Event, Thread, Lock
from time import perf_counter_ns
import csv
import json
import logging
import cv2
import numpy as np

from .config import AppConfig, ROLES, PIXEL_BITS
from .model import DiagnosticFrame, Measurement
from .timing import percentiles
from .camera_settings import SettingsEvent
from .capture_layout import RAW_FOLDER, READABLE_FOLDER
from .image_io import write_png
from .frame_io import write_npy_record
from .live_output import CaptureClock, CentroidTable, IMAGE_HEADER, ERROR_HEADER, initialize_readable

log = logging.getLogger(__name__)


def create_session(cfg: AppConfig, root: Path, simulated: bool) -> Path:
    """Create a date/time-only capture folder and return its raw-data directory."""
    base = Path(cfg.recording.output_dir)
    if not base.is_absolute():
        base = root/base
    started_ns = perf_counter_ns()
    started_utc = datetime.now(timezone.utc)
    local = started_utc.astimezone()  # Use the local timezone configured on the capture computer.
    stamp = local
    while True:
        capture = base/stamp.strftime("%Y-%m-%d_%H-%M-%S.%f")
        try:
            capture.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            stamp += timedelta(microseconds=1)
    session = capture/RAW_FOLDER
    session.mkdir()
    readable = capture/READABLE_FOLDER
    readable.mkdir()
    for role in ROLES:
        (session/"frames"/role).mkdir(parents=True)
    (session/"session.json").write_text(json.dumps({"schema": 3, "live_outputs": True, "simulated": simulated,
        "started_utc": started_utc.isoformat(), "started_local": local.isoformat(),
        "local_timezone": local.tzname(), "started_host_ns": started_ns, "host_clock": "perf_counter_ns",
        "layout": {"raw_data": RAW_FOLDER, "human_readable": READABLE_FOLDER,
                   "frame_index_paths_relative_to": RAW_FOLDER},
        "configuration": cfg.to_dict(), "angular_calibration": "DEMO ONLY" if simulated else "per-camera confirmation",
        "latency_scope": "host retrieval onward; does not measure exposure, network, or actuator settling"}, indent=2), encoding="utf-8")
    initialize_readable(session, cfg)
    (session/"readable_report_status.json").write_text(json.dumps({"state": "live", "mode": "written live"}), encoding="utf-8")
    return session


@dataclass(frozen=True, slots=True)
class LogEvent:
    host_ns: int
    level: str
    component: str
    message: str
    traceback: str = ""


class CaptureLogHandler(logging.Handler):
    """Preserve warnings/errors without disk work in camera or UI threads."""
    def __init__(self, recorder):
        super().__init__(logging.WARNING)
        self.recorder = recorder

    def emit(self, record):
        traceback = logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
        self.recorder.submit_event(LogEvent(perf_counter_ns(), record.levelname, record.name, record.getMessage(), traceback))


class AsyncRecorder:
    def __init__(self, cfg: AppConfig, session: Path):
        self.cfg, self.session = cfg.recording, session
        self.camera_configs = {c.role: c for c in cfg.cameras}
        self.readable = session.parent/READABLE_FOLDER
        self.clock = CaptureClock(session)
        self._schedule_lock = Lock()
        self._next_snapshot = {}
        self.frames: Queue = Queue(self.cfg.frame_queue_size)
        self.telemetry: Queue = Queue(self.cfg.telemetry_queue_size)
        self.recording = Event()
        if self.cfg.start_recording:
            self.recording.set()
        self._stop = Event()
        self._lock = Lock()
        self._counts = {"frames_saved": 0, "frames_dropped": 0, "telemetry_dropped": 0,
                        "png_frames_saved": 0,
                        "write_errors": 0, "last_error": "", "last_write_ms": 0.0,
                        "save_deadline_misses": 0}
        self._save_latencies = deque(maxlen=4096)
        self._target_ms = cfg.latency_target_ms
        self._threads = [Thread(target=self._frame_loop, name="frame-writer", daemon=True),
                         Thread(target=self._telemetry_loop, name="telemetry-writer", daemon=True)]

    def start(self):
        for thread in self._threads:
            thread.start()

    def stats(self, include_timing: bool = True) -> dict:
        with self._lock:
            result = {**self._counts, "frame_queue": self.frames.qsize(), "telemetry_queue": self.telemetry.qsize(),
                    "recording": self.recording.is_set()}
            if include_timing:
                latencies = tuple(self._save_latencies)
        if include_timing:
            result["host_to_saved"] = percentiles(latencies)
        return result

    def _count(self, key: str, amount: int = 1):
        with self._lock:
            self._counts[key] += amount

    def _error(self, exc: Exception):
        with self._lock:
            self._counts["write_errors"] += 1
            self._counts["last_error"] = str(exc)
        self.recording.clear()
        log.exception("Recording error; continuous frame recording disabled")

    def submit(self, item: DiagnosticFrame):
        if item.measurement is not None and item.frame.role in ("far_field", "near_field"):
            self.submit_event(item.measurement)
        # Scheduling is O(1) and never waits for disk or encoding. First frame is a snapshot.
        with self._schedule_lock:
            due = item.frame.received_ns >= self._next_snapshot.get(item.frame.role, 0)
            if due:
                self._next_snapshot[item.frame.role] = item.frame.received_ns+int(self.cfg.snapshot_interval_s*1e9)
        if due and not self.capture(item, "timed"):
            with self._schedule_lock:
                self._next_snapshot[item.frame.role] = 0  # Retry next frame if the queue was full.
        if self.recording.is_set() and item.frame.sequence % self.cfg.every_n_frames == 0:
            self.capture(item, "record")

    def set_snapshot_interval(self, seconds):
        if not isinstance(seconds, (int, float)) or not np.isfinite(seconds) or seconds <= 0:
            raise ValueError("Snapshot interval must be a finite positive number of seconds")
        from dataclasses import replace
        with self._schedule_lock:
            self.cfg = replace(self.cfg, snapshot_interval_s=float(seconds))
            self._next_snapshot.clear()

    def restart_snapshot_timer(self):
        with self._schedule_lock:
            self._next_snapshot.clear()  # Activates a fresh timed capture on each camera's next frame.

    def submit_settings(self, event: SettingsEvent):
        self.submit_event(event)

    def submit_event(self, event):
        try:
            self.telemetry.put_nowait(event)
        except Full:
            self._count("telemetry_dropped")

    def capture(self, item: DiagnosticFrame, kind: str = "snapshot") -> bool:
        if self._stop.is_set():
            return False
        try:
            self.frames.put_nowait((item, kind, perf_counter_ns()))
            return True
        except Full:
            self._count("frames_dropped")
            return False

    def _frame_loop(self):
        streams = {}
        stream_numbers = {role: 0 for role in ROLES}
        try:
            with (self.session/"frame_index.csv").open("w", newline="", encoding="utf-8") as f, \
                    (self.readable/"images_index.csv").open("w", newline="", encoding="utf-8-sig") as images_file:
                writer = csv.writer(f)
                images_writer = csv.writer(images_file)
                images_writer.writerow(IMAGE_HEADER)
                images_file.flush()
                writer.writerow(["role", "sequence", "received_ns", "camera_timestamp_ticks", "kind", "path",
                                 "width", "height", "dtype", "sensor_offset_x", "sensor_offset_y",
                                 "sdk_skipped", "queue_wait_ms", "write_ms", "byte_offset", "record_bytes",
                                 "host_to_saved_ms", "background_reference"])
                f.flush()
                while not self._stop.is_set() or not self.frames.empty():
                    try:
                        item, kind, queued = self.frames.get(timeout=0.05)
                    except Empty:
                        continue
                    try:
                        frame = item.frame
                        started = perf_counter_ns()
                        offset, record_bytes = 0, 0
                        if self.cfg.format == "npy_stream" and kind == "record":
                            stream = streams.get(frame.role)
                            if stream is not None and stream[1].tell() >= self.cfg.stream_rollover_MB*1000000:
                                stream[1].close()
                                del streams[frame.role]
                                stream_numbers[frame.role] += 1
                                stream = None
                            if stream is None:
                                relative = Path("frames")/frame.role/f"stream_{stream_numbers[frame.role]:05d}.npyseq"
                                # Unbuffered FileIO: write completion measures OS-cache acceptance, not a RAM queue.
                                stream = (relative, (self.session/relative).open("wb", buffering=0))
                                streams[frame.role] = stream
                            relative, handle = stream
                            offset = handle.tell()
                            try:
                                write_npy_record(handle, frame.image)
                            except Exception:
                                # A partial record is not indexed and must not be appended to again.
                                handle.close()
                                del streams[frame.role]
                                stream_numbers[frame.role] += 1
                                raise
                            record_bytes = handle.tell()-offset
                        else:
                            fmt = "npy" if self.cfg.format == "npy_stream" else self.cfg.format
                            relative = Path("frames")/frame.role/f"{frame.sequence:010d}_{frame.received_ns}_{kind}_{queued}.{fmt}"
                            target = self.session/relative
                            if fmt == "npy":
                                with target.open("wb", buffering=0) as handle:
                                    write_npy_record(handle, frame.image)
                            elif not write_png(target, frame.image):
                                raise OSError(f"Image encoder failed: {target}")
                            record_bytes = target.stat().st_size
                        completed = perf_counter_ns()
                        elapsed = (completed-started)/1e6
                        writer.writerow([frame.role, frame.sequence, frame.received_ns, frame.camera_timestamp,
                                         kind, relative.as_posix(), frame.image.shape[1], frame.image.shape[0],
                                         str(frame.image.dtype), frame.sensor_offset_x, frame.sensor_offset_y,
                                         frame.skipped, (started-queued)/1e6, elapsed, offset, record_bytes,
                                         (completed-frame.received_ns)/1e6, item.background_file])
                        f.flush()  # Even a busy queue must leave a recoverable index during acquisition.
                        with self._lock:
                            self._counts["frames_saved"] += 1
                            self._counts["last_write_ms"] = elapsed
                            age_ms = (completed-frame.received_ns)/1e6
                            self._save_latencies.append(age_ms)
                            self._counts["save_deadline_misses"] += age_ms > self._target_ms
                        if kind != "record":
                            self._write_readable_image(item, kind, queued, relative, offset, images_writer)
                            images_file.flush()
                            self._count("png_frames_saved")
                    except Exception as exc:
                        self._error(exc)
                    finally:
                        self.frames.task_done()
        except Exception as exc:
            self._error(exc)
        finally:
            for _, handle in streams.values():
                handle.close()

    def _write_readable_image(self, item, kind, queued, raw_relative, offset, writer):
        frame = item.frame
        image = item.corrected_image if item.corrected_image is not None else frame.image
        folder = self.readable/"images"/frame.role
        folder.mkdir(parents=True, exist_ok=True)
        target = folder/f"frame_{frame.sequence:010d}_{kind}_{queued}.png"
        if not write_png(target, image):
            raise OSError(f"Cannot encode {target}")
        preview = target
        scale = "Native 8-bit values"
        if image.dtype == np.uint16:
            bits = PIXEL_BITS[self.camera_configs[frame.role].pixel_format]
            preview = target.with_stem(target.stem+"_display")
            if not write_png(preview, cv2.convertScaleAbs(image, alpha=255/((1 << bits)-1))):
                raise OSError(f"Cannot encode {preview}")
            scale = f"Display only: 0..{(1 << bits)-1} to 0..255; native PNG retains original precision"
        local, elapsed = self.clock.time(frame.received_ns)
        writer.writerow([local, elapsed, frame.role.replace("_", " ").title(), frame.sequence, kind,
            target.relative_to(self.readable).as_posix(), preview.relative_to(self.readable).as_posix(),
            str(image.dtype), scale, "../raw_data/"+raw_relative.as_posix(), offset, item.background_file])

    def _telemetry_loop(self):
        names = [field.name for field in fields(Measurement)]
        try:
            with ExitStack() as stack:
                centroid_file = stack.enter_context((self.readable/"centroids.csv").open("w", newline="", encoding="utf-8-sig"))
                table = CentroidTable(centroid_file, self.clock)
                errors_file = stack.enter_context((self.readable/"errors_and_warnings.csv").open("w", newline="", encoding="utf-8-sig"))
                errors = csv.writer(errors_file)
                errors.writerow(ERROR_HEADER)
                errors_file.flush()
                history_file = stack.enter_context((self.readable/"settings_history.csv").open("w", newline="", encoding="utf-8-sig"))
                history = csv.writer(history_file)
                history.writerow(["Capture time", "Camera", "Outcome", "Requested change", "Applied values", "Message"])
                history_file.flush()
                reasons = {}
                self._write_telemetry(names, table, errors, errors_file, history, history_file, reasons)
                table.expire(perf_counter_ns(), final=True)
        except Exception as exc:
            self._error(exc)

    def _write_telemetry(self, names, table, errors, errors_file, history, history_file, reasons):
        with (self.session/"beam_data.csv").open("w", newline="", encoding="utf-8") as f, \
                (self.session/"settings_changes.jsonl").open("w", encoding="utf-8") as settings_file, \
                (self.session/"events.jsonl").open("w", encoding="utf-8") as events_file, \
                (self.session/"application.log").open("w", encoding="utf-8") as text_log:
            writer = csv.writer(f)
            writer.writerow(names+["host_processing_ms"])
            f.flush()
            while not self._stop.is_set() or not self.telemetry.empty():
                try:
                    m = self.telemetry.get(timeout=0.05)
                except Empty:
                    table.expire(perf_counter_ns())
                    continue
                try:
                    if isinstance(m, LogEvent):
                        events_file.write(json.dumps({name: getattr(m, name) for name in m.__dataclass_fields__})+"\n")
                        events_file.flush()
                        text_log.write(f"{m.host_ns} {m.level} {m.component}: {m.message}\n")
                        if m.traceback:
                            text_log.write(m.traceback+"\n")
                        text_log.flush()
                        local, elapsed = self.clock.time(m.host_ns)
                        errors.writerow([local, elapsed, elapsed, m.level.title(), m.component, 1, m.message])
                        errors_file.flush()
                        continue
                    if isinstance(m, SettingsEvent):
                        settings_file.write(json.dumps(m.to_dict())+"\n")
                        settings_file.flush()
                        history.writerow([self.clock.time(m.host_ns)[0], m.role, "Applied" if m.success else "Failed",
                            json.dumps(m.requested), json.dumps(m.actual), m.message])
                        history_file.flush()
                        if not m.success:
                            local, elapsed = self.clock.time(m.host_ns)
                            errors.writerow([local, elapsed, elapsed, "Error", m.role+" settings", 1, m.message])
                            errors_file.flush()
                        continue
                    writer.writerow([getattr(m, name) for name in names]+[m.host_processing_ms])
                    f.flush()
                    table.add(m)
                    if m.reason != reasons.get(m.role):
                        reasons[m.role] = m.reason
                        if m.reason != "ok":
                            local, elapsed = self.clock.time(m.received_ns)
                            errors.writerow([local, elapsed, elapsed, "Notice", m.role, 1, m.reason])
                            errors_file.flush()
                finally:
                    self.telemetry.task_done()

    def close(self):
        self.recording.clear()
        self._stop.set()
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(timeout=10)
        stats = self.stats()
        stats["drain_complete"] = all(not t.is_alive() for t in self._threads) and self.frames.empty() and self.telemetry.empty()
        (self.session/"recording_summary.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return stats
