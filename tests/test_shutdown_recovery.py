"""Shutdown recovers safe exchanges; only presence-stop escapes cancellation."""

import base64
import contextlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from network_fixtures import FakeSocket, FakeTimer
from wechat_dot_bridge import net_transport as net
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.media import CDN_BASE, MediaWorker, cdn_request, crypt
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, WeixinWorker
from wechat_dot_bridge.typing import TypingIndicator


class ProcessInterrupted(BaseException):
    """A synthetic process crash bypasses normal exception/result persistence."""


class ShutdownRecoveryTests(unittest.TestCase):
    def setUp(self):
        net.reset_cancellation()
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name) / "state"
        self.now = 1800000000.0
        self.store = Store(self.state, clock=lambda: self.now)
        self.binding = {"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token",
                        "base_url": "https://ilinkai.weixin.qq.com"}
        self.store.import_binding(self.binding)
        self.store.set_enabled(True)
        self.key = bytes(range(16))
        FakeTimer.instances = []

    def tearDown(self):
        self.store.close()
        net.reset_cancellation()
        self.directory.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.state, clock=lambda: self.now)
        net.reset_cancellation()

    def ingest(self, media=False):
        row = {"source_id": "fixture-inbound", "peer": "fixture-owner", "text": "synthetic inbound",
               "created_at": self.now, "context_token": "fixture-context"}
        if media:
            row["attachments"] = [{"kind": "file", "name": "fixture.json",
                                   "url": CDN_BASE + "/download?encrypted_query_param=fixture",
                                   "key": self.key.hex()}]
        self.store.ingest("fixture-account", [row], "fixture-cursor")
        return self.store.list_pending()[0]["message_id"]

    def events(self, subscribe=False, post=None):
        events = Events(self.store, ["callback.example.com"], post=post, clock=lambda: self.now)
        if subscribe:
            secret = "whsec_" + base64.b64encode(b"fixture-signing-key-32-byte-data!").decode()
            params = {"name": "weixin.message", "arguments": {}, "ttlMs": None,
                      "delivery": {"mode": "webhook", "url": "https://callback.example.com/fixture", "secret": secret}}

            def verification(url, body, headers, *args):
                return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode()

            with patch("wechat_dot_bridge.events.secure_post", side_effect=verification):
                events.subscribe(params)
        return events

    @contextlib.contextmanager
    def network(self, status=200, body=b"", handshake=None, lookup=None):
        raw = FakeSocket()
        response = (b"HTTP/1.1 " + str(status).encode() + b" fixture\r\nContent-Length: " +
                    str(len(body)).encode() + b"\r\n\r\n" + body)
        tls = FakeSocket(response)
        tls.on_handshake = handshake
        context = Mock()
        context.wrap_socket.return_value = tls
        entries = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]
        with patch("wechat_dot_bridge.net_transport.socket.socket", return_value=raw) as factory, \
                patch("wechat_dot_bridge.net_transport.socket.getaddrinfo", side_effect=lookup,
                      return_value=entries) as dns, \
                patch("wechat_dot_bridge.net_transport.ssl.create_default_context", return_value=context), \
                patch("wechat_dot_bridge.net_transport.threading.Timer", FakeTimer):
            yield tls, factory, dns

    def delivery(self):
        return self.store.db.execute("SELECT event_id,body,status,attempts,error FROM deliveries").fetchone()

    def attachment(self):
        return self.store.db.execute("SELECT id,status,attempts,error FROM attachments").fetchone()

    def test_default_callback_cancellation_recovers_stable_event_without_spending_attempts(self):
        events = self.events(subscribe=True)
        self.ingest()
        # A real failure remains counted through more than MAX_ATTEMPTS stops.
        with self.network(status=503):
            self.assertTrue(events.deliver_once())
        identity = tuple(self.delivery()[:2])
        self.now += 400
        for _ in range(7):
            with self.network(handshake=net.cancel_all) as (tls, _, _):
                self.assertTrue(events.deliver_once())
            row = self.delivery()
            self.assertEqual(tuple(row[:2]), identity)
            self.assertEqual(tuple(row[2:]), ("retry", 1, "callback_cancelled"))
            self.assertEqual(tls.sent, [])
            self.reopen()
            events = self.events()
        with self.network(status=204):
            self.assertTrue(events.deliver_once())
        self.assertEqual(tuple(self.delivery()[:2]), identity)
        self.assertEqual(tuple(self.delivery()[2:]), ("acknowledged", 2, None))

    def test_callback_incomplete_claim_recovers_without_spending_attempts(self):
        events = self.events(subscribe=True)
        self.ingest()
        identity = None
        for _ in range(7):
            def crash(*args):
                raise ProcessInterrupted()
            events.post = crash
            with self.assertRaises(ProcessInterrupted):
                events.deliver_once()
            self.assertEqual(tuple(self.delivery()[2:4]), ("sending", 1))
            identity = identity or tuple(self.delivery()[:2])
            self.reopen()
            events = self.events(post=lambda *args: (204, b""))
            self.assertEqual(tuple(self.delivery()[:2]), identity)
            self.assertEqual(tuple(self.delivery()[2:]), ("retry", 0, "callback_interrupted"))
        self.assertTrue(events.deliver_once())
        self.assertEqual(tuple(self.delivery()[2:4]), ("acknowledged", 1))

    def test_callback_real_network_failures_still_exhaust_bounded_budget(self):
        events = self.events(subscribe=True)
        self.ingest()
        for attempt in range(1, 6):
            with self.network(status=503):
                self.assertTrue(events.deliver_once())
            self.assertEqual(self.delivery()[3], attempt)
            self.now += 400
        self.assertEqual(tuple(self.delivery()[2:]), ("failed", 5, "retry_exhausted"))
        self.reopen()
        with self.network() as (_, factory, _):
            self.assertFalse(self.events().deliver_once())
        factory.assert_not_called()

    def test_default_download_cancellation_recovers_without_spending_attempts(self):
        self.ingest(media=True)
        worker = MediaWorker(self.store)
        with self.network(status=503):
            worker.tick()
        self.now += 100
        aid = self.attachment()[0]
        for _ in range(5):
            with self.network(handshake=net.cancel_all) as (tls, _, _):
                worker.tick()
            self.assertEqual(tuple(self.attachment()), (aid, "pending", 1, "media_cancelled"))
            self.assertEqual(tls.sent, [])
            self.reopen()
            worker = MediaWorker(self.store)
        with self.network(body=crypt(b"{}", self.key)):
            worker.tick()
        self.assertEqual(tuple(self.attachment()), (aid, "ready", 2, None))
        self.assertEqual(self.store.read_attachment(aid)["data"], b"{}")

    def test_download_incomplete_claim_recovers_without_spending_attempts(self):
        self.ingest(media=True)
        worker = MediaWorker(self.store)
        aid = self.attachment()[0]
        for _ in range(5):
            self.assertEqual(self.store.next_attachment()["id"], aid)
            self.assertEqual(tuple(self.attachment()[1:3]), ("downloading", 1))
            self.reopen()
            worker = MediaWorker(self.store)
            self.assertEqual(tuple(self.attachment()), (aid, "pending", 0, "media_interrupted"))
        with self.network(body=crypt(b"{}", self.key)):
            worker.tick()
        self.assertEqual(tuple(self.attachment()), (aid, "ready", 1, None))

    def test_download_dns_cancellation_recovers_and_real_failures_stay_bounded(self):
        self.ingest(media=True)
        worker = MediaWorker(self.store)
        net.cancel_all()
        with self.network() as (_, factory, dns):
            worker.tick()
        self.assertEqual(tuple(self.attachment()[1:]), ("pending", 0, "media_cancelled"))
        factory.assert_not_called()
        dns.assert_not_called()
        net.reset_cancellation()
        for attempt in range(1, 4):
            with self.network(status=503):
                worker.tick()
            self.assertEqual(self.attachment()[2], attempt)
            self.now += 100
        self.assertEqual(tuple(self.attachment()[1:]), ("failed", 3, "media_http_error"))
        self.reopen()
        with self.network() as (_, factory, _):
            MediaWorker(self.store).tick()
        factory.assert_not_called()

    def test_shutdown_presence_stop_survives_cancel_without_resuming_business_requests(self):
        client = IlinkClient()
        typing = TypingIndicator(self.store, client)
        typing.active = (self.binding, "fixture-ticket")
        stop_signal = net.cancellation_token()
        net.cancel_all()
        # A repeated global cancellation during TLS must not abort cleanup.
        with self.network(handshake=net.cancel_all) as (tls, factory, dns):
            typing.close()
            wire = b"".join(tls.sent)
            self.assertIn(b"POST /ilink/bot/sendtyping HTTP/1.1", wire)
            body = json.loads(wire.split(b"\r\n\r\n", 1)[1])
            self.assertEqual(body["status"], 2)
            self.assertEqual(body["ilink_user_id"], "fixture-owner")
            self.assertEqual(typing.status()["outcomes"], {"stop_ok": 1})
            self.assertIs(net.cancellation_token(), stop_signal)
            self.assertTrue(stop_signal.is_set())
            before = (factory.call_count, dns.call_count)
            for status in (1, 2):
                with self.assertRaisesRegex(BridgeError, "network_cancelled"):
                    client.send_typing(self.binding, "fixture-ticket", status)
            with self.assertRaisesRegex(BridgeError, "media_cancelled"):
                cdn_request(CDN_BASE + "/download?encrypted_query_param=fixture")
            self.assertEqual((factory.call_count, dns.call_count), before)

    def test_cancelled_presence_refresh_keeps_target_for_final_cleanup(self):
        client = IlinkClient()
        typing = TypingIndicator(self.store, client)
        mid = self.ingest()
        self.store.read_message(mid)
        typing.sync(mid)
        binding = self.store.get_binding()
        typing.ticket = "fixture-ticket"
        typing.ticket_key = (binding['id'], binding['owner'], binding['base_url'])
        typing.ticket_until = typing.clock() + 600
        typing.active = (binding, typing.ticket)
        net.cancel_all()
        with self.network() as (tls, factory, _):
            # A refresh racing shutdown fails both start and ordinary stop.
            typing.tick()
            factory.assert_not_called()
            self.assertEqual(typing.active, (binding, typing.ticket))
            typing.close()
        self.assertIsNone(typing.active)
        wire = b''.join(tls.sent)
        self.assertIn(b'POST /ilink/bot/sendtyping HTTP/1.1', wire)
        self.assertEqual(json.loads(wire.split(b'\r\n\r\n', 1)[1])['status'], 2)
        self.assertTrue(net.cancellation_token().is_set())
        self.assertEqual(typing.status()['outcomes'], {'start_failed': 1, 'stop_failed': 1, 'stop_ok': 1})
        factory.assert_called_once()

    def test_presence_cleanup_rejects_start_or_message_payload_before_dns(self):
        stop = {"ilink_user_id": "fixture-owner", "typing_ticket": "fixture-ticket", "status": 2,
                "base_info": IlinkClient._base_info()}
        invalid = [dict(stop, status=1), {"msg": {"text": "synthetic"}}, dict(stop, path="/ilink/bot/sendmessage")]
        with self.network() as (_, factory, dns):
            for value in invalid:
                with self.assertRaises(net.NetworkFailure) as caught:
                    net.presence_stop("ilinkai.weixin.qq.com", json.dumps(value).encode(), {})
                self.assertEqual(caught.exception.category, "presence_stop_invalid")
        factory.assert_not_called()
        dns.assert_not_called()

    def test_presence_dns_and_http_share_three_second_total_deadline(self):
        monotonic = [100.0]

        def lookup(*args, **kwargs):
            monotonic[0] = 101.5
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

        def handshake():
            monotonic[0] = 103.1

        net.cancel_all()
        with patch("wechat_dot_bridge.net_transport.time.monotonic", side_effect=lambda: monotonic[0]), \
                self.network(lookup=lookup, handshake=handshake) as (tls, factory, _):
            with self.assertRaisesRegex(BridgeError, "network_timeout"):
                IlinkClient().stop_typing(self.binding, "fixture-ticket")
        factory.assert_called_once()
        self.assertEqual(FakeTimer.instances[-1].interval, 1.5)
        self.assertEqual(tls.sent, [])
        self.assertTrue(tls.closed)
        self.assertTrue(net.cancellation_token().is_set())

    def test_presence_expired_dns_deadline_never_opens_http_connection(self):
        monotonic = [100.0]

        def lookup(*args, **kwargs):
            monotonic[0] = 103.1
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

        net.cancel_all()
        with patch("wechat_dot_bridge.net_transport.time.monotonic", side_effect=lambda: monotonic[0]), \
                self.network(lookup=lookup) as (_, factory, _):
            with self.assertRaisesRegex(BridgeError, "network_timeout"):
                IlinkClient().stop_typing(self.binding, "fixture-ticket")
        factory.assert_not_called()
        self.assertEqual(FakeTimer.instances, [])

    def test_default_weixin_post_cancelled_after_write_is_uncertain_without_resend(self):
        mid = self.ingest()
        self.store.queue_reply(mid, "synthetic reply")
        client = IlinkClient()
        with self.network() as (tls, factory, _):
            def cancel_before_response(count):
                net.cancel_all()
                raise OSError("fixture shutdown")

            tls.recv = cancel_before_response
            self.assertTrue(WeixinWorker(self.store, client).send_once())
        factory.assert_called_once()
        self.assertEqual(b"".join(tls.sent).count(b'"client_id"'), 1)
        self.assertEqual(self.store.send_status(mid)["status"], "uncertain")
        self.assertEqual(self.store.send_status(mid)["error"], "network_cancelled")
        self.reopen()
        self.store.recover_inflight()
        self.assertIsNone(self.store.claim_send())
        with self.network() as (_, factory, _):
            self.assertFalse(WeixinWorker(self.store, client).send_once())
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
