"""Camera adapters. Only the acquisition worker touches a camera's SDK object."""
from __future__ import annotations

from dataclasses import asdict, replace
from threading import Event
from time import perf_counter, perf_counter_ns
from typing import Protocol
import logging
import math
import numpy as np

from .config import CameraConfig, PIXEL_BITS
from .model import Frame
from .camera_settings import ACQUISITION_FIELDS, live_values, with_live_settings

log = logging.getLogger(__name__)


class CameraSource(Protocol):
    info: dict
    def open(self) -> None: ...
    def read(self, stop: Event) -> Frame | None: ...
    def close(self) -> None: ...
    def control_state(self) -> dict: ...
    def apply_settings(self, patch: dict) -> CameraConfig: ...


class SimulatedCamera:
    """Deterministic subpixel Gaussian jitter. No simulated actuator feedback is implied."""
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.info = {"backend": "simulation", "settings": asdict(cfg)}
        self.sequence = 0
        self._phase = {"far_field": 0.0, "dark_field": 0.7, "near_field": 1.3}[cfg.role]
        self._x = np.arange(cfg.width, dtype=np.float32)
        self._y = np.arange(cfg.height, dtype=np.float32)
        self._start = 0.0
        self._next = 0.0

    def open(self) -> None:
        self._start = self._next = perf_counter()

    def read(self, stop: Event) -> Frame | None:
        delay = max(0.0, self._next-perf_counter())
        wait = min(delay, self.cfg.grab_timeout_ms/1000)
        if stop.wait(wait):
            return None
        if delay > wait:
            return None  # Keep command servicing responsive at low FPS / long exposure.
        t = perf_counter()-self._start
        effective_fps = min(self.cfg.fps, 1e6/self.cfg.exposure_us)
        self._next = max(self._next+1/effective_fps, perf_counter())
        cx = (self.cfg.width-1)/2+0.35*math.sin(2*math.pi*1.1*t+self._phase)
        cy = (self.cfg.height-1)/2+0.28*math.cos(2*math.pi*0.8*t+self._phase)
        sigma = 10.0 if self.cfg.role != "near_field" else 16.0
        gx = np.exp(-0.5*((self._x-cx)/sigma)**2)
        gy = np.exp(-0.5*((self._y-cy)/sigma)**2)
        bits = PIXEL_BITS[self.cfg.pixel_format]
        scale = ((1 << bits)-1)/255.0
        amplitude = 210*scale*(self.cfg.exposure_us/2000.0)*10**(self.cfg.gain/20.0)
        signal = 8.0+amplitude*gy[:, None]*gx[None, :]
        np.clip(signal, 0, (1 << bits)-1, out=signal)
        image = signal.astype(np.uint8 if bits == 8 else np.uint16)
        image.flags.writeable = False
        self.sequence += 1
        now = perf_counter_ns()
        return Frame(self.cfg.role, self.sequence, now, now, image, 0,
                     self.cfg.offset_x, self.cfg.offset_y)

    def close(self) -> None:
        pass

    def control_state(self) -> dict:
        return {"values": live_values(self.cfg), "resulting_fps": min(self.cfg.fps, 1e6/self.cfg.exposure_us),
                "capabilities": {
                    "exposure_us": {"min": 1.0, "max": 1000000.0, "increment": 1.0, "unit": "µs", "supported": True},
                    "gain": {"min": 0.0, "max": 48.0, "increment": 0.1, "unit": "dB", "supported": True},
                    "fps": {"min": 1.0, "max": 200.0, "increment": 0.1, "unit": "fps", "supported": True}}}

    def apply_settings(self, patch: dict) -> CameraConfig:
        cfg = with_live_settings(self.cfg, patch)
        for name, bounds in self.control_state()["capabilities"].items():
            value = max(bounds["min"], min(bounds["max"], getattr(cfg, name)))
            cfg = replace(cfg, **{name: value})
        self.cfg = cfg
        self._next = perf_counter()+1/min(cfg.fps, 1e6/cfg.exposure_us)
        self.info["live_values"] = live_values(cfg)
        return cfg


def discover_basler() -> list[dict]:
    from pypylon import pylon
    return [{"serial": d.GetSerialNumber(), "model": d.GetModelName(),
             "interface": d.GetDeviceClass(), "ip": d.GetIpAddress() if d.GetDeviceClass() == "BaslerGigE" else ""}
            for d in pylon.TlFactory.GetInstance().EnumerateDevices()]


