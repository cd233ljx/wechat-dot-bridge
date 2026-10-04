"""Only an already-written getupdates response deadline is an empty poll."""

import contextlib
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpcore

from network_fixtures import FakeSocket, FakeTimer
from wechat_dot_bridge import net_transport as net
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, WeixinWorker


class PollDeadlineTests(unittest.TestCase):
    def setUp(self):
        net.reset_cancellation()
        FakeTimer.instances = []
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'state')
        self.store.import_binding({'id': 'fixture-account', 'owner': 'fixture-owner',
                                  'token': 'fixture-token', 'base_url': 'https://ilinkai.weixin.qq.com'})
        self.store.set_enabled(True)
        self.store.ingest('fixture-account', [], 'fixture-cursor')
        self.raw, self.tls = FakeSocket(), FakeSocket()
        self.context = Mock()
        self.context.wrap_socket.return_value = self.tls
        self.client = IlinkClient()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()
        net.reset_cancellation()

    @contextlib.contextmanager
    def network(self):
        entries = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        with patch('wechat_dot_bridge.net_transport.socket.socket', return_value=self.raw), \
                patch('wechat_dot_bridge.net_transport.socket.getaddrinfo', return_value=entries), \
                patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=self.context), \
                patch('wechat_dot_bridge.net_transport.threading.Timer', FakeTimer):
            yield

    def assert_empty_round(self):
        with self.network():
            worker = WeixinWorker(self.store, self.client)
            self.assertTrue(worker.poll_once())
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor')
        self.assertEqual(worker.poll_added, 0)
        self.assertEqual(self.store.get_status()['pending'], 0)
        self.assertNotIn('last_error', self.store.get_status())
        self.assertIn(b'POST /ilink/bot/getupdates HTTP/1.1', b''.join(self.tls.sent))

    def test_response_read_timeout_is_empty_round_and_keeps_cursor(self):
        self.tls.recv = Mock(side_effect=TimeoutError('synthetic deadline'))
        self.assert_empty_round()

    def test_timer_closing_socket_during_response_read_is_empty_round(self):
        def read(_):
            FakeTimer.instances[-1].fire()
            raise OSError('synthetic closed socket')
        self.tls.recv = read
        self.assert_empty_round()

    def test_partial_body_write_then_read_timeout_is_a_real_failure(self):
        send = self.tls.sendall
        def write(value):
            if b'"fixture-cursor"' in value:
                raise httpcore.WriteError('synthetic partial request write')
            send(value)
        self.tls.sendall = write
        self.tls.recv = Mock(side_effect=TimeoutError('synthetic response deadline'))
        with self.network():
            self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
        self.assertEqual(self.store.get_status()['last_error'], 'network_timeout')
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor')

    def test_deadline_expired_just_before_response_read_is_empty_round(self):
        send = self.tls.sendall
        def write(value):
            send(value)
            # HTTPCore writes h11's empty EndOfMessage after the complete
            # Content-Length body, immediately before its response read.
            if not value:
                FakeTimer.instances[-1].fire()
        self.tls.sendall = write
        self.assert_empty_round()

    def test_dns_connect_tls_and_request_write_deadlines_remain_failures(self):
        for stage in ('dns', 'connect', 'tls', 'write'):
            self.raw, self.tls = FakeSocket(), FakeSocket()
            self.context.wrap_socket.return_value = self.tls
            with self.subTest(stage=stage), self.network():
                if stage == 'dns':
                    context = patch('wechat_dot_bridge.transport._resolve_public', side_effect=BridgeError('network_timeout'))
                else:
                    attribute = {'connect': 'connect', 'tls': 'do_handshake', 'write': 'sendall'}[stage]
                    target = self.raw if stage == 'connect' else self.tls
                    context = patch.object(target, attribute, side_effect=TimeoutError('synthetic deadline'))
                with context:
                    self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
                self.assertEqual(self.store.get_status()['last_error'], 'network_timeout')
                self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor')

    def test_cancel_published_before_socket_abort_cannot_be_empty_round(self):
        def read(_):
            # The global signal is published before cancel_all aborts its
            # captured sockets; emulate that exact interruption window.
            net.cancellation_token().set()
            raise TimeoutError('synthetic cancelled read')
        self.tls.recv = read
        with self.network():
            self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
        self.assertEqual(self.store.get_status()['last_error'], 'network_cancelled')

    def test_protocol_and_auth_errors_are_not_empty_rounds(self):
        for response, diagnostic in [(b'bad HTTP\r\n\r\n', 'network_error'),
                                     (b'HTTP/1.1 401 Nope\r\nContent-Length: 0\r\n\r\n', 'auth_required')]:
            self.raw, self.tls = FakeSocket(), FakeSocket(response)
            self.context.wrap_socket.return_value = self.tls
            with self.subTest(diagnostic=diagnostic), self.network():
                self.store.set_enabled(True)
                self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
                self.assertEqual(self.store.get_status()['last_error'], diagnostic)

    def test_deadline_cannot_mask_a_known_rejection_or_protocol_error(self):
        for response in (b'bad HTTP\r\n\r\n',
                         b'HTTP/1.1 401 Nope\r\nContent-Length: 0\r\n\r\n'):
            self.raw, self.tls = FakeSocket(), FakeSocket(response)
            self.context.wrap_socket.return_value = self.tls
            receive = self.tls.recv
            def read(count):
                data = receive(count)
                FakeTimer.instances[-1].fire()
                return data
            self.tls.recv = read
            with self.subTest(response=response), self.network():
                self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
                self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor')

    def test_send_response_read_timeout_stays_uncertain_and_never_resends(self):
        self.store.ingest('fixture-account', [{'source_id': 'fixture-input', 'peer': 'fixture-owner',
                         'text': 'synthetic input', 'created_at': self.store.clock(),
                         'context_token': 'fixture-context'}], 'fixture-cursor')
        mid = self.store.list_pending()[0]['message_id']
        self.store.queue_reply(mid, 'synthetic reply')
        self.tls.recv = Mock(side_effect=TimeoutError('synthetic deadline'))
        worker = WeixinWorker(self.store, self.client)
        with self.network():
            self.assertTrue(worker.send_once())
            self.assertFalse(worker.send_once())
        self.assertEqual(self.store.send_status(mid)['status'], 'uncertain')
        self.assertEqual(self.store.send_status(mid)['error'], 'network_timeout')
        self.assertEqual(b''.join(self.tls.sent).count(b'POST /ilink/bot/sendmessage HTTP/1.1'), 1)


if __name__ == '__main__':
    unittest.main()
