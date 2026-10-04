import base64
import hashlib
import hmac
import io
import json
import os
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import (Events, MAX_ATTEMPTS, MAX_DNS_RESPONSE,
                                      _google_addresses, _https_request,
                                      secure_post, signed_headers)
from wechat_dot_bridge.store import Store


class FakeClock:
    def __init__(self):
        self.now = 1800000000.0

    def __call__(self):
        return self.now


class Callback:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.challenge_ok = True

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        value = json.loads(body)
        if value.get("type") == "verification":
            challenge = value["challenge"] if self.challenge_ok else "wrong"
            return 200, json.dumps({"challenge": challenge}).encode()
        return self.status, b"{}"


class EventTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = Store(self.directory.name, clock=self.clock)
        self.store.import_binding({"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token", "base_url": "https://ilinkai.weixin.qq.com"})
        self.store.set_enabled(True)
        self.callback = Callback()
        self.events = Events(self.store, ["callback.example.com"], self.callback, self.clock)
        self.secret = "whsec_" + base64.b64encode(b"fixture-signing-key-32-byte-data!").decode()
        self.params = {"name": "weixin.message", "arguments": {}, "delivery": {"mode": "webhook", "url": "https://callback.example.com/events/private-route", "secret": self.secret}}

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def ingest(self, source="1", text="Private fixture message"):
        self.store.ingest("fixture-account", [{"source_id": source, "peer": "fixture-owner", "text": text, "created_at": self.clock(), "context_token": "fixture-context"}], source)
        return self.store.list_pending()[-1]["message_id"]

    def unsubscribe(self):
        params = json.loads(json.dumps(self.params))
        params["delivery"].pop("secret")
        return self.events.unsubscribe(params)

    def test_media_event_waits_for_download_but_failed_media_is_not_lost(self):
        self.events.subscribe(self.params)
        self.store.ingest("fixture-account", [{"source_id": "media-fixture", "peer": "fixture-owner", "text": "[微信附件]", "created_at": self.clock(), "context_token": "fixture-context", "attachments": [{"kind": "file", "name": "fixture.txt", "url": "fixture"}]}], "media-cursor")
        self.assertFalse(self.events.deliver_once())
        mid = self.store.list_pending()[0]["message_id"]
        aid = self.store.read_message(mid)["attachments"][0]["attachment_id"]
        self.store.fail_attachment(aid, "media_decryption_failed")
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(json.loads(self.callback.calls[-1][1])["data"]["message_id"], mid)

    def test_first_attempts_preserve_ingest_order_for_equal_timestamps(self):
        self.events.subscribe(self.params)
        expected = [self.ingest(str(index)) for index in range(1, 11)]
        for _ in expected:
            self.assertTrue(self.events.deliver_once())
        delivered = [json.loads(body)["data"]["message_id"] for _, body, _ in self.callback.calls
                     if json.loads(body).get("name")]
        self.assertEqual(delivered, expected)
        self.assertFalse(self.events.deliver_once())

    def test_backoff_does_not_block_new_messages_and_due_retry_cannot_starve(self):
        self.events.subscribe(self.params)
        first = self.ingest("retry-first")
        self.callback.status = 503
        self.assertTrue(self.events.deliver_once())
        second = self.ingest("ready-second")
        self.callback.status = 200
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(json.loads(self.callback.calls[-1][1])["data"]["message_id"], second)
        self.clock.now += 3
        third = self.ingest("ready-third")
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(json.loads(self.callback.calls[-1][1])["data"]["message_id"], first)
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(json.loads(self.callback.calls[-1][1])["data"]["message_id"], third)

    def test_pending_media_is_bypassed_then_returns_to_ingest_order_when_ready(self):
        self.events.subscribe(self.params)
        self.store.ingest("fixture-account", [{"source_id": "slow-media", "peer": "fixture-owner",
            "text": "[微信附件]", "created_at": self.clock(), "context_token": "fixture-context",
            "attachments": [{"kind": "file", "name": "fixture.txt", "url": "fixture"}]}], "media-cursor")
        slow = self.store.list_pending()[0]["message_id"]
        second = self.ingest("ready-second")
        third = self.ingest("ready-third")
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(json.loads(self.callback.calls[-1][1])["data"]["message_id"], second)
        self.clock.now += 1
        aid = self.store.attachment_metadata(slow)[0]["attachment_id"]
        self.store.finish_attachment(aid, b"fixture")
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(json.loads(self.callback.calls[-1][1])["data"]["message_id"], slow)
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(json.loads(self.callback.calls[-1][1])["data"]["message_id"], third)

    def test_interim_reply_keeps_notification_retry_eligible_until_final(self):
        self.events.subscribe(self.params)
        mid = self.ingest()
        self.callback.status = 503
        self.events.deliver_once()
        self.store.queue_reply(mid, "processing", reply_key="progress", final=False)
        self.clock.now += 3
        self.callback.status = 200
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(self.events.status()["deliveries"].get("acknowledged"), 1)

    def test_verification_signed_and_stable_refresh(self):
        first = self.events.subscribe(self.params)
        self.assertEqual(first["id"], self.events.subscribe(self.params)["id"])
        self.assertIsNone(first["cursor"])
        self.assertEqual(len(self.callback.calls), 1)
        _, body, headers = self.callback.calls[0]
        signed = (headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode() + body
        expected = "v1," + base64.b64encode(hmac.new(base64.b64decode(self.secret[6:]), signed, hashlib.sha256).digest()).decode()
        self.assertEqual(headers["webhook-signature"], expected)

    def test_rejects_failed_challenge_and_invalid_signing_secret(self):
        self.callback.challenge_ok = False
        with self.assertRaisesRegex(BridgeError, "challenge_failed"):
            self.events.subscribe(self.params)
        self.assertEqual(self.events.status()["active_subscriptions"], 0)
        self.assertEqual(self.events.status()["last_error"], "challenge_failed")
        self.params["delivery"]["secret"] = "whsec_bm8="
        with self.assertRaisesRegex(BridgeError, "invalid_signing_secret"):
            self.events.subscribe(self.params)

    def test_empty_allowlist_exposes_only_observed_host(self):
        events = Events(self.store, post=self.callback, clock=self.clock)
        with self.assertRaisesRegex(BridgeError, "callback_not_allowed"):
            events.subscribe(self.params)
        status = events.status()
        self.assertEqual(status["observed_callback_host"], "callback.example.com")
        self.assertNotIn("private-route", json.dumps(status))
        self.assertNotIn(self.secret, json.dumps(status))
        self.assertFalse(self.callback.calls)

    def test_message_id_only_payload_and_no_reply_committed_replay(self):
        self.events.subscribe(self.params)
        message_id = self.ingest()
        self.assertTrue(self.events.deliver_once())
        value = json.loads(self.callback.calls[-1][1])
        self.assertEqual(value["data"], {"message_id": message_id})
        self.assertNotIn("Private", self.callback.calls[-1][1].decode())
        self.assertNotIn("fixture-context", self.callback.calls[-1][1].decode())
        self.store.queue_reply(message_id, "Fixture reply")
        self.unsubscribe()
        self.events.subscribe(self.params)
        self.assertFalse(self.events.deliver_once())

    def test_single_active_destination_and_idempotent_cancel(self):
        self.events.subscribe(self.params)
        different = json.loads(json.dumps(self.params))
        different["delivery"]["url"] += "-other-chat"
        with self.assertRaisesRegex(BridgeError, "subscription_conflict"):
            self.events.subscribe(different)
        self.assertEqual(self.unsubscribe(), {})
        self.assertEqual(self.unsubscribe(), {})
        self.events.subscribe(different)
        self.assertEqual(self.events.status()["active_subscriptions"], 1)

    def test_rotation_survives_restart_and_sends_two_signatures(self):
        subscription = self.events.subscribe(self.params)
        self.params["delivery"]["secret"] = "whsec_" + base64.b64encode(b"another-fixture-key-32-byte-data!").decode()
        refreshed = self.events.subscribe(self.params)
        self.assertEqual(subscription["id"], refreshed["id"])
        self.store.close()
        self.store = Store(self.directory.name, clock=self.clock)
        self.events = Events(self.store, ["callback.example.com"], self.callback, self.clock)
        self.ingest()
        self.events.deliver_once()
        self.assertEqual(len(self.callback.calls[-1][2]["webhook-signature"].split()), 2)
        self.clock.now += 301
        self.ingest("2")
        self.events.deliver_once()
        self.assertEqual(len(self.callback.calls[-1][2]["webhook-signature"].split()), 1)

    def test_bounded_retry_stable_event_fresh_signature_and_explicit_recovery(self):
        self.events.subscribe(self.params)
        self.ingest()
        self.callback.status = 503
        for _ in range(MAX_ATTEMPTS):
            self.assertTrue(self.events.deliver_once())
            self.clock.now += 400
        events = [call for call in self.callback.calls if json.loads(call[1]).get("name")]
        self.assertEqual(len({call[1] for call in events}), 1)
        self.assertEqual(len({call[2]["webhook-timestamp"] for call in events}), MAX_ATTEMPTS)
        self.assertFalse(self.events.deliver_once())
        self.assertEqual(self.events.status()["deliveries"]["failed"], 1)
        self.assertEqual(self.events.status()["last_error"], "retry_exhausted")
        self.callback.status = 200
        self.events.subscribe(self.params)
        self.assertTrue(self.events.deliver_once())

    def test_expiry_and_cancel_recover_same_identity_without_protocol_cursor(self):
        self.params["ttlMs"] = 60000
        self.events.subscribe(self.params)
        first = self.ingest()
        self.clock.now += 61
        self.assertFalse(self.events.deliver_once())
        self.assertEqual(self.events.status()["subscriptions"][0]["status"], "expired")
        self.events.subscribe(self.params)
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(json.loads(self.callback.calls[-1][1])["data"]["message_id"], first)
        self.store.queue_reply(first, "Fixture reply committed before cancellation")
        self.unsubscribe()
        second = self.ingest("2")
        self.events.subscribe(self.params)
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(json.loads(self.callback.calls[-1][1])["data"]["message_id"], second)

    def test_nonexpiring_refresh_survives_restart_and_can_be_cancelled(self):
        original = self.events.subscribe(self.params)
        self.params["ttlMs"] = None
        forever = self.events.subscribe(self.params)
        self.assertEqual(forever["id"], original["id"])
        self.assertIsNone(forever["refreshBefore"])
        self.store.close()
        self.clock.now += 10 * 365 * 86400
        self.store = Store(self.directory.name, clock=self.clock)
        self.events = Events(self.store, ["callback.example.com"], self.callback, self.clock)
        self.assertEqual(self.store.get_status()["active_subscriptions"], 1)
        self.assertIsNone(self.events.status()["subscriptions"][0]["refreshBefore"])
        self.ingest()
        self.assertTrue(self.events.deliver_once())
        self.unsubscribe()
        self.assertEqual(self.store.get_status()["active_subscriptions"], 0)
        self.assertFalse(self.events.deliver_once())

    def test_finite_refresh_replaces_nonexpiring_subscription(self):
        self.params["ttlMs"] = None
        self.events.subscribe(self.params)
        self.params["ttlMs"] = 60000
        result = self.events.subscribe(self.params)
        self.assertIsNotNone(result["refreshBefore"])
        self.clock.now += 61
        self.assertEqual(self.store.get_status()["active_subscriptions"], 0)
        self.assertEqual(self.events.status()["subscriptions"][0]["status"], "expired")

    def test_local_owner_policy_survives_refresh_and_cancellation_clears_it(self):
        self.events.subscribe(self.params)
        self.ingest()
        self.events.deliver_once()
        calls = len(self.callback.calls)
        result = self.events.set_current_subscription_nonexpiring()
        self.assertIsNone(result["refreshBefore"])
        self.assertEqual(len(self.callback.calls), calls)
        self.assertFalse(self.events.deliver_once())
        self.store.close()
        self.clock.now += 86400 * 30
        self.store = Store(self.directory.name, clock=self.clock)
        self.events = Events(self.store, ["callback.example.com"], self.callback, self.clock)
        self.assertIsNone(self.events.subscribe(self.params)["refreshBefore"])
        self.unsubscribe()
        self.assertIsNotNone(self.events.subscribe(self.params)["refreshBefore"])

    def test_local_owner_policy_pins_lifetime_across_finite_refresh(self):
        self.events.subscribe(self.params)
        self.events.set_current_subscription_nonexpiring()
        self.params["ttlMs"] = 60000
        self.assertIsNone(self.events.subscribe(self.params)["refreshBefore"])
        self.clock.now += 61
        self.assertEqual(self.events.status()["active_subscriptions"], 1)
        self.assertIsNone(self.events.subscribe(self.params)["refreshBefore"])
        self.unsubscribe()
        self.assertIsNotNone(self.events.subscribe(self.params)["refreshBefore"])

    def test_omitted_ttl_keeps_finite_default(self):
        result = self.events.subscribe(self.params)
        self.assertIsNotNone(result["refreshBefore"])
        self.clock.now += 3601
        self.assertEqual(self.events.status()["active_subscriptions"], 0)

    def test_new_callback_does_not_emit_preexisting_inbox(self):
        self.ingest()
        self.events.subscribe(self.params)
        self.assertFalse(self.events.deliver_once())
        self.assertEqual(len(self.store.list_pending()), 1)

    def test_acknowledged_without_reply_recovers_only_after_inactive_subscription(self):
        self.events.subscribe(self.params)
        self.ingest()
        self.events.deliver_once()
        self.events.subscribe(self.params)
        self.assertFalse(self.events.deliver_once())
        self.unsubscribe()
        self.events.subscribe(self.params)
        self.assertTrue(self.events.deliver_once())
        self.clock.now += 3601
        self.events.status()
        self.events.subscribe(self.params)
        self.assertTrue(self.events.deliver_once())

    def test_410_stops_subscription_413_never_retries(self):
        self.events.subscribe(self.params)
        self.ingest()
        self.callback.status = 413
        self.events.deliver_once()
        self.clock.now += 10
        self.assertFalse(self.events.deliver_once())
        self.ingest("2")
        self.callback.status = 410
        self.events.deliver_once()
        self.assertEqual(self.events.status()["active_subscriptions"], 0)
        self.assertFalse(self.events.deliver_once())

    def test_http_408_is_transient(self):
        self.events.subscribe(self.params)
        self.ingest()
        self.callback.status = 408
        self.events.deliver_once()
        self.assertEqual(self.events.status()["deliveries"]["retry"], 1)
        self.clock.now += 3
        self.callback.status = 200
        self.assertTrue(self.events.deliver_once())
        self.assertEqual(self.events.status()["deliveries"]["acknowledged"], 1)


class NetworkTests(unittest.TestCase):
    def test_bad_proxy_and_dns_options_reject_before_network(self):
        invalid = ["http://localhost:9000", "http://127.0.0.1:0", "http://127.0.0.1:65536", "https://127.0.0.1:9000", "http://127.0.0.1:9000/", "http://127.0.0.1:9000?q=x", "http://127.0.0.1:9000#x", "http://user:pass@127.0.0.1:9000", "http://127.0.0.1:9000\n", "http://192.168.1.1:9000"]
        with patch("wechat_dot_bridge.events.socket.getaddrinfo") as dns, patch("wechat_dot_bridge.events._https_request") as request:
            for proxy in invalid:
                with self.assertRaisesRegex(BridgeError, "invalid_callback_proxy"):
                    secure_post("https://callback.example.com/x", b"{}", {}, {"callback.example.com"}, proxy=proxy, dns="google")
            with self.assertRaisesRegex(BridgeError, "invalid_callback_dns"):
                secure_post("https://callback.example.com/x", b"{}", {}, {"callback.example.com"}, dns="untrusted")
            dns.assert_not_called()
            request.assert_not_called()

    def test_allowlist_is_checked_before_google_dns(self):
        with patch("wechat_dot_bridge.events._google_addresses") as doh, patch("wechat_dot_bridge.events.socket.getaddrinfo") as dns:
            with self.assertRaisesRegex(BridgeError, "callback_not_allowed"):
                secure_post("https://unapproved.example.com/private", b"{}", {}, {"callback.example.com"}, proxy="http://127.0.0.1:9000", dns="google")
            doh.assert_not_called()
            dns.assert_not_called()

    def test_google_dns_pins_provider_and_exposes_only_hostname(self):
        answer = json.dumps({"Status": 0, "Answer": [{"type": 5, "data": "alias.example.net."}, {"type": 1, "data": "93.184.216.34"}]}).encode()
        webhook_headers = {"webhook-id": "fixture-event", "webhook-signature": "fixture-secret", "X-MCP-Subscription-Id": "fixture-subscription"}
        proxy = "http://127.0.0.1:9000"
        with patch("wechat_dot_bridge.events.socket.getaddrinfo") as system_dns, patch("wechat_dot_bridge.events._https_request", side_effect=[(200, answer), (204, b"")]) as request:
            result = secure_post("https://callback.example.com/private-route?callback=fixture", b'{"fixture":"private-body"}', webhook_headers, {"callback.example.com"}, proxy=proxy, dns="google")
            self.assertEqual(result, (204, b""))
            first, second = request.call_args_list
            self.assertEqual(first.args[0], "dns.google")
            self.assertEqual(first.args[1][0][3], ("8.8.8.8", 443))
            self.assertEqual(first.args[2:5], ("GET", "/resolve?name=callback.example.com&type=A", None))
            self.assertEqual(first.args[5], {"Accept": "application/dns-json"})
            self.assertEqual(first.args[6:], (proxy, MAX_DNS_RESPONSE))
            self.assertNotIn("private", str(first))
            self.assertNotIn("fixture-secret", str(first))
            self.assertEqual(second.args[0], "callback.example.com")
            self.assertEqual(second.args[1][0][3], ("93.184.216.34", 443))
            self.assertEqual(second.args[6], proxy)
            system_dns.assert_not_called()

    def test_google_bad_answers_do_not_post_or_fallback(self):
        cases = [b"not-json", b"{}", b'{"Status":true,"Answer":[{"type":1,"data":"93.184.216.34"}]}',
                 b'{"Status":3,"Answer":[]}', b'{"Status":0,"Answer":[]}',
                 b'{"Status":0,"Answer":[{"type":5,"data":"alias.example.com"}]}',
                 b'{"Status":0,"Answer":[{"type":1,"data":"127.0.0.1"}]}',
                 b'{"Status":0,"Answer":[{"type":1,"data":"93.184.216.34"},{"type":1,"data":"10.0.0.1"}]}',
                 b'{"Status":0,"Answer":[{"type":"1","data":"93.184.216.34"}]}',
                 b'{"Status":0,"Answer":[{"type":1,"data":"2606:4700::1111"}]}',
                 json.dumps({"Status": 0, "Answer": [{"type": 1, "data": "93.184.216.34"}] * 17}).encode()]
        for answer in cases:
            with patch("wechat_dot_bridge.events.socket.getaddrinfo") as system_dns, patch("wechat_dot_bridge.events._https_request", return_value=(200, answer)) as request:
                with self.assertRaises(BridgeError):
                    secure_post("https://callback.example.com/x", b"{}", {}, {"callback.example.com"}, proxy="http://127.0.0.1:9000", dns="google")
                request.assert_called_once()
                system_dns.assert_not_called()

    def test_google_redirect_and_size_bound_stop_before_callback(self):
        from wechat_dot_bridge.net_transport import NetworkFailure, NetworkResponse
        for result, code in [(NetworkResponse(302, b"", ()), "callback_redirect"),
                             (NetworkFailure("response_too_large"), "callback_response_too_large")]:
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with patch("wechat_dot_bridge.events.socket.getaddrinfo") as dns, patch("wechat_dot_bridge.events.request", **kwargs) as request:
                with self.assertRaisesRegex(BridgeError, code):
                    secure_post("https://callback.example.com/x", b"{}", {}, {"callback.example.com"}, proxy="http://127.0.0.1:9000", dns="google")
                request.assert_called_once()
                self.assertEqual(request.call_args.args[0], "dns.google")
                self.assertEqual(request.call_args.kwargs["max_response"], MAX_DNS_RESPONSE)
                dns.assert_not_called()

    def test_dns_resolution_timeout_is_bounded(self):
        release = threading.Event()

        def delayed(*args, **kwargs):
            release.wait(1)
            return []

        try:
            with patch("wechat_dot_bridge.events.DNS_TIMEOUT", 0.01), patch("wechat_dot_bridge.events.socket.getaddrinfo", side_effect=delayed):
                with self.assertRaisesRegex(BridgeError, "callback_timeout"):
                    secure_post("https://callback.example.com/x", b"{}", {}, {"callback.example.com"})
        finally:
            release.set()

    def test_url_rejections_make_no_dns_or_connection(self):
        cases = ["http://callback.example.com/x", "https://callback.example.com.evil.com/x", "https://user:pass@callback.example.com/x", "https://callback.example.com:8443/x", "https://callback.example.com/x#fragment", "https://127.0.0.1/x"]
        with patch("wechat_dot_bridge.events.socket.getaddrinfo") as dns:
            for url in cases:
                with self.assertRaises(BridgeError):
                    secure_post(url, b"{}", {}, {"callback.example.com"})
            dns.assert_not_called()

    def test_mixed_public_private_dns_and_rebinding_blocked_each_attempt(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        with patch("wechat_dot_bridge.events.socket.getaddrinfo", return_value=[public, private]):
            with self.assertRaisesRegex(BridgeError, "callback_dns_blocked"):
                secure_post("https://callback.example.com/x", b"{}", {}, {"callback.example.com"})
        from wechat_dot_bridge.net_transport import NetworkResponse
        with patch("wechat_dot_bridge.events.socket.getaddrinfo", side_effect=[[public], [private]]) as dns, patch("wechat_dot_bridge.events.request", return_value=NetworkResponse(204, b"", ())) as request:
            self.assertEqual(secure_post("https://callback.example.com/x", b"{}", {}, {"callback.example.com"}), (204, b""))
            with self.assertRaisesRegex(BridgeError, "callback_dns_blocked"):
                secure_post("https://callback.example.com/x", b"{}", {}, {"callback.example.com"})
            self.assertEqual(dns.call_count, 2)
            request.assert_called_once()
            self.assertEqual(request.call_args.args[0], "callback.example.com")

    def test_redirect_never_followed(self):
        from wechat_dot_bridge.net_transport import NetworkResponse
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        with patch("wechat_dot_bridge.events.socket.getaddrinfo", return_value=[public]), patch("wechat_dot_bridge.events.request", return_value=NetworkResponse(302, b"", ())) as request:
            with self.assertRaisesRegex(BridgeError, "callback_redirect"):
                secure_post("https://callback.example.com/x", b"{}", {}, {"callback.example.com"})
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
