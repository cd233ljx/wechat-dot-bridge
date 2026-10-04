"""Fixture-only protocol/security tests. Nothing connects to Weixin."""

import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.net_transport import NetworkFailure, NetworkResponse
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import (
    IlinkClient, MAX_RESPONSE_BYTES, WeixinWorker,
    _endpoint, _normalize_message, _resolve_public, load_existing_binding,
)


FIXTURE = {"id": "fixture@im.bot", "owner": "fixture@im.wechat",
           "token": "fixture-token-NOT-REAL", "base_url": "https://ilinkai.weixin.qq.com",
           "cursor": "fixture-cursor", "enabled": True, "status": "ready"}


def message(**changes):
    result = {"message_id": 18446744073709551615, "from_user_id": FIXTURE["owner"],
              "message_type": 1, "message_state": 2, "create_time_ms": 1700000000123,
              "context_token": "fixture-context-NOT-REAL",
              "item_list": [{"type": 1, "text_item": {"text": "你好，dot"}}]}
    result.update(changes)
    return result


class FakeStore:
    def __init__(self):
        self.binding = dict(FIXTURE)
        self.ingests = []
        self.errors = []
        self.finished = []
        self.reply = {"message_id": "opaque-message", "text": "收到", "peer": FIXTURE["owner"],
                      "context_token": "fixture-context-NOT-REAL", "client_id": "fixed-client-id"}

    def get_binding(self):
        return self.binding

    def set_enabled(self, enabled):
        self.binding["enabled"] = enabled

    def record_error(self, code):
        self.errors.append(code)

    def ingest(self, account, messages, cursor):
        self.ingests.append((account, messages, cursor))

    def claim_send(self):
        reply, self.reply = self.reply, None
        return reply

    def finish_send(self, message_id, status, error=None, reply_id=None):
        self.finished.append((message_id, status, error))


