import base64
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.operations import HealthMonitor,diagnostics,subscriptions,backup
from wechat_dot_bridge.artifacts import detect_kind,stage_bytes,download,url_parts
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.net_transport import NetworkFailure
from test_media import picture


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=1000000
        self.store=Store(Path(self.tmp.name)/'state',clock=lambda:self.now)
        self.store.import_binding(dict(id='bot',owner='owner',token='PRIVATE_TOKEN',base_url='https://ilinkai.weixin.qq.com'))
        self.store.set_enabled(True)
        self.store.ingest('bot',[dict(source_id='1',peer='owner',text='PRIVATE_TEXT',created_at=self.now,context_token='PRIVATE_CONTEXT')],'1')
        self.mid=self.store.list_pending()[0]['message_id']
        self.events=Events(self.store,['callback.example.com'],clock=lambda:self.now)

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def test_failure_notice_preserves_original_reply_status(self):
        self.store.queue_reply(self.mid,'failed response')
        failed=self.store.claim_send()
        self.store.finish_send(self.mid,'failed',reply_id=failed['reply_id'])
        self.now+=301
        monitor=HealthMonitor(self.store);monitor.tick()
        reply=self.store.claim_send()
        self.assertTrue(reply['text'].startswith('【桥接状态】'))
        self.store.finish_send(self.mid,'accepted',reply_id=reply['reply_id'])
        self.assertEqual(self.store.db.execute('SELECT state FROM messages WHERE id=?',(self.mid,)).fetchone()[0],'reply_committed')
        self.assertEqual(self.store.send_status(self.mid)['status'],'failed')
        self.now+=301;HealthMonitor(self.store).tick()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies WHERE is_notice=1').fetchone()[0],1)
        self.store.queue_reply(self.mid,'actual response')
        reply=self.store.claim_send();self.store.finish_send(self.mid,'accepted',reply_id=reply['reply_id'])
        self.assertEqual(self.store.list_pending(),[])

    def test_waiting_messages_remain_diagnostic_without_automatic_notices(self):
        self.store.ingest('bot',[dict(source_id='2',peer='owner',text='file task',created_at=self.now,context_token='PRIVATE_CONTEXT')],'2')
        second=self.store.list_pending()[-1]['message_id']
        self.store.record_tool_call('read_weixin_message',second,'ok')
        self.store.report_progress(second)
        self.now+=601
        for _ in range(3):
            HealthMonitor(self.store).tick()
            self.now+=301
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(len(self.store.list_pending()),2)
        self.assertEqual({r['code'] for r in diagnostics(self.store)['issues']},
                         {'message_unread','processing_stalled','subscription_inactive'})

    def test_uncertain_send_never_triggers_notice_or_resend(self):
        self.store.queue_reply(self.mid,'response')
        reply=self.store.claim_send()
        self.store.finish_send(self.mid,'uncertain',reply_id=reply['reply_id'])
        self.now+=601
        HealthMonitor(self.store).tick()
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.send_status(self.mid)['status'],'uncertain')

    def test_rate_limited_reply_remains_diagnostic_without_extra_notice(self):
        self.store.queue_reply(self.mid, 'response')
        reply = self.store.claim_send()
        self.store.finish_send(self.mid, 'failed', 'rate_limited', reply_id=reply['reply_id'])
        self.now += 601
        for _ in range(3):
            HealthMonitor(self.store).tick()
            self.now += 31
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies WHERE is_notice=1').fetchone()[0], 0)
        self.assertIn({'message_id': self.mid, 'code': 'reply_failed', 'error': 'rate_limited'},
                      diagnostics(self.store)['issues'])

    def test_prior_other_failure_does_not_bypass_later_rate_limit(self):
        self.store.queue_reply(self.mid, 'earlier response', reply_key='earlier')
        first = self.store.claim_send()
        self.store.finish_send(self.mid, 'failed', 'send_rejected', reply_id=first['reply_id'])
        self.store.queue_reply(self.mid, 'later response', reply_key='later')
        second = self.store.claim_send()
        self.store.finish_send(self.mid, 'failed', 'rate_limited', reply_id=second['reply_id'])
        self.now += 601
        HealthMonitor(self.store).tick()
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies WHERE is_notice=1').fetchone()[0], 0)

    def test_active_progress_suppresses_stall_notice(self):
        self.now+=301;self.store.report_progress(self.mid)
        HealthMonitor(self.store).tick()
        self.assertIsNone(self.store.claim_send())

    def test_diagnostics_and_backup_restore_keep_private_data_out_of_output(self):
        self.now+=301
        result=diagnostics(self.store)
        self.assertIn('message_unread',[r['code'] for r in result['issues']])
        self.assertNotIn('PRIVATE',json.dumps(result))
        dest=Path(self.tmp.name)/'backup.sqlite3'
        self.assertEqual(backup(self.store,dest)['integrity'],'ok')
        self.assertEqual(dest.stat().st_mode&0o777,0o600)
        recovered=Path(self.tmp.name)/'restored';recovered.mkdir(mode=0o700)
        import shutil
        shutil.copyfile(dest,recovered/'bridge.sqlite3');(recovered/'bridge.sqlite3').chmod(0o600)
        other=Store(recovered,clock=lambda:self.now)
        self.assertEqual(other.list_pending(),self.store.list_pending());other.close()
        with self.assertRaises(FileExistsError):backup(self.store,dest)

    def test_auto_kind_and_repeated_large_import(self):
        data=picture()
        self.assertEqual(detect_kind(data,'unknown.bin'),'image')
        a=stage_bytes(self.store,data,'photo.bin')
        self.assertEqual(a['kind'],'image')
        huge=b'a'*(1024*1024+10)
        a=stage_bytes(self.store,huge,'example.txt');b=stage_bytes(self.store,huge,'example.txt')
        self.assertEqual(a['attachment_id'],b['attachment_id'])
        self.assertEqual(self.store.read_attachment(a['attachment_id'])['data'],huge)

    def test_import_rejects_internal_paths_and_private_dns(self):
        for url in ['sandbox:/mnt/data/private.txt','http://example.com/file','https://user:secret@example.com/file']:
            with self.assertRaises(BridgeError):url_parts(url)
        with patch('wechat_dot_bridge.artifacts.resolve_addresses',side_effect=NetworkFailure('dns_blocked')):
            with self.assertRaisesRegex(BridgeError,'artifact_endpoint_rejected'):download('https://example.com/file?PRIVATE_SECRET')

    def test_https_import_revalidates_redirect_and_omits_auth(self):
        from wechat_dot_bridge.net_transport import NetworkResponse
        first = NetworkResponse(302, b'', ((b'location', b'https://cdn.example.com/file?PRIVATE_SECRET'),))
        second = NetworkResponse(200, b'hello', ((b'content-length', b'5'),))
        with patch('wechat_dot_bridge.artifacts.resolve_addresses', return_value=[(2, 1, 6, ('93.184.216.34', 443))]) as dns, patch('wechat_dot_bridge.artifacts.request', side_effect=[first, second]) as request:
            self.assertEqual(download('https://example.com/file'), b'hello')
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[0], 'cdn.example.com')
        self.assertEqual(request.call_args.args[5], {})
        self.assertEqual(request.call_args.kwargs['body_statuses'], {200})

    def test_admin_cancel_does_not_modify_owner_messages(self):
        params={'name':'weixin.message','arguments':{},'delivery':{'mode':'webhook','url':'https://callback.example.com/private'}}
        sid,principal,url,_=self.events._identity(params,False)
        self.store.db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?,?,NULL,NULL,0,0,'active',0)",(sid,principal,'weixin.message','{}',url,'PRIVATE_SECRET'))
        self.events.cancel_by_id(sid)
        self.assertEqual(subscriptions(self.store)[0]['status'],'cancelled')
        self.assertEqual(len(self.store.list_pending()),1)
        self.assertNotIn('PRIVATE',str(subscriptions(self.store)))

    def test_notice_does_not_cancel_retrying_event(self):
        params={'name':'weixin.message','arguments':{},'delivery':{'mode':'webhook','url':'https://callback.example.com/private'}}
        sid,principal,url,_=self.events._identity(params,False)
        secret='whsec_'+base64.b64encode(b'0'*32).decode()
        self.store.db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?,?,NULL,NULL,0,0,'active',0)",(sid,principal,'weixin.message','{}',url,secret))
        self.events.post=lambda *args:(503,b'')
        self.events.deliver_once()
        self.now+=301;HealthMonitor(self.store).tick()
        self.events.deliver_once()
        self.assertEqual(self.store.db.execute('SELECT status FROM deliveries').fetchone()[0],'retry')

    def test_operator_resolution_preserves_accepted_parts(self):
        self.store.queue_reply(self.mid,'x'*10000)
        first=self.store.claim_send();self.store.finish_send(self.mid,'accepted',reply_id=first['reply_id'])
        second=self.store.claim_send();self.store.finish_send(self.mid,'uncertain',reply_id=second['reply_id'])
        self.store.resolve_uncertain(second['reply_id'],True)
        third=self.store.claim_send()
        self.assertNotEqual(third['client_id'],second['client_id'])
        self.assertEqual(self.store.send_status(self.mid)['parts_accepted'],2)

    def test_restore_refuses_existing_database(self):
        from wechat_dot_bridge.operations import restore
        dest=Path(self.tmp.name)/'snapshot';backup(self.store,dest)
        restored=Path(self.tmp.name)/'copy';restored.mkdir(mode=0o700)
        self.assertTrue(restore(dest,restored)['restored'])
        with self.assertRaises(FileExistsError):restore(dest,restored)
