"""Offline end-to-end protocol fixtures; no real Weixin, dot, or Tunnel calls.

Only the network boundary is replaced. The real transport builders, Store,
Events signing/lifecycle, and MCP tool dispatcher perform the entire workflow.
"""

import base64
import hashlib
import hmac
import json
import tempfile
import unittest

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.mcp import MCPServer
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, WeixinWorker


ACCOUNT = "synthetic@im.bot"
OWNER = "synthetic@im.wechat"
BOT_TOKEN = "synthetic-bot-token-not-real"
CONTEXT = "synthetic-context-not-real"
SIGNING_BYTES = b"only-a-synthetic-signing-key-32!!"
SIGNING_SECRET = "whsec_" + base64.b64encode(SIGNING_BYTES).decode("ascii")
CALLBACK_URL = "https://callback.example.com/synthetic-private-route"


class FixtureClock:
    def __init__(self):
        self.now = 1800001000.0

    def __call__(self):
        return self.now


class FixtureWeixin(IlinkClient):
    """Use real request builders, intercepting requests before network access."""
    def __init__(self, clock):
        super().__init__(clock)
        self.poll_response = {"ret": 0, "msgs": [], "get_updates_buf": ""}
        self.send_response = {}  # Explicitly allowed by Tencent's official tests.
        self.send_failure = None
        self.posts = []

    def _post(self, binding, path, body, timeout, allow_empty=False):
        if path == "/ilink/bot/getupdates":
            return json.loads(json.dumps(self.poll_response))
        if path != "/ilink/bot/sendmessage":
            raise AssertionError("Unexpected fixture endpoint")
        self.posts.append(json.loads(json.dumps(body)))
        if self.send_failure:
            raise BridgeError(self.send_failure)
        return json.loads(json.dumps(self.send_response))


