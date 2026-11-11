"""Immutable dark references; calibration files carry their acquisition signature."""
import json
from pathlib import Path
import cv2
import numpy as np

SIGNATURE_FIELDS = ("serial", "width", "height", "offset_x", "offset_y", "pixel_format", "exposure_us", "gain")


def signature(cfg):
    return {key: getattr(cfg, key) for key in SIGNATURE_FIELDS}


def load_background(cfg, config_path, *, simulated=None):
    if not cfg.background_file:
        return None
    path = Path(cfg.background_file)
    if not path.is_absolute():
        path = Path(config_path).parent/path
    with np.load(path, allow_pickle=False) as saved:
        metadata = json.loads(str(saved["metadata"]))
        if metadata["signature"] != signature(cfg):
            raise ValueError("Dark reference does not match camera/ROI/format/exposure/gain; capture a new background")
        if simulated is not None and metadata.get("simulated", False) != simulated:
            raise ValueError("Simulation and real cameras require separate background references")
        image = saved["image"]
    dtype = np.uint8 if cfg.pixel_format == "Mono8" else np.uint16
    if image.shape != (cfg.height, cfg.width) or image.dtype != dtype:
        raise ValueError("Dark reference has an incompatible shape or data type")
    image.flags.writeable = False
    return image


def save_background(path, image, cfg, *, simulated=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        np.savez(stream, image=image, metadata=json.dumps({"signature": signature(cfg), "averaged_frames": cfg.background_frames,
                                                         "simulated": simulated}))
    temporary.replace(path)


def subtract_background(image, background):
    if background is None:
        return None
    corrected = cv2.subtract(image, background)  # Saturating unsigned subtraction, no underflow.
    corrected.flags.writeable = False
    return corrected
