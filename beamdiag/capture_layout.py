"""Capture layout shared by readers and post-capture reporting; legacy folders remain readable."""
from pathlib import Path

RAW_FOLDER = "raw_data"
READABLE_FOLDER = "human_readable"


def capture_paths(path):
    path = Path(path).resolve()
    if (path/RAW_FOLDER).is_dir():
        return path, path/RAW_FOLDER, path/READABLE_FOLDER
    if path.name == RAW_FOLDER and (path.parent/READABLE_FOLDER).is_dir():
        return path.parent, path, path.parent/READABLE_FOLDER
    # Older sessions used a flat folder. Readers/exporters support both layouts.
    return path, path, path/READABLE_FOLDER


def raw_directory(path):
    return capture_paths(path)[1]
