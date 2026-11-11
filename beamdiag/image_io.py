"""Use Python's Unicode file APIs so PNG paths work across Windows usernames and drives."""
from pathlib import Path
import cv2
import numpy as np


def write_png(path, image):
    okay, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not okay:
        raise OSError(f"PNG encoding failed: {path}")
    with Path(path).open("wb") as stream:
        stream.write(memoryview(encoded))
    return True


def read_image(path):
    with Path(path).open("rb") as stream:
        encoded = np.frombuffer(stream.read(), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise OSError(f"Cannot decode image: {path}")
    return image
