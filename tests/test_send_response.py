"""Upstream sendMessage response semantics over real HTTPCore framing."""

import base64
import contextlib
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from network_fixtures import FakeSocket, FakeTimer
from wechat_dot_bridge import net_transport as net
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, WeixinWorker


class SendResponseTests(unittest.TestCase):
    def setUp(self):
        net.reset_cancellation()

    def tearDown(self):
        net.reset_cancellation()

    @contextlib.contextmanager
    def store(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            store = Store(state)
            store.import_binding({'id': 'fixture-account', 'owner': 'fixture-owner', 'token': 'fixture-token',
                                  'base_url': 'https://ilinkai.weixin.qq.com'})
            store.set_enabled(True)
            store.ingest('fixture-account', [{'source_id': 'fixture-source', 'peer': 'fixture-owner',
                                             'text': 'synthetic inbound', 'created_at': store.clock(),
                                             'context_token': 'fixture-context'}], 'fixture-cursor')
            try:
                yield store, state, store.list_pending()[0]['message_id']
            finally:
                store.close()

    @contextlib.contextmanager
    def network(self, status, body):
        raw = FakeSocket()
        tls = FakeSocket(b'HTTP/1.1 ' + str(status).encode() + b' fixture\r\nContent-Length: ' +
                         str(len(body)).encode() + b'\r\n\r\n' + body)
        context = Mock()
        context.wrap_socket.return_value = tls
        entries = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        with patch('wechat_dot_bridge.net_transport.socket.socket', return_value=raw) as factory, \
                patch('wechat_dot_bridge.net_transport.socket.getaddrinfo', return_value=entries), \
                patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=context), \
                patch('wechat_dot_bridge.net_transport.threading.Timer', FakeTimer):
            yield tls, factory

    def check_send(self, status, body, expected, error=None, cancel=False):
        net.reset_cancellation()
        with self.store() as (store, state, mid):
            queued = store.queue_reply(mid, 'synthetic reply')
            client = IlinkClient()
            with self.network(status, body) as (tls, factory):
                if cancel:
                    def cancelled_response(count):
                        net.cancel_all()
                        raise OSError('fixture shutdown')
                    tls.recv = cancelled_response
                self.assertTrue(WeixinWorker(store, client).send_once())
            result = store.send_status(mid)
            self.assertEqual(result['status'], expected)
            self.assertEqual(result.get('error'), error)
            self.assertEqual(result['reply_id'], queued['reply_id'])
            factory.assert_called_once()
            wire = b''.join(tls.sent)
            self.assertIn(b'POST /ilink/bot/sendmessage HTTP/1.1', wire)
            self.assertEqual(wire.count(b'"client_id"'), 1)
            store.close()
            net.reset_cancellation()
            restored = Store(state)
            try:
                restored.recover_inflight()
                self.assertEqual(restored.send_status(mid)['status'], expected)
                self.assertIsNone(restored.claim_send())
                with self.network(200, b'') as (_, factory):
                    self.assertFalse(WeixinWorker(restored, client).send_once())
                factory.assert_not_called()
            finally:
                restored.close()

    def test_empty_and_whitespace_http_200_are_accepted_without_resending(self):
        # cc-connect dfad194 client.go:175 accepts TrimSpace(raw)==empty.
        for body in (b'', b' \t\r\n ', b'{"ret":0}'):
            with self.subTest(body=body):
                self.check_send(200, body, 'accepted')

    def test_non_200_responses_keep_existing_fixed_outcomes(self):
        for status, expected, error in (
            (204, 'uncertain', 'http_error'), (500, 'uncertain', 'http_error'),
            (401, 'auth_required', 'auth_required'), (403, 'auth_required', 'auth_required'),
            (429, 'failed', 'rate_limited'), (302, 'failed', 'endpoint_rejected'),
        ):
            with self.subTest(status=status):
                self.check_send(status, b'', expected, error)

    def test_nonempty_invalid_json_and_application_errors_keep_existing_outcomes(self):
        for body, expected, error in (
            (b'not-json', 'uncertain', 'response_invalid'),
            (b'[]', 'uncertain', 'response_invalid'),
            (b'null', 'uncertain', 'response_invalid'),
            (b'{"ret":"0"}', 'uncertain', 'response_invalid'),
            (b'{"ret":-1}', 'failed', 'gateway_error'),
            (b'{"ret":-14}', 'auth_required', 'auth_required'),
            (b'{"ret":-2,"errmsg":"prepare failed"}', 'failed', 'rate_limited'),
            (b'{"ret":1,"errmsg":"context_token rejected"}', 'context_required', 'context_required'),
        ):
            with self.subTest(body=body):
                self.check_send(200, body, expected, error)

    def test_cancelled_after_post_write_stays_uncertain_without_resending(self):
        self.check_send(200, b'', 'uncertain', 'network_cancelled', cancel=True)

    def test_empty_poll_response_is_not_relaxed(self):
        with self.store() as (store, _, _), self.network(200, b''):
            self.assertFalse(WeixinWorker(store, IlinkClient()).poll_once())
            self.assertEqual(store.get_binding()['last_error'], 'response_invalid')
            self.assertEqual(store.get_binding()['cursor'], 'fixture-cursor')

    def test_empty_upload_preparation_response_never_reaches_sendmessage(self):
        with self.store() as (store, _, mid):
            staged = store.stage_attachment(base64.b64encode(b'{}').decode(), 'file', 'fixture.json')
            store.queue_reply(mid, '', attachment_id=staged['attachment_id'])
            with self.network(200, b'') as (tls, factory):
                self.assertTrue(WeixinWorker(store, IlinkClient()).send_once())
            self.assertEqual(store.send_status(mid)['status'], 'uncertain')
            self.assertEqual(store.send_status(mid)['error'], 'response_invalid')
            factory.assert_called_once()
            wire = b''.join(tls.sent)
            self.assertIn(b'POST /ilink/bot/getuploadurl HTTP/1.1', wire)
            self.assertNotIn(b'POST /ilink/bot/sendmessage', wire)


if __name__ == '__main__':
    unittest.main()
