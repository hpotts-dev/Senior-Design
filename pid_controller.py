"""Standalone nominal PID process. Run directly, or let diagnostic_app.py launch it."""
from __future__ import annotations

import argparse
import csv
from dataclasses import fields
from pathlib import Path
from queue import Queue, Empty, Full
from threading import Thread, Event
from time import perf_counter_ns
import json
import logging
import select
import signal
import struct

from beamdiag.config import load_config, PID_SOURCE_ROLE
from beamdiag.pid import NominalController, PIDResult
from beamdiag.parent_watch import ParentWatch
from beamdiag.transport import ErrorPacket, ControlPacket, local_socket
from beamdiag.transport import SETTINGS_MAGIC, ACK_MAGIC, settings_message
from beamdiag.feedback_settings import pid_values, with_pid_settings
from beamdiag.windows_paths import default_config_path
from beamdiag.live_output import CaptureClock, finite, write_pid_equation
from beamdiag.transport import STATES

log = logging.getLogger("pid")


class ControlLog:
    """Slow storage never holds the PID loop. Overflow is counted in the final summary."""
    def __init__(self, session: Path, initial_pid=None):
        self.session = session
        self.queue: Queue = Queue(4096)
        self.stop = Event()
        self.drops = 0
        self.error = ""
        self.thread = Thread(target=self.run, name="control-log", daemon=True)
        self.initial_pid = initial_pid

    def run(self):
        try:
            output = self.session.parent/"human_readable"
            output.mkdir(exist_ok=True)
            clock = CaptureClock(self.session) if (self.session/"session.json").exists() else None
            with (self.session/"control.csv").open("w", newline="", encoding="utf-8") as f, \
                    (self.session/"pid_settings_changes.jsonl").open("w", encoding="utf-8") as settings_log, \
                    (output/"pid_feedback.csv").open("w", newline="", encoding="utf-8-sig") as readable, \
                    (output/"pid_settings.csv").open("w", newline="", encoding="utf-8-sig") as readable_settings:
                writer = csv.writer(f)
                writer.writerow([v.name for v in fields(ControlPacket)]+["host_to_pid_ms"])
                human = csv.writer(readable)
                human.writerow(["Far Field Capture Time", "Far Field frame", "PID state", "Nominal correction X (normalized)",
                    "Nominal correction Y (normalized)", "Pointing error X (urad)", "Pointing error Y (urad)",
                    "Frame retrieval to PID reply (ms)", "PID calculation time (ms)"])
                human_settings = csv.writer(readable_settings)
                human_settings.writerow(["Capture time", "Outcome", "Applied PID values", "Message"])
                if self.initial_pid is not None:
                    human_settings.writerow(["Startup", "Applied", json.dumps(pid_values(self.initial_pid)), "Startup configuration"])
                    write_pid_equation(output, pid_values(self.initial_pid))
                f.flush()
                readable.flush()
                readable_settings.flush()
                while not self.stop.is_set() or not self.queue.empty():
                    try:
                        packet = self.queue.get(timeout=0.05)
                    except Empty:
                        continue
                    if isinstance(packet, dict):
                        settings_log.write(json.dumps(packet, allow_nan=False)+"\n")
                        settings_log.flush()
                        human_settings.writerow([clock.time(packet["host_ns"])[0] if clock else packet["host_ns"],
                            "Applied" if packet["success"] else "Rejected", json.dumps(packet["values"]), packet["message"]])
                        readable_settings.flush()
                        if packet["success"]:
                            write_pid_equation(output, packet["values"])
                    else:
                        sample = None
                        if isinstance(packet, tuple):
                            packet, sample = packet
                        writer.writerow([getattr(packet, v.name) for v in fields(ControlPacket)]+
                                        [(packet.finished_ns-packet.received_ns)/1e6])
                        f.flush()
                        human.writerow([clock.time(packet.received_ns)[0] if clock else packet.received_ns, packet.sequence,
                            STATES[packet.state].title(), packet.x, packet.y,
                            finite(sample.x_urad) if sample and sample.valid else "", finite(sample.y_urad) if sample and sample.valid else "",
                            (packet.finished_ns-packet.received_ns)/1e6, packet.compute_ms])
                        readable.flush()
                    self.queue.task_done()
        except Exception as exc:
            self.error = str(exc)
            log.exception("Control log failed")

    def submit(self, packet: ControlPacket):
        try:
            self.queue.put_nowait(packet)
        except Full:
            self.drops += 1

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        (self.session/"control_summary.json").write_text(json.dumps({"log_drops": self.drops,
            "log_error": self.error, "drain_complete": not self.thread.is_alive() and self.queue.empty()}, indent=2), encoding="utf-8")


