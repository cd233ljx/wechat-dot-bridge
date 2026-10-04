"""Real community HTTP framing against synthetic sockets; no external traffic."""

import base64
import contextlib
import hashlib
import hmac
import io
import logging
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from network_fixtures import FakeSocket, FakeTimer
from wechat_dot_bridge import net_transport as net
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, WeixinWorker
from wechat_dot_bridge.webhook_signing import signed_headers


class CommunityNetworkTests(unittest.TestCase):
    HOST = "ilinkai.weixin.qq.com"
    ADDRESSES = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("8.8.8.8", 443))]

    def setUp(self):
        net.reset_cancellation()
        FakeTimer.instances = []
        self.raw = FakeSocket()
        self.tls = FakeSocket(b'HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\n{"ret":0}')
        self.context = Mock()
        self.context.wrap_socket.return_value = self.tls

    def tearDown(self):
        net.reset_cancellation()

    @contextlib.contextmanager
    def network(self):
        with patch("wechat_dot_bridge.net_transport.socket.socket", return_value=self.raw) as factory, \
                patch("wechat_dot_bridge.net_transport.socket.getaddrinfo", side_effect=AssertionError("real DNS forbidden")), \
                patch("wechat_dot_bridge.net_transport.ssl.create_default_context", return_value=self.context), \
                patch("wechat_dot_bridge.net_transport.threading.Timer", FakeTimer):
            yield factory

    def request(self, limit=1024, proxy=None, addresses=None):
        return net.request(self.HOST, addresses or self.ADDRESSES, "POST", "/private-route?secret=private-query",
                           b"private-body", {"Authorization": "Bearer " + "fixture-token"},
                           timeout=10, max_response=limit, proxy=proxy)

    def test_chunked_response_has_a_streamed_size_limit(self):
        self.tls.response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nabcd\r\n4\r\nefgh\r\n0\r\n\r\n"
        with self.network() as factory, self.assertRaises(net.NetworkFailure) as caught:
            self.request(limit=7)
        self.assertEqual(caught.exception.category, "response_too_large")
        factory.assert_called_once()
        self.assertTrue(self.tls.closed)

    def test_declared_length_rejects_before_waiting_for_large_body(self):
        self.tls.response = b"HTTP/1.1 200 OK\r\nContent-Length: 99999999\r\n\r\n"
        with self.network(), self.assertRaises(net.NetworkFailure) as caught:
            self.request(limit=10)
        self.assertEqual(caught.exception.category, "response_too_large")
        self.assertTrue(self.tls.closed)

    def test_total_deadline_interrupts_trickled_body(self):
        parts = iter([b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n", b"a"])

        def receive(count):
            result = next(parts)
            if result == b"a":
                FakeTimer.instances[-1].fire()
            return result

        self.tls.recv = receive
        with self.network(), self.assertRaises(net.NetworkFailure) as caught:
            self.request()
        self.assertEqual(caught.exception.category, "timeout")
        self.assertTrue(self.tls.closed)
        self.assertEqual(self.tls.shutdowns, [socket.SHUT_RDWR])

    def test_ipv6_connect_is_numeric_and_bracketed_with_original_tls(self):
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                      ("2606:4700:4700::1111", 443, 0, 0))]
        with self.network():
            result = self.request(proxy="http://127.0.0.1:9000", addresses=addresses)
        self.assertEqual(result.body, b'{"ret":0}')
        self.assertIn(b"CONNECT [2606:4700:4700::1111]:443 HTTP/1.1", b"".join(self.raw.sent))
        self.context.wrap_socket.assert_called_once_with(self.raw, server_hostname=self.HOST,
                                                        do_handshake_on_connect=False)
        self.assertIn(b"Host: ilinkai.weixin.qq.com\r\n", b"".join(self.tls.sent))

    def test_private_httpcore_debug_trace_is_suppressed(self):
        self.tls.response = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nx-private-header: private-secret\r\n\r\n"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("httpcore")
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            with self.network():
                self.request()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertEqual(stream.getvalue(), "")

    def test_cancel_during_dns_window_prevents_new_network_exchange(self):
        net.cancel_all()
        with self.network() as factory, self.assertRaises(net.NetworkAbort):
            self.request()
        factory.assert_not_called()

    def test_shutdown_interrupts_dns_wait_without_joining_stuck_libc(self):
        entered, release = threading.Event(), threading.Event()
        failed = []

        def lookup(*args, **kwargs):
            entered.set()
            release.wait(1)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

        def resolve():
            try:
                net.resolve_public_addresses(self.HOST, 10, threading.BoundedSemaphore(1))
            except net.NetworkFailure as error:
                failed.append(error.category)

        with patch("wechat_dot_bridge.net_transport.socket.getaddrinfo", side_effect=lookup):
            worker = threading.Thread(target=resolve)
            worker.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            net.cancel_all()
            worker.join(0.5)
            release.set()
            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(failed, ["cancelled"])

    def test_cancel_during_body_closes_socket_and_interrupts_retry_wait(self):
        stop_signal = net.cancellation_token()
        parts = iter([b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n", b"abcd"])

        def receive(count):
            result = next(parts)
            if result == b"abcd":
                net.cancel_all()
            return result

        self.tls.recv = receive
        with self.network(), self.assertRaises(net.NetworkAbort):
            self.request()
        self.assertTrue(self.tls.closed)
        started = time.monotonic()
        with self.assertRaises(net.NetworkAbort):
            net.wait_for_retry(30, stop_signal)
        self.assertLess(time.monotonic() - started, 0.1)

    def test_failure_after_actual_send_bytes_is_uncertain_and_never_retried(self):
        def fail_after_write(value):
            self.tls.sent.append(bytes(value))
            if b'"client_id"' in value:
                raise TimeoutError("private-route private-token")

        self.tls.sendall = fail_after_write
        binding = {"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token-NOT-REAL",
                   "base_url": "https://ilinkai.weixin.qq.com", "cursor": ""}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            store = Store(state)
            store.import_binding(binding)
            store.set_enabled(True)
            store.ingest(binding["id"], [{"source_id": "1", "peer": binding["owner"], "text": "fixture",
                                         "created_at": 100, "context_token": "private-context"}], "")
            mid = store.list_pending()[0]["message_id"]
            store.queue_reply(mid, "fixture reply")
            client = IlinkClient()
            try:
                with self.network() as factory, patch("wechat_dot_bridge.transport._resolve_public", return_value="8.8.8.8"):
                    self.assertTrue(WeixinWorker(store, client).send_once())
                self.assertEqual(store.send_status(mid)["status"], "uncertain")
                self.assertEqual(store.send_status(mid)["error"], "network_timeout")
                factory.assert_called_once()
                self.assertEqual(b"".join(self.tls.sent).count(b'"client_id"'), 1)
            finally:
                store.close()
            reopened = Store(state)
            try:
                reopened.recover_inflight()
                self.assertIsNone(reopened.claim_send())
                self.assertEqual(reopened.send_status(mid)["status"], "uncertain")
            finally:
                reopened.close()


class StandardWebhookInteropTests(unittest.TestCase):
    def test_exact_unicode_bytes_and_both_rotation_keys_match_reference_protocol(self):
        body = '{"text":"中文 😀","spacing":"  "}'.encode("utf-8")
        keys = (b"a" * 32, b"b" * 32)
        secrets = ["whsec_" + base64.b64encode(key).decode("ascii") for key in keys]
        result = signed_headers("evt_fixture", body, secrets[0], 1800000000.999,
                                "sub_fixture", old_secret=secrets[1])
        signed = b"evt_fixture.1800000000." + body
        expected = ["v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
                    for key in keys]
        self.assertEqual(result["webhook-signature"], " ".join(expected))
        self.assertEqual(result["webhook-timestamp"], "1800000000")
        self.assertEqual(result["X-MCP-Subscription-Id"], "sub_fixture")

    def test_non_utf8_payload_is_rejected_with_fixed_category(self):
        secret = "whsec_" + base64.b64encode(b"a" * 32).decode("ascii")
        with self.assertRaisesRegex(BridgeError, "^invalid_signing_payload$"):
            signed_headers("evt_fixture", b"\xffprivate-payload", secret, 1800000000, "sub_fixture")
