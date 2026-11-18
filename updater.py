"""Download, verify, and activate stable Windows releases from GitHub."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

from beamdiag.windows_paths import default_config_path
from portable_launch import current_release, release_version, runtime, version_key


REPOSITORY = "hpotts-dev/Senior-Design"
LATEST = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ZIP_NAME = "BeamDiagnostics-Windows-x64.zip"
CHECKSUM_NAME = f"{ZIP_NAME}.sha256"
DOWNLOAD_TIMEOUT = 180
MAX_ZIP_BYTES = 1024**3
MAX_EXPANDED_BYTES = 4 * 1024**3
USER_AGENT = "BeamDiagnostics-Windows-Updater"


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urlsplit(newurl)
        if (destination.scheme != "https" or destination.hostname not in
                {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}):
            raise URLError("Unexpected GitHub redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, accept):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    return build_opener(GitHubRedirects()).open(request, timeout=5)


def read_small(url, limit, accept="application/octet-stream"):
    with fetch(url, accept) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError("GitHub response is larger than expected")
    return content


def release_asset(release, tag, name):
    expected = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    matches = [asset for asset in release["assets"] if asset["name"] == name]
    if len(matches) != 1 or matches[0]["browser_download_url"] != expected:
        raise ValueError(f"Release {tag} needs a GitHub download for {name}")
    return matches[0], expected


def download_zip(url, path, size, checksum):
    deadline = monotonic() + DOWNLOAD_TIMEOUT
    digest = sha256()
    total = 0
    with fetch(url, "application/octet-stream") as response, path.open("xb") as stream:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > size or monotonic() > deadline:
                raise ValueError("Release download exceeded its declared size or timed out")
            digest.update(chunk)
            stream.write(chunk)
    if total != size or digest.hexdigest() != checksum:
        raise ValueError("Release ZIP failed SHA-256 or size verification")


def extract_release(path, folder):
    with ZipFile(path) as archive:
        if sum(member.file_size for member in archive.infolist()) > MAX_EXPANDED_BYTES:
            raise ValueError("Release ZIP expands beyond the portable package size limit")
        for member in archive.infolist():
            parts = member.filename.rstrip("/").split("/")
            if (parts[0] != "BeamDiagnostics"
                    or any(part in {"", ".", ".."} or part.endswith((".", " "))
                           or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                           or PureWindowsPath(part).is_reserved() for part in parts)
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError(f"Invalid path in release ZIP: {member.filename}")
            target = folder.joinpath(*parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    return folder / "BeamDiagnostics"


def install_update(root, current):
    release = json.loads(read_small(LATEST, 1024 * 1024, "application/vnd.github+json"))
    tag = release["tag_name"]
    if release["draft"] or release["prerelease"] or not tag.startswith("v"):
        return
    version = tag[1:]
    if version_key(version) <= current:
        return
    asset, zip_url = release_asset(release, tag, ZIP_NAME)
    _, checksum_url = release_asset(release, tag, CHECKSUM_NAME)
    manifest = read_small(checksum_url, 1024).decode("ascii").strip()
    match = re.fullmatch(r"([0-9a-fA-F]{64})  " + re.escape(ZIP_NAME), manifest)
    if not match:
        raise ValueError("Release checksum file has an invalid format")
    checksum = match[1].lower()
    digest = asset.get("digest")
    if digest and digest != f"sha256:{checksum}":
        raise ValueError("GitHub asset digest disagrees with the published checksum")
    size = asset["size"]
    if type(size) is not int or not 0 < size <= MAX_ZIP_BYTES:
        raise ValueError("Release ZIP size is outside the supported range")

    print(f"Installing Beam Diagnostics {tag}…", file=sys.stderr, flush=True)
    cache = root / ".updates"
    cache.mkdir(exist_ok=True)
    target = cache / version
    with TemporaryDirectory(prefix="staging-", dir=cache) as work:
        folder = Path(work)
        path = folder / ZIP_NAME
        download_zip(zip_url, path, size, checksum)
        staged = extract_release(path, folder)
        if release_version(staged) != version:
            raise ValueError("Release version does not match the GitHub tag")
        subprocess.run([str(runtime(staged)), "-B", "-c",
                        "import cv2, numpy, PySide6.QtWidgets, pypylon.pylon, updater"],
                       cwd=staged, check=True, timeout=30, capture_output=True)
        try:
            staged.rename(target)
        except OSError:
            # Another launcher can install the same validated version concurrently.
            if not target.is_dir() or release_version(target) != version:
                raise
    print(f"Beam Diagnostics {tag} is ready.", file=sys.stderr, flush=True)


def main(args):
    root = Path(args[0]).resolve()
    selected, current = current_release(root)
    if os.environ.get("BEAMDIAG_SKIP_UPDATE") != "1":
        try:
            install_update(root, current)
            selected, _ = current_release(root)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError,
                BadZipFile, subprocess.SubprocessError) as exc:
            print(f"Update unavailable. Continuing with the installed version. {exc}", file=sys.stderr)

    # Camera configuration and its relative capture/background paths belong to the original installation.
    env = dict(os.environ, BEAMDIAG_CONFIG=str(default_config_path(root)))
    entry, *options = args[1:]
    if entry == "diagnostic_app.py":
        entry = str(selected / entry)
    return subprocess.call([str(runtime(selected)), "-X", "utf8", entry, *options], env=env)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
