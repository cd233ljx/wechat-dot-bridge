import base64
import json
import tempfile
import unittest

from wechat_dot_bridge.events import Events
from wechat_dot_bridge.mcp import MCPServer, PROTOCOL
from wechat_dot_bridge.store import Store


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.events = Events(self.store, ["callback.example.com"], post=self.post)
        self.server = MCPServer(self.store, self.events)
        self.calls = []

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def post(self, url, body, headers):
        value = json.loads(body)
        self.calls.append(value)
        return 200, json.dumps({"challenge": value.get("challenge")}).encode()

    def request(self, method, params=None):
        return self.server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def call(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def bind(self):
        self.store.import_binding({"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token", "base_url": "https://ilinkai.weixin.qq.com"})
        self.store.set_enabled(True)
        self.store.ingest("fixture-account", [{"source_id": "1", "peer": "fixture-owner", "text": "User fixture", "created_at": 1800000000, "context_token": "fixture-context"}], "1")
        return self.store.list_pending()[0]["message_id"]

    def test_mcp2_discovery_and_initialize_compatibility(self):
        result = self.request("server/discover")["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["supportedVersions"], [PROTOCOL])
        self.assertEqual(set(result["capabilities"]), {"tools", "events"})
        result = self.request("initialize", {"protocolVersion": "2023-01-01"})
        self.assertEqual(result["error"]["data"]["reason"], "unsupported_protocol")
        for version in [PROTOCOL, "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]:
            self.assertEqual(self.request("initialize", {"protocolVersion": version})["result"]["protocolVersion"], version)

    def test_exact_tools_and_unbound_status_only(self):
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"get_weixin_status", "read_weixin_message", "reply_weixin_message", "get_weixin_send_status", "list_pending_weixin_messages", "read_weixin_attachment", "stage_weixin_attachment", "reply_weixin_attachment", "retry_weixin_operation", "report_weixin_progress", "import_weixin_attachment"})
        self.assertTrue(all(tool["inputSchema"]["additionalProperties"] is False for tool in tools))
        self.assertFalse(self.call("get_weixin_status")["result"]["isError"])
        self.assertTrue(self.call("list_pending_weixin_messages")["result"]["isError"])

    def test_tool_workflow_and_immutable_reply(self):
        message_id = self.bind()
        data = self.call("read_weixin_message", {"message_id": message_id})["result"]["structuredContent"]
        self.assertEqual(data["text"], "User fixture")
        self.assertNotIn("fixture-context", json.dumps(data))
        params = {"message_id": message_id, "text": "Reply fixture", "reply_key": "answer"}
        first = self.call("reply_weixin_message", params)["result"]
        self.assertFalse(first["isError"])
        self.assertEqual(first["structuredContent"], self.call("reply_weixin_message", params)["result"]["structuredContent"])
        params["text"] = "Changed reply"
        self.assertTrue(self.call("reply_weixin_message", params)["result"]["isError"])
        self.assertEqual(self.call("list_pending_weixin_messages")["result"]["structuredContent"], {"messages": []})

    def test_no_send_without_original_message_and_no_owner_override(self):
        self.bind()
        self.assertTrue(self.call("reply_weixin_message", {"message_id": "unknown", "text": "Reply"})["result"]["isError"])
        result = self.call("reply_weixin_message", {"message_id": "unknown", "text": "Reply", "peer": "someone-else"})
        self.assertEqual(result["error"]["code"], -32602)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0)

    def test_schema_errors_and_secret_safe_internal_failures(self):
        for name, args in [("get_weixin_status", {"extra": "secret"}), ("read_weixin_message", {}), ("list_pending_weixin_messages", {"limit": True}), ("list_pending_weixin_messages", {"limit": 101}), ("reply_weixin_message", {"message_id": "m", "text": "字" * 90000}), ("send_weixin_message", {})]:
            response = self.call(name, args)
            self.assertEqual(response["error"]["code"], -32602)
            self.assertNotIn("secret", json.dumps(response))
        self.store.get_status = lambda: (_ for _ in ()).throw(RuntimeError("fixture-secret"))
        result = self.call("get_weixin_status")
        self.assertEqual(result["error"]["code"], -32603)
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_notifications_cannot_mutate_or_reply(self):
        message_id = self.bind()
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "reply_weixin_message", "arguments": {"message_id": message_id, "text": "Reply"}}}))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0)
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_events_discovery_subscribe_unsubscribe_and_callback_errors(self):
        self.bind()
        definition = self.request("events/list")["result"]["events"][0]
        self.assertEqual(definition["name"], "weixin.message")
        secret = "whsec_" + base64.b64encode(b"fixture-signing-key-32-byte-data!").decode()
        params = {"name": "weixin.message", "arguments": {}, "delivery": {"mode": "webhook", "url": "https://callback.example.com/route", "secret": secret}}
        self.assertIn("id", self.request("events/subscribe", params)["result"])
        params["delivery"].pop("secret")
        self.assertEqual(self.request("events/unsubscribe", params)["result"], {})
        params["delivery"]["secret"] = secret
        params["delivery"]["url"] = "https://unapproved.example.com/route"
        error = self.request("events/subscribe", params)["error"]
        self.assertEqual(error["code"], -32015)
        self.assertNotIn(secret, json.dumps(error))
        self.assertNotIn("/route", json.dumps(error))


if __name__ == "__main__":
    unittest.main()
