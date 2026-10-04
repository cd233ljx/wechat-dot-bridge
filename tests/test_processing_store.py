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

    def test_excluded_members_old_token_cannot_modify_new_group(self):
        for operation in ('heartbeat', 'completion', 'interim_reply', 'final_reply'):
            with self.subTest(operation=operation):
                a, b, c = [self.message(operation + source) for source in ('a', 'b', 'c')]
                for mid in (a, b, c):
                    self.token(mid)
                group = self.store.report_progress(a, message_ids=[a, b, c])['work']
                old = {key: group[key] for key in ('processing_id', 'revision')}
                self.store.report_progress(a, message_ids=[a, b], **old)
                before = {mid: self.store.work_status(mid) for mid in (a, b, c)}
                replies_before = self.store.db.execute('SELECT count(*) FROM replies').fetchone()[0]
                with self.assertRaisesRegex(BridgeError, 'processing_generation_stale'):
                    if operation in ('heartbeat', 'completion'):
                        self.store.report_progress(c, active=operation == 'heartbeat',
                            completed=operation == 'completion', message_ids=[a, b, c], **old)
                    else:
                        self.store.queue_reply(c, operation, operation,
                            final=operation == 'final_reply', message_ids=[a, b, c], **old)
                self.assertEqual({mid: self.store.work_status(mid) for mid in (a, b, c)}, before)
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies').fetchone()[0], replies_before)
                self.assertEqual(set(self.store.work_members(a)), {a, b})
                self.assertEqual(self.store.work_members(c), [c])

    def test_partial_merge_retires_all_source_groups_and_preserves_remainders(self):
        a, b, c, d, e, f = [self.message(source) for source in 'abcdef']
        for mid in (a, b, c, d, e, f):
            self.token(mid)
        left = self.store.report_progress(a, message_ids=[a, b, c])['work']
        left_token = {key: left[key] for key in ('processing_id', 'revision')}
        self.now += 30
        right = self.store.report_progress(d, message_ids=[d, e, f])['work']
        right_token = {key: right[key] for key in ('processing_id', 'revision')}
        before = {mid: self.store.work_status(mid) for mid in (a, b, c, d, e, f)}
        mirrors = dict(self.store.db.execute('SELECT message_id,expires FROM processing'))
        self.now += 20
        merged = self.store.report_progress(a, message_ids=[a, d], **left_token)['work']
        self.assertEqual(set(self.store.work_members(a)), {a, d})
        self.assertEqual(set(self.store.work_members(b)), {b, c})
        self.assertEqual(set(self.store.work_members(e)), {e, f})
        self.assertEqual(len({self.store.work_status(mid)['processing_id'] for mid in (a, b, e)}), 3)
        for mid in (b, c, e, f):
            value = self.store.work_status(mid)
            self.assertNotEqual(value['processing_id'], before[mid]['processing_id'])
            self.assertGreater(value['revision'], before[mid]['revision'])
            self.assertEqual(value['state'], before[mid]['state'])
            self.assertEqual(value['expires_at'], before[mid]['expires_at'])
            self.assertEqual(self.store.db.execute('SELECT expires FROM processing WHERE message_id=?', (mid,)).fetchone()[0], mirrors[mid])
        for mid, old in ((b, left_token), (e, right_token)):
            with self.assertRaisesRegex(BridgeError, 'processing_generation_stale'):
                self.store.queue_reply(mid, 'late merged completion', 'late-' + mid,
                                       message_ids=[a, b, c, d, e, f], **old)
        current = {key: merged[key] for key in ('processing_id', 'revision')}
        self.store.report_progress(a, active=False, completed=True, **current)
        self.assertEqual({row['message_id'] for row in self.store.list_pending()}, {b, c, e, f})
        recovered = self.store.read_message(b)['processing']
        self.assertEqual(recovered['processing_id'], self.store.work_status(c)['processing_id'])
        self.assertEqual(recovered['expires_at'], left['expires_at'])

    def test_regrouping_preserves_excluded_terminal_or_expired_state(self):
        for state in ('completed', 'interrupted', 'failed', 'expired'):
            with self.subTest(state=state):
                a, b, c = [self.message(state + source) for source in ('a', 'b', 'c')]
                for mid in (a, b, c):
                    self.token(mid)
                self.store.report_progress(a, message_ids=[a, b, c])
                if state in ('completed', 'interrupted'):
                    self.store.report_progress(a, active=False, completed=state == 'completed')
                elif state == 'failed':
                    self.store.db.execute("UPDATE message_work SET state='failed',lease_until=NULL WHERE message_id IN (?,?,?)", (a, b, c))
                    self.store.db.execute('DELETE FROM processing WHERE message_id IN (?,?,?)', (a, b, c))
                else:
                    self.now += 301
                previous = {mid: dict(self.store.db.execute('SELECT * FROM message_work WHERE message_id=?', (mid,)).fetchone()) for mid in (b, c)}
                self.store.queue_reply(a, 'new scoped progress', state, final=False, message_ids=[a])
                for mid in (b, c):
                    row = self.store.db.execute('SELECT * FROM message_work WHERE message_id=?', (mid,)).fetchone()
                    self.assertNotEqual(row['run_id'], previous[mid]['run_id'])
                    self.assertGreater(row['revision'], previous[mid]['revision'])
                    for key in ('state', 'lease_until', 'read_at'):
                        self.assertEqual(row[key], previous[mid][key])
                self.assertEqual(set(self.store.work_members(b)), {b, c})
                if state != 'completed':
                    resumed = self.store.read_message(b)['processing']
                    self.assertEqual(resumed['state'], 'active')
                    self.assertEqual(self.store.work_status(c), resumed)

    def test_failed_scope_transition_rolls_back_remainders_and_reply(self):
        for operation in ('progress', 'reply'):
            with self.subTest(operation=operation):
                a, b, c = [self.message(operation + source) for source in ('a', 'b', 'c')]
                for mid in (a, b, c):
                    self.token(mid)
                self.store.report_progress(a, message_ids=[a, b, c])
                before = [tuple(row) for row in self.store.db.execute('SELECT * FROM message_work ORDER BY message_id')]
                mirrors = [tuple(row) for row in self.store.db.execute('SELECT * FROM processing ORDER BY message_id')]
                replies = self.store.db.execute('SELECT count(*) FROM replies').fetchone()[0]
                # Abort the selected member update after the excluded members
                # have already been forked and the reply has been inserted.
                self.store.db.execute("CREATE TEMP TRIGGER abort_scope_change BEFORE UPDATE ON message_work WHEN NEW.message_id='" + a + "' BEGIN SELECT RAISE(ABORT, 'fixture_scope_failure'); END")
                try:
                    with self.assertRaisesRegex(BridgeError, 'database_unavailable'):
                        if operation == 'progress':
                            self.store.report_progress(a, message_ids=[a, b])
                        else:
                            self.store.queue_reply(a, 'scoped progress', 'rollback', final=False, message_ids=[a, b])
                finally:
                    self.store.db.execute('DROP TRIGGER abort_scope_change')
                self.assertEqual([tuple(row) for row in self.store.db.execute('SELECT * FROM message_work ORDER BY message_id')], before)
                self.assertEqual([tuple(row) for row in self.store.db.execute('SELECT * FROM processing ORDER BY message_id')], mirrors)
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies').fetchone()[0], replies)

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
