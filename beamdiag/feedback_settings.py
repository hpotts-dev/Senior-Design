"""Validated, infrequent feedback configuration; never infer an optical calibration."""
from dataclasses import replace
import json
import math
from pathlib import Path

from .config import AppConfig, CameraConfig, PIDConfig, PID_SOURCE_ROLE, validate

FEEDBACK_FIELDS = ("urad_per_pixel", "calibration_confirmed", "analysis_roi", "reference_px")
PID_FIELDS = ("enabled", "kp", "ki", "kd", "derivative_tau_s", "integral_limit",
              "output_limit", "slew_per_s", "deadband_urad", "max_sample_age_ms", "watchdog_ms")


def feedback_values(cfg):
    return {key: getattr(cfg, key) for key in FEEDBACK_FIELDS}


def pid_values(cfg):
    return {key: getattr(cfg, key) for key in PID_FIELDS}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def with_feedback_settings(cfg: CameraConfig, patch: dict) -> CameraConfig:
    if cfg.role != PID_SOURCE_ROLE:
        raise ValueError("Feedback calibration is exclusively for Far Field")
    if set(patch)-set(FEEDBACK_FIELDS):
        raise ValueError("Unsupported feedback setting")
    patch = dict(patch)
    if "calibration_confirmed" in patch and type(patch["calibration_confirmed"]) is not bool:
        raise ValueError("Calibration confirmation must be true/false")
    if "urad_per_pixel" in patch:
        matrix = patch["urad_per_pixel"]
        if (not isinstance(matrix, (tuple, list)) or len(matrix) != 2 or
                any(not isinstance(row, (tuple, list)) or len(row) != 2 or
                    not all(_number(v) for v in row) for row in matrix)):
            raise ValueError("Calibration must be a finite 2×2 matrix in µrad/pixel")
        patch["urad_per_pixel"] = tuple(tuple(row) for row in matrix)
    for key in ("analysis_roi", "reference_px"):
        if key in patch:
            if not isinstance(patch[key], (tuple, list)):
                raise ValueError(f"{key} must be a list of coordinates")
            patch[key] = tuple(patch[key])
    updated = replace(cfg, **patch)
    if updated.reference_px:
        ref = updated.reference_px
        if (len(ref) != 2 or not all(_number(v) for v in ref) or
                not 0 <= ref[0] < updated.width or not 0 <= ref[1] < updated.height):
            raise ValueError("Reference must be inside the delivered Far Field frame")
    cameras = (updated, CameraConfig("dark_field"), CameraConfig("near_field"))
    validate(AppConfig(cameras=cameras))
    return updated


def with_pid_settings(cfg: PIDConfig, patch: dict) -> PIDConfig:
    if set(patch)-set(PID_FIELDS):
        raise ValueError("Unsupported PID setting (the source and local port cannot change live)")
    patch = dict(patch)
    for key, value in patch.items():
        if key == "enabled":
            if type(value) is not bool:
                raise ValueError("PID enabled must be true/false")
        elif key in ("kp", "ki", "kd"):
            if not isinstance(value, (tuple, list)) or len(value) != 2 or not all(_number(v) for v in value):
                raise ValueError(f"{key} needs finite X and Y gains")
            patch[key] = tuple(value)
        elif not _number(value):
            raise ValueError(f"{key} must be finite")
    updated = replace(cfg, **patch)
    validate(AppConfig(pid=updated))
    return updated


def optical_scale(pixel_pitch_um, focal_length_mm, magnification):
    if not all(_number(v) and v > 0 for v in (pixel_pitch_um, focal_length_mm, magnification)):
        raise ValueError("Pixel pitch, effective focal length and magnification must be positive")
    return 1000*pixel_pitch_um/(focal_length_mm*magnification)


def save_profile(path: Path, camera: CameraConfig, pid: PIDConfig):
    # Export the applied values, not an unacknowledged UI draft. No startup file changes.
    data = {"schema": 1, "source_role": PID_SOURCE_ROLE,
            "far_field": feedback_values(camera), "pid": pid_values(pid)}
    path.write_text(json.dumps(data, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def load_profile(path: Path, camera: CameraConfig, pid: PIDConfig):
    if path.stat().st_size > 65536:
        raise ValueError("Feedback profile exceeds 64 KiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("source_role") != PID_SOURCE_ROLE:
        raise ValueError("Expected a Far Field feedback profile (schema 1)")
    return with_feedback_settings(camera, data["far_field"]), with_pid_settings(pid, data["pid"])
