import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).absolute().parent.parent
SPEC = importlib.util.spec_from_file_location("install_local", ROOT / "scripts/install_local.py")
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.binary = self.base / "tunnel-client"
        self.binary.write_text("fixture")
        self.config = self.base / "config"
        self.state = self.base / "state"
        self.bin = self.base / "bin"
        self.units = self.base / "systemd/user"
        self.key = self.base / "credentials/runtime.key"

    def prepare(self, **changes):
        arguments = dict(config_dir=self.config, state_dir=self.state, bin_dir=self.bin,
                         unit_dir=self.units, source_dir=ROOT, tunnel_bin=self.binary,
                         tunnel_id="tunnel_" + "a" * 32, key_file=self.key)
        arguments.update(changes)
        return INSTALL.prepare(**arguments)

    def shared_directory(self, path, mode=0o755):
        path.mkdir(parents=True)
        path.chmod(mode)

    def test_existing_shared_directories_keep_permissions_and_other_units(self):
        self.shared_directory(self.units)
        self.shared_directory(self.bin)
        other = self.units / "other.service"
        other.write_text("existing-user-service")
        other.chmod(0o644)
        result = self.prepare()
        self.assertEqual(self.units.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.bin.stat().st_mode & 0o777, 0o755)
        self.assertEqual(other.read_text(), "existing-user-service")
        self.assertEqual(other.stat().st_mode & 0o777, 0o644)
        for directory in (self.config, self.state):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for file in (self.config / "bridge.json", self.config / "tunnel.yaml",
                     self.units / "wechat-dot-bridge.service"):
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.bin / "wechat-dot-bridge").stat().st_mode & 0o777, 0o755)
        self.assertIn(str(ROOT / '.venv/bin/python'), (self.bin / 'wechat-dot-bridge').read_text())
        self.assertFalse(result["started"])
        self.assertFalse(result["enabled"])

    def test_all_directories_are_checked_before_creating_any_destination(self):
        for unsafe in (self.units, self.bin, self.config, self.state):
            with self.subTest(directory=unsafe.name):
                self.shared_directory(unsafe, 0o777 if unsafe in (self.units, self.bin) else 0o755)
                with self.assertRaisesRegex(ValueError, "unsafe_directory"):
                    self.prepare()
                for destination in (self.config, self.state, self.bin, self.units):
                    if destination != unsafe:
                        self.assertFalse(destination.exists())
                unsafe.rmdir()

    def test_shared_directory_must_belong_to_current_user(self):
        self.shared_directory(self.units)
        with patch.object(INSTALL.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "unsafe_owner"):
                self.prepare()
        self.assertFalse(self.config.exists())

    def test_symlink_directory_and_ancestor_are_rejected_before_creating_files(self):
        actual = self.base / "actual"
        actual.mkdir()
        self.units.parent.mkdir()
        self.units.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe_path"):
            self.prepare()
        self.assertFalse(self.config.exists())
        self.assertEqual(list(actual.iterdir()), [])
        self.units.unlink()
        self.units.parent.rmdir()
        self.units.parent.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe_path"):
            self.prepare()
        self.assertFalse(self.config.exists())
        self.assertEqual(list(actual.iterdir()), [])

    def test_existing_last_target_is_preserved_without_partial_install(self):
        self.shared_directory(self.units)
        target = self.units / "wechat-dot-bridge.service"
        target.write_text("existing-service")
        with self.assertRaisesRegex(ValueError, "target_exists"):
            self.prepare()
        self.assertEqual(target.read_text(), "existing-service")
        for destination in (self.config, self.state, self.bin):
            self.assertFalse(destination.exists())

    def test_dangling_output_symlink_is_preserved_and_rejected(self):
        self.shared_directory(self.units)
        target = self.units / "wechat-dot-bridge.service"
        target.symlink_to(self.base / "missing")
        with self.assertRaisesRegex(ValueError, "target_exists"):
            self.prepare()
        self.assertTrue(target.is_symlink())
        self.assertFalse(self.config.exists())

    def test_write_failure_rolls_back_new_files_and_directories(self):
        with patch.object(INSTALL.os, "fsync", side_effect=[None, None, OSError("fixture-write-error")]):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(list(self.base.iterdir()), [self.binary])
        self.prepare()
        self.assertTrue((self.units / "wechat-dot-bridge.service").is_file())

    def test_write_failure_preserves_existing_shared_directories_and_files(self):
        self.shared_directory(self.units)
        marker = self.units / "other.service"
        marker.write_text("existing-service")
        with patch.object(INSTALL.os, "fsync", side_effect=[None, None, None, OSError("fixture-write-error")]):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(list(self.units.iterdir()), [marker])
        self.assertEqual(marker.read_text(), "existing-service")
        self.assertEqual(self.units.stat().st_mode & 0o777, 0o755)
        for destination in (self.config, self.state, self.bin):
            self.assertFalse(destination.exists())

    def test_target_created_after_preflight_is_preserved_on_rollback(self):
        self.shared_directory(self.units)
        target = self.units / "wechat-dot-bridge.service"
        write = INSTALL.new_file

        def concurrent_target(path, text, mode):
            if path == target:
                target.write_text("concurrent-service")
            return write(path, text, mode)

        with patch.object(INSTALL, "new_file", side_effect=concurrent_target):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertEqual(target.read_text(), "concurrent-service")
        for destination in (self.config, self.state, self.bin):
            self.assertFalse(destination.exists())

    def test_output_paths_cannot_be_directory_or_runtime_key_destinations(self):
        for changes in ({"bin_dir": self.config / "bridge.json"},
                        {"key_file": self.config / "tunnel.yaml"}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "conflicting_paths"):
                    self.prepare(**changes)
                self.assertFalse(self.config.exists())

    def test_invalid_proxy_port_is_rejected_before_creating_destinations(self):
        for proxy in ("http://127.0.0.1:0", "http://127.0.0.1:65536"):
            with self.subTest(proxy=proxy):
                with self.assertRaisesRegex(ValueError, "invalid_proxy"):
                    self.prepare(proxy=proxy)
                self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
