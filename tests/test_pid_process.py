"""Exercise the actual standalone PID process and loopback wire protocol, without a camera."""
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, perf_counter_ns, sleep
import csv
import os
import socket
import struct
import subprocess
import sys
import unittest

from beamdiag.transport import ErrorPacket, ControlPacket

ROOT = Path(__file__).resolve().parents[1]


class StandalonePIDTest(unittest.TestCase):
    def test_process_freshness_watchdog_reference_and_shutdown(self):
        # Ask the OS for a free local port, then release it immediately before starting the child.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with TemporaryDirectory(dir=ROOT/"validation") as tmp:
            self.assertTrue(Path(tmp).resolve().is_relative_to(ROOT.resolve()))
            session = Path(tmp)
            config = session/"config.toml"
            config.write_text((ROOT/"config.example.toml").read_text().replace("port = 49152", f"port = {port}"))
            log_file = (session/"pid.log").open("w")
            process = subprocess.Popen([sys.executable, str(ROOT/"pid_controller.py"), "--config", str(config),
                "--session", str(session), "--token", "42", "--parent-pid", str(os.getpid())],
                stdout=log_file, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                deadline = perf_counter()+5
                while not (session/"pid_ready.json").exists():
                    self.assertIsNone(process.poll(), (session/"pid.log").read_text())
                    self.assertLess(perf_counter(), deadline, "PID did not start")
                    sleep(0.01)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.bind(("127.0.0.1", 0))
                    client.settimeout(1)
                    target = ("127.0.0.1", port)

                    def send(seq, valid=True, age_ms=0, revision=0, x=3.0):
                        stamp = perf_counter_ns()-int(age_ms*1e6)
                        client.sendto(ErrorPacket(42, seq, stamp, stamp, revision, x, -2.0, valid).pack(), target)
                        return ControlPacket.unpack(client.recvfrom(256)[0])

                    client.sendto(b"malformed", target)
                    active = send(1)
                    self.assertEqual(active.state, 1)
                    self.assertLess(active.x, 0)
                    self.assertGreater(active.y, 0)
                    invalid = send(2, valid=False)
                    self.assertEqual((invalid.state, invalid.x, invalid.y), (2, 0, 0))
                    stale = send(3, age_ms=100)
                    self.assertEqual((stale.state, stale.x, stale.y), (3, 0, 0))
                    self.assertEqual(send(4).state, 1)
                    watchdog = ControlPacket.unpack(client.recvfrom(256)[0])
                    self.assertEqual((watchdog.state, watchdog.x, watchdog.y), (4, 0, 0))
                    # Foreign-token packet cannot take over the established stream.
                    now = perf_counter_ns()
                    client.sendto(ErrorPacket(99, 1000, now, now, 0, 100, 100, True).pack(), target)
                    reference = send(5, revision=1, x=0)
                    self.assertEqual((reference.state, reference.x), (1, 0))
                    client.sendto(b"STOP"+struct.pack("!Q", 42), target)
                    stopped = ControlPacket.unpack(client.recvfrom(256)[0])
                    self.assertEqual((stopped.state, stopped.x, stopped.y), (5, 0, 0))
                self.assertEqual(process.wait(timeout=5), 0)
                with (session/"control.csv").open(newline="") as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual([int(r["state"]) for r in rows], [1, 2, 3, 1, 4, 1, 5])
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                log_file.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
