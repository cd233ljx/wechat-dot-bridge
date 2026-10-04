"""Default HTTP presence ACK and generation-aware recovery; no real traffic."""

import contextlib
import json
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

from network_fixtures import FakeSocket, FakeTimer
from wechat_dot_bridge import net_transport as net
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, MAX_RESPONSE_BYTES
from wechat_dot_bridge.typing import TypingIndicator


class TypingRecoveryTests(unittest.TestCase):
    def setUp(self):
        net.reset_cancellation()
        self.directory = tempfile.TemporaryDirectory()
        self.now = 0.0
        self.store = Store(Path(self.directory.name) / 'state', clock=lambda: 1800000000 + self.now)
        self.binding = {'id': 'fixture-account', 'owner': 'fixture-owner', 'token': 'fixture-token',
                        'base_url': 'https://ilinkai.weixin.qq.com'}
        self.store.import_binding(self.binding)
        self.store.set_enabled(True)
        self.store.ingest('fixture-account', [{'source_id': str(index), 'peer': 'fixture-owner',
                          'text': 'synthetic inbound', 'created_at': self.store.clock(),
                          'context_token': 'fixture-context'} for index in range(2)], 'fixture-cursor')
        self.mids = [row['message_id'] for row in self.store.list_pending()]
        self.client = IlinkClient(clock=self.store.clock)
        self.typing = TypingIndicator(self.store, self.client, clock=lambda: self.now)
        # Every request must be covered by an explicit synthetic exchange.
        self.block_dns = patch('wechat_dot_bridge.net_transport.socket.getaddrinfo',
                               side_effect=AssertionError('real DNS forbidden'))
        self.block_socket = patch('wechat_dot_bridge.net_transport.socket.socket',
                                  side_effect=AssertionError('real network forbidden'))
        self.block_dns.start()
        self.block_socket.start()

    def tearDown(self):
        try:
            self.typing.close()
            self.store.close()
            net.reset_cancellation()
        finally:
            self.block_socket.stop()
            self.block_dns.stop()
            self.directory.cleanup()

    @contextlib.contextmanager
    def network(self, responses):
        wires = []
        remaining = iter(responses)
        context = Mock()

        def wrap(raw, **kwargs):
            status, body = next(remaining)
            tls = FakeSocket(b'HTTP/1.1 ' + str(status).encode() + b' fixture\r\nContent-Length: ' +
                             str(len(body)).encode() + b'\r\n\r\n' + body)
            wires.append(tls)
            return tls

        context.wrap_socket.side_effect = wrap
        entries = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        with patch('wechat_dot_bridge.net_transport.socket.socket', side_effect=lambda *args: FakeSocket()) as factory, \
                patch('wechat_dot_bridge.net_transport.socket.getaddrinfo', return_value=entries), \
                patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=context), \
                patch('wechat_dot_bridge.net_transport.threading.Timer', FakeTimer):
            yield wires, factory

    def read(self, mid):
        value = self.store.read_message(mid)
        self.typing.sync(mid)
        return value['processing']

    @staticmethod
    def request(tls):
        wire = b''.join(tls.sent)
        headers, body = wire.split(b'\r\n\r\n', 1)
        return headers.split(b'\r\n', 1)[0], json.loads(body)

    @staticmethod
    def ticket():
        return 200, b'{"ret":0,"typing_ticket":"fixture-ticket"}'

    def test_opaque_typing_ack_preserves_start_refresh_and_stop(self):
        with self.network([self.ticket(), (200, b'OK'), (200, b'{"unexpected":"opaque"}'),
                           (200, b'non-json stop ack')]) as (wires, factory):
            self.typing.prewarm()
            self.assertEqual(len(wires), 1)
            self.assertIsNone(self.typing.active)
            self.read(self.mids[0])
            self.typing.tick()
            self.now = 5
            self.typing.tick()
            self.typing.progress(self.mids[0], False)
            self.typing.tick()
        self.assertEqual(self.typing.status()['outcomes'], {'ticket_ok': 1, 'start_ok': 2, 'stop_ok': 1})
        self.assertIsNone(self.typing.active)
        self.assertEqual([self.request(tls)[1]['status'] for tls in wires[1:]], [1, 1, 2])
        self.assertEqual(factory.call_count, 4)

    def test_shutdown_stop_uses_same_opaque_ack_without_resuming_business(self):
        with self.network([self.ticket(), (200, b'OK'), (200, b'opaque shutdown ack')]):
            self.read(self.mids[0])
            self.typing.tick()
            stopped = net.cancellation_token()
            net.cancel_all()
            self.typing.close()
        self.assertEqual(self.typing.status()['outcomes'], {'ticket_ok': 1, 'start_ok': 1, 'stop_ok': 1})
        self.assertIsNone(self.typing.active)
        self.assertIs(net.cancellation_token(), stopped)
        self.assertTrue(stopped.is_set())

    def test_refresh_failure_keeps_valid_ticket_for_bounded_retry(self):
        with self.network([self.ticket(), (200, b'OK'), (500, b'fixture error'),
                           (200, b'stop ack'), (200, b'recovered ack'), (200, b'final stop')]) as (wires, _):
            self.typing.prewarm()
            self.read(self.mids[0])
            self.typing.tick()
            valid_until = self.typing.ticket_until
            self.now = 5
            self.typing.tick()
            self.assertEqual(self.typing.ticket_until, valid_until)
            self.assertEqual(self.typing.next_send, 10)
            self.now = 9
            self.typing.tick()
            self.assertEqual(len(wires), 4)
            self.now = 10
            self.typing.tick()
            self.typing.close()
        self.assertEqual(self.typing.status()['outcomes'],
                         {'ticket_ok': 1, 'start_ok': 2, 'start_failed': 1, 'stop_ok': 2})
        self.assertEqual(sum(b'getconfig' in self.request(tls)[0] for tls in wires), 1)

    def check_generation_recovery(self, same_member):
        responses = [self.ticket(), (500, b'fixture'), (200, b'stop'),
                     (500, b'fixture'), (200, b'stop'), (500, b'fixture'), (200, b'stop'),
                     (200, b'recovered'), (200, b'next refresh'), (200, b'final stop')]
        with self.network(responses) as (wires, _):
            self.read(self.mids[0])
            for at in (0, 5, 15):
                self.now = at
                self.typing.tick()
            self.assertEqual(self.typing.next_send, 35)
            self.assertEqual(self.typing.status()['outcomes']['ticket_ok'], 1)
            self.now = 16
            self.store.report_progress(self.mids[0], False, completed=not same_member)
            self.typing.sync(self.mids[0])
            mid = self.mids[0] if same_member else self.mids[1]
            work = self.read(mid)
            # No idle tick occurs between the old and new generations.
            self.typing.tick()
            self.assertEqual(self.typing.status()['outcomes']['start_ok'], 1)
            self.assertEqual(self.typing.next_send, 21)
            self.assertEqual(self.typing.failures, 0)
            count = len(wires)
            self.read(mid)
            self.typing.tick()
            self.now = 17
            self.store.report_progress(mid, True, processing_id=work['processing_id'], revision=work['revision'])
            self.typing.sync(mid)
            self.typing.tick()
            self.store.queue_reply(mid, 'synthetic progress', reply_key='fixture-progress', final=False)
            self.typing.sync(mid)
            self.typing.tick()
            self.store.queue_reply(mid, 'synthetic progress', reply_key='fixture-progress', final=False)
            self.typing.sync(mid)
            self.typing.tick()
            self.assertEqual(len(wires), count)
            self.assertEqual(self.typing.next_send, 21)
            self.now = 21
            self.typing.tick()
            self.typing.close()
        self.assertEqual(sum(b'getconfig' in self.request(tls)[0] for tls in wires), 1)

    def test_new_member_turn_does_not_inherit_previous_failure_backoff(self):
        self.check_generation_recovery(False)

    def test_resumed_same_member_generation_does_not_inherit_previous_backoff(self):
        self.check_generation_recovery(True)

    def test_added_active_member_recovers_backoff_without_amplifying_successful_presence(self):
        with self.network([self.ticket(), (500, b'fixture'), (200, b'stop'),
                           (200, b'recovered'), (200, b'final stop')]) as (wires, _):
            self.read(self.mids[0])
            self.typing.tick()
            self.assertEqual(self.typing.next_send, 5)
            self.now = 1
            self.read(self.mids[1])
            self.typing.tick()
            self.assertEqual(self.typing.status()['outcomes']['start_ok'], 1)
            self.assertEqual(self.typing.next_send, 6)
            count = len(wires)
            # A real regrouping changes both generations but owner presence
            # already succeeded. Keep its cadence instead of another start.
            self.store.report_progress(self.mids[0], True, message_ids=self.mids)
            self.typing.sync(self.mids[0])
            self.typing.tick()
            self.assertEqual(len(wires), count)
            self.assertEqual(self.typing.next_send, 6)
            self.typing.close()
        self.assertEqual(sum(b'getconfig' in self.request(tls)[0] for tls in wires), 1)

    def test_void_typing_ack_preserves_http_status_size_tls_and_cancellation(self):
        for status, body, expected in ((401, b'', 'auth_required'), (429, b'', 'rate_limited'),
                                       (500, b'non-json failure', 'http_error')):
            with self.subTest(status=status), self.network([(status, body)]), \
                    self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                self.client.send_typing(self.binding, 'fixture-ticket', 1)
        with self.network([(200, b'x' * (MAX_RESPONSE_BYTES + 1))]), \
                self.assertRaisesRegex(BridgeError, '^response_too_large$'):
            self.client.send_typing(self.binding, 'fixture-ticket', 1)
        with self.network([(200, b'OK')]) as (wires, _):
            # Force a TLS failure before application bytes.
            def failed_handshake():
                raise ssl.SSLError('fixture TLS failure')
            context = Mock()
            tls = FakeSocket()
            tls.on_handshake = failed_handshake
            context.wrap_socket.return_value = tls
            with patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=context), \
                    self.assertRaisesRegex(BridgeError, '^network_error$'):
                self.client.send_typing(self.binding, 'fixture-ticket', 1)
            self.assertEqual(tls.sent, [])
        net.cancel_all()
        with self.network([(200, b'OK')]) as (_, factory), \
                self.assertRaisesRegex(BridgeError, '^network_cancelled$'):
            self.client.send_typing(self.binding, 'fixture-ticket', 1)
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
