"""Shared DNS uses real HTTPCore framing with synthetic provider/application sockets."""

import contextlib
import json
import os
import socket
import ssl
import unittest
from unittest.mock import Mock, patch

from network_fixtures import FakeSocket, FakeTimer
from wechat_dot_bridge import media, net_transport as net
from wechat_dot_bridge.artifacts import download
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import secure_post


class SharedDNSTransportTests(unittest.TestCase):
    PROXY = 'http://127.0.0.1:9000'
    IP = '93.184.216.34'

    def setUp(self):
        net.reset_cancellation()
        self.previous = media.NETWORK_PROXY, media.NETWORK_DNS
        media.configure_network(self.PROXY, 'google')
        self.block_dns = patch('wechat_dot_bridge.net_transport.socket.getaddrinfo',
                               side_effect=AssertionError('real DNS forbidden'))
        self.block_socket = patch('wechat_dot_bridge.net_transport.socket.socket',
                                  side_effect=AssertionError('real network forbidden'))
        self.block_dns.start()
        self.block_socket.start()

    def tearDown(self):
        self.block_socket.stop()
        self.block_dns.stop()
        media.configure_network(*self.previous)
        net.reset_cancellation()

    @staticmethod
    def response(status, body, *, declared=None):
        size = len(body) if declared is None else declared
        return b'HTTP/1.1 ' + str(status).encode() + b' fixture\r\nContent-Length: ' + str(size).encode() + b'\r\n\r\n' + body

    def answer(self, records=None):
        return json.dumps({'Status': 0, 'Answer': records or [
            {'type': 5, 'data': 'alias.example.net.'}, {'type': 1, 'data': self.IP}]}).encode()

    @contextlib.contextmanager
    def network(self, responses, *, interrupt=False, tls_failure=False):
        raws, tls_sockets, names = [], [], []
        remaining = iter(responses)
        context = Mock()

        def raw_socket(*args):
            raw = FakeSocket()
            raws.append(raw)
            return raw

        def wrap(raw, **kwargs):
            names.append(kwargs['server_hostname'])
            tls = FakeSocket(next(remaining))
            tls_sockets.append(tls)
            if tls_failure:
                def fail():
                    raise ssl.SSLCertVerificationError('fixture-private-provider-detail')
                tls.on_handshake = fail
            if interrupt:
                receive = tls.recv

                def cancelled_read(count):
                    data = receive(count)
                    net.cancel_all()
                    return data

                tls.recv = cancelled_read
            return tls

        context.wrap_socket.side_effect = wrap
        with patch('wechat_dot_bridge.net_transport.socket.socket', side_effect=raw_socket) as factory, \
                patch('wechat_dot_bridge.net_transport.socket.getaddrinfo',
                      side_effect=AssertionError('unexpected DNS fallback')) as dns, \
                patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=context), \
                patch('wechat_dot_bridge.net_transport.threading.Timer', FakeTimer):
            yield raws, tls_sockets, names, factory, dns

    def invoke(self, kind):
        if kind == 'callback':
            return secure_post('https://callback.example.com/fixture-private-route?key=fixture-private-query',
                               b'{"body":"fixture-private-body"}', {'webhook-signature': 'fixture-private-signature'},
                               {'callback.example.com'}, proxy=self.PROXY, dns='google')
        if kind == 'media':
            return media.cdn_request(media.CDN_BASE + '/download?encrypted_query_param=fixture-private-media')
        return download('https://artifact.example.com/fixture-private-file?key=fixture-private-query')

    def test_google_provider_and_application_use_distinct_pinned_hosts_without_credentials(self):
        hosts = {'callback': 'callback.example.com', 'media': media.CDN_HOST, 'artifact': 'artifact.example.com'}
        ambient = {key: 'http://192.0.2.100:8080' for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY')}
        for kind, host in hosts.items():
            status, body = (204, b'') if kind == 'callback' else (200, b'fixture-data')
            with self.subTest(kind=kind), patch.dict(os.environ, ambient), \
                    self.network([self.response(200, self.answer()), self.response(status, body)]) as (raws, sockets, names, factory, dns):
                self.invoke(kind)
            self.assertEqual(names, ['dns.google', host])
            self.assertEqual([raw.connected for raw in raws], [[('127.0.0.1', 9000)]] * 2)
            self.assertIn(b'CONNECT 8.8.8.8:443 HTTP/', b''.join(raws[0].sent))
            self.assertIn(b'CONNECT 93.184.216.34:443 HTTP/', b''.join(raws[1].sent))
            provider = b''.join(sockets[0].sent)
            self.assertIn(('GET /resolve?name=' + host + '&type=A HTTP/').encode(), provider)
            self.assertIn(b'Host: dns.google\r\n', provider)
            self.assertIn(('Host: ' + host + '\r\n').encode(), b''.join(sockets[1].sent))
            self.assertNotIn(b'Authorization', provider)
            self.assertNotIn(b'fixture-private', provider)
            self.assertNotIn(b'webhook-signature', provider)
            self.assertNotIn(b'alias.example.net', provider)
            self.assertTrue(all(tls.closed for tls in sockets))
            self.assertEqual(factory.call_count, 2)
            dns.assert_not_called()

    def test_mixed_private_doh_answers_reject_before_application_and_never_fall_back(self):
        records = [{'type': 1, 'data': self.IP}, {'type': 1, 'data': '127.0.0.1'}]
        for kind, expected in (('callback', 'callback_dns_blocked'), ('media', 'media_network_failed'),
                               ('artifact', 'artifact_endpoint_rejected')):
            with self.subTest(kind=kind), self.network([self.response(200, self.answer(records))]) as (_, _, names, factory, dns), \
                    self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                self.invoke(kind)
            self.assertEqual(names, ['dns.google'])
            factory.assert_called_once()
            dns.assert_not_called()

    def test_provider_cancellation_keeps_each_callers_fixed_category_and_never_retries(self):
        for kind, expected in (('callback', 'callback_cancelled'), ('media', 'media_cancelled'),
                               ('artifact', 'artifact_cancelled')):
            net.reset_cancellation()
            with self.subTest(kind=kind), self.network([self.response(200, self.answer())], interrupt=True) as (_, sockets, names, factory, dns), \
                    self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                self.invoke(kind)
            self.assertTrue(net.cancellation_token().is_set())
            self.assertTrue(sockets[0].closed)
            self.assertEqual(names, ['dns.google'])
            factory.assert_called_once()
            dns.assert_not_called()

    def test_provider_tls_failure_and_redirect_never_reach_application(self):
        for kind, tls_code, redirect_code in (('callback', 'callback_tls_failed', 'callback_redirect'),
                                               ('media', 'media_network_failed', 'media_network_failed'),
                                               ('artifact', 'artifact_download_failed', 'artifact_download_failed')):
            for failure, expected in ((True, tls_code), (False, redirect_code)):
                with self.subTest(kind=kind, tls_failure=failure), \
                        self.network([self.response(302, b'')], tls_failure=failure) as (_, sockets, names, factory, dns), \
                        self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                    self.invoke(kind)
                self.assertEqual(names, ['dns.google'])
                self.assertNotIn(b'fixture-private', b''.join(sockets[0].sent))
                factory.assert_called_once()
                dns.assert_not_called()

    def test_provider_size_limit_does_not_claim_the_media_itself_is_oversized(self):
        for kind, expected in (('callback', 'callback_response_too_large'), ('media', 'media_network_failed'),
                               ('artifact', 'artifact_download_failed')):
            with self.subTest(kind=kind), \
                    self.network([self.response(200, b'', declared=net.MAX_DNS_RESPONSE + 1)]) as (_, _, names, factory, dns), \
                    self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                self.invoke(kind)
            self.assertEqual(names, ['dns.google'])
            factory.assert_called_once()
            dns.assert_not_called()

    def test_malformed_provider_response_is_a_fixed_dns_failure_without_fallback(self):
        bodies = [b'not-json', b'[' * 2000 + b']' * 2000,
                  b'{"Status":0,"TC":true,"Answer":[{"type":1,"data":"93.184.216.34"}]}']
        for body in bodies:
            with self.subTest(body_size=len(body)), self.network([self.response(200, body)]) as (_, _, names, factory, dns), \
                    self.assertRaises(net.NetworkFailure) as raised:
                net.resolve_addresses('artifact.example.com', dns='google', proxy=self.PROXY)
            self.assertEqual(str(raised.exception), 'dns_failed')
            self.assertEqual(names, ['dns.google'])
            factory.assert_called_once()
            dns.assert_not_called()

    def test_proxy_dns_and_hostname_validation_precede_all_provider_io(self):
        cases = ({'proxy': 'http://127.0.0.1:0'}, {'dns': 'fixture-invalid-dns'},
                 {'host': 'artifact.example.com/fixture-private-route'})
        for case in cases:
            options = {'dns': 'google', 'proxy': self.PROXY, **case}
            host = options.pop('host', 'artifact.example.com')
            with self.subTest(case=case), self.network([]) as (_, _, _, factory, dns), \
                    self.assertRaises(net.NetworkFailure):
                net.resolve_addresses(host, **options)
            factory.assert_not_called()
            dns.assert_not_called()


if __name__ == '__main__':
    unittest.main()
