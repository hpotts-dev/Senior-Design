"""Start the updater that ships with the newest complete portable installation."""
from pathlib import Path
import re
import subprocess
import sys


def version_key(value):
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}", value):
        raise ValueError("Expected a stable release version")
    return tuple(map(int, value.split(".")))


def runtime(root):
    return root / ".runtime" / "python.exe"


def release_version(root):
    version = (root / "VERSION").read_text(encoding="ascii").strip()
    version_key(version)
    for name in ("updater.py", "portable_launch.py", "diagnostic_app.py", "config.example.toml",
                 "pid_controller.py", "beamdiag/__init__.py", ".runtime/python.exe"):
        if not (root / name).is_file():
            raise ValueError(f"Portable release is missing {name}")
    return version


def current_release(root):
    selected = root
    newest = version_key(release_version(root))
    cache = root / ".updates"
    if not cache.is_dir():
        return selected, newest
    for candidate in cache.iterdir():
        try:
            version = version_key(candidate.name)
            if version > newest and version_key(release_version(candidate)) == version:
                selected, newest = candidate, version
        except (OSError, ValueError):
            continue
    return selected, newest


def main():
    root = Path(__file__).resolve().parent
    try:
        selected, _ = current_release(root)
    except (OSError, ValueError):
        selected = root
    return subprocess.call([str(runtime(selected)), "-X", "utf8", str(selected / "updater.py"),
                            str(root), *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