class NetworkSafetyTests(unittest.TestCase):
    def test_only_tencent_https_origins(self):
        self.assertEqual(_endpoint(FIXTURE["base_url"]), ("ilinkai.weixin.qq.com", FIXTURE["base_url"]))
        for bad in ["http://ilinkai.weixin.qq.com", "https://evil.example", "https://weixin.qq.com.evil.example",
                    "https://ilinkai.weixin.qq.com@evil.example", "https://user:password@ilinkai.weixin.qq.com",
                    "https://ilinkai.weixin.qq.com:8443", "https://ilinkai.weixin.qq.com/path",
                    "https://ilinkai.weixin.qq.com/?token=fixture", "https://ilinkai.weixin.qq.com/#fragment",
                    "https://127.0.0.1", "https://ilinkai.weixin.qq.com.", "https://foo..weixin.qq.com"]:
            with self.subTest(url=bad), self.assertRaises(BridgeError) as caught:
                _endpoint(bad)
            self.assertEqual(str(caught.exception), "endpoint_rejected")

    def test_dns_rejects_private_or_mixed_answers(self):
        def answer(ip):
            return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))
        for addresses in [[answer("127.0.0.1")], [answer("10.1.2.3")], [answer("169.254.169.254")],
                          [answer("224.0.0.1")], [answer("8.8.8.8"), answer("192.168.1.1")], []]:
            with patch("wechat_dot_bridge.transport.socket.getaddrinfo", return_value=addresses):
                with self.assertRaises(BridgeError) as caught:
                    _resolve_public("ilinkai.weixin.qq.com")
                self.assertEqual(caught.exception.code, "dns_rejected")
        with patch("wechat_dot_bridge.transport.socket.getaddrinfo", return_value=[answer("8.8.8.8")]):
            self.assertEqual(_resolve_public("ilinkai.weixin.qq.com"), "8.8.8.8")

    def test_hanging_dns_has_deadline_and_at_most_four_resolvers(self):
        release = threading.Event()
        threads = []
        actual_thread = threading.Thread
        slots = threading.BoundedSemaphore(4)
        answer = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

        def lookup(*_args, **_kwargs):
            release.wait(timeout=2)
            return answer

        def new_thread(*args, **kwargs):
            thread = actual_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch("wechat_dot_bridge.transport._DNS_SLOTS", slots), \
             patch("wechat_dot_bridge.transport.DNS_TIMEOUT_SECONDS", 0.01), \
             patch("wechat_dot_bridge.transport.threading.Thread", side_effect=new_thread), \
             patch("wechat_dot_bridge.transport.socket.getaddrinfo", side_effect=lookup) as resolver:
            try:
                started = time.monotonic()
                for _ in range(4):
                    with self.assertRaises(BridgeError) as caught:
                        _resolve_public("ilinkai.weixin.qq.com")
                    self.assertEqual(caught.exception.code, "network_timeout")
                self.assertLess(time.monotonic() - started, 0.5)
                for _ in range(10):
                    with self.assertRaises(BridgeError) as caught:
                        _resolve_public("ilinkai.weixin.qq.com")
                    self.assertEqual(caught.exception.code, "dns_rejected")
                self.assertEqual(resolver.call_count, 4)
                self.assertTrue(all(thread.daemon for thread in threads))
            finally:
                release.set()
                for thread in threads:
                    thread.join(timeout=1)
            self.assertEqual(_resolve_public("ilinkai.weixin.qq.com"), "8.8.8.8")

    def test_dns_exceptions_do_not_escape_from_resolver_thread(self):
        with patch("wechat_dot_bridge.transport.socket.getaddrinfo", side_effect=RuntimeError("fixture-private")):
            with self.assertRaises(BridgeError) as caught:
                _resolve_public("ilinkai.weixin.qq.com")
        self.assertEqual(str(caught.exception), "dns_rejected")

    def test_no_redirect_following_and_bounded_response(self):
        for status, raw, expected in [(302, b"", "endpoint_rejected"), (401, b"", "auth_required"),
                                      (500, b"fixture sensitive body", "http_error"),
                                      (200, b"x" * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
                                      (200, b'{"ret":0,"ret":1}', "response_invalid")]:
            connection = Mock()
            if len(raw) > MAX_RESPONSE_BYTES:
                connection.execute.side_effect = NetworkFailure("response_too_large")
            else:
                connection.execute.return_value = NetworkResponse(status, raw, ())
            with patch("wechat_dot_bridge.transport._resolve_public", return_value="8.8.8.8"), \
                 patch("wechat_dot_bridge.transport.PinnedRequest", return_value=connection):
                with self.assertRaises(BridgeError) as caught:
                    IlinkClient().poll(FIXTURE)
                self.assertEqual(str(caught.exception), expected)
                connection.execute.assert_called_once()
                self.assertEqual(connection.execute.call_args.args[4], MAX_RESPONSE_BYTES)
                self.assertEqual(connection.execute.call_args.args[5], {200})

    def test_network_exception_is_safe_code(self):
        connection = Mock()
        connection.execute.side_effect = NetworkFailure("timeout")
        with patch("wechat_dot_bridge.transport._resolve_public", return_value="8.8.8.8"), \
             patch("wechat_dot_bridge.transport.PinnedRequest", return_value=connection):
            with self.assertRaises(BridgeError) as caught:
                IlinkClient().send(FIXTURE, FakeStore().reply)
        self.assertEqual(str(caught.exception), "network_timeout")
        connection.execute.assert_called_once()

    def test_send_retains_fixed_id_and_only_owner_route(self):
        client = IlinkClient()
        with patch.object(client, "_post", return_value={"ret": 0}) as post:
            reply = FakeStore().reply
            client.send(FIXTURE, reply)
            body = post.call_args.args[2]
            self.assertEqual(body["msg"]["client_id"], "fixed-client-id")
            self.assertEqual(body["msg"]["to_user_id"], FIXTURE["owner"])
            self.assertEqual(body["msg"]["context_token"], reply["context_token"])
            reply["peer"] = "other@im.wechat"
            with self.assertRaises(BridgeError) as caught:
                client.send(FIXTURE, reply)
            self.assertEqual(caught.exception.code, "invalid_reply")
            self.assertEqual(post.call_count, 1)


class MessageAndWorkerTests(unittest.TestCase):
    def test_uint64_and_fallback_identity_are_stable(self):
        result = _normalize_message(message(), FIXTURE, 100)
        self.assertEqual(result["source_id"], "message:18446744073709551615")
        self.assertEqual(result["created_at"], 1700000000.123)
        fallback = message(message_id=None, client_id="fixture-client", seq=42)
        first = _normalize_message(fallback, FIXTURE, 100)
        second = _normalize_message(fallback, FIXTURE, 200)
        self.assertEqual(first["source_id"], second["source_id"])
        self.assertTrue(first["source_id"].startswith("fallback:"))
        self.assertIsNone(_normalize_message(message(message_id=None), FIXTURE, 100))

    def test_filters_other_users_groups_media_voice_echo_and_incomplete(self):
        cases = [message(from_user_id="other@im.wechat"), message(group_id="fixture-group"),
                 message(room_id="fixture-room"), message(delete_time_ms=1700000000123),
                 message(to_user_id="other@im.bot"), message(to_user_id=None),
                 message(message_type=2), message(message_type=True), message(message_state=1),
                 message(item_list=[{"type": 1, "text_item": {"text": " "}}])]
        for case in cases:
            with self.subTest(case=case):
                self.assertIsNone(_normalize_message(case, FIXTURE, 100))

    def test_poll_calls_atomic_ingest_with_only_normalized_owner_text(self):
        store = FakeStore()
        client = Mock()
        client.clock.return_value = 100
        client.poll.return_value = {"ret": 0, "msgs": [message(), message(from_user_id="other@im.wechat")],
                                    "get_updates_buf": "next-fixture-cursor"}
        self.assertTrue(WeixinWorker(store, client).poll_once())
        self.assertEqual(len(store.ingests), 1)
        account, messages, cursor = store.ingests[0]
        self.assertEqual(account, FIXTURE["id"])
        self.assertEqual(len(messages), 1)
        self.assertEqual(cursor, "next-fixture-cursor")
        self.assertEqual(set(messages[0]), {"source_id", "peer", "text", "created_at", "context_token", "attachments", "quotes", "aliases", "received_at"})
        self.assertEqual(store.errors[-1], None)

    def test_bad_poll_never_advances_cursor_and_stale_auth_disables(self):
        for response, expected in [({"ret": -14, "errmsg": "fixture-private"}, "auth_required"),
                                   ({"ret": 0, "msgs": "invalid"}, "response_invalid"),
                                   ({"unexpected": "fixture"}, "response_invalid")]:
            store = FakeStore()
            client = Mock()
            client.poll.return_value = response
            self.assertFalse(WeixinWorker(store, client).poll_once())
            self.assertEqual(store.ingests, [])
            self.assertEqual(store.errors, [expected])
            if expected == "auth_required":
                self.assertFalse(store.binding["enabled"])

    def test_send_timeout_is_uncertain_and_never_retried(self):
        store = FakeStore()
        client = Mock()
        client.send.side_effect = BridgeError("network_timeout")
        worker = WeixinWorker(store, client)
        self.assertTrue(worker.send_once())
        self.assertEqual(store.finished, [("opaque-message", "uncertain", "network_timeout")])
        self.assertFalse(worker.send_once())
        client.send.assert_called_once()

    def test_send_acceptance_is_not_claimed_as_confirmed_delivery(self):
        store = FakeStore()
        client = Mock()
        client.send.return_value = {"ret": 0}
        self.assertTrue(WeixinWorker(store, client).send_once())
        self.assertEqual(store.finished, [("opaque-message", "accepted", None)])

    def test_send_business_failure_and_context_failure_are_safe(self):
        cases = [({"ret": -14}, "auth_required", "auth_required"),
                 ({"ret": 12, "errmsg": "context_token fixture secret"}, "context_required", "context_required"),
                 ({"ret": 12, "errmsg": "fixture secret"}, "failed", "gateway_error"),
                 ({"unexpected": "fixture"}, "uncertain", "response_invalid")]
        for response, status, error in cases:
            store, client = FakeStore(), Mock()
            client.send.return_value = response
            WeixinWorker(store, client).send_once()
            self.assertEqual(store.finished, [("opaque-message", status, error)])

    def test_official_omitted_null_and_zero_success_codes(self):
        for response in [{}, {"ret": None}, {"ret": 0}, {"errcode": None}, {"message_id": "123"}]:
            store, client = FakeStore(), Mock()
            client.send.return_value = response
            self.assertTrue(WeixinWorker(store, client).send_once())
            self.assertEqual(store.finished, [("opaque-message", "accepted", None)])
        for ret in [None, 0, "absent"]:
            store, client = FakeStore(), Mock()
            response = {"msgs": [], "get_updates_buf": "fixture-next"}
            if ret != "absent":
                response["ret"] = ret
            client.poll.return_value = response
            self.assertTrue(WeixinWorker(store, client).poll_once())
            self.assertEqual(store.ingests[0][2], "fixture-next")

    def test_recipient_accepts_official_raw_normalized_account_alias(self):
        raw = message(to_user_id="fixture@im.bot", delete_time_ms=0)
        self.assertIsNotNone(_normalize_message(raw, FIXTURE, 100))
        binding = dict(FIXTURE, id="fixture-im-bot")
        self.assertIsNotNone(_normalize_message(raw, binding, 100))

    def test_disabled_binding_has_no_network_calls(self):
        store, client = FakeStore(), Mock()
        store.binding["enabled"] = False
        worker = WeixinWorker(store, client)
        self.assertFalse(worker.poll_once())
        self.assertFalse(worker.send_once())
        client.poll.assert_not_called()
        client.send.assert_not_called()


class BindingImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def official(self, name="fixture@im.bot.json", **changes):
        data = {"token": FIXTURE["token"], "baseUrl": FIXTURE["base_url"], "userId": FIXTURE["owner"]}
        data.update(changes)
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def toml(self, blocks):
        path = self.root / "fixture-config.toml"
        path.write_text('[[projects]]\nname="fixture-project"\n' + blocks, encoding="utf-8")
        return path

    @staticmethod
    def block(account="fixture-account", owner="fixture@im.wechat", extra=""):
        return ('[[projects.platforms]]\ntype="weixin"\n[projects.platforms.options]\n'
                f'token="{FIXTURE["token"]}"\naccount_id="{account}"\nallow_from="{owner}"\n' + extra)

    def test_official_account_and_only_documented_sibling_cursor(self):
        source = self.official()
        source.with_name(source.stem + ".sync.json").write_text('{"get_updates_buf":"fixture-resume"}')
        # Unrelated credential files must never be discovered.
        (self.root / "unrelated.json").write_text("invalid")
        result = load_existing_binding(source)
        self.assertEqual(result, {"id": FIXTURE["id"], "owner": FIXTURE["owner"], "token": FIXTURE["token"],
                                  "base_url": FIXTURE["base_url"], "cursor": "fixture-resume"})

    def test_import_requires_owner_and_safe_endpoint(self):
        for changes in [{"userId": None}, {"userId": "*"}, {"baseUrl": "http://127.0.0.1"},
                        {"token": "fixture\nprivate"}, {"token": "${TOKEN}"}]:
            with self.subTest(changes=changes), self.assertRaises(BridgeError):
                load_existing_binding(self.official(**changes))

    def test_cc_exact_single_account_and_explicit_selection(self):
        path = self.toml(self.block())
        self.assertEqual(load_existing_binding(path)["owner"], FIXTURE["owner"])
        path = self.toml(self.block("first") + self.block("second"))
        with self.assertRaises(BridgeError) as caught:
            load_existing_binding(path)
        self.assertEqual(caught.exception.code, "binding_ambiguous")
        self.assertEqual(load_existing_binding(path, "second")["id"], "second")

    def test_cc_rejects_wildcard_multiple_owners_and_route_tag(self):
        for owner, extra in [("*", ""), ("", ""), ("a@im.wechat,b@im.wechat", ""),
                             (FIXTURE["owner"], 'route_tag="fixture-route"\n')]:
            with self.subTest(owner=owner, extra=extra), self.assertRaises(BridgeError):
                load_existing_binding(self.toml(self.block(owner=owner, extra=extra)))

    def test_json_duplicate_fields_and_missing_file_fail_without_contents(self):
        path = self.root / "fixture.json"
        path.write_text('{"token":"fixture-secret","token":"fixture-secret-two"}')
        with self.assertRaises(BridgeError) as caught:
            load_existing_binding(path)
        self.assertEqual(str(caught.exception), "binding_invalid")
        with self.assertRaises(BridgeError) as caught:
            load_existing_binding(self.root / "missing.json")
        self.assertEqual(str(caught.exception), "import_failed")

    def test_import_rejects_source_cursor_and_parent_symlinks(self):
        source = self.official()
        link = self.root / "linked-account.json"
        link.symlink_to(source)
        with self.assertRaises(BridgeError) as caught:
            load_existing_binding(link)
        self.assertEqual(caught.exception.code, "binding_invalid")
        cursor_target = self.root / "private-cursor.json"
        cursor_target.write_text('{"get_updates_buf":"fixture-cursor"}')
        cursor_link = source.with_name(source.stem + ".sync.json")
        cursor_link.symlink_to(cursor_target)
        with self.assertRaises(BridgeError) as caught:
            load_existing_binding(source)
        self.assertEqual(caught.exception.code, "binding_invalid")
        cursor_link.unlink()
        parent_link = self.root / "linked-parent"
        parent_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(BridgeError) as caught:
            load_existing_binding(parent_link / source.name)
        self.assertEqual(caught.exception.code, "binding_invalid")

    def test_import_rejects_nonregular_fifo_without_blocking(self):
        import os
        path = self.root / "fixture-fifo.json"
        os.mkfifo(path)
        with self.assertRaises(BridgeError) as caught:
            load_existing_binding(path)
        self.assertEqual(caught.exception.code, "binding_invalid")


class PersistenceIntegrationTests(unittest.TestCase):
    def test_restart_never_resends_uncertain_post_and_duplicate_poll_deduplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            client = Mock()
            client.clock.return_value = 100
            client.poll.return_value = {"ret": 0, "msgs": [message()], "get_updates_buf": "fixture-next"}
            client.send.side_effect = BridgeError("network_timeout")
            store = Store(state)
            try:
                store.import_binding(FIXTURE)
                store.set_enabled(True)
                worker = WeixinWorker(store, client)
                self.assertTrue(worker.poll_once())
                pending = store.list_pending()
                self.assertEqual(len(pending), 1)
                message_id = pending[0]["message_id"]
                store.queue_reply(message_id, "收到")
                self.assertTrue(worker.send_once())
                self.assertEqual(store.send_status(message_id)["status"], "uncertain")
                self.assertEqual(store.get_binding()["cursor"], "fixture-next")
            finally:
                store.close()
            store = Store(state)
            try:
                store.recover_inflight()
                worker = WeixinWorker(store, client)
                self.assertFalse(worker.send_once())
                self.assertTrue(worker.poll_once())
                self.assertEqual(store.list_pending(), [])
                self.assertEqual(store.send_status(message_id)["status"], "uncertain")
                client.send.assert_called_once()
            finally:
                store.close()

    def test_missing_context_blocks_post_until_new_owner_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Mock()
            client.clock.return_value = 100
            client.poll.return_value = {"ret": 0, "msgs": [message(context_token="")], "get_updates_buf": "first"}
            client.send.return_value = {"ret": 0}
            store = Store(Path(temporary) / "state")
            try:
                store.import_binding(FIXTURE)
                store.set_enabled(True)
                worker = WeixinWorker(store, client)
                self.assertTrue(worker.poll_once())
                message_id = store.list_pending()[0]["message_id"]
                store.queue_reply(message_id, "收到")
                self.assertFalse(worker.send_once())
                self.assertEqual(store.send_status(message_id)["status"], "context_required")
                client.send.assert_not_called()
                client.poll.return_value = {"ret": 0, "msgs": [message(message_id=123, create_time_ms=1700000001123)],
                                            "get_updates_buf": "second"}
                self.assertTrue(worker.poll_once())
                self.assertTrue(worker.send_once())
                self.assertEqual(store.send_status(message_id)["status"], "accepted")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
