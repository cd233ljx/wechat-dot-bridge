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

    def test_new_inbound_context_reuses_peer_ticket(self):
        self.indicator.begin(self.mid)
        self.indicator.tick()
        self.store.ingest('fixture-account', [dict(source_id='2', peer='fixture-owner',
            text='next', created_at=1800000001, context_token='new-context')], '2')
        self.now = 5
        self.indicator.tick()
        self.assertEqual(self.client.typing_ticket.call_count, 1)
        self.assertEqual(self.client.send_typing.call_count, 2)
        self.now = 601
        self.indicator.progress(self.mid)
        self.indicator.tick()
        self.assertEqual(self.client.typing_ticket.call_count, 2)
        self.assertEqual(self.client.typing_ticket.call_args.args[1], 'new-context')

    def test_transient_failure_recovers_during_short_reply_window(self):
        self.client.typing_ticket.side_effect = [RuntimeError('PRIVATE'), 'fixture-ticket']
        self.indicator.begin(self.mid)
        self.indicator.tick()
        self.now = 4
        self.indicator.tick()
        self.client.send_typing.assert_not_called()
        self.now = 5
        self.indicator.tick()
        self.client.send_typing.assert_called_once()
        self.assertEqual(self.client.send_typing.call_args.args[-1], 1)
        self.assertIsNone(self.indicator.status()['last_error'])

    def test_prewarm_does_not_show_typing_and_read_uses_cached_ticket(self):
        self.indicator.prewarm()
        self.client.typing_ticket.assert_called_once()
        self.client.send_typing.assert_not_called()
        self.assertEqual(self.store.work_status(self.mid)['state'], 'waiting')
        self.indicator.begin(self.mid)
        self.indicator.prewarm()
        self.indicator.tick()
        self.client.typing_ticket.assert_called_once()
        self.assertEqual(self.client.send_typing.call_args.args[-1], 1)

    def test_prewarm_failure_backoff_survives_idle_ticks_and_active_work(self):
        self.client.typing_ticket.side_effect = RuntimeError('PRIVATE')
        for at, expected_calls in ((0, 1), (1, 1), (4, 1), (5, 2), (14, 2), (15, 3)):
            self.now = at
            self.indicator.prewarm()
            self.indicator.tick()
            self.assertEqual(self.client.typing_ticket.call_count, expected_calls)
        self.indicator.begin(self.mid)
        self.indicator.tick()
        previous = self.client.typing_ticket.call_count
        self.now += 1
        self.indicator.prewarm()
        self.indicator.tick()
        self.assertEqual(self.client.typing_ticket.call_count, previous)

    def test_persistent_failures_back_off_and_new_work_starts_immediately_after_idle(self):
        self.client.typing_ticket.side_effect = RuntimeError('PRIVATE')
        self.indicator.begin(self.mid)
        for at, next_at in [(0, 5), (5, 15), (15, 35), (35, 65)]:
            self.now = at
            self.indicator.tick()
            self.assertEqual(self.indicator.next_send, next_at)
        self.indicator.progress(self.mid, False)
        self.indicator.tick()
        self.client.typing_ticket.side_effect = None
        self.indicator.begin(self.mid)
        self.indicator.tick()
        self.client.send_typing.assert_called_once()

    def test_finishing_or_expiring_during_ticket_request_does_not_start_typing(self):
        for finish in ('stop', 'expire'):
            with self.subTest(finish=finish):
                indicator = TypingIndicator(self.store, self.client, lambda: self.now)
                indicator.begin(self.mid)
                def ticket(*args):
                    if finish == 'stop':
                        indicator.progress(self.mid, False)
                    else:
                        self.now += 301
                    return 'fixture-ticket'
                self.client.typing_ticket.side_effect = ticket
                indicator.tick()
                self.client.send_typing.assert_not_called()
                indicator.close()

    def test_status_exposes_fixed_outcomes_without_private_data(self):
        from wechat_dot_bridge.events import Events
        from wechat_dot_bridge.mcp import MCPServer
        import json
        self.client.send_typing.side_effect = RuntimeError('PRIVATE fixture-ticket fixture-context')
        self.indicator.begin(self.mid)
        self.indicator.tick()
        server = MCPServer(self.store, Events(self.store, []), typing=self.indicator)
        status = server._call({'name': 'get_weixin_status', 'arguments': {}})['structuredContent']['typing']
        self.assertEqual(status['outcomes'], {'ticket_ok': 1, 'start_failed': 1, 'stop_failed': 1})
        self.assertEqual(status['last_error'], 'stop_failed')
        for secret in ('PRIVATE', 'fixture-ticket', 'fixture-context', 'fixture-owner'):
            self.assertNotIn(secret, json.dumps(status))


if __name__ == "__main__":
    unittest.main()
