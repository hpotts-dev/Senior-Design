"""Typed configuration. Optical scales and controller gains are never inferred from hardware."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import math
import tomllib

ROLES = ("far_field", "dark_field", "near_field")
PID_SOURCE_ROLE = "far_field"
PIXEL_BITS = {"Mono8": 8, "Mono12": 12, "Mono16": 16}


@dataclass(frozen=True, slots=True)
class CameraConfig:
    role: str
    serial: str = ""
    width: int = 512
    height: int = 512
    offset_x: int = 0
    offset_y: int = 0
    fps: float = 10.0
    exposure_us: float = 2000.0
    gain: float = 0.0
    pixel_format: str = "Mono12"
    packed_transport: bool = False  # Mono12p on the wire; processing/recording remains native uint16.
    packet_size: int = 1500
    throughput_limit_bps: int = 25000000  # BYTES per second, despite GenICam's name
    inter_packet_delay_ticks: int = 0
    frame_transmission_delay_ticks: int = 0
    grab_timeout_ms: int = 100
    max_buffers: int = 4
    # Software analysis ROI: [x, y, width, height] in the delivered frame; empty = full frame.
    analysis_roi: tuple[int, ...] = ()
    background: float = 8.0
    threshold: float = 10.0  # intensity above background; low pixels are discarded
    min_flux: float = 500.0
    max_saturated_fraction: float = 0.01
    saturation_level: int = 0  # 0 = native ADC maximum; otherwise pixels >= this DN are clipped
    reference_px: tuple[float, ...] = ()  # empty = center of delivered frame
    urad_per_pixel: tuple[tuple[float, float], ...] = ((17.25, 0.0), (0.0, 17.25))
    calibration_confirmed: bool = False
    background_file: str = ""  # Per-pixel dark reference, relative to config.toml when possible.
    background_frames: int = 16


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    output_dir: str = "captures"
    format: str = "npy_stream"
    stream_rollover_MB: int = 512
    frame_queue_size: int = 48
    telemetry_queue_size: int = 4096
    every_n_frames: int = 1
    start_recording: bool = False
    generate_readable: bool = True
    readable_image_interval_s: float = 60.0  # Legacy/offline export cadence.
    snapshot_interval_s: float = 60.0  # Always-on timed native + PNG snapshots, independent of Record.


@dataclass(frozen=True, slots=True)
class PIDConfig:
    source_role: str = PID_SOURCE_ROLE
    host: str = "127.0.0.1"
    port: int = 49152
    enabled: bool = True
    max_sample_age_ms: float = 50.0
    watchdog_ms: float = 150.0
    kp: tuple[float, float] = (0.02, 0.02)
    ki: tuple[float, float] = (0.005, 0.005)
    kd: tuple[float, float] = (0.0, 0.0)
    derivative_tau_s: float = 0.02
    integral_limit: float = 0.5
    output_limit: float = 1.0
    slew_per_s: float = 10.0
    deadband_urad: float = 0.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    cameras: tuple[CameraConfig, ...] = field(default_factory=lambda: tuple(CameraConfig(r) for r in ROLES))
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    pid: PIDConfig = field(default_factory=PIDConfig)
    display_fps: float = 60.0
    display_width: int = 480
    latency_target_ms: float = 50.0
    network_budget_MBps: float = 90.0

    def to_dict(self) -> dict:
        return asdict(self)


def _finite(value) -> bool:
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, (tuple, list)):
        return all(_finite(v) for v in value)
    return not isinstance(value, (int, float)) or math.isfinite(value)


def validate(cfg: AppConfig) -> AppConfig:
    if tuple(c.role for c in cfg.cameras) != ROLES:
        raise ValueError(f"Exactly these cameras, in order, are required: {ROLES}")
    serials = [c.serial for c in cfg.cameras if c.serial]
    if len(set(serials)) != len(serials):
        raise ValueError("Camera serial numbers must be unique")
    if not _finite(asdict(cfg)):
        raise ValueError("Configuration contains non-finite numbers")
    if min(cfg.display_fps, cfg.display_width, cfg.latency_target_ms, cfg.network_budget_MBps) <= 0:
        raise ValueError("Display, latency, and network limits must be positive")
    for c in cfg.cameras:
        integer_values = (c.width, c.height, c.offset_x, c.offset_y, c.packet_size, c.throughput_limit_bps,
                          c.inter_packet_delay_ticks, c.frame_transmission_delay_ticks,
                          c.grab_timeout_ms, c.max_buffers, c.saturation_level, c.background_frames, *c.analysis_roi)
        if any(type(v) is not int for v in integer_values):
            raise ValueError(f"{c.role}: image dimensions, ROI and transport integers must be whole numbers")
        if type(c.calibration_confirmed) is not bool or not isinstance(c.serial, str):
            raise ValueError(f"{c.role}: serial must be text and calibration_confirmed must be true/false")
        if min(c.width, c.height, c.fps, c.exposure_us, c.grab_timeout_ms) <= 0:
            raise ValueError(f"{c.role}: invalid dimensions or timing")
        if c.pixel_format not in PIXEL_BITS:
            raise ValueError(f"{c.role}: use Mono8, unpacked Mono12, or Mono16")
        if type(c.packed_transport) is not bool or (c.packed_transport and c.pixel_format != "Mono12"):
            raise ValueError(f"{c.role}: packed_transport must be true/false and requires Mono12")
        if min(c.offset_x, c.offset_y, c.gain, c.background, c.threshold, c.min_flux,
               c.inter_packet_delay_ticks, c.frame_transmission_delay_ticks) < 0:
            raise ValueError(f"{c.role}: negative camera/centroid setting")
        if c.max_buffers < 2 or c.packet_size < 576 or c.throughput_limit_bps <= 0:
            raise ValueError(f"{c.role}: invalid transport settings")
        if not isinstance(c.background_file, str) or not 1 <= c.background_frames <= 1024:
            raise ValueError(f"{c.role}: background file must be text; background_frames must be 1..1024")
        if not 0 <= c.max_saturated_fraction <= 1:
            raise ValueError(f"{c.role}: invalid saturation fraction")
        if not 0 <= c.saturation_level <= (1 << PIXEL_BITS[c.pixel_format])-1:
            raise ValueError(f"{c.role}: clipping level exceeds the pixel format's ADC range")
        if c.reference_px and len(c.reference_px) != 2:
            raise ValueError("reference_px must be empty or [x, y]")
        if c.analysis_roi:
            if len(c.analysis_roi) != 4:
                raise ValueError("analysis_roi must be empty or [x, y, width, height]")
            x, y, w, h = c.analysis_roi
            if min(x, y) < 0 or min(w, h) <= 0 or x+w > c.width or y+h > c.height:
                raise ValueError(f"{c.role}: analysis ROI outside delivered image")
        if len(c.urad_per_pixel) != 2 or any(len(row) != 2 for row in c.urad_per_pixel):
            raise ValueError("urad_per_pixel must be a 2x2 matrix")
        a, b = c.urad_per_pixel
        if abs(a[0]*b[1]-a[1]*b[0]) < 1e-12:
            raise ValueError("Angular calibration must be invertible")
    r, p = cfg.recording, cfg.pid
    if p.source_role != PID_SOURCE_ROLE:
        raise ValueError("PID feedback must use far_field; Dark Field and Near Field are diagnostic only")
    if type(p.enabled) is not bool:
        raise ValueError("PID enabled must be true/false")
    if any(type(v) is not int for v in (r.frame_queue_size, r.telemetry_queue_size, r.every_n_frames, r.stream_rollover_MB, p.port)):
        raise ValueError("Recording queue sizes, rollover size and PID port must be whole numbers")
    if type(r.start_recording) is not bool:
        raise ValueError("start_recording must be true/false")
    if type(r.generate_readable) is not bool or r.readable_image_interval_s < 0:
        raise ValueError("generate_readable must be true/false; readable image interval must be nonnegative")
    if r.snapshot_interval_s <= 0:
        raise ValueError("snapshot_interval_s must be positive; timed snapshots are always enabled")
    if r.format not in ("npy_stream", "npy", "png") or min(r.frame_queue_size, r.telemetry_queue_size, r.every_n_frames, r.stream_rollover_MB) < 1:
        raise ValueError("Invalid recording format or queue size")
    if p.host != "127.0.0.1" or not 1024 <= p.port <= 65535 or p.source_role not in ROLES:
        raise ValueError("PID must use a valid local UDP port and camera role")
    if min(p.max_sample_age_ms, p.watchdog_ms, p.output_limit, p.slew_per_s) <= 0:
        raise ValueError("PID timeouts and output limits must be positive")
    if p.output_limit > 1:
        raise ValueError("Normalized PID output limit must not exceed 1")
    if p.watchdog_ms < p.max_sample_age_ms:
        raise ValueError("watchdog_ms must be >= max_sample_age_ms")
    if min(p.derivative_tau_s, p.integral_limit, p.deadband_urad) < 0:
        raise ValueError("PID filter, integral limit and deadband cannot be negative")
    if any(len(v) != 2 for v in (p.kp, p.ki, p.kd)):
        raise ValueError("PID gains must have two axes")
    # Avoid accidentally allocating multi-gigabyte queues at large resolutions.
    biggest = max(c.width*c.height*(1 if c.pixel_format == "Mono8" else 2) for c in cfg.cameras)
    if biggest*r.frame_queue_size > 512*1024**2:
        raise ValueError("Frame queue exceeds 512 MiB; reduce frame_queue_size or camera ROI")
    return cfg


def load_config(path: str | Path) -> AppConfig:
    with Path(path).open("rb") as f:
        raw = tomllib.load(f)
    return config_from_dict(raw)


def parse_config(text: str) -> AppConfig:
    return config_from_dict(tomllib.loads(text))


def config_from_dict(raw: dict) -> AppConfig:
    unknown = set(raw)-{"app", "recording", "pid", "cameras"}
    if unknown:
        raise ValueError(f"Unknown configuration sections: {unknown}")
    cameras = []
    for entry in raw.get("cameras", [asdict(CameraConfig(r)) for r in ROLES]):
        entry = dict(entry)
        for k in ("analysis_roi", "reference_px"):
            if k in entry:
                entry[k] = tuple(entry[k])
        if "urad_per_pixel" in entry:
            entry["urad_per_pixel"] = tuple(tuple(row) for row in entry["urad_per_pixel"])
        cameras.append(CameraConfig(**entry))
    pid = dict(raw.get("pid", {}))
    for k in ("kp", "ki", "kd"):
        if k in pid:
            pid[k] = tuple(pid[k])
    return validate(AppConfig(cameras=tuple(cameras), recording=RecordingConfig(**raw.get("recording", {})),
                              pid=PIDConfig(**pid), **raw.get("app", {})))


def bandwidth_report(cfg: AppConfig) -> dict:
    rates = {c.role: c.width*c.height*c.fps*(1.5 if c.packed_transport else 1 if c.pixel_format == "Mono8" else 2)/1e6
             for c in cfg.cameras}
    total = sum(rates.values())
    return {"payload_MBps": rates, "total_payload_MBps": total,
            "budget_MBps": cfg.network_budget_MBps, "within_budget": total <= cfg.network_budget_MBps,
            "note": "Payload estimate only; Ethernet/GVSP overhead, resends and exposure are additional."}
