"""Acknowledgment/retry behavior must not expose old tuning as applied."""
from dataclasses import replace
from unittest.mock import Mock, patch
import unittest

from beamdiag.config import PIDConfig
from beamdiag.feedback_settings import pid_values
from beamdiag.transport import ErrorPublisher, ACK_MAGIC, settings_message, ErrorPacket
import tests.test_pid_routing as routing_fixture


class FeedbackTransportTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.sock = Mock()
        def receive(_):
            if self.received:
                return self.received.pop(0)
            raise BlockingIOError
        self.sock.recvfrom.side_effect = receive
        with patch("beamdiag.transport.local_socket", return_value=self.sock):
            self.publisher = ErrorPublisher("127.0.0.1", 49152, 42)

    def tearDown(self):
        self.publisher.close()

    def ack(self, request_id, success=True, token=42, sender=None):
        values = pid_values(replace(PIDConfig(), enabled=False))
        data = settings_message(ACK_MAGIC, {"token": token, "request_id": request_id,
            "success": success, "message": "acknowledged", "values": values})
        self.received.append((data, sender or self.publisher.address))

    def test_feedback_is_invalidated_until_a_matching_ack_and_actual_values_are_read_back(self):
        request_id = self.publisher.configure_pid(pid_values(replace(PIDConfig(), enabled=False)))
        self.assertTrue(self.publisher.feedback_blocked)
        _, _, measurement = routing_fixture.FarFieldPIDRoutingTests().measurement("far_field")
        self.publisher.send(measurement)
        wire = self.sock.sendto.call_args.args[0]
        self.assertFalse(ErrorPacket.unpack(wire).valid)
        self.ack(request_id, token=99)
        self.ack(request_id, sender=("127.0.0.1", 12345))
        self.ack(request_id+1)
        self.publisher.poll()
        self.assertTrue(self.publisher.feedback_blocked)
        self.ack(request_id)
        self.publisher.poll()
        self.assertFalse(self.publisher.feedback_blocked)
        self.assertEqual(self.publisher.settings_state["status"], "applied")
        self.assertFalse(self.publisher.settings_state["values"]["enabled"])

    def test_retries_are_identical_and_a_timeout_keeps_output_blocked(self):
        self.publisher.configure_pid(pid_values(PIDConfig()))
        pending = self.publisher._pending_settings
        original = self.sock.sendto.call_args.args[0]
        self.publisher._service_settings(pending["last_send_ns"]+201_000_000)
        self.assertEqual(self.sock.sendto.call_args.args[0], original)
        self.publisher._service_settings(pending["deadline_ns"])
        self.assertEqual(self.publisher.settings_state["status"], "failed")
        self.assertTrue(self.publisher.feedback_blocked)
        self.assertIn("3 seconds", self.publisher.settings_state["message"])
        self.assertIsNone(self.publisher._pending_settings)

    def test_rejected_settings_do_not_become_live_and_can_be_retried(self):
        request_id = self.publisher.configure_pid(pid_values(PIDConfig()))
        with self.assertRaisesRegex(ValueError, "awaiting"):
            self.publisher.configure_pid(pid_values(PIDConfig()))
        self.ack(request_id, success=False)
        self.publisher.poll()
        self.assertEqual(self.publisher.settings_state["status"], "failed")
        self.assertTrue(self.publisher.feedback_blocked)
        retry_id = self.publisher.configure_pid(pid_values(PIDConfig()))
        self.ack(retry_id)
        self.publisher.poll()
        self.assertFalse(self.publisher.feedback_blocked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
