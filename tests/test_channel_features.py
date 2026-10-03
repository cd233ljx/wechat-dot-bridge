import base64
import hashlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import _normalize_message
from wechat_dot_bridge.media import upload_item
from wechat_dot_bridge.message_format import partial_quote, split_text
from wechat_dot_bridge.errors import BridgeError


class ChannelFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.binding = dict(id='bot', owner='owner', token='fixture', base_url='https://ilinkai.weixin.qq.com')
        self.store.import_binding(self.binding)
        self.store.set_enabled(True)
        self.store.ingest('bot', [dict(source_id='message:1', peer='owner', text='fixture', created_at=1, context_token='fixture')], 'c')
        self.mid = self.store.list_pending()[0]['message_id']

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def restart(self):
        self.store.close()
        self.store = Store(self.tmp.name)
        self.store.recover_inflight()

    def test_long_unicode_reply_survives_restart_without_resending_accepted_parts(self):
        text = '中文🙂abc\n' * 3000
        receipt = self.store.queue_reply(self.mid, text, 'long')
        self.assertGreater(receipt['parts_total'], 3)
        chunks, ids = [], []
        for _ in range(receipt['parts_total']):
            reply = self.store.claim_send()
            chunks.append(reply['text']); ids.append(reply['client_id'])
            self.assertLessEqual(len(reply['text'].encode()), 8192)
            self.assertLessEqual(len(reply['text']), 3800)
            self.store.finish_send(self.mid, 'accepted', reply_id=reply['reply_id'])
            self.restart()
        self.assertEqual(''.join(chunks), text)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertIsNone(self.store.claim_send())
        status = self.store.queue_reply(self.mid, text, 'long')
        self.assertEqual(status['status'], 'accepted')
        self.assertEqual(status['parts_accepted'], status['parts_total'])

    def test_uncertain_part_stops_remaining_parts_but_allows_completion(self):
        self.store.queue_reply(self.mid, 'x' * 12000)
        first = self.store.claim_send()
        self.store.finish_send(self.mid, 'accepted', reply_id=first['reply_id'])
        second = self.store.claim_send()
        self.restart()
        status = self.store.send_status(self.mid, second['reply_id'])
        self.assertEqual(status['status'], 'uncertain')
        self.assertEqual(status['parts_accepted'], 1)
        self.assertIsNone(self.store.claim_send())
        self.store.queue_reply(self.mid, 'completion')
        self.assertEqual(self.store.claim_send()['text'], 'completion')

    def test_chunk_upload_retries_and_restart_and_last_chunk_idempotency(self):
        a, b = b'a' * (1024 * 1024), b'b' * 123
        stage = self.store.stage_attachment
        args = (base64.b64encode(a).decode(), 'file', 'test.bin', 'upload', 0, False)
        self.assertEqual(stage(*args)['next_offset'], len(a))
        self.assertEqual(stage(*args)['next_offset'], len(a))
        self.restart()
        args = (base64.b64encode(b).decode(), 'file', 'test.bin', 'upload', len(a), True)
        result = self.store.stage_attachment(*args)
        self.assertEqual(self.store.read_attachment(result['attachment_id'])['data'], a+b)
        self.restart()
        self.assertEqual(self.store.stage_attachment(*args), result)
        with self.assertRaises(BridgeError):
            self.store.stage_attachment(base64.b64encode(b'changed').decode(), 'file', 'test.bin', 'upload', len(a), True)

    def test_inline_quote_and_partial_quote_and_voice_asr(self):
        ref = dict(message_item=dict(type=1, text_item=dict(text='before selected after')), partial_text=dict(start='selected', end='selected', startindex=0, endindex=0, quotemd5=hashlib.md5(b'selected').hexdigest()))
        msg = dict(message_id=2, from_user_id='owner', message_type=1, message_state=2, item_list=[dict(type=1, text_item=dict(text='explain'), ref_msg=ref)])
        result = _normalize_message(msg, self.binding, 1)
        self.assertIn('[引用]\nselected\n[/引用]', result['text'])
        self.assertIn('explain', result['text'])
        msg['item_list'] = [dict(type=3, voice_item=dict(text='语音转写'))]
        self.assertEqual(_normalize_message(msg, self.binding, 1)['text'], '语音转写')
        self.assertEqual(partial_quote('hello', dict(start='x')), 'hello')

    def test_native_amr_and_silk_and_unknown_audio_file_fallback(self):
        client = Mock()
        client._post.return_value = {'upload_param': 'fixture'}
        for data, expected, encoding in [(b'#!AMR\nfixture', 3, 0), (b'\x02#!SILK_V3fixture', 3, 1), (b'RIFFfixture', 4, None)]:
            with patch('wechat_dot_bridge.media.cdn_request', return_value=(b'', 'fixture')):
                item = upload_item(client, self.binding, dict(data=data, kind='voice', name='voice.bin'))
            self.assertEqual(item['type'], expected)
            if expected == 3:
                self.assertEqual(item['voice_item']['encode_type'], encoding)

    def test_quote_id_cache_restored_after_restart(self):
        self.store.remember_sent_quote('123', {'text': 'previous bot reply'})
        self.restart()
        for sid, expected in [('1', 'fixture'), ('123', 'previous bot reply')]:
            wire = dict(message_id=int(sid)+1000, from_user_id='owner', message_type=1, message_state=2,
                        item_list=[dict(type=1, text_item=dict(text='explain'), ref_msg=dict(svr_id=sid))])
            msg = _normalize_message(wire, self.binding, 2)
            self.store.ingest('bot', [msg], 'next')
            row = self.store.db.execute('SELECT text FROM messages WHERE source_id=?', (msg['source_id'],)).fetchone()
            self.assertIn('[引用缓存]\n' + expected, row['text'])

    def test_markdown_preserves_code_and_urls(self):
        from wechat_dot_bridge.message_format import markdown_to_text
        rendered = markdown_to_text('# Title\n**bold** [link](https://example.com)\n```python\nvalue = "**keep**"\n```\n')
        self.assertEqual(rendered, 'Title\nbold link (https://example.com)\nvalue = "**keep**"\n')
