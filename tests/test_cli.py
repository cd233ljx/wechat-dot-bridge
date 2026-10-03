import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace

from wechat_dot_bridge.cli import load_config
from wechat_dot_bridge.errors import BridgeError

ROOT = Path(__file__).absolute().parent.parent


def script_module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace(".", "_"), ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StdioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name) / "state"
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.directory.cleanup()

    def start(self):
        process = subprocess.Popen([sys.executable, "-m", "wechat_dot_bridge", "--state-dir", str(self.state), "serve"],
                                   cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def request(self, process, value):
        process.stdin.write(json.dumps(value).encode() + b"\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, "stdio response missing")
        return json.loads(process.stdout.readline())

    def test_actual_mcp_process_survives_invalid_frames_and_eof(self):
        process = self.start()
        result = self.request(process, {"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        self.assertEqual(result["result"]["supportedVersions"], ["2026-07-28"])
        process.stdin.write(b"invalid-json\n")
        process.stdin.flush()
        self.assertEqual(json.loads(process.stdout.readline())["error"]["code"], -32700)
        process.stdin.write(b"x" * (2 * 1024 * 1024 + 1) + b"\n")
        process.stdin.flush()
        self.assertEqual(json.loads(process.stdout.readline())["error"]["code"], -32600)
        response = self.request(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_weixin_status"}})
        self.assertFalse(response["result"]["structuredContent"]["bound"])
        process.stdin.close()
        process.stdin = None
        _, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(errors, b"")

    def test_process_lock_prevents_second_receiver_and_mutation(self):
        first = self.start()
        self.request(first, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        second = self.start()
        output, error = second.communicate(timeout=5)
        self.assertEqual(output, b"")
        self.assertEqual(second.returncode, 2)
        self.assertEqual(json.loads(error), {"error": "already_running"})
        mutation = subprocess.run([sys.executable, "-m", "wechat_dot_bridge", "--state-dir", str(self.state), "resume", "--confirm-single-consumer"],
                                  cwd=ROOT, capture_output=True, timeout=5)
        self.assertEqual(mutation.returncode, 2)
        self.assertEqual(json.loads(mutation.stderr), {"error": "already_running"})
        self.assertEqual(self.request(first, {"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"], {})

    def test_signal_exit_releases_worker_lock(self):
        first = self.start()
        self.request(first, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        first.terminate()
        first.communicate(timeout=5)
        self.assertEqual(first.returncode, 0)
        second = self.start()
        self.assertEqual(self.request(second, {"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"], {})


class SetupTests(unittest.TestCase):
    def test_callback_network_options_are_operator_only_and_loopback_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = {"state_dir": str(Path(directory) / "state"), "callback_hosts": ["callback.example.com"],
                      "callback_proxy": "http://127.0.0.1:7890", "callback_dns": "google"}
            path.write_text(json.dumps(config))
            options = load_config(Namespace(config=str(path), state_dir=None))
            self.assertEqual(options[3:5], ("http://127.0.0.1:7890", "google"))
            for proxy in ("http://192.168.1.1:7890", "http://localhost:7890", "https://127.0.0.1:7890",
                          "http://user:secret@127.0.0.1:7890", "http://127.0.0.1:0", "http://127.0.0.1:7890/", "http://127.0.0.1:65536"):
                config["callback_proxy"] = proxy
                path.write_text(json.dumps(config))
                with self.assertRaisesRegex(BridgeError, "config_invalid"):
                    load_config(Namespace(config=str(path), state_dir=None))
            config["callback_proxy"] = "http://127.0.0.1:7890"
            config["callback_dns"] = "untrusted.example.com"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(BridgeError, "config_invalid"):
                load_config(Namespace(config=str(path), state_dir=None))

    def test_hidden_key_save_private_and_no_overwrite_or_symlink(self):
        helper = script_module("save_runtime_key.py")
        with tempfile.TemporaryDirectory() as directory:
            key = "sk-" + "fixture_ONLY_not_a_real_key" * 2
            path = Path(directory) / "credentials" / "runtime.key"
            helper.save_key(path, key)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                helper.save_key(path, key)
            other = Path(directory) / "other.key"
            other.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "unsafe_path"):
                helper.save_key(other, key)
        refused = subprocess.run([sys.executable, str(ROOT / "scripts/save_runtime_key.py"), "--path", "/should-never-be-written.key"],
                                 input=b"fixture-never-display\n", capture_output=True, timeout=5)
        self.assertEqual(refused.returncode, 2)
        self.assertNotIn(b"fixture-never-display", refused.stdout + refused.stderr)

    def test_install_generates_private_loopback_service_without_start_or_secret(self):
        helper = script_module("install_local.py")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            binary = base / "tunnel-client"
            binary.write_text("fixture")
            result = helper.prepare(base / "config", base / "state", base / "bin", base / "units", ROOT,
                                    binary, "tunnel_" + "a" * 32, base / "private/runtime.key", "http://127.0.0.1:7890")
            yaml = (base / "config/tunnel.yaml").read_text()
            unit = (base / "units/wechat-dot-bridge.service").read_text()
            self.assertIn('listen_addr: "127.0.0.1:0"', yaml)
            self.assertIn('api_key: "file:', yaml)
            self.assertNotIn("sk-", yaml)
            self.assertNotIn("0.0.0.0", yaml)
            self.assertIn("StandardOutput=null", unit)
            self.assertIn("KillMode=control-group", unit)
            self.assertFalse(result["started"])
            self.assertFalse(result["enabled"])
            self.assertEqual((base / "config/tunnel.yaml").stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "target_exists"):
                helper.prepare(base / "config", base / "state", base / "bin", base / "units", ROOT,
                               binary, "tunnel_" + "a" * 32, base / "private/runtime.key")


if __name__ == "__main__":
    unittest.main()
