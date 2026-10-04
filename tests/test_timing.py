import json
import tempfile
import unittest
from pathlib import Path
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.timing import record, report


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=1000000.
        self.store=Store(Path(self.tmp.name)/'state',clock=lambda:self.now)
        self.store.import_binding(dict(id='bot',owner='owner',token='PRIVATE_TOKEN',base_url='https://ilinkai.weixin.qq.com'))
        self.store.set_enabled(True)
        self.store.ingest('bot',[dict(source_id='1',peer='owner',text='PRIVATE_TEXT',created_at=10,received_at=self.now,context_token='PRIVATE_CONTEXT')],'1')
        self.mid=self.store.list_pending()[0]['message_id']

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_phases_clock_domains_and_durable_tool_measurement(self):
        record(self.store,self.mid,'event_start',at=self.now+.2)
        record(self.store,self.mid,'event_result',at=self.now+.5,outcome='acknowledged',elapsed_ms=300)
        self.now+=89.502
        self.store.record_tool_call('read_weixin_message',self.mid,'ok',started_at=self.now-.002,elapsed_ms=2)
        self.now+=1
        self.store.queue_reply(self.mid,'PRIVATE_REPLY')
        self.store.record_tool_call('reply_weixin_message',self.mid,'ok',started_at=self.now,elapsed_ms=3)
        self.now+=.1
        reply=self.store.claim_send()
        self.now+=.3
        self.store.finish_send(self.mid,'accepted',reply_id=reply['reply_id'])
        self.store.db.execute('DELETE FROM tool_calls')
        result=report(self.store)
        phase=result['messages'][0]['phase_ms']
        self.assertEqual(phase['receive_to_event'],200)
        self.assertEqual(phase['callback_to_read'],89000)
        self.assertEqual(phase['read_server'],2)
        self.assertEqual(phase['reply_tool_server'],3)
        self.assertEqual(phase['queue_to_send'],100)
        self.assertEqual(phase['send_to_first_accept'],300)
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_old_rows_are_not_fabricated_and_notices_do_not_count(self):
        self.store.db.execute('DELETE FROM message_timeline')
        self.store.queue_reply(self.mid,'notice',notice=True)
        reply=self.store.claim_send()
        self.store.finish_send(self.mid,'accepted',reply_id=reply['reply_id'])
        result=report(self.store)['messages'][0]
        self.assertIsNone(result['timestamps_unix']['bridge_received'])
        self.assertIsNone(result['timestamps_unix']['weixin_first_accepted'])
        self.assertIsNone(result['phase_ms']['receive_to_first_accept'])
