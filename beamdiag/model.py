from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Generic, TypeVar
import numpy as np

T = TypeVar("T")


class LatestValue(Generic[T]):
    """Single-slot mailbox: readers never build a backlog or retain SDK buffers."""
    def __init__(self):
        self._lock = Lock()
        self._value: T | None = None

    def publish(self, value: T) -> None:
        with self._lock:
            self._value = value

    def read(self) -> T | None:
        with self._lock:
            return self._value


@dataclass(frozen=True, slots=True)
class Frame:
    role: str
    sequence: int
    received_ns: int  # host perf_counter_ns, immediately AFTER RetrieveResult
    camera_timestamp: int  # device ticks, deliberately not mixed with host time
    image: np.ndarray  # owned, C-contiguous, read-only; SDK buffer has already been released
    skipped: int = 0  # SDK's skipped-image count (does not include all transport losses)
    sensor_offset_x: int = 0
    sensor_offset_y: int = 0
    camera_to_host_ms: float | None = None  # Approximate age from a latched camera clock, when supported.


@dataclass(frozen=True, slots=True)
class Measurement:
    role: str
    sequence: int
    received_ns: int
    computed_ns: int
    centroid_x: float
    centroid_y: float
    reference_x: float
    reference_y: float
    error_x_px: float
    error_y_px: float
    error_x_urad: float
    error_y_urad: float
    flux: float
    peak: float
    saturated_fraction: float
    centroid_valid: bool
    angular_valid: bool
    reason: str
    reference_revision: int
    centroid_ms: float

    @property
    def host_processing_ms(self) -> float:
        return (self.computed_ns-self.received_ns)/1e6


@dataclass(frozen=True, slots=True)
class DiagnosticFrame:
    frame: Frame
    measurement: Measurement | None  # Dark Field is display/capture only.
    corrected_image: np.ndarray | None = None
    background_file: str = ""


@dataclass(frozen=True, slots=True)
class ReferenceChange:
    role: str
    applied: bool
    reason: str
    x: float | None = None
    y: float | None = None
