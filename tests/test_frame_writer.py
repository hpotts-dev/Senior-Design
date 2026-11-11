"""NPY compatibility, short writes and recovery after interrupted frame writes."""
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from unittest.mock import patch
import csv
import unittest
import numpy as np

from beamdiag.config import AppConfig, RecordingConfig
from beamdiag.frame_io import write_npy_record, load_saved_frame
from beamdiag.model import Frame, DiagnosticFrame
from beamdiag.recording import AsyncRecorder, create_session

ROOT = Path(__file__).resolve().parents[1]


class FrameWriterTests(unittest.TestCase):
    def test_mixed_native_records_are_numpy_readable_at_exact_offsets(self):
        images = [np.arange(24, dtype=np.uint8).reshape(4, 6),
                  np.array([[0, 4095, 1024], [1, 2048, 23]], dtype=np.uint16),
                  np.array([[0, 65535]], dtype=np.uint16)]
        stream, offsets = BytesIO(), []
        for image in images:
            offsets.append(stream.tell())
            write_npy_record(stream, image)
        end = stream.tell()
        for offset, expected in zip(offsets, images):
            stream.seek(offset)
            actual = np.load(stream, allow_pickle=False)
            self.assertEqual(actual.dtype, expected.dtype)
            np.testing.assert_array_equal(actual, expected)
        self.assertEqual(stream.tell(), end)

    def test_strided_fortran_and_big_endian_arrays_preserve_pixels(self):
        base = np.arange(48, dtype=np.uint16).reshape(6, 8)
        for image in (base[:, ::2], np.asfortranarray(base), base.astype('>u2')):
            with self.subTest(dtype=image.dtype, strides=image.strides):
                stream = BytesIO()
                write_npy_record(stream, image)
                stream.seek(0)
                actual = np.load(stream, allow_pickle=False)
                self.assertEqual(actual.dtype, image.dtype)
                np.testing.assert_array_equal(actual, image)

    def test_short_writes_are_completed_and_zero_writes_raise(self):
        class ShortWriter(BytesIO):
            def write(self, data):
                return super().write(data[:7])
        image = np.arange(100, dtype=np.uint16).reshape(10, 10)
        stream = ShortWriter()
        write_npy_record(stream, image)
        stream.seek(0)
        np.testing.assert_array_equal(np.load(stream, allow_pickle=False), image)
        with self.assertRaises(OSError):
            write_npy_record(type('StuckWriter', (), {'write': lambda self, data: 0})(), image)

    def test_object_arrays_are_rejected_before_any_bytes_are_written(self):
        stream = BytesIO()
        with self.assertRaises(ValueError):
            write_npy_record(stream, np.array([object()], dtype=object))
        self.assertEqual(stream.getvalue(), b'')

    def test_recorder_rotates_stream_and_does_not_index_a_partial_record(self):
        with TemporaryDirectory(dir=ROOT/'validation') as folder:
            cfg = replace(AppConfig(), recording=RecordingConfig(output_dir=folder))
            session = create_session(cfg, Path(folder), False)
            recorder = AsyncRecorder(cfg, session)
            first = True
            image = np.arange(64, dtype=np.uint16).reshape(8, 8)

            def interrupted(handle, data):
                nonlocal first
                if first:
                    first = False
                    handle.write(b'incomplete')
                    raise OSError('Injected interrupted frame write')
                write_npy_record(handle, data)

            with self.assertLogs('beamdiag.recording', level='ERROR'), \
                    patch('beamdiag.recording.write_npy_record', side_effect=interrupted):
                recorder.start()
                for sequence in (1, 2):
                    recorder.capture(DiagnosticFrame(Frame('far_field', sequence,
                        perf_counter_ns(), sequence, image), None), 'record')
                result = recorder.close()
            with (session/'frame_index.csv').open(newline='', encoding='utf-8') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(result['write_errors'], 1)
            self.assertTrue(result['drain_complete'])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['sequence'], '2')
            self.assertTrue(rows[0]['path'].endswith('stream_00001.npyseq'))
            self.assertEqual(rows[0]['byte_offset'], '0')
            np.testing.assert_array_equal(load_saved_frame(session, rows[0]), image)


if __name__ == '__main__':
    unittest.main()
