"""Small, allowlisted live-control messages; geometry and calibration are not changed here."""
from dataclasses import asdict, dataclass, replace
import math

from .config import CameraConfig, PIXEL_BITS

ACQUISITION_FIELDS = ("exposure_us", "gain", "fps")
DETECTION_FIELDS = ("background", "threshold", "min_flux", "saturation_level", "max_saturated_fraction")
LIVE_FIELDS = ACQUISITION_FIELDS+DETECTION_FIELDS


def live_values(cfg: CameraConfig) -> dict:
    return {name: getattr(cfg, name) for name in LIVE_FIELDS}


def with_live_settings(cfg: CameraConfig, patch: dict) -> CameraConfig:
    unknown = set(patch)-set(LIVE_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported live settings: {', '.join(sorted(unknown))}")
    for name, value in patch.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
    updated = replace(cfg, **patch)
    if min(updated.exposure_us, updated.fps) <= 0 or min(updated.gain, updated.background, updated.threshold, updated.min_flux) < 0:
        raise ValueError("Exposure and FPS must be positive; gain and detection settings must be nonnegative")
    if not 0 <= updated.max_saturated_fraction <= 1:
        raise ValueError("Maximum clipped fraction must be between 0 and 1")
    if type(updated.saturation_level) is not int or not 0 <= updated.saturation_level <= (1 << PIXEL_BITS[updated.pixel_format])-1:
        raise ValueError("Clipping level must be a whole number within the pixel format's ADC range")
    return updated


@dataclass(frozen=True, slots=True)
class SettingsEvent:
    role: str
    request_id: int
    host_ns: int
    next_sequence: int
    success: bool
    requested: dict
    actual: dict
    message: str
    feedback_revision: int
    category: str = "camera"

    def to_dict(self):
        return asdict(self)
