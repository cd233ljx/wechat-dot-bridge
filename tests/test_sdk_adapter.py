"""Wire contracts for the official SDK and the existing Tunnel protocol."""

import base64
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.shared.message import SessionMessage
import mcp_types as types

from wechat_dot_bridge.events import Events
from wechat_dot_bridge.mcp import MCPServer, PROTOCOL, tool_definitions
from wechat_dot_bridge.sdk_adapter import SDKAdapter
from wechat_dot_bridge.store import Store


ROOT = Path(__file__).absolute().parent.parent
CHILD = """
import anyio, json, sys
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.mcp import MCPServer
from wechat_dot_bridge.sdk_adapter import run_stdio
store = Store(sys.argv[1])
def post(url, body, headers):
    value = json.loads(body)
    return 200, json.dumps({'challenge': value.get('challenge')}).encode()
events = Events(store, ['callback.example.com'], post=post)
if len(sys.argv) > 2 and sys.argv[2] == 'bound':
    store.import_binding({'id': 'fixture-sdk-account', 'owner': 'fixture-sdk-owner',
                          'token': 'fixture-sdk-token', 'base_url': 'https://ilinkai.weixin.qq.com'})
    store.set_enabled(True)
try:
    anyio.run(run_stdio, MCPServer(store, events))
finally:
    store.close()
"""


class SDKWireTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.processes = []
        self.counter = 0

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.directory.cleanup()

    def start(self, bound=False):
        self.counter += 1
        state = Path(self.directory.name) / ("state-" + str(self.counter))
        process = subprocess.Popen([sys.executable, "-c", CHILD, str(state)] + (["bound"] if bound else []), cwd=ROOT,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def receive(self, process):
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, "SDK wire response missing")
        return json.loads(process.stdout.readline())

    def request(self, process, method, params=None, modern=False):
        self.counter += 1
        params = dict(params or {})
        if modern:
            params["_meta"] = {types.PROTOCOL_VERSION_META_KEY: PROTOCOL,
                               types.CLIENT_CAPABILITIES_META_KEY: {},
                               types.CLIENT_INFO_META_KEY: {"name": "fixture-sdk-client", "version": "1"}}
        request = {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}
        process.stdin.write(json.dumps(request).encode() + b"\n")
        process.stdin.flush()
        return self.receive(process)

    def finish(self, process):
        process.stdin.close()
        process.stdin = None
        _, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(errors, b"")

    def test_standard_modern_client_keeps_sdk_envelope_and_webhook_capability(self):
        process = self.start()
        result = self.request(process, "server/discover", modern=True)["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(set(result["capabilities"]), {"tools", "events"})
        self.assertIn(types.SERVER_INFO_META_KEY, result["_meta"])
        listed = self.request(process, "tools/list", modern=True)["result"]
        self.assertEqual({tool["name"] for tool in listed["tools"]}, {tool["name"] for tool in tool_definitions()})
        called = self.request(process, "tools/call", {"name": "get_weixin_status"}, modern=True)["result"]
        self.assertFalse(called["isError"])
        self.assertFalse(called["structuredContent"]["bound"])
        rejected = self.request(process, "initialize", {"protocolVersion": PROTOCOL}, modern=True)
        self.assertEqual(rejected["error"]["code"], -32022)
        self.finish(process)

    def test_standard_legacy_handshakes_preserve_every_existing_version(self):
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            process = self.start()
            result = self.request(process, "initialize", {"protocolVersion": version,
                                  "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}})["result"]
            self.assertEqual(result["protocolVersion"], version)
            self.assertIn("events", result["capabilities"])
            self.assertEqual(len(self.request(process, "tools/list")["result"]["tools"]), 11)
            self.assertIn("events", self.request(process, "events/list")["result"])
            self.finish(process)

    def test_tunnel_bare_requests_and_old_modern_initialize_remain_compatible(self):
        process = self.start()
        self.assertEqual(self.request(process, "server/discover")["result"]["supportedVersions"], [PROTOCOL])
        initialized = self.request(process, "initialize", {"protocolVersion": PROTOCOL})["result"]
        self.assertEqual(initialized["protocolVersion"], PROTOCOL)
        self.assertEqual(self.request(process, "ping")["result"], {})
        self.assertFalse(self.request(process, "tools/call", {"name": "get_weixin_status"})["result"]["isError"])
        self.finish(process)

    def test_invalid_and_oversized_frames_are_fixed_errors_and_runtime_survives(self):
        process = self.start()
        self.request(process, "server/discover")
        for raw, code in ((b'fixture_private_invalid_json\n', -32700),
                          (b'\xff\n', -32700),
                          (b'[]\n', -32600),
                          (b'x' * (2 * 1024 * 1024 + 1) + b'\n', -32600)):
            process.stdin.write(raw)
            process.stdin.flush()
            error = self.receive(process)
            self.assertEqual(error["error"]["code"], code)
            self.assertNotIn("fixture_private", json.dumps(error))
        self.assertEqual(self.request(process, "ping")["result"], {})
        self.finish(process)

    def test_sdk_errors_do_not_echo_method_arguments_or_exceptions(self):
        process = self.start()
        for method, params in (("fixture_private_method", {}),
                               ("tools/call", {"name": "get_weixin_status", "arguments": {"fixture_private_argument": "private"}}),
                               ("tools/call", {"name": "fixture_private_tool"}),
                               ("tools/list", {"cursor": "fixture_private_cursor"})):
            result = self.request(process, method, params)
            self.assertIn("error", result)
            self.assertNotIn("fixture_private", json.dumps(result))
        self.finish(process)

    def test_webhook_events_extension_has_same_safe_errors_on_sdk_wire(self):
        process = self.start(bound=True)
        event = self.request(process, "events/list", modern=True)["result"]["events"][0]
        self.assertEqual(event["name"], "weixin.message")
        secret = "whsec_" + base64.b64encode(b"fixture-key-is-not-a-runtime-key!").decode()
        params = {"name": "weixin.message", "arguments": {},
                  "delivery": {"mode": "webhook", "url": "https://private.example.com/fixture_private_path", "secret": secret}}
        error = self.request(process, "events/subscribe", params, modern=True)["error"]
        self.assertEqual(error["code"], -32015)
        self.assertNotIn(secret, json.dumps(error))
        self.assertNotIn("fixture_private_path", json.dumps(error))
        params["delivery"]["url"] = "https://callback.example.com/fixture-private-route"
        subscribed = self.request(process, "events/subscribe", params, modern=True)["result"]
        self.assertIn("id", subscribed)
        self.assertNotIn(secret, json.dumps(subscribed))
        params["delivery"].pop("secret")
        result = self.request(process, "events/unsubscribe", params, modern=True)["result"]
        self.assertEqual(result["resultType"], "complete")
        self.finish(process)

    def test_official_sdk_client_can_negotiate_and_call_bridge(self):
        async def verify():
            params = StdioServerParameters(command=sys.executable,
                        args=["-c", CHILD, str(Path(self.directory.name) / "sdk-client")], cwd=ROOT)
            async with Client(params, read_timeout_seconds=5) as client:
                tools = await client.list_tools()
                self.assertEqual(len(tools.tools), 11)
                result = await client.call_tool("get_weixin_status")
                self.assertFalse(result.is_error)
                self.assertFalse(result.structured_content["bound"])
        anyio.run(verify)


class SDKEmbeddingTests(unittest.TestCase):
    def test_sdk_concurrency_and_cancellation_do_not_repeat_business_effects(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                bridge = MCPServer(store, Events(store))
                adapter = SDKAdapter(bridge)
                started, release = threading.Event(), threading.Event()
                calls = []
                def call(params):
                    calls.append(params["name"])
                    started.set()
                    if not release.wait(5):
                        raise RuntimeError("fixture timeout")
                    return {"content": [{"type": "text", "text": "{}"}], "structuredContent": {}, "isError": False}
                bridge._call = call

                async def verify():
                    send, incoming = anyio.create_memory_object_stream(1)
                    outgoing, receive = anyio.create_memory_object_stream(1)
                    async with send, receive, anyio.create_task_group() as tasks:
                        tasks.start_soon(adapter.run_streams, incoming, outgoing)
                        await send.send(SessionMessage(types.JSONRPCRequest(jsonrpc="2.0", id=1,
                                        method="tools/call", params={"name": "get_weixin_status"})))
                        self.assertTrue(await anyio.to_thread.run_sync(started.wait, 2))
                        await send.send(SessionMessage(types.JSONRPCRequest(jsonrpc="2.0", id=2, method="tools/list")))
                        with anyio.fail_after(2):
                            listed = await receive.receive()
                        self.assertEqual(listed.message.id, 2)
                        await send.send(SessionMessage(types.JSONRPCNotification(jsonrpc="2.0", method="notifications/cancelled",
                                                       params={"requestId": 1})))
                        # Give SDK cancellation a turn, then let the atomic
                        # business call finish. Its response must remain silenced.
                        await anyio.sleep(0.02)
                        release.set()
                        with anyio.move_on_after(0.1) as timeout:
                            unexpected = await receive.receive()
                            self.fail("Cancelled request returned " + str(unexpected.message.id))
                        self.assertTrue(timeout.cancel_called)
                        tasks.cancel_scope.cancel()
                try:
                    anyio.run(verify)
                finally:
                    release.set()
                self.assertEqual(calls, ["get_weixin_status"])
            finally:
                store.close()

    def test_protocol_failure_cannot_emit_secret_in_error_or_sdk_log(self):
        import io
        import logging
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                bridge = MCPServer(store, Events(store))
                adapter = SDKAdapter(bridge)
                logs = io.StringIO()
                handler = logging.StreamHandler(logs)
                logger = logging.getLogger("mcp.server.runner")
                logger.addHandler(handler)
                try:
                    with patch.object(store, "get_status", side_effect=RuntimeError("fixture_private_exception")):
                        result = adapter.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": "get_weixin_status"}})
                    self.assertEqual(result["error"]["code"], -32603)
                    self.assertNotIn("fixture_private_exception", json.dumps(result) + logs.getvalue())
                finally:
                    logger.removeHandler(handler)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
