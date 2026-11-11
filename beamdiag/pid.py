"""Two-axis nominal PID, in normalized command units. This module never drives hardware."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .config import PIDConfig
from .transport import ErrorPacket


def clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class PIDAxis:
    def __init__(self, cfg: PIDConfig, axis: int):
        self.cfg, self.axis = cfg, axis
        self.reset()

    def reset(self):
        self.integral = 0.0
        self.derivative = 0.0
        self.previous_error: float | None = None
        self.output = 0.0

    def step(self, displacement: float, dt: float) -> float:
        c, i = self.cfg, self.axis
        if not isfinite(displacement) or not isfinite(dt) or dt <= 0:
            raise ValueError("PID requires finite error and positive elapsed time")
        # Measured displacement = centroid - reference; correcting action has the opposite sign.
        error = -displacement if abs(displacement) > c.deadband_urad else 0.0
        raw_derivative = 0.0 if self.previous_error is None else (error-self.previous_error)/dt
        alpha = dt/(c.derivative_tau_s+dt)
        self.derivative += alpha*(raw_derivative-self.derivative)
        delta_i = c.ki[i]*error*dt
        candidate_i = clamp(self.integral+delta_i, c.integral_limit)
        base = c.kp[i]*error+c.kd[i]*self.derivative
        # Anti-windup includes both amplitude and slew saturation.
        low = max(-c.output_limit, self.output-c.slew_per_s*dt)
        high = min(c.output_limit, self.output+c.slew_per_s*dt)
        proposed = base+candidate_i
        if not ((proposed > high and delta_i > 0) or (proposed < low and delta_i < 0)):
            self.integral = candidate_i
        self.output = max(low, min(high, base+self.integral))
        self.previous_error = error
        return self.output


@dataclass(frozen=True, slots=True)
class PIDResult:
    x: float
    y: float
    state: int


class NominalController:
    def __init__(self, cfg: PIDConfig, nominal_dt: float):
        self.cfg, self.nominal_dt = cfg, nominal_dt
        self.axes = (PIDAxis(cfg, 0), PIDAxis(cfg, 1))
        self.last_sequence = -1
        self.last_sample_ns: int | None = None
        self.last_valid_received_ns: int | None = None
        self.revision: int | None = None

    def reset(self):
        for axis in self.axes:
            axis.reset()
        self.last_sample_ns = None

    def reconfigure(self, cfg: PIDConfig):
        # Keep the monotonic sequence guard; a retry must not integrate old data.
        self.cfg = cfg
        self.axes = (PIDAxis(cfg, 0), PIDAxis(cfg, 1))
        self.last_sample_ns = None
        self.last_valid_received_ns = None
        self.revision = None

    def update(self, sample: ErrorPacket, now_ns: int) -> PIDResult | None:
        if sample.sequence <= self.last_sequence:
            return None  # duplicate or reordered packet must not integrate twice
        self.last_sequence = sample.sequence
        if not self.cfg.enabled:
            self.reset()
            return PIDResult(0.0, 0.0, 6)
        age = now_ns-sample.received_ns
        if (age < 0 or age > self.cfg.max_sample_age_ms*1e6 or
                sample.computed_ns < sample.received_ns or sample.computed_ns > now_ns):
            self.reset()
            return PIDResult(0.0, 0.0, 3)
        if not sample.valid or not all(isfinite(v) for v in (sample.x_urad, sample.y_urad)):
            self.reset()
            return PIDResult(0.0, 0.0, 2)
        if self.revision != sample.reference_revision:
            self.reset()
            self.revision = sample.reference_revision
        dt = self.nominal_dt if self.last_sample_ns is None else (sample.received_ns-self.last_sample_ns)/1e9
        if dt <= 0:
            self.reset()
            return PIDResult(0.0, 0.0, 3)
        if dt > self.cfg.watchdog_ms/1000:
            self.reset()
            dt = self.nominal_dt
        x, y = (axis.step(error, dt) for axis, error in zip(self.axes, (sample.x_urad, sample.y_urad)))
        self.last_sample_ns = sample.received_ns
        self.last_valid_received_ns = sample.received_ns
        return PIDResult(x, y, 1)

    def watchdog(self, now_ns: int) -> PIDResult | None:
        if self.last_valid_received_ns is not None and now_ns-self.last_valid_received_ns > self.cfg.watchdog_ms*1e6:
            self.reset()
            self.last_valid_received_ns = None
            return PIDResult(0.0, 0.0, 4)
        return None