def serve(config_path: Path, session: Path, expected_token: int | None = None,
          idle_exit_s: float = 0.0, parent_pid: int | None = None) -> int:
    cfg = load_config(config_path)
    source = next(c for c in cfg.cameras if c.role == PID_SOURCE_ROLE)
    controller = NominalController(cfg.pid, 1/source.fps)
    parent = ParentWatch(parent_pid)
    sock = local_socket()
    # No address reuse: a second controller must fail instead of sharing a stream.
    sock.bind((cfg.pid.host, cfg.pid.port))
    sock.setblocking(False)
    session.mkdir(parents=True, exist_ok=True)
    control_log = ControlLog(session, cfg.pid)
    control_log.thread.start()
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    # A loopback-only parent stop command supports graceful Windows subprocess shutdown.
    token = expected_token
    peer = None
    last_sample = None
    last_settings_id, last_settings_ack = 0, None
    started = last_packet_ns = perf_counter_ns()
    (session/"pid_ready.json").write_text(json.dumps({"pid_port": cfg.pid.port, "nominal_only": True,
        "token": expected_token, "started_ns": started}), encoding="utf-8")

    def emit(result: PIDResult, sample: ErrorPacket, started_ns: int):
        packet = ControlPacket(sample.token, sample.sequence, sample.received_ns, perf_counter_ns(),
                               result.x, result.y, (perf_counter_ns()-started_ns)/1e6, result.state)
        try:
            sock.sendto(packet.pack(), peer)
        except OSError:
            pass
        control_log.submit((packet, sample))

    try:
        while not stop.is_set() and parent.alive():
            select.select([sock], [], [], 0.01)
            newest = None
            for _ in range(128):
                try:
                    data, sender = sock.recvfrom(8192)
                except (BlockingIOError, ConnectionResetError):
                    break
                if (expected_token is not None and data == b"STOP"+struct.pack("!Q", expected_token)
                        and sender[0] == "127.0.0.1"):
                    stop.set()
                    break
                if data.startswith(SETTINGS_MAGIC):
                    try:
                        request = json.loads(data[4:])
                        request_id = request["request_id"]
                        # A standalone listener can only be configured by its
                        # established stream. Auto-launched children know the token.
                        if (token is None or request["token"] != token or
                                (peer is not None and sender != peer) or
                                type(request_id) is not int or request_id <= 0):
                            continue
                        if peer is None:
                            peer = sender
                        if request_id == last_settings_id:
                            sock.sendto(last_settings_ack, sender)
                            continue
                        if request_id < last_settings_id:
                            continue
                        try:
                            if type(request.get("reset", False)) is not bool:
                                raise ValueError("Reset must be true/false")
                            updated = with_pid_settings(controller.cfg, request["values"])
                            controller.reconfigure(updated)
                            success, message = True, "PID settings applied; integral and derivative history reset"
                            if last_sample is not None:
                                emit(PIDResult(0.0, 0.0, 7 if updated.enabled else 6), last_sample, perf_counter_ns())
                        except (ValueError, TypeError, KeyError) as exc:
                            success, message = False, str(exc)
                        ack = {"token": token, "request_id": request_id, "success": success,
                               "message": message, "values": pid_values(controller.cfg)}
                        last_settings_id, last_settings_ack = request_id, settings_message(ACK_MAGIC, ack)
                        control_log.submit({**ack, "host_ns": perf_counter_ns(),
                                            "source_sequence": last_sample.sequence if last_sample else None})
                        sock.sendto(last_settings_ack, sender)
                    except (ValueError, KeyError, TypeError, OSError):
                        pass
                    continue
                try:
                    sample = ErrorPacket.unpack(data)
                except (ValueError, struct.error):
                    continue
                if token is None:
                    token, peer = sample.token, sender
                elif peer is None and sample.token == token:
                    peer = sender
                if sample.token != token or sender != peer:
                    continue
                if newest is None or sample.sequence > newest.sequence:
                    newest = sample
            if newest is not None and not stop.is_set():
                tick = perf_counter_ns()
                result = controller.update(newest, tick)
                if result is not None:
                    last_packet_ns = tick
                    last_sample = newest
                    emit(result, newest, tick)
            tick = perf_counter_ns()
            result = controller.watchdog(tick)
            if result is not None and last_sample is not None:
                emit(result, last_sample, tick)
            if idle_exit_s and tick-last_packet_ns > idle_exit_s*1e9:
                break
    finally:
        if last_sample is not None:
            emit(PIDResult(0.0, 0.0, 5), last_sample, perf_counter_ns())
        sock.close()
        parent.close()
        control_log.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config_path(Path(__file__).resolve().parent))
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--token", type=int)
    parser.add_argument("--idle-exit", type=float, default=0)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return serve(args.config.resolve(), args.session.resolve(), args.token, args.idle_exit, args.parent_pid)
    except (ValueError, OSError) as exc:
        log.error("PID could not start: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
