"""Portable environments, writable data/config paths and safe per-PC camera assignment."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import unittest
import zipfile
import numpy as np

import bootstrap
from beamdiag.config import AppConfig, load_config
from beamdiag.windows_paths import (assign_cameras, default_config_path, ensure_config,
                                    resolve_recording_path, save_config, user_data_directory)
from tools.build_windows_package import build_package
import tests.test_reference_ui as reference_ui

ROOT = Path(__file__).resolve().parents[1]


class WindowsPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir=ROOT/"validation")
        self.root = Path(self.tmp.name)
        self.local = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root/"Local App Data")})
        self.local.start()

    def tearDown(self):
        self.local.stop()
        self.tmp.cleanup()

    def test_runtime_architecture_version_and_free_threading_checks(self):
        good = {"version": [3, 12], "bits": 64, "platform": "win-amd64"}
        self.assertTrue(bootstrap.supported(good))
        for change in ({"bits": 32}, {"version": [3, 10]}, {"version": [3, 15]},
                       {"platform": "win-arm64"}, {"free_threaded": True}):
            self.assertFalse(bootstrap.supported({**good, **change}))

    def test_unusable_copied_environment_is_preserved_before_recreation(self):
        old = self.root/".venv"
        old.mkdir()
        (old/"old-installation.txt").write_text("belongs to another PC")
        good = {"version": [3, 12], "bits": 64, "platform": "win-amd64"}
        with patch.object(bootstrap, "runtime_info", return_value=good), patch.object(bootstrap.subprocess, "run") as run:
            result = bootstrap.prepare_environment(self.root)
        backups = list(self.root.glob(".venv.backup_*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0]/"old-installation.txt").exists())
        self.assertEqual(result, bootstrap.environment_python(self.root))
        self.assertIn("venv", run.call_args.args[0])

    def test_readonly_application_uses_per_user_environment_and_configuration(self):
        with patch("bootstrap.writable_directory", return_value=False):
            python = bootstrap.environment_python(self.root)
        self.assertTrue(python.is_relative_to(user_data_directory()/"environments"))
        # Even a bundled config must fall back before the first live edit, not
        # only after a per-user copy happens to exist.
        save_config(AppConfig(), self.root/"config.toml")
        with patch("beamdiag.windows_paths.writable_directory", return_value=False):
            path = default_config_path(self.root)
        self.assertEqual(path, user_data_directory()/"config.toml")

    def test_config_roundtrip_unicode_paths_and_generic_defaults(self):
        path = self.root/"Team & optics (移動)"/"settings.toml"
        cfg = replace(AppConfig(), recording=replace(AppConfig().recording, output_dir="data\\beam results ä"))
        save_config(cfg, path)
        self.assertEqual(load_config(path), cfg)
        generated = ensure_config(self.root/"new.toml", self.root)
        self.assertTrue(all(not c.serial and not c.calibration_confirmed for c in load_config(generated).cameras))

    def test_output_path_is_relative_to_config_and_explicit_unwritable_does_not_redirect(self):
        cfg = AppConfig()
        config = self.root/"profiles"/"config.toml"
        resolved = resolve_recording_path(cfg, config)
        self.assertEqual(Path(resolved.recording.output_dir), config.parent/"captures")
        def writable(path, create=False):
            return Path(path).is_relative_to(user_data_directory())
        with patch("beamdiag.windows_paths.writable_directory", side_effect=writable):
            fallback = resolve_recording_path(cfg, config)
            self.assertEqual(Path(fallback.recording.output_dir), user_data_directory()/"captures")
            with self.assertRaises(PermissionError):
                resolve_recording_path(cfg, config, self.root/"explicit")

    def test_camera_replacement_clears_optical_confirmation_and_never_reuses_a_camera(self):
        cfg = AppConfig()
        cfg = replace(cfg, cameras=tuple(replace(c, serial=str(i), calibration_confirmed=True,
            reference_px=(20, 20), analysis_roi=(1, 1, 32, 32)) for i, c in enumerate(cfg.cameras)))
        mapping = {"far_field": "new", "dark_field": "1", "near_field": "2"}
        updated = assign_cameras(cfg, mapping)
        self.assertFalse(updated.cameras[0].calibration_confirmed)
        self.assertEqual(updated.cameras[0].reference_px, ())
        self.assertEqual(updated.cameras[0].analysis_roi, ())
        self.assertTrue(updated.cameras[1].calibration_confirmed)
        with self.assertRaises(ValueError):
            assign_cameras(cfg, {**mapping, "dark_field": "new"})

    def test_package_excludes_machine_environment_recordings_and_personal_config(self):
        target = build_package(ROOT, self.root/"transfer.zip")
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            self.assertFalse(any("/.venv/" in n or "/captures/" in n or n.endswith(".npyseq") for n in names))
            template = archive.read("Control_Sys_Diagnostic_Code/config.toml").decode("utf-8")
            self.assertNotIn("40680007", template)
            self.assertIn('serial = ""', template)
            self.assertIn("Control_Sys_Diagnostic_Code/bootstrap.py", names)
            self.assertIn("Control_Sys_Diagnostic_Code/WINDOWS_SETUP.md", names)

    def test_launch_environment_does_not_inherit_another_python_or_qt_tree(self):
        with patch.dict(os.environ, {"PYTHONPATH": "other", "PYTHONHOME": "other",
                                    "QT_PLUGIN_PATH": "other", "QT_QPA_PLATFORM_PLUGIN_PATH": "other"}):
            env = bootstrap.isolated_environment()
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("QT_PLUGIN_PATH", env)
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_png_roundtrip_uses_unicode_file_apis_and_preserves_16_bit_pixels(self):
        from beamdiag.image_io import read_image, write_png
        path = self.root/"画像 & optics (ä).png"
        image = np.array([[0, 4095], [1234, 65535]], dtype=np.uint16)
        with patch("cv2.imwrite", side_effect=AssertionError("Do not use native filename I/O")), \
             patch("cv2.imread", side_effect=AssertionError("Do not use native filename I/O")):
            write_png(path, image)
            np.testing.assert_array_equal(read_image(path), image)

    @unittest.skipUnless(reference_ui.QT_AVAILABLE, "GUI dependency not installed")
    def test_camera_discovery_failure_does_not_invalidate_simulation_or_writable_storage(self):
        from beamdiag.doctor import inspect
        path = self.root/"config.toml"
        save_config(AppConfig(), path)
        with patch("beamdiag.cameras.discover_basler", side_effect=RuntimeError("Camera transport unavailable")):
            report = inspect(self.root, path, discover=True)
        self.assertTrue(report["ready_for_simulation"])
        self.assertFalse(report["ready_for_basler"])
        self.assertTrue(report["checks"]["configuration_and_storage"]["ok"])
        self.assertIn("transport", report["checks"]["camera_discovery"]["error"])


@unittest.skipUnless(reference_ui.QT_AVAILABLE, "GUI dependency not installed")
class CameraAssignmentUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = reference_ui.create_application()

    def test_assignments_are_explicit_and_written_without_starting_acquisition(self):
        from beamdiag.camera_setup_gui import CameraAssignmentDialog
        with TemporaryDirectory(dir=ROOT/"validation") as tmp:
            path = Path(tmp)/"camera mapping.toml"
            with patch.object(CameraAssignmentDialog, "refresh_cameras"):
                dialog = CameraAssignmentDialog(AppConfig(), path)
            try:
                dialog.saved_choices = {}
                dialog.show_cameras([{"serial": str(i), "model": "Example", "ip": "", "interface": "BaslerGigE"} for i in range(3)], "")
                self.assertTrue(all(combo.currentData() == "" for combo in dialog.selectors.values()))
                dialog.save()
                self.assertFalse(path.exists())
                for i, combo in enumerate(dialog.selectors.values()):
                    combo.setCurrentIndex(i+1)
                dialog.save()
                self.assertEqual(tuple(c.serial for c in load_config(path).cameras), ("0", "1", "2"))
            finally:
                dialog.deleteLater()
                self.app.sendPostedEvents(None, reference_ui.QtCore.QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main(verbosity=2)
