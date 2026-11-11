"""Capture a full-sensor survey to locate beams before selecting a low-latency hardware ROI."""
from __future__ import annotations

import os
for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(key, "1")
import argparse
from dataclasses import replace
from pathlib import Path
import sys
from threading import Event
from time import perf_counter
import json
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beamdiag.cameras import BaslerCamera, discover_basler
from beamdiag.config import load_config
from beamdiag.image_io import write_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT/"validation"/"hardware_survey")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cv2.setNumThreads(1)
    from pypylon import pylon
    factory = pylon.TlFactory.GetInstance()
    devices = {d.GetSerialNumber(): d for d in factory.EnumerateDevices()}
    report = {"devices": discover_basler(), "cameras": {}}
    for cfg in load_config(ROOT/"config.toml").cameras:
        probe = pylon.InstantCamera(factory.CreateDevice(devices[cfg.serial]))
        try:
            probe.Open()
            width, height = int(probe.Width.Max), int(probe.Height.Max)
            link = int(probe.DeviceLinkSpeed.Value)
        finally:
            probe.Close()
        source = BaslerCamera(replace(cfg, width=width, height=height, offset_x=0, offset_y=0,
                                      fps=5, grab_timeout_ms=500))
        try:
            source.open()
            frame = None
            deadline = perf_counter()+5
            while (frame is None or frame.sequence < 2) and perf_counter() < deadline:
                candidate = source.read(Event())
                if candidate is not None:
                    frame = candidate
            if frame is None:
                raise RuntimeError(f"No full-sensor frame from {cfg.role}")
            image = frame.image
            np.save(args.output/f"{cfg.role}_full.npy", image, allow_pickle=False)
            write_png(args.output/f"{cfg.role}_full.png", image)
            mask = cv2.compare(image, cfg.background+cfg.threshold, cv2.CMP_GT)
            n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
            components = []
            for label in range(1, n):
                x, y, w, h, area = (int(v) for v in stats[label])
                if area < 8:
                    continue
                selection = labels[y:y+h, x:x+w] == label
                weights = np.where(selection, np.maximum(image[y:y+h, x:x+w].astype(np.float32)-cfg.background, 0), 0)
                m = cv2.moments(weights)
                components.append({"x": x, "y": y, "width": w, "height": h, "area": area,
                    "flux": m["m00"], "centroid_x": x+m["m10"]/m["m00"],
                    "centroid_y": y+m["m01"]/m["m00"]})
            report["cameras"][cfg.role] = {"serial": cfg.serial, "link_speed_bps": link,
                "width": width, "height": height, "peak": int(image.max()),
                "saturated_fraction": float(np.count_nonzero(image == 255)/image.size),
                "components": sorted(components, key=lambda c: c["flux"], reverse=True)[:10],
                "settings": source.info}
        finally:
            source.close()
    (args.output/"survey.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({r: {k: v for k, v in d.items() if k != "settings"} for r, d in report["cameras"].items()}, indent=2))


if __name__ == "__main__":
    main()
