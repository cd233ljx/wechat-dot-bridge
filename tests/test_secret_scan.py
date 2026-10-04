import base64
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).absolute().parent.parent
SCRIPT = ROOT / "scripts/scan_secrets.py"
SPEC = importlib.util.spec_from_file_location("scan_secrets", SCRIPT)
SCAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCAN)


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "scan-test")
        self.git("config", "user.email", "scan-test@example.invalid")
        self.private = "sk-" + secrets.token_hex(24)

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repo), *arguments], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def commit(self, message="fixture commit"):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)

    def run_scan(self):
        process = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertNotIn(self.private.encode(), process.stdout + process.stderr)
        self.assertEqual(process.stderr, b"")
        return process.returncode, json.loads(process.stdout)

    def test_removed_secret_is_reported_from_reachable_history(self):
        path = self.repo / "settings.py"
        path.write_text("KEY = " + repr(self.private) + "\n")
        self.commit()
        path.unlink()
        self.commit("remove fixture setting")
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertTrue(any(item["scope"] == "history" and item["category"] == "openai_token"
                            and item["file"] == "settings.py" and item["line"] == 1
                            for item in result["findings"]))
        self.assertFalse(any(item["scope"] == "worktree" for item in result["findings"]))

    def test_untracked_secret_and_test_directory_are_scanned(self):
        path = self.repo / "tests/settings.py"
        path.parent.mkdir()
        path.write_text("KEY = " + repr(self.private) + "\n")
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertTrue(any(item["scope"] == "worktree" and item["file"] == "tests/settings.py"
                            for item in result["findings"]))

    def test_git_replacement_does_not_hide_original_blob(self):
        path = self.repo / "settings.py"
        path.write_text("KEY = " + repr(self.private) + "\n")
        self.commit()
        original = self.git("rev-parse", "HEAD:settings.py").strip().decode()
        path.write_text("clean fixture\n")
        self.commit()
        replacement = self.git("rev-parse", "HEAD:settings.py").strip().decode()
        self.git("replace", original, replacement)
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertTrue(any(item.get("object") == original and item["category"] == "openai_token"
                            for item in result["findings"]))

    def test_explicit_dummy_values_and_dynamic_fixture_construction_are_allowed(self):
        dummy = "sk-" + "fixture_ONLY_not_a_real_key" * 2
        webhook = "whsec_" + base64.b64encode(b"fixture-signing-key-32-byte-data!").decode()
        text = ("api_key = " + repr(dummy) + "\nsecret = " + repr(webhook)
                + '\ntoken = "synthetic-bot-token-not-real"\n'
                + 'url = "https://callback.example.com/?token=fixture"\n'
                + 'token = "${WEIXIN_TOKEN}"\n')
        (self.repo / "fixtures.py").write_text(text)
        self.commit()
        code, result = self.run_scan()
        self.assertEqual(code, 0)
        self.assertEqual(result["findings"], [])

    def test_literal_webhook_wechat_and_url_credentials_are_detected_without_values(self):
        github = "ghp_" + secrets.token_hex(20)
        webhook = "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()
        wechat = secrets.token_urlsafe(32)
        password = secrets.token_urlsafe(24)
        text = ("a=" + repr(github) + "\nsecret=" + repr(webhook)
                + "\nbot_token=" + repr(wechat)
                + "\nurl=" + repr("https://callback.example.invalid/?secret=" + password)
                + "\nurl=" + repr("https://user:" + password + "@callback.example.invalid/events") + "\n")
        (self.repo / "settings.py").write_text(text)
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertTrue({"github_token", "webhook_secret", "credential_literal", "secret_url"}
                        <= {item["category"] for item in result["findings"]})
        rendered = json.dumps(result)
        for value in (github, webhook, wechat, password):
            self.assertNotIn(value, rendered)

    def test_opaque_callback_route_and_fragment_token_are_detected(self):
        opaque = secrets.token_urlsafe(32)
        text = ("callback_url=" + repr("https://example.invalid/events/" + opaque)
                + "\nurl=" + repr("https://example.invalid/#accessToken=" + opaque))
        (self.repo / "settings.py").write_text(text)
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertEqual([item["category"] for item in result["findings"]], ["secret_url", "secret_url"])
        self.assertNotIn(opaque, json.dumps(result))

    def test_commit_and_tag_messages_are_anonymous_and_scan_all_refs(self):
        (self.repo / "README.md").write_text("fixture\n")
        self.commit("fixture " + self.private)
        self.git("tag", "-a", "fixture-tag", "-m", "fixture " + self.private)
        self.git("checkout", "--orphan", "clean", "--quiet")
        (self.repo / "README.md").write_text("clean fixture\n")
        self.commit()
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        anonymous = [item for item in result["findings"] if item["scope"] == "history" and "file" not in item]
        self.assertGreaterEqual(len(anonymous), 2)
        self.assertTrue(all("object" in item for item in anonymous))
        rendered = json.dumps(result)
        self.assertNotIn("scan-test@example.invalid", rendered)
        self.assertNotIn("fixture-tag", rendered)

    def test_ignored_runtime_is_not_read_but_tracked_runtime_is_reported(self):
        (self.repo / ".gitignore").write_text("runtime/\n*.sqlite*\n")
        runtime = self.repo / "runtime"
        runtime.mkdir()
        private = runtime / "binding.json"
        private.write_text(self.private)
        private.chmod(0)
        self.addCleanup(private.chmod, 0o600)
        code, result = self.run_scan()
        self.assertEqual(code, 0)
        self.assertEqual(result["findings"], [])
        private.chmod(0o600)
        self.git("add", "--force", "runtime/binding.json")
        self.git("commit", "--quiet", "-m", "fixture runtime path")
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertEqual({item["category"] for item in result["findings"]}, {"runtime_file"})

    def test_private_key_database_and_archive_paths_are_reported(self):
        (self.repo / "source.py").write_text("-----BEGIN " + "PRIVATE KEY-----\nfixture\n")
        (self.repo / "backup.zip").write_bytes(b"fixture")
        (self.repo / "cache.sqlite").write_bytes(b"SQLite format 3\0fixture")
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertTrue({"private_key", "archive_file", "runtime_database"}
                        <= {item["category"] for item in result["findings"]})

    def test_symlink_to_external_file_is_reported_without_reading_it(self):
        external = self.repo / "external"
        external.write_text(self.private)
        (self.repo / ".gitignore").write_text("external\n")
        (self.repo / "settings.py").symlink_to(external)
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertEqual([item["category"] for item in result["findings"]], ["symlink_path"])

    def test_token_in_filename_is_redacted(self):
        (self.repo / (self.private + ".txt")).write_text("clean fixture\n")
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertTrue(all(self.private not in item.get("file", "") for item in result["findings"]))

    def test_shallow_history_does_not_report_a_complete_clean_scan(self):
        (self.repo / "README.md").write_text("fixture\n")
        self.commit()
        (self.repo / ".git/shallow").write_bytes(self.git("rev-parse", "HEAD"))
        code, result = self.run_scan()
        self.assertEqual(code, 1)
        self.assertFalse(result["scanned"]["complete_local_history"])
        self.assertEqual([item["category"] for item in result["findings"]], ["shallow_history"])

    def test_git_failure_outputs_only_fixed_error(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        empty = Path(directory.name)
        process = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(empty)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout), {"error": "secret_scan_failed"})
        self.assertEqual(process.stderr, b"")
        self.assertNotIn(str(empty).encode(), process.stdout)

    def test_invalid_cli_argument_outputs_only_fixed_error(self):
        process = subprocess.run([sys.executable, str(SCRIPT), "--unknown", self.private],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout), {"error": "secret_scan_failed"})
        self.assertEqual(process.stderr, b"")
        self.assertNotIn(self.private.encode(), process.stdout)


if __name__ == "__main__":
    unittest.main()
