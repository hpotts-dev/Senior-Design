"""Export indexed raw captures to individual NPY or PNG files, without changing the session."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beamdiag.frame_io import load_saved_frame
from beamdiag.capture_layout import raw_directory
from beamdiag.image_io import write_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--format", choices=("npy", "png"), default="npy")
    parser.add_argument("--role", choices=("far_field", "dark_field", "near_field"))
    parser.add_argument("--limit", type=int, default=0, help="0 exports all indexed frames")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    count = 0
    with (raw_directory(args.session)/"frame_index.csv").open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if args.role and row["role"] != args.role:
                continue
            image = load_saved_frame(args.session, row)
            folder = args.output/row["role"]
            folder.mkdir(parents=True, exist_ok=True)
            target = folder/f"{int(row['sequence']):010d}_{row['received_ns']}_{row['kind']}_{count:08d}.{args.format}"
            if target.exists():
                raise FileExistsError(f"Refusing to overwrite {target}")
            if args.format == "npy":
                np.save(target, image, allow_pickle=False)
            elif not write_png(target, image):
                raise OSError(f"Could not write {target}")
            count += 1
            if args.limit and count >= args.limit:
                break
    print(f"Exported {count} frames to {args.output.resolve()}")


if __name__ == "__main__":
    main()
