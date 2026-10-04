"""The upgrade exercise works in a source archive without local Git history."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UpgradeScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "wechat_dot_bridge", self.root / "wechat_dot_bridge",
                        ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
        (self.root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts" / "verify-upgrade.py", self.root / "scripts")
        self.env = dict(os.environ)
        self.env.pop("PYTHONPATH", None)
        self.env["PYTHONNOUSERSITE"] = "1"

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, *arguments):
        return subprocess.run([sys.executable, "scripts/verify-upgrade.py", *arguments],
                              cwd=self.root, env=self.env, capture_output=True, text=True,
                              timeout=30)

    def test_upgrade_and_uncertain_recovery_without_git_history(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("uncertain-send preservation, backup/restore and part recovery: passed", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_explicit_unavailable_revision_does_not_fall_back_or_echo_input(self):
        revision = "synthetic-private-revision"
        result = self.run_script("--old-revision", revision)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout),
                         {"passed": False, "error": "old_revision_unavailable"})
        self.assertNotIn(revision, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
