import tempfile
import unittest
from unittest.mock import Mock

from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient
from wechat_dot_bridge.typing import TypingIndicator


class TypingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.store.import_binding({"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token",
                                   "base_url": "https://ilinkai.weixin.qq.com"})
        self.store.set_enabled(True)
        self.store.ingest("fixture-account", [{"source_id": "1", "peer": "fixture-owner", "text": "fixture",
                          "created_at": 1800000000, "context_token": "fixture-context"}], "1")
        self.mid = self.store.list_pending()[0]["message_id"]
        self.now = 0
        self.client = Mock()
        self.client.typing_ticket.return_value = "fixture-ticket"
        self.indicator = TypingIndicator(self.store, self.client, lambda: self.now)

    def tearDown(self):
        self.indicator.close()
        self.store.close()
        self.directory.cleanup()

    def test_only_read_messages_start_then_repeat_and_reply_cancels(self):
        self.indicator.tick()
        self.client.send_typing.assert_not_called()
        self.indicator.begin(self.mid)
        self.indicator.tick()
        self.indicator.tick()
        self.assertEqual(self.client.send_typing.call_count, 1)
        self.now = 5
        self.indicator.tick()
        self.assertEqual(self.client.send_typing.call_count, 2)
        self.assertEqual(self.client.typing_ticket.call_count, 1)
        self.store.queue_reply(self.mid, "fixture reply")
        self.indicator.tick()
        self.assertEqual(self.client.send_typing.call_args.args[-1], 2)
        self.indicator.tick()
        self.assertEqual(self.client.send_typing.call_count, 3)

    def test_timeout_repeated_read_does_not_extend_and_restart_does_not_resume(self):
        self.indicator.begin(self.mid)
        self.indicator.tick()
        self.now = 290
        self.indicator.begin(self.mid)
        self.now = 301
        self.indicator.tick()
        self.assertEqual(self.client.send_typing.call_args.args[-1], 2)
        other = TypingIndicator(self.store, self.client, lambda: self.now)
        previous = self.client.send_typing.call_count
        other.tick()
        self.assertEqual(self.client.send_typing.call_count, previous)

    def test_disabled_unknown_and_failures_do_not_send_replies(self):
        self.indicator.begin("unknown")
        self.indicator.tick()
        self.client.send_typing.assert_not_called()
        self.indicator.begin(self.mid)
        self.client.typing_ticket.side_effect = RuntimeError("private fixture")
        self.indicator.tick()
        self.assertEqual(self.store.get_status()["reply_status"], {})
        self.client.typing_ticket.side_effect = None
        self.now = 31
        self.indicator.tick()
        self.store.set_enabled(False)
        self.indicator.tick()
        self.assertEqual(self.client.send_typing.call_args.args[-1], 2)

    def test_protocol_only_targets_owner_and_never_sends_message_content(self):
        client = IlinkClient()
        client._post = Mock(return_value={"ret": 0, "typing_ticket": "fixture-ticket"})
        binding = self.store.get_binding()
        ticket = client.typing_ticket(binding, "fixture-context")
        self.assertEqual(client._post.call_args.args[1], "/ilink/bot/getconfig")
        self.assertEqual(client._post.call_args.args[2]["ilink_user_id"], "fixture-owner")
        client._post.return_value = {}
        client.send_typing(binding, ticket, 1)
        self.assertEqual(client._post.call_args.args[1], "/ilink/bot/sendtyping")
        self.assertEqual(set(client._post.call_args.args[2]), {"ilink_user_id", "typing_ticket", "status", "base_info"})
        self.assertTrue(client._post.call_args.kwargs["allow_empty"])


if __name__ == "__main__":
    unittest.main()
