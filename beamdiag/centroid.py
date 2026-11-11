"""Intensity centroid using compiled OpenCV moments and a reusable float32 scratch image."""
from __future__ import annotations

from threading import Lock
from time import perf_counter_ns
import math
import cv2
import numpy as np

from .config import CameraConfig, PIXEL_BITS
from .model import Frame, Measurement


class CentroidProcessor:
    """One instance per acquisition worker; references can be changed from the UI."""
    def __init__(self, cfg: CameraConfig, simulated: bool = False):
        self.cfg = cfg
        self._demo_calibration = simulated
        self.calibrated = cfg.calibration_confirmed or simulated
        self.matrix = np.asarray(cfg.urad_per_pixel, dtype=np.float64)
        self._scratch: np.ndarray | None = None
        self._x_coordinates: np.ndarray | None = None
        self._y_coordinates: np.ndarray | None = None
        self._reference = tuple(cfg.reference_px) if cfg.reference_px else None
        self._revision = 0
        self._lock = Lock()

    def set_reference(self, x: float, y: float) -> None:
        if not math.isfinite(x+y):
            raise ValueError("Reference must be finite")
        with self._lock:
            self._reference = (x, y)
            self._revision += 1

    def reference_state(self, width: int, height: int) -> tuple[float, float, int]:
        with self._lock:
            x, y = self._reference or ((width-1)/2, (height-1)/2)
            return x, y, self._revision

    def update_settings(self, cfg: CameraConfig, *, explicit_calibration=False, update_reference=False) -> None:
        """Called only by the acquisition worker, between frames; preserve the optical target."""
        with self._lock:
            self.cfg = cfg
            if explicit_calibration:
                self._demo_calibration = False
            self.calibrated = cfg.calibration_confirmed or self._demo_calibration
            self.matrix = np.asarray(cfg.urad_per_pixel, dtype=np.float64)
            if update_reference:
                self._reference = tuple(cfg.reference_px) or None
            # A new estimator/exposure must not reuse an old PID integral or derivative history.
            self._revision += 1

    def process(self, frame: Frame, native_image=None) -> Measurement:
        started = perf_counter_ns()
        # One coherent configuration/reference snapshot per frame. The UI can set
        # a reference concurrently, but an error must never mix calibration epochs.
        with self._lock:
            cfg, matrix, calibrated = self.cfg, self.matrix, self.calibrated
            reference, revision = self._reference, self._revision
        image = frame.image
        if image.ndim != 2 or image.dtype not in (np.uint8, np.uint16):
            raise ValueError("Centroid input must be a 2D uint8/uint16 monochrome image")
        height, width = image.shape
        x0, y0, w, h = cfg.analysis_roi or (0, 0, width, height)
        if x0+w > width or y0+h > height:
            raise ValueError("Analysis ROI exceeds actual camera dimensions")
        roi = image[y0:y0+h, x0:x0+w]
        if self._scratch is None or self._scratch.shape != roi.shape:
            self._scratch = np.empty(roi.shape, dtype=np.float32)
            self._x_coordinates = np.arange(w, dtype=np.float64)
            self._y_coordinates = np.arange(h, dtype=np.float64)
        signal = self._scratch
        np.subtract(roi, cfg.background, out=signal, dtype=np.float32)
        # Threshold TOZERO retains above-threshold intensity for an intensity-weighted centroid.
        cv2.threshold(signal, cfg.threshold, 0, cv2.THRESH_TOZERO, dst=signal)
        if roi.size >= 65536:
            # Only first moments are needed. SIMD row/column reductions avoid
            # calculating all the second/third-order moments of a full sensor.
            columns = cv2.reduce(signal, 0, cv2.REDUCE_SUM, dtype=cv2.CV_64F).ravel()
            rows = cv2.reduce(signal, 1, cv2.REDUCE_SUM, dtype=cv2.CV_64F).ravel()
            flux = float(columns.sum())
            mx = float(columns @ self._x_coordinates)
            my = float(rows @ self._y_coordinates)
        else:
            m = cv2.moments(signal, binaryImage=False)
            flux, mx, my = m["m00"], m["m10"], m["m01"]
        native_roi = native_image[y0:y0+h, x0:x0+w] if native_image is not None else roi
        peak = float(cv2.minMaxLoc(native_roi)[1])
        saturation = cfg.saturation_level or (1 << PIXEL_BITS[cfg.pixel_format])-1
        fraction = (cv2.countNonZero(cv2.compare(native_roi, saturation, cv2.CMP_GE))/roi.size
                    if peak >= saturation else 0.0)
        valid = math.isfinite(flux) and flux > cfg.min_flux and fraction <= cfg.max_saturated_fraction
        reason = "ok" if valid else ("saturation" if fraction > cfg.max_saturated_fraction else "low_flux")
        cx = mx/flux+x0 if flux > 0 else math.nan
        cy = my/flux+y0 if flux > 0 else math.nan
        rx, ry = reference or ((width-1)/2, (height-1)/2)
        dx, dy = cx-rx, cy-ry
        angular_valid = valid and calibrated
        if angular_valid:
            a = matrix
            ax, ay = float(a[0, 0]*dx+a[0, 1]*dy), float(a[1, 0]*dx+a[1, 1]*dy)
        else:
            ax = ay = math.nan
            if valid:
                reason = "uncalibrated"
        finished = perf_counter_ns()
        return Measurement(cfg.role, frame.sequence, frame.received_ns, finished, cx, cy, rx, ry,
                           dx, dy, ax, ay, flux, peak, fraction, valid, angular_valid, reason,
                           revision, (finished-started)/1e6)
