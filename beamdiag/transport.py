"""Versioned, fixed-size local UDP messages. No images, JSON, or pickle in the control path."""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter_ns
import socket
import struct
import sys
import json
from typing import TYPE_CHECKING
from .config import PID_SOURCE_ROLE, PIDConfig
from .feedback_settings import pid_values

if TYPE_CHECKING:
    from .model import Measurement

ERROR_STRUCT = struct.Struct("!4sQQQQIddB")
CONTROL_STRUCT = struct.Struct("!4sQQQQdddB")
STATES = {1: "active", 2: "invalid", 3: "stale", 4: "watchdog", 5: "stopped", 6: "paused", 7: "reset"}
SETTINGS_MAGIC, ACK_MAGIC = b"BPS1", b"BPA1"


def settings_message(magic, data):
    return magic+json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8")


def local_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
    if sys.platform == "win32":
        # An absent standalone PID listener must not poison subsequent UDP receives on Windows.
        try:
            sock.ioctl(getattr(socket, "SIO_UDP_CONNRESET", 0x9800000C), False)
        except (OSError, ValueError):
            pass  # Some Python builds restrict ioctl codes; receive paths handle reset errors too.
    return sock


@dataclass(frozen=True, slots=True)
class ErrorPacket:
    token: int
    sequence: int
    received_ns: int
    computed_ns: int
    reference_revision: int
    x_urad: float
    y_urad: float
    valid: bool

    def pack(self) -> bytes:
        return ERROR_STRUCT.pack(b"BPE1", self.token, self.sequence, self.received_ns, self.computed_ns,
                                 self.reference_revision, self.x_urad, self.y_urad, self.valid)

    @classmethod
    def unpack(cls, data: bytes) -> ErrorPacket:
        values = ERROR_STRUCT.unpack(data)
        if values[0] != b"BPE1" or values[-1] not in (0, 1):
            raise ValueError("Unsupported error packet")
        return cls(*values[1:-1], bool(values[-1]))


@dataclass(frozen=True, slots=True)
class ControlPacket:
    token: int
    sequence: int
    received_ns: int
    finished_ns: int
    x: float
    y: float
    compute_ms: float
    state: int

    def pack(self) -> bytes:
        return CONTROL_STRUCT.pack(b"BPC1", self.token, self.sequence, self.received_ns, self.finished_ns,
                                   self.x, self.y, self.compute_ms, self.state)

    @classmethod
    def unpack(cls, data: bytes) -> ControlPacket:
        values = CONTROL_STRUCT.unpack(data)
        if values[0] != b"BPC1" or values[-1] not in STATES:
            raise ValueError("Unsupported control packet")
        return cls(*values[1:])


class ErrorPublisher:
    def __init__(self, host: str, port: int, token: int, pid_cfg=None):
        self.address, self.token = (host, port), token
        self.socket = local_socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.setblocking(False)
        self.send_drops = 0
        self.latest: ControlPacket | None = None
        self.latest_arrival_ns = 0
        self.received_count = 0
        self.feedback_blocked = False
        self._settings_id = 0
        self._pending_settings = None
        self.settings_state = {"request_id": 0, "status": "ready", "message": "PID defaults loaded",
                               "values": pid_values(pid_cfg or PIDConfig())}

    def configure_pid(self, values: dict, *, reset=False) -> int:
        if self._pending_settings is not None:
            raise ValueError("A PID update is already awaiting acknowledgment")
        self._settings_id += 1
        request_id = self._settings_id
        data = settings_message(SETTINGS_MAGIC, {"token": self.token, "request_id": request_id,
                                               "values": values, "reset": reset})
        now = perf_counter_ns()
        self._pending_settings = {"data": data, "last_send_ns": 0, "deadline_ns": now+3_000_000_000}
        # Do not display/reuse commands based on the old tuning while an update
        # is pending. Images and raw recording continue without interruption.
        self.feedback_blocked = True
        self.latest = None
        self.settings_state = {**self.settings_state, "request_id": request_id, "status": "queued",
                               "message": "Waiting for the PID process to acknowledge settings"}
        self._service_settings(now)
        return request_id

    def _service_settings(self, now):
        pending = self._pending_settings
        if pending is None:
            return
        if now >= pending["deadline_ns"]:
            self.settings_state = {**self.settings_state, "status": "failed",
                "message": "PID did not acknowledge within 3 seconds. Check that the controller process is running, then retry."}
            self._pending_settings = None
        elif now-pending["last_send_ns"] >= 200_000_000:
            try:
                self.socket.sendto(pending["data"], self.address)
            except OSError:
                pass
            pending["last_send_ns"] = now

    def send(self, m: Measurement) -> None:
        if m.role != PID_SOURCE_ROLE:
            raise ValueError("Only Far Field measurements may be sent to the PID")
        packet = ErrorPacket(self.token, m.sequence, m.received_ns, m.computed_ns, m.reference_revision,
                             m.error_x_urad, m.error_y_urad, m.angular_valid and not self.feedback_blocked)
        try:
            self.socket.sendto(packet.pack(), self.address)
        except OSError:
            self.send_drops += 1

    def poll(self) -> ControlPacket | None:
        # Bounded work even if another process sends an excessive number of datagrams.
        for _ in range(64):
            try:
                data, sender = self.socket.recvfrom(8192)
            except (BlockingIOError, ConnectionResetError):
                break
            if sender != self.address:
                continue
            if data.startswith(ACK_MAGIC):
                try:
                    ack = json.loads(data[4:])
                    if (self._pending_settings is not None and ack["token"] == self.token and
                            ack["request_id"] == self.settings_state["request_id"] and type(ack["success"]) is bool):
                        self.settings_state = {"request_id": ack["request_id"],
                            "status": "applied" if ack["success"] else "failed",
                            "message": ack["message"], "values": ack["values"]}
                        self._pending_settings = None
                        self.feedback_blocked = not ack["success"]
                except (ValueError, KeyError, TypeError):
                    pass
                continue
            try:
                reply = ControlPacket.unpack(data)
            except (ValueError, struct.error):
                continue
            if reply.token == self.token and (self.latest is None or reply.sequence >= self.latest.sequence):
                self.latest, self.latest_arrival_ns = reply, perf_counter_ns()
                self.received_count += 1
        self._service_settings(perf_counter_ns())
        return self.latest

    def close(self):
        self.socket.close()