class BaslerCamera:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.camera = None
        self.info: dict = {}
        self.sequence = 0
        self._use_zero_copy = False
        self._clock_offset_ns = None
        self._clock_tick_hz = 1e9

    def _set(self, name: str, value, required: bool = False, align: bool = False) -> bool:
        from pypylon import genicam
        node = self.camera.GetNodeMap().GetNode(name)
        if node is None or not genicam.IsWritable(node):
            if required:
                raise RuntimeError(f"{self.cfg.role}: required GenICam node {name} is not writable")
            return False
        requested = value
        if align:
            lo, hi, inc = int(node.GetMin()), int(node.GetMax()), max(1, int(node.GetInc()))
            value = lo+(min(hi, max(lo, int(value)))-lo)//inc*inc
        node.SetValue(value)
        actual = node.GetValue()
        self.info.setdefault("applied", {})[name] = actual
        if requested != actual:
            log.warning("%s: %s requested %s, applied %s", self.cfg.role, name, requested, actual)
        return True

    def _read_node(self, *names, default=None):
        from pypylon import genicam
        for name in names:
            node = self.camera.GetNodeMap().GetNode(name)
            if node is not None and genicam.IsReadable(node):
                return node.GetValue()
        return default

    def _control_node(self, field):
        from pypylon import genicam
        alternatives = {"exposure_us": ("ExposureTime", "ExposureTimeAbs"),
                        "gain": ("Gain", "GainRaw"), "fps": ("AcquisitionFrameRate", "AcquisitionFrameRateAbs")}
        for name in alternatives[field]:
            node = self.camera.GetNodeMap().GetNode(name)
            if node is not None and genicam.IsReadable(node):
                return name, node
        return None, None

    def _configure_stream_retention(self):
        """Allow a complete image to arrive even over a slower camera link."""
        from pypylon import genicam
        node = self.camera.GetStreamGrabberNodeMap().GetNode("FrameRetention")
        if node is None or not genicam.IsReadable(node):
            return
        throughput = float(self.info.get("throughput_limit_bps") or self.cfg.throughput_limit_bps)
        link_speed = self.info.get("device_link_speed_bps")
        if link_speed:
            throughput = min(throughput, float(link_speed)/8)
        payload_ms = 1000*self.info["payload_bytes"]/throughput
        # Include protocol overhead and resend headroom. The SDK default can be
        # shorter than a single full-sensor image on a 100 Mb/s connection.
        required = math.ceil(2*payload_ms+100)
        current = int(node.GetValue())
        if current < required:
            if not genicam.IsWritable(node):
                raise RuntimeError(f"{self.cfg.role}: FrameRetention must be at least {required} ms for this image/link")
            low, high, increment = int(node.GetMin()), int(node.GetMax()), max(1, int(node.GetInc()))
            value = low+math.ceil(max(0, required-low)/increment)*increment
            if value > high:
                raise RuntimeError(f"{self.cfg.role}: receiver FrameRetention limit is too short for this image/link")
            node.SetValue(value)
        self.info["stream_grabber"] = {"frame_retention_ms": int(node.GetValue()),
                                        "estimated_payload_transfer_ms": payload_ms}

    def _latch_clock(self):
        """Expose camera/network delay separately from host processing; do not synchronize or reset the camera."""
        from pypylon import genicam
        nodes = self.camera.GetNodeMap()
        self._clock_offset_ns = None
        command = next((node for name in ("GevTimestampControlLatch", "TimestampLatch")
                        if (node := nodes.GetNode(name)) is not None and genicam.IsWritable(node)), None)
        value = next((node for name in ("GevTimestampValue", "TimestampLatchValue")
                      if (node := nodes.GetNode(name)) is not None and genicam.IsReadable(node)), None)
        if command is None or value is None:
            return
        try:
            self._clock_tick_hz = self.info.get("timestamp_tick_frequency_hz") or 1e9
            samples = []
            for _ in range(3):
                before = perf_counter_ns()
                command.Execute()
                stamp = int(value.GetValue())
                after = perf_counter_ns()
                samples.append((after-before, (before+after)/2-stamp*1e9/self._clock_tick_hz))
            rtt, self._clock_offset_ns = min(samples)
            self.info["camera_clock_latch_rtt_ms"] = rtt/1e6
        except Exception:
            self._clock_offset_ns = None  # Timing support is optional; acquisition must still work.

    def control_state(self) -> dict:
        values, capabilities = live_values(self.cfg), {}
        for field in ACQUISITION_FIELDS:
            name, node = self._control_node(field)
            if node is None:
                capabilities[field] = {"supported": False}
                continue
            increment = 0.0
            try:
                increment = float(node.GetInc())
            except Exception:
                pass  # Some floating-point nodes have no fixed increment.
            unit = {"exposure_us": "µs", "gain": "raw" if name == "GainRaw" else "dB", "fps": "fps"}[field]
            capabilities[field] = {"supported": True, "node": name, "min": float(node.GetMin()),
                                   "max": float(node.GetMax()), "increment": increment, "unit": unit}
            values[field] = float(node.GetValue())
        return {"values": values, "capabilities": capabilities,
                "resulting_fps": self._read_node("ResultingFrameRate", "ResultingFrameRateAbs")}

    def _write_acquisition(self, cfg: CameraConfig):
        from pypylon import genicam
        current_fps = self._read_node("AcquisitionFrameRate", "AcquisitionFrameRateAbs", default=cfg.fps)
        # Lower FPS first when requesting longer exposures; shorten exposure first when raising FPS.
        order = ("fps", "exposure_us", "gain") if cfg.fps <= current_fps else ("exposure_us", "gain", "fps")
        self._set("ExposureAuto", "Off")
        self._set("GainAuto", "Off")
        self._set("AcquisitionFrameRateEnable", True)
        for field in order:
            name, node = self._control_node(field)
            if node is None:
                if getattr(cfg, field) != getattr(self.cfg, field):
                    raise ValueError(f"{field} is not supported by this camera")
                continue
            if not genicam.IsWritable(node):
                raise ValueError(f"{field} is not writable while the camera is stopped")
            low, high = float(node.GetMin()), float(node.GetMax())
            value = max(low, min(high, getattr(cfg, field)))
            if name == "GainRaw":
                increment = max(1, int(node.GetInc()))
                value = int(low)+(int(value)-int(low))//increment*increment
            node.SetValue(value)
            self.info.setdefault("applied", {})[name] = node.GetValue()

    def apply_settings(self, patch: dict) -> CameraConfig:
        """SDK operations stay in the camera worker. Restart only this stream to discard old frames."""
        from pypylon import pylon
        cfg = with_live_settings(self.cfg, patch)
        previous, grabbing = self.cfg, self.camera.IsGrabbing()
        if grabbing:
            self.camera.StopGrabbing()
        try:
            self._write_acquisition(cfg)
        except Exception:
            self._write_acquisition(previous)
            raise
        finally:
            if grabbing:
                self.camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        state = self.control_state()
        actual = {key: state["values"][key] for key in ACQUISITION_FIELDS}
        self.cfg = replace(cfg, **actual)
        self.info["live_values"] = live_values(self.cfg)
        self.info["resulting_fps"] = state["resulting_fps"]
        return self.cfg

    def open(self) -> None:
        from pypylon import pylon
        self._use_zero_copy = hasattr(pylon.GrabResult, "GetArrayZeroCopy")
        cfg = self.cfg
        if not cfg.serial:
            raise ValueError(f"Set the {cfg.role} serial in config.toml (use --list-cameras)")
        factory = pylon.TlFactory.GetInstance()
        matches = [d for d in factory.EnumerateDevices() if d.GetSerialNumber() == cfg.serial]
        if len(matches) != 1:
            raise RuntimeError(f"{cfg.role}: serial {cfg.serial!r} is missing or ambiguous")
        device = matches[0]
        if device.GetDeviceClass() != "BaslerGigE":
            raise RuntimeError(f"{cfg.role}: expected a GigE camera, got {device.GetDeviceClass()}")
        self.info = {"backend": "basler", "serial": cfg.serial, "model": device.GetModelName(),
                     "ip": device.GetIpAddress(), "requested": asdict(cfg), "applied": {}}
        self.camera = pylon.InstantCamera(factory.CreateDevice(device))
        try:
            self.camera.Open()
            self._set("AcquisitionMode", "Continuous", required=True)
            # Disable triggers for every supported selector, including persistent acquisition triggers.
            selector = self.camera.GetNodeMap().GetNode("TriggerSelector")
            if selector is not None:
                from pypylon import genicam
                if genicam.IsWritable(selector):
                    for symbol in selector.GetSymbolics():
                        entry = selector.GetEntryByName(symbol)
                        if genicam.IsAvailable(entry):
                            selector.SetValue(symbol)
                            self._set("TriggerMode", "Off", required=True)
            else:
                self._set("TriggerMode", "Off")
            self._set("ExposureAuto", "Off")
            self._set("GainAuto", "Off")
            transport_format = "Mono12p" if cfg.packed_transport else cfg.pixel_format
            self._set("PixelFormat", transport_format, required=True)
            self.info["transport_pixel_format"] = transport_format
            self.info["native_pixel_format"] = cfg.pixel_format
            self._set("OffsetX", 0, align=True)
            self._set("OffsetY", 0, align=True)
            self._set("Width", cfg.width, required=True, align=True)
            self._set("Height", cfg.height, required=True, align=True)
            self._set("OffsetX", cfg.offset_x, required=bool(cfg.offset_x), align=True)
            self._set("OffsetY", cfg.offset_y, required=bool(cfg.offset_y), align=True)
            if not self._set("ExposureTime", cfg.exposure_us):
                self._set("ExposureTimeAbs", cfg.exposure_us, required=True)
            if not self._set("Gain", cfg.gain):
                self._set("GainRaw", int(cfg.gain), required=True, align=True)
            self._set("AcquisitionFrameRateEnable", True)
            if not self._set("AcquisitionFrameRate", cfg.fps):
                self._set("AcquisitionFrameRateAbs", cfg.fps, required=True)
            self._set("GevSCPSPacketSize", cfg.packet_size, required=True, align=True)
            self._set("DeviceLinkThroughputLimitMode", "On")
            limited = self._set("DeviceLinkThroughputLimit", cfg.throughput_limit_bps, align=True)
            delay = cfg.inter_packet_delay_ticks
            if not limited and not delay:
                tick_hz = self._read_node("GevTimestampTickFrequency")
                packet = self._read_node("GevSCPSPacketSize", default=cfg.packet_size)
                if tick_hz:
                    delay = math.ceil(max(0, packet/cfg.throughput_limit_bps-packet/125e6)*tick_hz)
                else:
                    log.warning("%s: no throughput limiter/tick frequency; set GevSCPD manually", cfg.role)
            # Do not overwrite the delay that DeviceLinkThroughputLimit has just selected.
            if delay or not limited:
                self._set("GevSCPD", delay, align=True)
            self._set("GevSCFTD", cfg.frame_transmission_delay_ticks, align=True)
            self.camera.MaxNumBuffer.Value = cfg.max_buffers
            self.info["width"] = int(self._read_node("Width"))
            self.info["height"] = int(self._read_node("Height"))
            self.info["offset_x"] = int(self._read_node("OffsetX", default=0))
            self.info["offset_y"] = int(self._read_node("OffsetY", default=0))
            self.info["resulting_fps"] = self._read_node("ResultingFrameRate", "ResultingFrameRateAbs")
            self.info["payload_bytes"] = self._read_node("PayloadSize")
            self.info["timestamp_tick_frequency_hz"] = self._read_node("GevTimestampTickFrequency")
            self.info["device_link_speed_bps"] = self._read_node("DeviceLinkSpeed")
            self.info["throughput_limit_bps"] = self._read_node("DeviceLinkThroughputLimit")
            self._configure_stream_retention()
            self._latch_clock()
            self.camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        except BaseException:
            self.close()
            raise

    def read(self, stop: Event) -> Frame | None:
        from pypylon import pylon
        if stop.is_set():
            return None
        result = self.camera.RetrieveResult(self.cfg.grab_timeout_ms, pylon.TimeoutHandling_Return)
        received = perf_counter_ns()
        # TimeoutHandling_Return may return an empty GrabResult smart pointer,
        # rather than None. Dereferencing it (even GrabSucceeded) throws the SDK's
        # NULL-pointer exception. Polling can normally time out at low FPS.
        if result is None or not result.IsValid():
            return None
        try:
            if not result.GrabSucceeded():
                raise RuntimeError(f"Grab failed: {result.GetErrorCode()} {result.GetErrorDescription()}")
            # pypylon unpacks Mono12p to right-aligned uint16 without discarding bits.
            # Keep one owned copy so GUI/disk threads never outlive the SDK buffer lease.
            if self._use_zero_copy:
                with result.GetArrayZeroCopy() as borrowed:
                    image = np.array(borrowed, copy=True, order="C")
                    del borrowed
            else:
                image = np.array(result.Array, copy=True, order="C")
            image.flags.writeable = False
            self.sequence += 1
            stamp = int(result.GetTimeStamp())
            camera_age = ((received-(stamp*1e9/self._clock_tick_hz+self._clock_offset_ns))/1e6
                          if self._clock_offset_ns is not None else None)
            return Frame(self.cfg.role, self.sequence, received, stamp, image,
                         int(result.GetNumberOfSkippedImages()), self.info["offset_x"], self.info["offset_y"], camera_age)
        finally:
            result.Release()

    def close(self) -> None:
        if self.camera is not None:
            try:
                if self.camera.IsGrabbing():
                    try:
                        from pypylon import genicam
                        nodemap = self.camera.GetStreamGrabberNodeMap()
                        statistics = {}
                        for name in ("Statistic_Total_Buffer_Count", "Statistic_Failed_Buffer_Count",
                                     "Statistic_Failed_Packet_Count", "Statistic_Buffer_Underrun_Count",
                                     "Statistic_Resend_Request_Count", "Statistic_Resend_Packet_Count",
                                     "Statistic_Missed_Frame_Count"):
                            node = nodemap.GetNode(name)
                            if node is not None and genicam.IsReadable(node):
                                statistics[name] = node.GetValue()
                        self.info["final_stream_statistics"] = statistics
                    except Exception as exc:
                        self.info["stream_statistics_error"] = str(exc)
                    self.camera.StopGrabbing()
            finally:
                if self.camera.IsOpen():
                    self.camera.Close()
