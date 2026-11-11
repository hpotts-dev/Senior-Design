"""One acquisition/analysis worker per camera; presentation and storage stay off the feedback path."""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter, perf_counter_ns
import json
import logging
import math
import os
import secrets
import struct
import subprocess
import sys
import shutil
import numpy as np

from .cameras import BaslerCamera, SimulatedCamera
from .centroid import CentroidProcessor
from .config import AppConfig, CameraConfig, PID_SOURCE_ROLE, validate, bandwidth_report
from .model import DiagnosticFrame, LatestValue, ReferenceChange
from .recording import AsyncRecorder, CaptureLogHandler, create_session
from .capture_layout import capture_paths
from .transport import ErrorPublisher, STATES
from .timing import percentiles
from .camera_settings import ACQUISITION_FIELDS, SettingsEvent, live_values, with_live_settings
from .feedback_settings import feedback_values, pid_values, with_feedback_settings, with_pid_settings
from .background import load_background, save_background, signature, subtract_background
from .windows_paths import save_config, resolve_recording_path

log = logging.getLogger(__name__)


class CameraWorker:
    def __init__(self, cfg: CameraConfig, engine: DiagnosticEngine):
        self.cfg, self.engine = cfg, engine
        self.latest: LatestValue[DiagnosticFrame] = LatestValue()
        self.processor = CentroidProcessor(cfg, simulated=engine.simulated)
        self.background_status = "No saved background"
        self._background = None
        self._background_archive = ""
        self._background_request = Event()
        self._dark_sum = None
        self._dark_count = 0
        self._dark_future = None
        if cfg.background_file:
            try:
                self._background = load_background(cfg, engine.config_path, simulated=engine.simulated)
                self._background_archive = engine.archive_background(cfg.background_file)
                self.background_status = "Saved background active"
            except (OSError, ValueError, KeyError) as exc:
                self.background_status = f"Background inactive: {exc}"
                log.warning("%s: %s", cfg.role, self.background_status)
        self.thread = Thread(target=self.run, name=f"camera-{cfg.role}", daemon=True)
        self._lock = Lock()
        self._state = "starting"
        self._error = ""
        self._frames = self._errors = self._timeouts = self._skipped = 0
        self._arrivals = deque(maxlen=60)
        self._latencies = deque(maxlen=4096)
        self._centroid_times = deque(maxlen=4096)
        self._camera_latencies = deque(maxlen=4096)
        self._deadline_misses = 0
        self.info = {}
        self._pending_lock = Lock()
        self._settings_pending = Event()
        self._pending_settings = None
        self._applying_settings = False
        self._request_id = 0
        self._last_update = {"completed_request_id": 0, "last_update_status": "", "last_update_message": ""}
        self.controls: LatestValue[dict] = LatestValue()
        self.controls.publish({"ready": False, "request_id": 0, "status": "waiting",
                               "message": "Waiting for camera", "values": live_values(cfg), "capabilities": {},
                               "feedback_values": feedback_values(cfg)})

    def queue_settings(self, patch: dict, category="camera") -> int:
        with self._pending_lock:
            if self._pending_settings is not None or self._applying_settings:
                raise ValueError("A settings update is already in progress")
            self._request_id += 1
            request_id = self._request_id
            self._pending_settings = (request_id, dict(patch), category)
            previous = self.controls.read()
            self.controls.publish({**previous, "request_id": request_id, "status": "queued",
                                   "message": "Queued for the camera worker"})
            self._settings_pending.set()
            return request_id

    def _publish_controls(self, source, status="ready", message="Live camera settings", request_id=0):
        state = source.control_state()
        payload = self.info.get("payload_bytes") or self.cfg.width*self.cfg.height*(1 if self.cfg.pixel_format == "Mono8" else 2)
        throughput = self.info.get("throughput_limit_bps") or self.cfg.throughput_limit_bps
        self.controls.publish({**state, **self._last_update, "values": {**state["values"], **{k: v for k, v in live_values(self.cfg).items() if k not in ACQUISITION_FIELDS}},
            "feedback_values": feedback_values(self.cfg),
            "ready": True, "request_id": request_id, "status": status, "message": message,
            "delivery_budget_max_fps": 0.9*throughput/payload})

    def _apply_pending_settings(self, source):
        if not self._settings_pending.is_set():
            return
        with self._pending_lock:
            pending = self._pending_settings
            self._pending_settings = None
            self._settings_pending.clear()
            self._applying_settings = True
        if pending is None:
            with self._pending_lock:
                self._applying_settings = False
            return
        request_id, patch, category = pending
        success, message = False, ""
        previous = self.cfg
        try:
            cfg = with_feedback_settings(previous, patch) if category == "feedback" else with_live_settings(previous, patch)
            hardware_changed = any(getattr(cfg, key) != getattr(previous, key) for key in ACQUISITION_FIELDS)
            if hardware_changed:
                actual = source.apply_settings({key: getattr(cfg, key) for key in ACQUISITION_FIELDS})
                cfg = replace(cfg, **{key: getattr(actual, key) for key in ACQUISITION_FIELDS})
            self.cfg = source.cfg = cfg
            if signature(cfg) != signature(previous):
                self._background = None
                self._background_archive = ""
                self._dark_sum = None
                self._dark_count = 0
                self._background_request.clear()
                self.background_status = "Camera settings changed; capture a new background"
            if cfg != previous or category == "feedback":
                self.processor.update_settings(cfg, explicit_calibration=category == "feedback",
                                               update_reference="reference_px" in patch)
            actual = feedback_values(cfg) if category == "feedback" else live_values(cfg)
            clamped = [key for key in patch if actual[key] != patch[key]]
            message = "Applied"+(f"; camera adjusted {', '.join(clamped)} to supported values" if clamped else "")
            success = True
        except Exception as exc:
            message = f"Update failed: {exc}"
            try:
                state = source.control_state()
                self.cfg = source.cfg = replace(previous, **{key: state["values"][key] for key in ACQUISITION_FIELDS})
                if self.cfg != previous:
                    self.processor.update_settings(self.cfg)
            except Exception:
                pass  # The acquisition loop will reconnect if the device is no longer accessible.
            log.warning("%s: %s", self.cfg.role, message)
        finally:
            self._last_update = {"completed_request_id": request_id, "last_update_status": "applied" if success else "failed",
                                 "last_update_message": message}
            source.info["live_values"] = live_values(self.cfg)
            try:
                self._publish_controls(source, "applied" if success else "failed", message, request_id)
            except Exception as exc:
                self.controls.publish({**self.controls.read(), **self._last_update, "ready": False, "request_id": request_id,
                                       "status": "failed", "message": f"{message}; readback failed: {exc}"})
            if hasattr(self.engine, "_settings_finished"):
                self.engine._settings_finished(self.cfg.role, self.cfg)
            _, _, revision = self.processor.reference_state(self.cfg.width, self.cfg.height)
            self.engine.recorder.submit_settings(SettingsEvent(self.cfg.role, request_id, perf_counter_ns(),
                self._frames+1, success, patch,
                feedback_values(self.cfg) if category == "feedback" else live_values(self.cfg),
                message, revision, category))
            with self._pending_lock:
                self._applying_settings = False

    def stats(self, include_timing: bool = True) -> dict:
        item = self.latest.read()
        with self._lock:
            fps = (len(self._arrivals)-1)/((self._arrivals[-1]-self._arrivals[0])/1e9) if len(self._arrivals) > 1 else 0.0
            result = {"state": self._state, "last_error": self._error, "frames": self._frames, "errors": self._errors,
                    "timeouts": self._timeouts, "sdk_skipped": self._skipped, "fps": fps,
                    "age_ms": (perf_counter_ns()-item.frame.received_ns)/1e6 if item else None,
                    "host_deadline_misses": self._deadline_misses}
            if include_timing:
                latencies, centroids = tuple(self._latencies), tuple(self._centroid_times)
                camera_latencies = tuple(self._camera_latencies)
        if include_timing:
            result.update(host_processing=percentiles(latencies), centroid=percentiles(centroids),
                          camera_to_host=percentiles(camera_latencies))
        return result

    def request_background(self):
        if self._background_request.is_set() or self._dark_future is not None:
            raise ValueError("A background capture is already in progress")
        self._background_request.set()
        self.background_status = f"Keep camera covered: averaging {self.cfg.background_frames} frames"

    def _background_step(self, frame):
        if self._dark_future is not None and self._dark_future.done():
            future, self._dark_future = self._dark_future, None
            try:
                image, cfg, archive = future.result()
                if signature(cfg) != signature(self.cfg):
                    raise ValueError("Camera settings changed while saving; recapture the background")
                self._background, self._background_archive = image, archive
                self.cfg = replace(self.cfg, background_file=cfg.background_file, background=0)
                self.processor.update_settings(self.cfg)
                self.engine._settings_finished(self.cfg.role, self.cfg)
                self.background_status = "Background saved and active — uncover camera"
            except Exception as exc:
                self.background_status = f"Background failed: {exc}"
                log.warning("%s: %s", self.cfg.role, self.background_status)
        if not self._background_request.is_set():
            return
        if self._dark_sum is None:
            self._dark_sum = np.zeros(frame.image.shape, np.float32)
        np.add(self._dark_sum, frame.image, out=self._dark_sum)
        self._dark_count += 1
        if self._dark_count >= self.cfg.background_frames:
            average = np.rint(self._dark_sum/self._dark_count).astype(frame.image.dtype)
            average.flags.writeable = False
            self._dark_sum, self._dark_count = None, 0
            self._background_request.clear()
            self._dark_future = self.engine._background_pool.submit(self.engine.persist_background, self.cfg, average)
            self.background_status = "Saving averaged background; keep camera covered"

    def run(self):
        stop = self.engine.stop
        retry_delay = 0.2
        while not stop.is_set():
            source = SimulatedCamera(self.cfg) if self.engine.simulated else BaslerCamera(self.cfg)
            try:
                with self._lock:
                    self._state = "opening"
                source.open()
                self.info = source.info
                self._publish_controls(source)
                (self.engine.session/f"camera_{self.cfg.role}.json").write_text(json.dumps(source.info, indent=2), encoding="utf-8")
                with self._lock:
                    self._state, self._error = "running", ""
                last_arrival = perf_counter()
                while not stop.is_set():
                    self._apply_pending_settings(source)
                    frame = source.read(stop)
                    if frame is None:
                        if stop.is_set():
                            break
                        with self._lock:
                            self._timeouts += 1
                        delivery_timeout = max(1.0, 3/self.cfg.fps, 3*self.cfg.exposure_us/1e6)
                        if perf_counter()-last_arrival > delivery_timeout:
                            raise TimeoutError(f"No frames for {delivery_timeout:g} seconds; reopening camera")
                        continue
                    last_arrival = perf_counter()
                    # Sequence remains monotonic across camera reconnects.
                    frame = replace(frame, sequence=self._frames+1)
                    self._background_step(frame)
                    corrected = subtract_background(frame.image, self._background)
                    analysis = replace(frame, image=corrected) if corrected is not None else frame
                    # Dark Field has no centroid/moment calculation or telemetry rows.
                    measurement = self.processor.process(analysis, native_image=frame.image) if self.cfg.role != "dark_field" else None
                    # Publish feedback before UI handoff and before any recording queue operations.
                    if self.cfg.role == PID_SOURCE_ROLE:
                        self.engine.publisher.send(measurement)
                    item = DiagnosticFrame(frame, measurement, corrected, self._background_archive)
                    self.latest.publish(item)
                    self.engine.recorder.submit(item)
                    retry_delay = 0.2
                    with self._lock:
                        self._frames += 1
                        self._skipped += frame.skipped
                        self._arrivals.append(frame.received_ns)
                        if frame.camera_to_host_ms is not None:
                            self._camera_latencies.append(frame.camera_to_host_ms)
                        if measurement is not None:
                            self._latencies.append(measurement.host_processing_ms)
                            self._centroid_times.append(measurement.centroid_ms)
                            self._deadline_misses += measurement.host_processing_ms > self.engine.cfg.latency_target_ms
            except Exception as exc:
                with self._lock:
                    self._state, self._error = "retrying", str(exc)
                    self._errors += 1
                log.warning("%s: %s; retrying in %.1fs", self.cfg.role, exc, retry_delay)
                self.controls.publish({**self.controls.read(), "ready": False, "status": "waiting", "message": str(exc)})
                stop.wait(retry_delay)
                retry_delay = min(2.0, retry_delay*2)
            finally:
                try:
                    source.close()
                    if source.info:
                        (self.engine.session/f"camera_{self.cfg.role}.json").write_text(
                            json.dumps(source.info, indent=2), encoding="utf-8")
                except Exception:
                    log.exception("%s: camera close failed", self.cfg.role)
        with self._lock:
            self._state = "stopped"


