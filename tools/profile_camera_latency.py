"""Measure exposure-to-host age using a latched camera clock; does not save frames."""
from __future__ import annotations
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(name, '1')
import argparse
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import perf_counter, perf_counter_ns
import json
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beamdiag.cameras import BaslerCamera, discover_basler
from beamdiag.config import load_config
from beamdiag.timing import percentiles


def clock_offset(camera, tick_hz):
    from pypylon import genicam
    nodes = camera.GetNodeMap()
    command = next((node for name in ('GevTimestampControlLatch','TimestampLatch')
                    if (node := nodes.GetNode(name)) is not None and genicam.IsWritable(node)), None)
    value = next((node for name in ('GevTimestampValue','TimestampLatchValue')
                  if (node := nodes.GetNode(name)) is not None and genicam.IsReadable(node)), None)
    if command is None or value is None:
        return None
    estimates = []
    for _ in range(5):
        before = perf_counter_ns()
        command.Execute()
        stamp = int(value.GetValue())
        after = perf_counter_ns()
        estimates.append((after-before, (before+after)/2-stamp*1e9/tick_hz))
    return min(estimates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=4)
    parser.add_argument('--fast', action='store_true')
    parser.add_argument('--packed', action='store_true')
    parser.add_argument('--role', choices=('far_field','dark_field','near_field'))
    parser.add_argument('--fps', type=float)
    parser.add_argument('--throughput-bps', type=int)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from pypylon import genicam
    cfg = load_config(ROOT/'config.toml')
    discover_basler()
    report = {}
    for settings in cfg.cameras:
        if args.role and settings.role != args.role:
            continue
        if args.fast:
            settings = replace(settings, throughput_limit_bps=100000000)
        if args.throughput_bps is not None:
            settings = replace(settings, throughput_limit_bps=args.throughput_bps)
        if args.fps is not None:
            settings = replace(settings, fps=args.fps)
        source = BaslerCamera(settings)
        try:
            source.open()
            if args.packed:
                from pypylon import pylon
                source.camera.StopGrabbing()
                source._set('PixelFormat', 'Mono12p', required=True)
                source.info['payload_bytes'] = source._read_node('PayloadSize')
                source._configure_stream_retention()
                source.camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
            nodes = source.camera.GetNodeMap()
            features = {}
            for name in ('GevSCPD','GevSCBWR','GevSCBWRA','GevSCFTD','GevSCDCT','GevSCDMT', 'ResultingFrameRate',
                         'DeviceLinkSpeed','DeviceLinkThroughputLimit','PayloadSize','ExposureTime'):
                node = nodes.GetNode(name)
                if node is not None and genicam.IsReadable(node):
                    features[name] = node.GetValue()
            formats = nodes.GetNode('PixelFormat').GetSymbolics()
            timing = clock_offset(source.camera, source.info.get('timestamp_tick_frequency_hz') or 1e9)
            samples, ages, received, device = [], [], [], []
            started = perf_counter()
            while perf_counter()-started < args.duration:
                before = perf_counter_ns()
                frame = source.read(Event())
                if frame is None:
                    continue
                assert frame.image.shape == (settings.height, settings.width)
                assert frame.image.dtype == np.uint16 and int(frame.image.max()) <= 4095
                samples.append((perf_counter_ns()-before)/1e6)
                received.append(frame.received_ns)
                device.append(frame.camera_timestamp)
                if timing:
                    ages.append((frame.received_ns-(frame.camera_timestamp*1e9/source.info['timestamp_tick_frequency_hz']+timing[1]))/1e6)
            report[settings.role] = {'features':features,'pixel_formats':list(formats), 'frames':len(received),
                'camera_to_host_ms':percentiles(ages), 'clock_latch_rtt_ms':timing[0]/1e6 if timing else None,
                'fps':(len(received)-1)*1e9/(received[-1]-received[0]) if len(received)>1 else 0,
                'device_interval_ms':percentiles(np.diff(device)*1000/source.info['timestamp_tick_frequency_hz']),
                'host_interval_ms':percentiles(np.diff(received)/1e6)}
        finally:
            source.close()
        report[settings.role]['stream_statistics'] = source.info.get('final_stream_statistics')
        print(json.dumps({settings.role:report[settings.role]}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
