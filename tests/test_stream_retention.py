"""Regressions for full-size frames failing the SDK's default receive deadline."""
import unittest
from unittest.mock import Mock, patch

from beamdiag.cameras import BaslerCamera
from beamdiag.config import CameraConfig


class StreamRetentionTests(unittest.TestCase):
    def source(self, throughput=11000000, link=100000000, retention=300):
        source = BaslerCamera(CameraConfig('far_field'))
        source.info = {'payload_bytes': 1456*1088*2, 'throughput_limit_bps': throughput,
                       'device_link_speed_bps': link}
        node = Mock()
        node.GetValue.return_value = retention
        node.GetMin.return_value = 1
        node.GetMax.return_value = 10000
        node.GetInc.return_value = 1
        node.SetValue.side_effect = lambda value: setattr(node.GetValue, 'return_value', value)
        source.camera = Mock()
        source.camera.GetStreamGrabberNodeMap.return_value.GetNode.return_value = node
        return source, node

    @patch('pypylon.genicam.IsWritable', return_value=True)
    @patch('pypylon.genicam.IsReadable', return_value=True)
    def test_full_frame_on_100mbit_link_gets_time_to_arrive(self, *mocks):
        source, node = self.source()
        source._configure_stream_retention()
        self.assertGreater(node.GetValue(), 600)
        self.assertGreater(node.GetValue(), 2*source.info['stream_grabber']['estimated_payload_transfer_ms'])
        self.assertEqual(source.info['stream_grabber']['frame_retention_ms'], node.GetValue())

    @patch('pypylon.genicam.IsWritable', return_value=True)
    @patch('pypylon.genicam.IsReadable', return_value=True)
    def test_actual_link_limits_transfer_even_if_requested_throughput_is_higher(self, *mocks):
        source, node = self.source(throughput=40000000)
        source._configure_stream_retention()
        self.assertGreater(node.GetValue(), 600)

    @patch('pypylon.genicam.IsReadable', return_value=True)
    def test_fast_link_and_existing_longer_deadlines_are_preserved(self, *mocks):
        for throughput, link, retention in ((40000000, 1000000000, 300), (11000000, 100000000, 1200)):
            with self.subTest(retention=retention):
                source, node = self.source(throughput, link, retention)
                source._configure_stream_retention()
                node.SetValue.assert_not_called()
                self.assertEqual(node.GetValue(), retention)

    @patch('pypylon.genicam.IsWritable', return_value=False)
    @patch('pypylon.genicam.IsReadable', return_value=True)
    def test_short_readonly_deadline_reports_the_problem(self, *mocks):
        source, node = self.source()
        with self.assertRaisesRegex(RuntimeError, 'FrameRetention'):
            source._configure_stream_retention()


if __name__ == '__main__':
    unittest.main()