class DiagnosticEngine:
    def __init__(self, cfg: AppConfig, config_path: Path, root: Path, simulated: bool,
                 launch_pid: bool = True, generate_reports: bool = True):
        self.cfg, self.config_path, self.root = cfg, config_path, root
        self.simulated, self.launch_pid = simulated, launch_pid
        self.session = create_session(cfg, root, simulated)
        self.capture_dir, _, self.readable_dir = capture_paths(self.session)
        self.generate_reports = generate_reports and cfg.recording.generate_readable
        self._started = False
        self._reporter = None
        self.stop = Event()
        self.token = secrets.randbits(63)
        self.publisher = ErrorPublisher(cfg.pid.host, cfg.pid.port, self.token, cfg.pid)
        self.recorder = AsyncRecorder(cfg, self.session)
        self._capture_log = CaptureLogHandler(self.recorder)
        logging.getLogger().addHandler(self._capture_log)
        self._background_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="background-save")
        self._config_save_lock = Lock()
        self._persisted_backgrounds = {}
        self.workers = {c.role: CameraWorker(c, self) for c in cfg.cameras}
        self._pid: subprocess.Popen | None = None
        self._pid_log = None
        self._closed = False
        self._display_ages = deque(maxlen=4096)
        self._camera_display_ages = deque(maxlen=4096)
        self._pid_latencies = deque(maxlen=4096)
        self._last_pid_sequence = -1
        self._last_pid_settings_id = 0
        self._pid_deadline_misses = 0
        self._display_deadline_misses = 0
        self._settings_lock = Lock()
        self._desired_configs = {c.role: c for c in cfg.cameras}

    def archive_background(self, configured_path):
        source = Path(configured_path)
        if not source.is_absolute():
            source = self.config_path.parent/source
        relative = Path("backgrounds")/source.name
        target = self.session/relative
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(source, target)
        return relative.as_posix()

    def current_config(self):
        saved = self._persisted_backgrounds.copy()
        cameras = []
        for worker in self.workers.values():
            camera = replace(worker.cfg, reference_px=worker.processor.reference_state(worker.cfg.width, worker.cfg.height)[:2])
            pending = saved.get(camera.role)
            if pending and pending[0] == signature(camera) and camera.background_file != pending[1]:
                camera = replace(camera, background_file=pending[1], background=0)
            cameras.append(camera)
        return replace(self.cfg, recording=replace(self.cfg.recording, start_recording=self.recorder.recording.is_set()),
            cameras=tuple(cameras))

    def persist_background(self, camera, average):
        name = f"{camera.role}_{secrets.token_hex(8)}.npz"
        relative = Path("backgrounds")/name
        save_background(self.config_path.parent/relative, average, camera, simulated=self.simulated)
        cfg = replace(camera, background_file=relative.as_posix(), background=0)
        archive = self.archive_background(cfg.background_file)
        with self._config_save_lock:
            current = self.current_config()
            active = self.workers[camera.role].cfg
            if signature(active) != signature(camera):
                raise ValueError("Camera settings changed during background capture")
            current = replace(current, cameras=tuple(replace(c, background_file=cfg.background_file, background=0)
                if c.role == camera.role else c for c in current.cameras))
            save_config(current, self.config_path)
            # Another camera's save may finish before this worker consumes its
            # future. Preserve every completed reference across those saves.
            self._persisted_backgrounds[camera.role] = (signature(camera), cfg.background_file)
        self.recorder.submit_settings(SettingsEvent(camera.role, 0, perf_counter_ns(), 0, True,
            {"background_file": cfg.background_file}, {"background_file": cfg.background_file},
            "Averaged dark reference saved; native raw frames remain unmodified", 0, "background"))
        return average, cfg, archive

    def set_snapshot_interval(self, seconds):
        self.recorder.set_snapshot_interval(seconds)
        self.cfg = replace(self.cfg, recording=replace(self.cfg.recording, snapshot_interval_s=float(seconds)))
        with self._config_save_lock:
            save_config(self.current_config(), self.config_path)

    def reconfigure(self, cfg):
        """Apply every setting without closing the UI, draining the old session first.

        Full config edits may change SDK geometry, ports, paths and queue capacities;
        an orderly session restart makes these changes coherent and recoverable.
        """
        validate(cfg)
        if not bandwidth_report(cfg)["within_budget"]:
            raise ValueError("Configured rates exceed the shared Ethernet payload budget")
        previous = self.current_config()
        config_path, root, simulated, launch_pid = self.config_path, self.root, self.simulated, self.launch_pid
        cfg = resolve_recording_path(cfg, config_path)
        result = self.close()
        if not result.get("workers_stopped") or not result.get("recording", {}).get("drain_complete"):
            raise RuntimeError("Previous session could not drain; close the app before changing configuration")
        try:
            save_config(cfg, config_path)
            self.__init__(cfg, config_path, root, simulated, launch_pid)
            self.start()
        except Exception:
            self.close()
            save_config(previous, config_path)
            self.__init__(previous, config_path, root, simulated, launch_pid)
            self.start()
            raise

    def start(self):
        self.recorder.start()
        self._started = True
        if self.launch_pid:
            self._pid_log = (self.session/"pid_process.log").open("w", encoding="utf-8")
            self._pid = subprocess.Popen([sys.executable, str(self.root/"pid_controller.py"),
                "--config", str(self.config_path), "--session", str(self.session), "--token", str(self.token),
                "--parent-pid", str(os.getpid())], cwd=self.root, stdout=self._pid_log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = perf_counter()+10
            while not (self.session/"pid_ready.json").exists():
                if self._pid.poll() is not None:
                    raise RuntimeError(f"PID process failed. See {self.session/'pid_process.log'} (port may be in use)")
                if perf_counter() > deadline:
                    raise TimeoutError("PID process did not become ready in 10 seconds")
                self.stop.wait(0.01)
        for worker in self.workers.values():
            worker.thread.start()
        log.info("Capture: %s", self.capture_dir)

    def poll_control(self) -> dict:
        reply = self.publisher.poll()
        settings = self.publisher.settings_state
        if settings["status"] == "applied" and settings["request_id"] != self._last_pid_settings_id:
            pid = with_pid_settings(self.cfg.pid, settings["values"])
            if pid != self.cfg.pid:
                self.cfg = replace(self.cfg, pid=pid)
            self._last_pid_settings_id = settings["request_id"]
        reason = self.feedback_reason()
        if self.publisher.feedback_blocked:
            return {"state": "updating" if settings["status"] == "queued" else "settings failed",
                    "x": 0.0, "y": 0.0, "host_to_pid_ms": None, "reason": settings["message"]}
        if reply is None:
            return {"state": "waiting", "x": 0.0, "y": 0.0, "host_to_pid_ms": None, "reason": reason}
        age = (perf_counter_ns()-reply.finished_ns)/1e6
        state = STATES[reply.state]
        if age > self.cfg.pid.watchdog_ms:
            state = "offline"
        if reply.sequence > self._last_pid_sequence and reply.state == 1:
            elapsed = (reply.finished_ns-reply.received_ns)/1e6
            self._pid_latencies.append(elapsed)
            self._pid_deadline_misses += elapsed > self.cfg.latency_target_ms
            self._last_pid_sequence = reply.sequence
        return {"state": state, "x": reply.x if state == "active" else 0.0,
                "y": reply.y if state == "active" else 0.0,
                "host_to_pid_ms": (reply.finished_ns-reply.received_ns)/1e6,
                "pid_compute_ms": reply.compute_ms, "reply_age_ms": age,
                "reason": "Nominal PID is paused" if state == "paused" else reason}

    def feedback_reason(self):
        worker = self.workers[PID_SOURCE_ROLE]
        item = worker.latest.read()
        if item is None:
            return "Waiting for a Far Field frame"
        reasons = {"uncalibrated": "Far Field calibration is not confirmed",
                   "saturation": "Far Field clipping exceeds the allowed percentage; reduce exposure or gain",
                   "low_flux": "Far Field signal is below the detection threshold"}
        if item.measurement.reason in reasons:
            return reasons[item.measurement.reason]
        if (perf_counter_ns()-item.frame.received_ns)/1e6 > self.cfg.pid.watchdog_ms:
            return "Waiting for fresh Far Field feedback; the watchdog interval has elapsed"
        return "Far Field angular feedback is valid" if item.measurement.reason == "ok" else item.measurement.reason

    def queue_feedback_settings(self, patch: dict) -> int:
        worker = self.workers[PID_SOURCE_ROLE]
        with self._settings_lock:
            camera = with_feedback_settings(self._desired_configs[PID_SOURCE_ROLE], patch)
            if not worker.controls.read()["ready"]:
                raise ValueError("Wait for the Far Field camera to connect")
            request_id = worker.queue_settings(patch, category="feedback")
            self._desired_configs[PID_SOURCE_ROLE] = camera
            return request_id

    def queue_pid_settings(self, patch: dict, *, reset=False) -> int:
        pid = with_pid_settings(self.cfg.pid, patch)
        return self.publisher.configure_pid(pid_values(pid), reset=reset)

    def feedback_snapshot(self):
        worker = self.workers[PID_SOURCE_ROLE]
        # An immutable worker config plus its current reference is suitable for
        # profile exports and UI readback; SDK access remains on its owner worker.
        camera = worker.cfg
        x, y, _ = worker.processor.reference_state(camera.width, camera.height)
        return replace(camera, reference_px=(x, y)), self.cfg.pid

    def note_display(self, received_ns: int, camera_to_host_ms: float | None = None):
        elapsed = (perf_counter_ns()-received_ns)/1e6
        self._display_ages.append(elapsed)
        if camera_to_host_ms is not None:
            self._camera_display_ages.append(elapsed+camera_to_host_ms)
        self._display_deadline_misses += elapsed > self.cfg.latency_target_ms

    def snapshot(self) -> int:
        return sum(self.recorder.capture(item) for worker in self.workers.values()
                   if (item := worker.latest.read()) is not None)

    def set_reference(self, role: str, x: float, y: float) -> ReferenceChange:
        """Set a manual target even when beam detection is invalid; never changes calibration."""
        worker = self.workers[role]
        item = worker.latest.read()
        if item is None:
            return ReferenceChange(role, False, "Waiting for the first frame")
        height, width = item.frame.image.shape
        if not all(math.isfinite(v) for v in (x, y)) or not (0 <= x <= width-1 and 0 <= y <= height-1):
            return ReferenceChange(role, False, f"Target must be inside 0–{width-1} X and 0–{height-1} Y")
        worker.processor.set_reference(x, y)
        return ReferenceChange(role, True, "Reference set", x, y)

    def queue_camera_settings(self, role: str, patch: dict) -> int:
        """Validate and reserve shared bandwidth on the UI thread; the worker alone calls the SDK."""
        worker = self.workers[role]
        with self._settings_lock:
            camera = with_live_settings(self._desired_configs[role], patch)
            proposed = {**self._desired_configs, role: camera}
            cfg = replace(self.cfg, cameras=tuple(proposed[c.role] for c in self.cfg.cameras))
            validate(cfg)
            if not bandwidth_report(cfg)["within_budget"]:
                raise ValueError("Requested rates exceed the shared Ethernet payload budget")
            controls = worker.controls.read()
            maximum = controls.get("delivery_budget_max_fps")
            if maximum and camera.fps > maximum:
                raise ValueError(f"This camera's paced link supports at most {maximum:.2f} fps at the current ROI (with transport headroom)")
            request_id = worker.queue_settings(patch)
            self._desired_configs[role] = camera
            return request_id

    def _settings_finished(self, role: str, cfg: CameraConfig):
        with self._settings_lock:
            self._desired_configs[role] = cfg

    def use_centroid_reference(self, role: str) -> ReferenceChange:
        item = self.workers[role].latest.read()
        if item is None:
            return ReferenceChange(role, False, "Waiting for the first frame")
        if perf_counter_ns()-item.frame.received_ns >= 1e9:
            return ReferenceChange(role, False, "Frame is stale; wait for live images")
        m = item.measurement
        if m is None:
            return ReferenceChange(role, False, "Centroid calculation is disabled for Dark Field")
        if not m.centroid_valid:
            reason = {"low_flux": "No beam detected above the intensity threshold",
                      "saturation": "Image is saturated; reduce exposure"}.get(m.reason, "No valid centroid")
            return ReferenceChange(role, False, reason)
        return self.set_reference(role, m.centroid_x, m.centroid_y)

    def center_reference(self, role: str) -> ReferenceChange:
        item = self.workers[role].latest.read()
        if item is None:
            return ReferenceChange(role, False, "Waiting for the first frame")
        height, width = item.frame.image.shape
        return self.set_reference(role, (width-1)/2, (height-1)/2)

    def set_references_detailed(self) -> tuple[ReferenceChange, ...]:
        return tuple(self.use_centroid_reference(role) for role in self.workers if role != "dark_field")

    def set_references(self) -> int:
        """Compatibility helper for scripts that only need the number of updated references."""
        return sum(change.applied for change in self.set_references_detailed())

    def stats(self, include_timing: bool = True) -> dict:
        return {"simulated": self.simulated, "cameras": {r: w.stats(include_timing) for r, w in self.workers.items()},
                "recording": self.recorder.stats(include_timing), "control": self.poll_control(),
                "timing": {"host_to_pid": percentiles(self._pid_latencies),
                           "host_to_display_submission": percentiles(self._display_ages),
                           "camera_to_display_submission": percentiles(self._camera_display_ages),
                           "pid_deadline_misses": self._pid_deadline_misses,
                           "display_deadline_misses": self._display_deadline_misses} if include_timing else {},
                "udp_send_drops": self.publisher.send_drops,
                "udp_control_replies": self.publisher.received_count,
                "pid_process_exit_code": self._pid.poll() if self._pid else None}

    def close(self) -> dict:
        if self._closed:
            return {}
        self._closed = True
        self.stop.set()
        for worker in self.workers.values():
            if worker.thread.ident is not None:
                worker.thread.join(timeout=max(3.0, worker.cfg.grab_timeout_ms/1000+1))
        self._background_pool.shutdown(wait=True)
        self.poll_control()
        if self._pid and self._pid.poll() is None:
            try:
                self.publisher.socket.sendto(b"STOP"+struct.pack("!Q", self.token), self.publisher.address)
                self._pid.wait(timeout=6)
            except (OSError, subprocess.TimeoutExpired):
                self._pid.terminate()
                self._pid.wait(timeout=3)
        recording = self.recorder.close()
        summary = self.stats()
        summary["recording"] = recording
        summary["workers_stopped"] = all(not w.thread.is_alive() for w in self.workers.values())
        summary["final_configuration"] = replace(self.cfg, cameras=tuple(
            replace(worker.cfg, reference_px=worker.processor.reference_state(worker.cfg.width, worker.cfg.height)[:2])
            for worker in self.workers.values())).to_dict()
        summary["capture_directory"] = str(self.capture_dir)
        summary["readable_report"] = {"state": "live files complete",
                                      "path": str(self.readable_dir/"START_HERE.html")}
        (self.session/"readable_report_status.json").write_text(json.dumps({"state": "ready",
            "mode": "written live", "saved_frames": recording["frames_saved"],
            "png_frames": recording["png_frames_saved"], "path": str(self.readable_dir/"START_HERE.html")}, indent=2), encoding="utf-8")
        (self.session/"summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self.publisher.close()
        logging.getLogger().removeHandler(self._capture_log)
        self._capture_log.close()
        if self._pid_log:
            self._pid_log.close()
        return summary

    def wait_for_readable_report(self, timeout=None):
        """Optional batch/validation wait; the interactive GUI never waits for PNG conversion."""
        return self._reporter.wait(timeout=timeout) if self._reporter is not None else None
