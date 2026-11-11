"""Run the real separate PID process while changing live configuration."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, perf_counter_ns, sleep
import json
import socket
import unittest

from beamdiag.config import load_config
from beamdiag.feedback_settings import pid_values
from beamdiag.model import Measurement
from beamdiag.runtime import DiagnosticEngine
from beamdiag.transport import SETTINGS_MAGIC, settings_message

ROOT = Path(__file__).resolve().parents[1]


class FeedbackProcessTests(unittest.TestCase):
    def test_tune_pause_resume_reset_reject_and_duplicate_ack(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with TemporaryDirectory(dir=ROOT/"validation") as tmp:
            folder = Path(tmp)
            config = folder/"config.toml"
            config.write_text((ROOT/"config.example.toml").read_text().replace("port = 49152", f"port = {port}"))
            cfg = load_config(config)
            cfg = replace(cfg, recording=replace(cfg.recording, output_dir=str(folder/"runs")))
            engine = DiagnosticEngine(cfg, config, ROOT, simulated=True)
            # Use deterministic Far Field error packets to verify actual gain
            # changes; workers are not started and no camera is accessed.
            seq = 0
            def sample():
                nonlocal seq
                seq += 1
                stamp = perf_counter_ns()
                return Measurement("far_field", seq, stamp, stamp, 1, 1, 0, 0, 1, 1,
                    2, -3, 100, 100, 0, True, True, "ok", 0, 0)

            def wait_for(predicate):
                deadline = perf_counter()+4
                while perf_counter() < deadline:
                    engine.publisher.send(sample())
                    sleep(.005)
                    control = engine.poll_control()
                    if predicate(control):
                        return control
                self.fail(f"PID did not reach expected state: {engine.publisher.settings_state}")

            # Start only the PID with the same application token/session contract.
            import subprocess, sys, os
            log_file = (engine.session/"test_pid.log").open("w")
            process = subprocess.Popen([sys.executable, str(ROOT/"pid_controller.py"), "--config", str(config),
                "--session", str(engine.session), "--token", str(engine.token), "--parent-pid", str(os.getpid())],
                stdout=log_file, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            engine._pid = process
            try:
                wait_for(lambda c: c["state"] == "active")
                request_id = engine.queue_pid_settings({"kp": (.1, .2), "ki": (0, 0), "kd": (0, 0),
                                                       "slew_per_s": 1000})
                original = engine.publisher._pending_settings["data"]
                active = wait_for(lambda c: c["state"] == "active" and engine.cfg.pid.kp == (.1, .2))
                self.assertAlmostEqual(active["x"], -.2)
                self.assertAlmostEqual(active["y"], .6)
                engine.publisher.socket.sendto(original, engine.publisher.address)  # retry after applied
                engine.queue_pid_settings({"enabled": False})
                paused = wait_for(lambda c: c["state"] == "paused")
                self.assertEqual((paused["x"], paused["y"]), (0, 0))
                self.assertFalse(engine.cfg.pid.enabled)
                engine.queue_pid_settings({"enabled": True})
                wait_for(lambda c: c["state"] == "active")
                # A different peer cannot reconfigure the established controller,
                # even when it knows the stream token and a higher request ID.
                forged = settings_message(SETTINGS_MAGIC, {"token": engine.token, "request_id": 9999,
                    "values": {"enabled": False}, "reset": False})
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as intruder:
                    intruder.sendto(forged, engine.publisher.address)
                self.assertEqual(wait_for(lambda c: c["state"] == "active")["state"], "active")
                engine.queue_pid_settings({}, reset=True)
                wait_for(lambda c: c["state"] == "active" and engine.publisher.settings_state["status"] == "applied")
                # Bypass parent validation to verify rejection in the child, too.
                engine.publisher.configure_pid({**pid_values(engine.cfg.pid), "output_limit": 2})
                wait_for(lambda c: c["state"] == "settings failed")
                self.assertEqual(engine.cfg.pid.output_limit, 1)
                self.assertTrue(engine.publisher.feedback_blocked)
                engine.queue_pid_settings({"output_limit": .25})
                recovered = wait_for(lambda c: c["state"] == "active" and engine.cfg.pid.output_limit == .25)
                self.assertAlmostEqual(recovered["y"], .25)
            finally:
                engine.close()
                log_file.close()
            self.assertEqual(process.returncode, 0)
            entries = [json.loads(line) for line in (engine.session/"pid_settings_changes.jsonl").read_text().splitlines()]
            self.assertEqual(len(entries), 6)  # retransmit was acknowledged without a second reset or log
            self.assertEqual([e["success"] for e in entries], [True, True, True, True, False, True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
