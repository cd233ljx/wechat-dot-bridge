"""Independent wire-level callback fixtures; no real sockets or credentials."""

import base64
import contextlib
import io
import json
import os
import socket
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events, _https_request, secure_post
from wechat_dot_bridge.net_transport import PinnedRequest
from wechat_dot_bridge.mcp import MCPServer
from wechat_dot_bridge.store import Store


from network_fixtures import FakeSocket, FakeTimer


class CallbackWireSecurityTests(unittest.TestCase):
    HOST = "callback.example.com"
    IP = "93.184.216.34"
    PROXY = "http://127.0.0.1:9000"
    PRIVATE_BODY = b'{"private":"fixture-private-body"}'
    PRIVATE_ROUTE = "/events/fixture-private-route?key=fixture-private-query"

    def setUp(self):
        FakeTimer.instances = []
        self.raw = FakeSocket()
        self.tls = FakeSocket(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        self.context = Mock()
        self.context.wrap_socket.return_value = self.tls
        self.addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (self.IP, 443))]
        self.headers = {"webhook-signature": "fixture-private-signature", "webhook-id": "fixture-event"}

    @contextlib.contextmanager
    def network(self):
        with patch("wechat_dot_bridge.net_transport.socket.socket", return_value=self.raw) as factory, \
                patch("wechat_dot_bridge.net_transport.socket.getaddrinfo", side_effect=AssertionError("unexpected real DNS")), \
                patch("wechat_dot_bridge.net_transport.ssl.create_default_context", return_value=self.context), \
                patch("wechat_dot_bridge.net_transport.threading.Timer", FakeTimer):
            yield factory

    def request(self, proxy=PROXY):
        return _https_request(self.HOST, self.addresses, "POST", self.PRIVATE_ROUTE,
                              self.PRIVATE_BODY, self.headers, proxy=proxy)

    def test_real_httpcore_connect_and_application_bytes_use_separate_hosts(self):
        with self.network() as factory:
            self.assertEqual(self.request(), (204, b""))
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        self.assertEqual(self.raw.connected, [("127.0.0.1", 9000)])
        connect = b"".join(self.raw.sent)
        self.assertIn(b"CONNECT 93.184.216.34:443 HTTP/", connect)
        self.assertNotIn(self.HOST.encode(), connect)
        for secret in [self.PRIVATE_BODY, self.PRIVATE_ROUTE.encode(), b"fixture-private-signature"]:
            self.assertNotIn(secret, connect)
        self.context.wrap_socket.assert_called_once_with(
            self.raw, server_hostname=self.HOST, do_handshake_on_connect=False)
        application = b"".join(self.tls.sent)
        self.assertIn(b"Host: callback.example.com\r\n", application)
        self.assertNotIn(b"Host: 93.184.216.34", application)
        self.assertIn(self.PRIVATE_BODY, application)
        self.assertTrue(FakeTimer.instances[0].cancelled)

    def test_ambient_proxy_variables_cannot_change_direct_or_explicit_routes(self):
        environment = {key: "http://192.0.2.100:8080" for key in
                       ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]}
        environment["NO_PROXY"] = "*"
        with patch.dict(os.environ, environment), self.network():
            self.assertEqual(self.request(proxy=None), (204, b""))
        self.assertEqual(self.raw.connected, [(self.IP, 443)])
        self.assertFalse(self.raw.sent)

    def test_default_tls_keeps_certificate_and_hostname_verification(self):
        captured = []
        from wechat_dot_bridge.net_transport import ssl as network_ssl
        actual = network_ssl.create_default_context

        def context():
            value = actual()
            captured.append(value)
            return value

        with patch("wechat_dot_bridge.net_transport.ssl.create_default_context", side_effect=context), \
                patch("wechat_dot_bridge.net_transport.socket.socket", side_effect=OSError("fixture")):
            with self.assertRaises(BridgeError):
                self.request()
        self.assertEqual(captured[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(captured[0].check_hostname)

    def test_timer_closes_published_tls_socket_during_handshake_before_post(self):
        self.tls.on_handshake = lambda: FakeTimer.instances[-1].fire()
        with self.network(), self.assertRaisesRegex(BridgeError, "^callback_timeout$"):
            self.request()
        self.assertEqual(self.tls.shutdowns, [socket.SHUT_RDWR])
        self.assertTrue(self.tls.closed)
        self.assertFalse(self.tls.sent)
        self.assertNotIn(self.PRIVATE_BODY, b"".join(self.raw.sent))

    def test_deadline_in_raw_to_tls_detach_window_blocks_handshake_and_post(self):
        def detach_then_expire(raw, **kwargs):
            FakeTimer.instances[-1].fire()
            return self.tls

        self.context.wrap_socket.side_effect = detach_then_expire
        handshake = Mock(wraps=self.tls.do_handshake)
        self.tls.do_handshake = handshake
        with self.network(), self.assertRaisesRegex(BridgeError, "^callback_timeout$"):
            self.request()
        handshake.assert_not_called()
        self.assertTrue(self.tls.closed)
        self.assertFalse(self.tls.sent)

    def test_tls_certificate_failure_exposes_only_fixed_category_without_body(self):
        def fail_certificate():
            raise ssl.SSLCertVerificationError("fixture-private-route fixture-private-signature")

        self.tls.on_handshake = fail_certificate
        with self.network(), self.assertRaises(BridgeError) as raised:
            self.request()
        self.assertEqual(str(raised.exception), "callback_tls_failed")
        self.assertFalse(self.tls.sent)
        self.assertTrue(self.tls.closed)

    def test_proxy_rejection_exposes_only_fixed_category_without_tls_or_body(self):
        self.raw.response = b"HTTP/1.1 407 fixture-private-route fixture-private-signature\r\n\r\n"
        with self.network(), self.assertRaises(BridgeError) as raised:
            self.request()
        self.assertEqual(str(raised.exception), "callback_network_failed")
        self.context.wrap_socket.assert_not_called()
        self.assertNotIn(self.PRIVATE_BODY, b"".join(self.raw.sent))

    def test_exact_host_allowlist_blocks_doh_and_callback_before_io(self):
        urls = ["https://callback.example.com.evil.com/fixture-private-route",
                "https://evil-callback.example.com/fixture-private-route",
                "https://user:fixture-private-password@callback.example.com/route",
                "https://callback.example.com:8443/route",
                "https://callback.example.com/route#fixture-private-fragment"]
        with patch("wechat_dot_bridge.events._google_addresses") as doh, \
                patch("wechat_dot_bridge.events._public_addresses") as dns, \
                patch("wechat_dot_bridge.events._https_request") as request:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(BridgeError) as raised:
                    secure_post(url, self.PRIVATE_BODY, self.headers, {self.HOST},
                                proxy=self.PROXY, dns="google")
                self.assertNotIn("fixture-private", str(raised.exception))
            doh.assert_not_called()
            dns.assert_not_called()
            request.assert_not_called()


class CallbackMCPIsolationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.store.import_binding({"id": "fixture-account", "owner": "fixture-owner",
                                   "token": "fixture-private-weixin-token",
                                   "base_url": "https://ilinkai.weixin.qq.com",
                                   "cursor": "fixture-private-cursor"})
        self.store.set_enabled(True)
        self.calls = []
        self.failure = False

        def callback(url, body, headers):
            self.calls.append((url, body, headers))
            if self.failure:
                raise OSError("fixture-private-callback-path fixture-private-weixin-token")
            return 200, json.dumps({"challenge": json.loads(body).get("challenge")}).encode()

        self.events = Events(self.store, ["callback.example.com"], post=callback,
                             proxy="http://127.0.0.1:9000", dns="google")
        self.server = MCPServer(self.store, self.events)
        self.secret = "whsec_" + base64.b64encode(b"fixture-signing-key-32-byte-data!").decode()
        self.params = {"name": "weixin.message", "arguments": {}, "delivery": {
            "mode": "webhook", "url": "https://callback.example.com/fixture-private-callback-path?key=fixture-private-query",
            "secret": self.secret}}
        self.private = [self.secret, "fixture-private-callback-path", "fixture-private-query",
                        "fixture-private-weixin-token", "fixture-private-context",
                        "fixture-private-cursor", "fixture-private-inbound", "fixture-private-reply"]

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def request(self, method, params=None):
        return self.server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def assert_private_absent(self, value):
        output = json.dumps(value)
        for private in self.private:
            self.assertNotIn(private, output)

    def test_subscription_tool_status_and_error_outputs_do_not_expose_credentials(self):
        subscribed = self.request("events/subscribe", self.params)
        self.assertIn("result", subscribed)
        self.assert_private_absent(subscribed)
        self.store.ingest("fixture-account", [{"source_id": "1", "peer": "fixture-owner",
                          "text": "fixture-private-inbound", "created_at": 1800000000,
                          "context_token": "fixture-private-context"}], "fixture-private-cursor-2")
        message_id = self.store.list_pending()[0]["message_id"]
        self.events.deliver_once()
        event = json.loads(self.calls[-1][1])
        self.assertEqual(event["data"], {"message_id": message_id})
        self.assert_private_absent(event)
        for method in ["server/discover", "tools/list", "events/list"]:
            self.assert_private_absent(self.request(method))
        for tool, arguments in [("get_weixin_status", {}), ("list_pending_weixin_messages", {}),
                                ("get_weixin_send_status", {"message_id": message_id})]:
            self.assert_private_absent(self.request("tools/call", {"name": tool, "arguments": arguments}))
        reply = self.request("tools/call", {"name": "reply_weixin_message", "arguments": {
            "message_id": message_id, "text": "fixture-private-reply"}})
        self.assert_private_absent(reply)
        self.params["delivery"]["url"] = "https://unauthorized.example.com/fixture-private-callback-path"
        self.assert_private_absent(self.request("events/subscribe", self.params))
        self.assert_private_absent(self.events.status())

    def test_network_exception_and_attempted_network_override_are_not_echoed_or_applied(self):
        self.failure = True
        response = self.request("events/subscribe", self.params)
        self.assertEqual(response["error"]["data"]["reason"], "challenge_failed")
        self.assert_private_absent(response)
        self.assert_private_absent(self.events.status())
        before = len(self.calls)
        for override in [{"callback_proxy": "http://192.0.2.100:8080"}, {"callback_dns": "fixture-private-resolver"}]:
            parameters = dict(self.params, **override)
            response = self.request("events/subscribe", parameters)
            self.assertEqual(response["error"]["data"]["reason"], "invalid_params")
            self.assert_private_absent(response)
        self.assertEqual(len(self.calls), before)


if __name__ == "__main__":
    unittest.main()
