"""Preview contrast only; never modify camera frames or measurement inputs."""
from __future__ import annotations

import cv2
import numpy as np

from .config import PIXEL_BITS


def preview_u8(image: np.ndarray, pixel_format: str, auto_contrast: bool = True) -> tuple[np.ndarray, float]:
    """Map unsigned monochrome pixels to a new 8-bit preview and report its white level in DN."""
    native_max = 255 if image.dtype == np.uint8 else (1 << PIXEL_BITS[pixel_format])-1
    white = float(native_max)
    if auto_contrast:
        # A few hot pixels must not hide the rest of a dim image. Keep zero black,
        # and allow the brightest 0.5% to clip in the preview, never in raw data.
        # Estimate contrast from at most ~16k evenly spaced preview pixels.
        # This avoids partitioning a full preview on every frame.
        step = max(1, int(np.ceil(np.sqrt(image.size/16384))))
        white = float(np.percentile(image[::step, ::step], 99.5))
        if white <= 0:
            # Preserve isolated small spots on an otherwise completely black frame.
            white = float(image.max())
        white = max(1.0, min(float(native_max), white))
    return cv2.convertScaleAbs(image, alpha=255/white), white


def resize_preview(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """Preserve the whole view while using fast integer averaging before fractional resizing."""
    while image.shape[1] >= 2*width and image.shape[0] >= 2*height:
        image = cv2.resize(image, ((image.shape[1]+1)//2, (image.shape[0]+1)//2),
                           interpolation=cv2.INTER_AREA)
    if image.shape != (height, width):
        image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    return image
