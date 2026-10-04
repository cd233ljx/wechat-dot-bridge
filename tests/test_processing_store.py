"""Processing recovery and reply idempotency contracts, separate from sends."""
import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.mcp import MCPServer
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.typing import TypingIndicator


class ProcessingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state'
        self.now = 1000
        self.store = Store(self.path, clock=lambda: self.now)
        self.store.import_binding(dict(id='bot', owner='owner', token='fixture-token',
            base_url='https://ilinkai.weixin.qq.com'))
        self.store.set_enabled(True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def message(self, source):
        self.store.ingest('bot', [dict(source_id=source, peer='owner', text='fixture',
            created_at=self.now, context_token='fixture-context')], source)
        return self.store.db.execute('SELECT id FROM messages WHERE source_id=?', (source,)).fetchone()[0]

    def token(self, mid):
        value = self.store.read_message(mid)['processing']
        return {key: value[key] for key in ('processing_id', 'revision')}

    def test_interim_accepted_survives_restart_and_replay_without_execution(self):
        mid = self.message('a')
        first = self.store.queue_reply(mid, 'progress', 'p', final=False)
        claimed = self.store.claim_send()
        self.store.finish_send(mid, 'accepted', reply_id=claimed['reply_id'])
        self.assertEqual(self.store.list_pending()[0]['message_id'], mid)
        self.store.close()
        self.store = Store(self.path, clock=lambda: self.now)
        self.store.recover_inflight()
        self.store.recover_work()
        self.assertEqual(self.store.work_status(mid)['state'], 'interrupted')
        self.assertEqual(self.store.queue_reply(mid, 'progress', 'p', final=False)['reply_id'], first['reply_id'])
        self.assertEqual(self.store.work_status(mid)['state'], 'interrupted')
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(len(self.store.list_pending()), 1)

    def test_stale_request_cannot_commit_reply_or_complete_new_generation(self):
        mid = self.message('a')
        old = self.token(mid)
        self.store.recover_work()
        new = self.token(mid)
        self.assertNotEqual(old, new)
        with self.assertRaisesRegex(BridgeError, 'processing_generation_stale'):
            self.store.queue_reply(mid, 'late', 'late', **old)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies').fetchone()[0], 0)
        self.assertEqual(self.store.work_status(mid)['state'], 'active')

    def test_late_first_interim_with_completed_token_cannot_restart(self):
        mid = self.message('a')
        token = self.token(mid)
        self.store.queue_reply(mid, 'done', 'done', **token)
        with self.assertRaisesRegex(BridgeError, 'processing_already_completed'):
            self.store.queue_reply(mid, 'late progress', 'late', final=False, **token)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies').fetchone()[0], 1)
        self.assertEqual(self.store.work_status(mid)['state'], 'completed')

    def test_completed_token_cannot_enqueue_new_final_or_consume_another_task(self):
        a, b = self.message('a'), self.message('b')
        token = self.token(a)
        self.token(b)
        first = self.store.queue_reply(a, 'done', 'done', **token)
        self.assertEqual(self.store.queue_reply(a, 'done', 'done', **token), first)
        for scope in (None, [a, b]):
            with self.assertRaisesRegex(BridgeError, 'processing_already_completed'):
                self.store.queue_reply(a, 'late final', 'late', message_ids=scope, **token)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies').fetchone()[0], 1)
        self.assertEqual(self.store.work_status(b)['state'], 'active')
        # An intentional additional owner reply remains supported without
        # claiming that the previous processing generation is still live.
        self.store.queue_reply(a, 'additional reply', 'additional')

    def test_reply_key_binds_final_flag_and_exact_message_scope(self):
        a, b, c = [self.message(source) for source in ('a', 'b', 'c')]
        for mid in (a, b, c):
            self.token(mid)
        first = self.store.queue_reply(a, 'progress', 'p', final=False, message_ids=[a, b])
        before = self.store.work_status(a)
        self.now += 20
        self.assertEqual(self.store.queue_reply(a, 'progress', 'p', final=False, message_ids=[b, a]), first)
        self.assertEqual(self.store.work_status(a), before)
        for kwargs in (dict(final=True, message_ids=[a, b]),
                       dict(final=False, message_ids=[a, c]), dict(final=False)):
            with self.assertRaisesRegex(BridgeError, 'reply_key_conflict'):
                self.store.queue_reply(a, 'progress', 'p', **kwargs)
        self.assertEqual(set(self.store.work_members(a)), {a, b})
        self.assertEqual(self.store.work_status(c)['state'], 'active')

    def test_group_reply_returns_new_token_for_next_heartbeat_and_completion(self):
        a, b = self.message('a'), self.message('b')
        token = self.token(a)
        self.token(b)
        progress = self.store.queue_reply(a, 'progress', 'p', final=False,
                                         message_ids=[a, b], **token)
        value = progress['work']
        renewed_token = {key: value[key] for key in ('processing_id', 'revision')}
        self.assertNotEqual(token, renewed_token)
        self.now += 10
        self.store.report_progress(a, **renewed_token)
        final = self.store.queue_reply(a, 'done', 'done', **renewed_token)
        self.assertEqual(final['work']['state'], 'completed')
        self.assertEqual(self.store.work_status(b)['state'], 'completed')
        self.assertEqual(self.store.send_status(a)['work'], final['work'])

    def test_group_unread_guard_rolls_back_queue_and_membership(self):
        a, b = self.message('a'), self.message('b')
        self.token(a)
        with self.assertRaisesRegex(BridgeError, 'processing_message_unread'):
            self.store.queue_reply(a, 'done', message_ids=[a, b])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies').fetchone()[0], 0)
        self.assertEqual(self.store.work_members(a), [a])
        self.assertEqual(self.store.work_status(b)['state'], 'waiting')

    def test_old_interim_mcp_replay_after_final_does_not_show_typing(self):
        mid = self.message('a')
        client = Mock()
        client.typing_ticket.return_value = 'fixture-ticket'
        indicator = TypingIndicator(self.store, client, clock=lambda: self.now)
        server = MCPServer(self.store, Events(self.store), typing=indicator)
        server._call({'name': 'read_weixin_message', 'arguments': {'message_id': mid}})
        progress = {'name': 'reply_weixin_message', 'arguments': dict(message_id=mid, text='progress', reply_key='p', final=False)}
        server._call(progress)
        indicator.tick()
        server._call({'name': 'reply_weixin_message', 'arguments': dict(message_id=mid, text='done', reply_key='done')})
        indicator.tick()
        previous = client.send_typing.call_count
        self.assertFalse(server._call(progress).get('isError', False))
        self.now += 5
        indicator.tick()
        self.assertEqual(client.send_typing.call_count, previous)
        self.assertEqual(client.send_typing.call_args.args[-1], 2)
        self.assertEqual(self.store.list_pending(), [])
        indicator.close()

    def test_attachment_read_renews_only_live_processing(self):
        mid = self.message('a')
        aid = self.store.stage_attachment(base64.b64encode(b'fixture').decode(), 'file', 'fixture.txt')['attachment_id']
        self.store.db.execute('UPDATE attachments SET message_id=? WHERE id=?', (mid, aid))
        self.assertEqual(self.store.touch_attachment_work(aid), [])
        self.token(mid)
        self.now += 100
        self.assertEqual(self.store.touch_attachment_work(aid), [mid])
        self.assertEqual(self.store.work_status(mid)['expires_at'], self.now + 300)
        self.now += 301
        self.assertEqual(self.store.touch_attachment_work(aid), [])
        self.assertEqual(self.store.work_status(mid)['state'], 'interrupted')

    def test_expired_status_is_consistent_and_read_only(self):
        mid = self.message('a')
        self.token(mid)
        self.now += 301
        before = self.store.db.total_changes
        value = self.store.get_status()
        self.assertEqual(value['processing_status'], {'interrupted': 1})
        self.assertEqual(value['pending_processing'][0]['work']['state'], 'interrupted')
        self.assertIn('processing_stalled', [item['code'] for item in value['diagnostics']['issues']])
        self.assertEqual(before, self.store.db.total_changes)

    def test_send_failure_does_not_confuse_completion_with_acceptance(self):
        mid = self.message('a')
        self.store.queue_reply(mid, 'done')
        send = self.store.claim_send()
        self.store.finish_send(mid, 'uncertain', 'network_timeout', reply_id=send['reply_id'])
        self.assertEqual(self.store.work_status(mid)['state'], 'completed')
        self.assertEqual(self.store.send_status(mid)['status'], 'uncertain')
        self.assertIsNone(self.store.claim_send())
        self.assertIn('reply_uncertain', [issue['code'] for issue in self.store.get_status()['diagnostics']['issues']])
