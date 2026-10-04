import base64
import io
import tempfile
import unittest
import wave
from unittest.mock import Mock
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge import audio


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000000
        self.store = Store(self.tmp.name, clock=lambda: self.now)
        self.store.import_binding(dict(id='bot',owner='owner',token='fixture',base_url='https://ilinkai.weixin.qq.com'))
        self.store.set_enabled(True)
        self.store.ingest('bot',[dict(source_id='message:1',peer='owner',text='fixture',created_at=self.now,context_token='fixture')],'1')
        self.mid = self.store.list_pending()[0]['message_id']

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_retention_preserves_queued_and_uncertain_but_releases_completed(self):
        aid = self.store.stage_attachment(base64.b64encode(b'fixture').decode(),'file','test.txt')['attachment_id']
        self.store.queue_reply(self.mid,'caption',attachment_id=aid)
        self.now += 8*86400
        self.assertEqual(self.store.cleanup_media(),0)
        reply = self.store.claim_send()
        self.store.finish_send(self.mid,'accepted',reply_id=reply['reply_id'])
        reply = self.store.claim_send()
        self.store.finish_send(self.mid,'uncertain',reply_id=reply['reply_id'])
        self.assertEqual(self.store.cleanup_media(),0)
        # Explicit rejection is safe to retry, unlike uncertain delivery.
        with self.assertRaisesRegex(BridgeError,'delivery_uncertain'):
            self.store.retry_reply(reply['reply_id'])
        self.store.db.execute("UPDATE replies SET status='failed' WHERE client_id=?",(reply['reply_id'],))
        self.assertEqual(self.store.cleanup_media(),1)
        self.store.stage_attachment(base64.b64encode(b'fixture').decode(),'file','test.txt')
        self.assertEqual(self.store.read_attachment(aid)['data'],b'fixture')

    def test_progress_survives_interim_reply_and_final_stops_typing(self):
        self.store.queue_reply(self.mid,'working',final=False)
        self.assertIsNotNone(self.store.typing_target([self.mid]))
        self.store.queue_reply(self.mid,'complete')
        self.assertIsNone(self.store.typing_target([self.mid]))
        with self.assertRaisesRegex(BridgeError, 'processing_already_completed'):
            self.store.report_progress(self.mid)
        self.assertEqual(self.store.work_status(self.mid)['state'], 'completed')
        self.assertIsNone(self.store.typing_target([self.mid]))
        self.now += 301
        self.assertIsNone(self.store.typing_target([self.mid]))

    def test_server_longpoll_timeout_and_cancel(self):
        client = IlinkClient()
        client._post = Mock(return_value={'longpolling_timeout_ms':35000})
        client.poll(self.store.get_binding())
        client.poll(self.store.get_binding())
        self.assertEqual(client._post.call_args.args[-1],40)
        connection=Mock();client.connections.add(connection);client.cancel()
        connection.cancel.assert_called_once()

    def test_tool_diagnostics_omit_message_and_credentials(self):
        self.store.record_tool_call('read_weixin_message',self.mid,'ok')
        result=self.store.get_status()
        self.assertTrue(result['pending_processing'][0]['read_observed'])
        self.assertNotIn('fixture',str(result))

    @unittest.skipUnless(audio.available(),'optional local codecs not installed')
    def test_real_wav_silk_wav_conversion(self):
        out=io.BytesIO()
        with wave.open(out,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
            wav.writeframes(b'\0\0'*24000)
        silk=audio.to_silk(out.getvalue())
        self.assertTrue(audio.is_silk(silk))
        decoded=audio.to_wav(silk)
        with wave.open(io.BytesIO(decoded)) as wav:
            self.assertEqual(wav.getframerate(),24000)
            self.assertGreater(wav.getnframes(),23000)
        with self.assertRaisesRegex(BridgeError,'media_audio_conversion_failed'):
            audio.to_silk(b'not audio')

    @unittest.skipUnless(__import__("shutil").which("node"),"optional Node not installed")
    def test_upstream_markdown_keeps_supported_constructs(self):
        from wechat_dot_bridge.message_format import markdown_to_text
        self.assertEqual(markdown_to_text('##### 标题\n*中文* **bold**\n```\n*原样*\n```'), '标题\n中文 **bold**\n```\n*原样*\n```')

    def test_text_file_staging_and_diagnostic(self):
        from wechat_dot_bridge.mcp import MCPServer
        from wechat_dot_bridge.events import Events
        server=MCPServer(self.store,Events(self.store))
        response=server._call({'name':'stage_weixin_attachment','arguments':{'text_content':'中文文件','kind':'file','name':'example.txt'}})
        aid=response['structuredContent']['attachment_id']
        self.assertEqual(self.store.read_attachment(aid)['data'],'中文文件'.encode())
        self.assertNotIn('中文文件',str(self.store.get_status()))

    def test_stale_session_cooldown_survives_restart_and_manual_disable(self):
        self.store.pause_session()
        self.store.resume_session()
        self.assertFalse(self.store.get_binding()['enabled'])
        self.now += 3601
        self.store.resume_session()
        self.assertTrue(self.store.get_binding()['enabled'])
        self.store.pause_session()
        self.store.set_enabled(False)
        self.now += 3601
        self.store.resume_session()
        self.assertFalse(self.store.get_binding()['enabled'])
