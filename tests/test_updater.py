import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError
from zipfile import ZipFile

from portable_launch import current_release, version_key
import updater


def release_files(version):
    files = dict.fromkeys(("updater.py", "portable_launch.py", "diagnostic_app.py", "config.example.toml",
                            "pid_controller.py", "beamdiag/__init__.py", ".runtime/python.exe"), b"fixture")
    return dict(files, VERSION=version.encode("ascii"))


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve() / "Beam test"
        for name, payload in release_files("1.1.0").items():
            file = self.root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(payload)
        (self.root / "config.toml").write_text("# local camera settings", encoding="ascii")
        (self.root / "captures").mkdir()
        (self.root / "captures" / "prior.csv").write_text("prior data", encoding="ascii")

    def mock_release(self, *, checksum_mismatch=False, unsafe_path=False):
        data = BytesIO()
        with ZipFile(data, "w") as archive:
            for name, payload in release_files("1.2.0").items():
                archive.writestr(f"BeamDiagnostics/{name}", payload)
            # A default configuration inside the release belongs to that stage only.
            archive.writestr("BeamDiagnostics/config.toml", "new default")
            if unsafe_path:
                archive.writestr("BeamDiagnostics/../../unsafe.txt", "unsafe")
        package = data.getvalue()
        checksum = "0" * 64 if checksum_mismatch else hashlib.sha256(package).hexdigest()
        prefix = f"https://github.com/{updater.REPOSITORY}/releases/download/v1.2.0/"
        release = {"tag_name": "v1.2.0", "draft": False, "prerelease": False, "assets": [
            {"name": name, "browser_download_url": prefix + name, "size": len(package)}
            for name in (updater.ZIP_NAME, updater.CHECKSUM_NAME)
        ]}
        resources = {updater.LATEST: json.dumps(release).encode("ascii"),
                     prefix + updater.ZIP_NAME: package,
                     prefix + updater.CHECKSUM_NAME: f"{checksum}  {updater.ZIP_NAME}\n".encode("ascii")}
        return patch("updater.fetch", side_effect=lambda url, _: BytesIO(resources[url]))

    def test_installs_complete_version_and_preserves_local_files(self):
        with self.mock_release(), patch("updater.subprocess.run") as preflight:
            updater.install_update(self.root, version_key("1.1.0"))
        selected, version = current_release(self.root)
        self.assertEqual(version, (1, 2, 0))
        self.assertEqual(selected, self.root / ".updates" / "1.2.0")
        self.assertEqual((self.root / "config.toml").read_text(), "# local camera settings")
        self.assertEqual((self.root / "captures" / "prior.csv").read_text(), "prior data")
        preflight.assert_called_once()

    def test_wrong_sha256_keeps_current_release(self):
        with self.mock_release(checksum_mismatch=True), self.assertRaisesRegex(ValueError, "SHA-256"):
            updater.install_update(self.root, version_key("1.1.0"))
        self.assertEqual(list((self.root / ".updates").iterdir()), [])
        self.assertEqual(current_release(self.root)[0], self.root)

    def test_zip_traversal_never_leaves_staging(self):
        with self.mock_release(unsafe_path=True), self.assertRaisesRegex(ValueError, "Invalid path"):
            updater.install_update(self.root, version_key("1.1.0"))
        self.assertFalse((self.root / "unsafe.txt").exists())
        self.assertEqual(list((self.root / ".updates").iterdir()), [])

    def test_offline_launch_uses_current_runtime_and_config_location(self):
        opts = ["diagnostic_app.py", "--simulate", "--output", "a folder"]
        with (
            patch.dict(os.environ, {"LOCALAPPDATA": str(self.root.parent)}, clear=True),
            patch("updater.fetch", side_effect=URLError("offline")),
            patch("updater.subprocess.call", return_value=7) as launch,
        ):
            code = updater.main([str(self.root), *opts])
        self.assertEqual(code, 7)
        self.assertEqual(launch.call_args.args[0][3:], [str(self.root / opts[0]), *opts[1:]])
        self.assertEqual(launch.call_args.kwargs["env"]["BEAMDIAG_CONFIG"], str(self.root / "config.toml"))

    def test_skip_check_uses_newest_installed_release(self):
        with self.mock_release(), patch("updater.subprocess.run"):
            updater.install_update(self.root, version_key("1.1.0"))
        with (
            patch.dict(os.environ, {"BEAMDIAG_SKIP_UPDATE": "1", "BEAMDIAG_CONFIG": str(self.root / "custom.toml")}),
            patch("updater.fetch") as network,
            patch("updater.subprocess.call", return_value=0) as launch,
        ):
            updater.main([str(self.root), "-m", "beamdiag.doctor"])
        network.assert_not_called()
        target = self.root / ".updates" / "1.2.0" / ".runtime" / "python.exe"
        self.assertEqual(launch.call_args.args[0][0], str(target))
        self.assertEqual(launch.call_args.kwargs["env"]["BEAMDIAG_CONFIG"], str(self.root / "custom.toml"))

    def test_current_release_is_checked_without_downloading_again(self):
        with self.mock_release() as network, patch("updater.subprocess.run") as preflight:
            updater.install_update(self.root, version_key("1.2.0"))
        network.assert_called_once_with(updater.LATEST, "application/vnd.github+json")
        preflight.assert_not_called()
        self.assertFalse((self.root / ".updates").exists())
