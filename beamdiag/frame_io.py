"""Lossless frame access for individual images and indexed streams of NPY records."""
from __future__ import annotations

from pathlib import Path
from functools import lru_cache
from io import BytesIO
import numpy as np
from .capture_layout import raw_directory
from .image_io import read_image


@lru_cache(maxsize=32)
def _npy_header(shape, dtype):
    header = BytesIO()
    np.lib.format.write_array_header_1_0(header, {
        "descr": np.lib.format.dtype_to_descr(dtype), "fortran_order": False, "shape": shape})
    return header.getvalue()


def _write_all(handle, data):
    remaining = memoryview(data).cast("B")
    while remaining:
        written = handle.write(remaining)
        if written is None or written <= 0 or written > len(remaining):
            raise OSError("Frame write did not make progress")
        remaining = remaining[written:]


def write_npy_record(handle, image: np.ndarray):
    """Append a standard NPY record through FileIO, without NumPy's tofile path.

    Camera buffers are already C-contiguous. Writing a memoryview avoids another
    full-frame bytes copy and keeps the same OS-cache completion semantics as the
    unbuffered stream. Short writes are completed before a record can be indexed.
    """
    if image.dtype.hasobject:
        raise ValueError("Object arrays cannot be saved without pickle")
    image = np.ascontiguousarray(image)
    _write_all(handle, _npy_header(image.shape, image.dtype))
    if image.size:
        _write_all(handle, image)


def load_saved_frame(session: str | Path, row: dict) -> np.ndarray:
    root = raw_directory(session)
    target = (root/row["path"]).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Frame index path leaves its session directory")
    if target.suffix == ".npyseq":
        with target.open("rb") as f:
            f.seek(int(row["byte_offset"]))
            return np.load(f, allow_pickle=False)
    if target.suffix == ".npy":
        return np.load(target, allow_pickle=False)
    image = read_image(target)
    if image is None:
        raise OSError(f"Cannot load frame {target}")
    return image