class VerifyingCallback:
    """Independent Standard Webhooks verification rather than calling the signer."""
    def __init__(self, clock):
        self.clock = clock
        self.events = []
        self.verifications = []
        self.response_codes = []

    def __call__(self, url, body, headers):
        if url != CALLBACK_URL:
            raise AssertionError("Unexpected fixture callback")
        material = (headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode("ascii") + body
        expected = "v1," + base64.b64encode(hmac.new(SIGNING_BYTES, material, hashlib.sha256).digest()).decode("ascii")
        if expected not in headers["webhook-signature"].split():
            raise AssertionError("Invalid signature")
        if int(headers["webhook-timestamp"]) != int(self.clock()):
            raise AssertionError("Stale signing time")
        payload = json.loads(body)
        if payload.get("type") == "verification":
            self.verifications.append(payload)
            return 200, json.dumps({"challenge": payload["challenge"]}).encode("utf-8")
        if payload["eventId"] != headers["webhook-id"]:
            raise AssertionError("Event ID/header mismatch")
        if set(payload["data"]) != {"message_id"}:
            raise AssertionError("Unexpected private data in event")
        if any(secret in body.decode("utf-8") for secret in (BOT_TOKEN, CONTEXT, OWNER)):
            raise AssertionError("Routing credential or identity in webhook")
        self.events.append(payload)
        return self.response_codes.pop(0) if self.response_codes else 200, b"{}"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = FixtureClock()
        self.store = Store(self.temporary.name, clock=self.clock)
        self.store.import_binding({"id": ACCOUNT, "owner": OWNER, "token": BOT_TOKEN,
                                   "base_url": "https://ilinkai.weixin.qq.com", "cursor": "fixture-initial"})
        self.store.set_enabled(True)
        self.client = FixtureWeixin(self.clock)
        self.callback = VerifyingCallback(self.clock)
        self.build_runtime()
        self.request_id = 0
        self.params = {"name": "weixin.message", "arguments": {},
                       "delivery": {"mode": "webhook", "url": CALLBACK_URL, "secret": SIGNING_SECRET}}

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def build_runtime(self):
        self.worker = WeixinWorker(self.store, self.client)
        self.events = Events(self.store, allowed_hosts=["callback.example.com"], post=self.callback, clock=self.clock)
        self.server = MCPServer(self.store, self.events)

    def request(self, method, params=None):
        self.request_id += 1
        return self.server.handle({"jsonrpc": "2.0", "id": self.request_id,
                                   "method": method, "params": {} if params is None else params})

    def call(self, name, arguments=None):
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        self.assertNotIn("error", response)
        self.assertFalse(response["result"]["isError"])
        return response["result"]["structuredContent"]

    def subscribe(self):
        response = self.request("events/subscribe", self.params)
        self.assertNotIn("error", response)
        self.assertIsNone(response["result"]["cursor"])
        return response["result"]

    def wire_message(self, identifier=18446744073709551615, text="闭环验收原文\n第二行", **changes):
        message = {"message_id": identifier, "from_user_id": OWNER, "to_user_id": ACCOUNT,
                   "create_time_ms": int(self.clock() * 1000), "message_type": 1,
                   "message_state": 2, "context_token": CONTEXT,
                   "item_list": [{"type": 1, "text_item": {"text": text}}]}
        message.update(changes)
        return message

    def receive(self, messages=None, with_ret=True):
        response = {"msgs": [self.wire_message()] if messages is None else messages,
                    "get_updates_buf": "fixture-next"}
        if with_ret:
            response["ret"] = 0
        self.client.poll_response = response
        self.assertTrue(self.worker.poll_once())
        self.assertEqual(self.store.get_binding()["cursor"], "fixture-next")

    def first_message_id(self):
        pending = self.call("list_pending_weixin_messages")["messages"]
        self.assertEqual(len(pending), 1)
        self.assertNotIn("text", pending[0])
        return pending[0]["message_id"]

    def test_complete_owner_text_webhook_read_reply_same_peer_once(self):
        self.assertEqual(self.request("server/discover")["result"]["supportedVersions"], ["2026-07-28"])
        self.assertEqual(self.request("events/list")["result"]["events"][0]["name"], "weixin.message")
        subscription = self.subscribe()
        self.assertEqual(len(self.callback.verifications), 1)
        self.receive(with_ret=False)  # ret is optional in the official response.
        identifier = self.first_message_id()
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(self.callback.events[-1]["data"], {"message_id": identifier})
        self.assertEqual(self.callback.events[-1]["name"], "weixin.message")
        self.assertNotIn("闭环验收原文", json.dumps(self.callback.events, ensure_ascii=False))
        read = self.call("read_weixin_message", {"message_id": identifier})
        self.assertEqual(read["text"], "闭环验收原文\n第二行")
        reply = {"message_id": identifier, "text": "验收回复：已收到。", "reply_key": "answer"}
        queued = self.call("reply_weixin_message", reply)
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["delivered"])
        self.assertTrue(self.worker.send_once())
        accepted = self.call("get_weixin_send_status", {"message_id": identifier})
        self.assertEqual(accepted["status"], "accepted")
        self.assertIsNone(accepted["delivered"])
        self.assertEqual(self.client.posts[0]["msg"]["to_user_id"], OWNER)
        self.assertEqual(self.client.posts[0]["msg"]["context_token"], CONTEXT)
        self.assertEqual(self.client.posts[0]["msg"]["item_list"], [{"type": 1, "text_item": {"text": reply["text"]}}])
        self.receive()  # Repeated poll result must not cause another event/reply.
        self.assertFalse(self.events.deliver_once())
        self.assertFalse(self.worker.send_once())
        self.assertEqual(self.call("reply_weixin_message", reply)["status"], "accepted")
        conflicting = self.request("tools/call", {"name": "reply_weixin_message",
                                                 "arguments": {"message_id": identifier, "text": "改成另一个答案", "reply_key": "answer"}})
        self.assertTrue(conflicting["result"]["isError"])
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.call("get_weixin_status")["active_subscriptions"], 1)
        self.assertTrue(subscription["id"])

    def test_only_original_owner_conversation_completed_text_is_admitted(self):
        self.subscribe()
        rejected = [self.wire_message(2, from_user_id="other@im.wechat"),
                    self.wire_message(3, to_user_id="other@im.bot"),
                    self.wire_message(4, room_id="fixture-room"),
                    self.wire_message(5, group_id="fixture-group"),
                    self.wire_message(6, delete_time_ms=int(self.clock() * 1000)),
                    self.wire_message(7, message_state=1), self.wire_message(8, message_type=2)]
        self.receive(rejected)
        self.assertEqual(self.call("list_pending_weixin_messages")["messages"], [])
        self.assertFalse(self.events.deliver_once())
        self.assertEqual(self.callback.events, [])
        self.assertEqual(self.client.posts, [])

    def test_uncertain_post_never_resends_after_restart_or_resubscription(self):
        self.subscribe()
        self.receive()
        identifier = self.first_message_id()
        self.events.deliver_once()
        self.call("read_weixin_message", {"message_id": identifier})
        reply = {"message_id": identifier, "text": "网络超时验收答案"}
        self.call("reply_weixin_message", reply)
        self.client.send_failure = "network_timeout"
        self.assertTrue(self.worker.send_once())
        self.assertEqual(self.call("get_weixin_send_status", {"message_id": identifier})["status"], "uncertain")
        original_client_id = self.client.posts[0]["msg"]["client_id"]
        self.store.close()
        self.store = Store(self.temporary.name, clock=self.clock)
        self.store.recover_inflight()
        self.build_runtime()
        self.subscribe()
        self.receive()
        self.assertFalse(self.events.deliver_once())
        self.assertFalse(self.worker.send_once())
        self.assertEqual(self.call("reply_weixin_message", reply)["status"], "uncertain")
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.posts[0]["msg"]["client_id"], original_client_id)
        public_status = json.dumps(self.call("get_weixin_status"))
        for secret in (BOT_TOKEN, CONTEXT, SIGNING_SECRET, CALLBACK_URL, OWNER, ACCOUNT, reply["text"]):
            self.assertNotIn(secret, public_status)

    def test_expired_subscription_recovers_pending_and_committed_reply_is_excluded(self):
        self.params["ttlMs"] = 60000
        self.subscribe()
        self.receive()
        identifier = self.first_message_id()
        self.clock.now += 61
        self.assertFalse(self.events.deliver_once())
        self.assertEqual(self.call("get_weixin_status")["active_subscriptions"], 0)
        self.assertEqual(self.first_message_id(), identifier)
        self.subscribe()
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(self.callback.events[-1]["data"]["message_id"], identifier)
        self.call("read_weixin_message", {"message_id": identifier})
        self.call("reply_weixin_message", {"message_id": identifier, "text": "恢复后的回答"})
        unsubscribe = {"name": self.params["name"], "arguments": {},
                       "delivery": {"mode": "webhook", "url": CALLBACK_URL}}
        self.assertEqual(self.request("events/unsubscribe", unsubscribe)["result"], {})
        self.assertEqual(self.store.get_status()["active_subscriptions"], 0)
        self.subscribe()
        self.assertFalse(self.events.deliver_once())
        self.assertTrue(self.worker.send_once())
        self.assertEqual(len(self.client.posts), 1)

    def test_408_callback_retry_retains_same_event_id_and_stops_after_commit(self):
        self.subscribe()
        self.receive()
        identifier = self.first_message_id()
        self.callback.response_codes = [408, 200]
        self.assertTrue(self.events.deliver_once())
        first = self.callback.events[-1]
        self.clock.now += 3
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(self.callback.events[-1]["eventId"], first["eventId"])
        self.assertEqual(self.callback.events[-1]["data"], {"message_id": identifier})
        self.call("reply_weixin_message", {"message_id": identifier, "text": "幂等验收回答"})
        self.subscribe()
        self.assertFalse(self.events.deliver_once())
        self.assertTrue(self.worker.send_once())
        self.assertEqual(len(self.callback.events), 2)
        self.assertEqual(len(self.client.posts), 1)

    def test_disabled_bridge_has_no_poll_event_or_send_side_effects(self):
        self.subscribe()
        self.receive()
        identifier = self.first_message_id()
        self.call("reply_weixin_message", {"message_id": identifier, "text": "暂存答案"})
        self.store.set_enabled(False)
        self.assertFalse(self.worker.poll_once())
        self.assertFalse(self.events.deliver_once())
        self.assertFalse(self.worker.send_once())
        self.assertEqual(self.callback.events, [])
        self.assertEqual(self.client.posts, [])


if __name__ == "__main__":
    unittest.main()
