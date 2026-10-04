"""QR login fixtures: no real QR acquisition, credentials or network requests."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.login import LOGIN_FILE, LoginClient, poll_login, start_login
from wechat_dot_bridge.store import Store


QR = {"qrcode": "fixture-private-qr-key", "qrcode_img_content": "https://fixture.weixin.qq.com/qr/private-fixture"}
CONFIRMED = {"status": "confirmed", "bot_token": "fixture-private-token-NOT-REAL",
             "ilink_bot_id": "fixture@im.bot", "ilink_user_id": "fixture@im.wechat",
             "baseurl": "https://ilinkai.weixin.qq.com"}


class LoginStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.client = Mock()
        self.client.start_login.return_value = dict(QR)
        self.client.poll_login.return_value = {"status": "wait"}
        self.clock = Mock(return_value=100.0)

    def tearDown(self):
        self.temporary.cleanup()

    def start(self):
        return start_login(self.state, client=self.client, clock=self.clock)

    def poll(self, code=None):
        return poll_login(self.state, verify_code=code, client=self.client, clock=self.clock)

    def private_state(self):
        return json.loads((self.state / LOGIN_FILE).read_text())

    def test_start_returns_only_safe_metadata_and_private_files(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.start()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(result["status"], "wait")
        self.assertEqual(result["expires_at"], 400)
        self.assertNotIn(QR["qrcode"], json.dumps(result))
        self.assertNotIn(QR["qrcode_img_content"], json.dumps(result))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / LOGIN_FILE).stat().st_mode), 0o600)
        self.assertEqual(self.private_state()["qrcode"], QR["qrcode"])
        self.start()
        self.client.start_login.assert_called_once()

    def test_phone_verification_is_requested_and_never_persisted(self):
        self.start()
        with self.assertRaises(BridgeError) as caught:
            self.poll("912345")
        self.assertEqual(caught.exception.code, "verify_code_not_requested")
        self.client.poll_login.return_value = {"status": "need_verifycode"}
        result = self.poll()
        self.assertTrue(result["requires_phone_verification"])
        self.client.poll_login.return_value = {"status": "scaned"}
        result = self.poll("912345")
        self.assertEqual(result["status"], "scaned")
        self.assertFalse(result["requires_phone_verification"])
        self.client.poll_login.assert_called_with(QR["qrcode"], "https://ilinkai.weixin.qq.com", "912345")
        self.assertNotIn("912345", (self.state / LOGIN_FILE).read_text())
        self.assertNotIn("912345", json.dumps(result))

    def test_confirmed_binding_is_saved_disabled_and_qr_is_cleared(self):
        self.start()
        self.client.poll_login.return_value = dict(CONFIRMED)
        result = self.poll()
        self.assertEqual(result, {"status": "confirmed", "bound": True, "enabled": False,
                                  "requires_phone_verification": False})
        private = (self.state / LOGIN_FILE).read_text()
        for secret in [QR["qrcode"], QR["qrcode_img_content"], CONFIRMED["bot_token"]]:
            self.assertNotIn(secret, private)
            self.assertNotIn(secret, json.dumps(result))
        store = Store(self.state)
        try:
            binding = store.get_binding()
            self.assertEqual(binding["id"], CONFIRMED["ilink_bot_id"])
            self.assertEqual(binding["owner"], CONFIRMED["ilink_user_id"])
            self.assertEqual(binding["token"], CONFIRMED["bot_token"])
            self.assertFalse(binding["enabled"])
        finally:
            store.close()
        self.poll()
        self.assertEqual(self.client.poll_login.call_count, 1)
        with self.assertRaises(BridgeError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "login_already_bound")

    def test_redirect_allows_only_tencent_origin_and_preserves_poll_target(self):
        self.start()
        self.client.poll_login.return_value = {"status": "scaned_but_redirect", "redirect_host": "idc.weixin.qq.com"}
        self.assertEqual(self.poll()["status"], "scaned_but_redirect")
        self.client.poll_login.return_value = {"status": "wait"}
        self.poll()
        self.client.poll_login.assert_called_with(QR["qrcode"], "https://idc.weixin.qq.com", None)
        for host in ["evil.example", "ilinkai.weixin.qq.com@evil.example", "idc.weixin.qq.com:8443", "127.0.0.1"]:
            self.client.poll_login.return_value = {"status": "scaned_but_redirect", "redirect_host": host}
            with self.subTest(host=host), self.assertRaises(BridgeError) as caught:
                self.poll()
            self.assertEqual(caught.exception.code, "endpoint_rejected")
            self.assertEqual(self.private_state()["base_url"], "https://idc.weixin.qq.com")

    def test_expiry_and_blocking_clear_qr_without_automatic_refresh(self):
        self.start()
        self.clock.return_value = 400
        self.assertEqual(self.poll()["status"], "expired")
        self.client.poll_login.assert_not_called()
        self.assertNotIn("qrcode", self.private_state())
        self.clock.return_value = 401
        self.start()
        self.client.poll_login.return_value = {"status": "verify_code_blocked"}
        self.assertEqual(self.poll()["status"], "verify_code_blocked")
        self.assertNotIn("qrcode", self.private_state())
        self.assertEqual(self.client.start_login.call_count, 2)

    def test_already_bound_server_without_credentials_is_not_usable_binding(self):
        self.start()
        self.client.poll_login.return_value = {"status": "binded_redirect"}
        result = self.poll()
        self.assertEqual(result["status"], "already_bound")
        self.assertFalse(result["bound"])
        store = Store(self.state)
        try:
            self.assertIsNone(store.get_binding())
        finally:
            store.close()

    def test_malformed_confirmed_response_does_not_create_binding(self):
        self.start()
        for changes in [{"bot_token": None}, {"ilink_user_id": None}, {"ilink_bot_id": None},
                        {"baseurl": "https://evil.example"}]:
            self.client.poll_login.return_value = dict(CONFIRMED, **changes)
            with self.subTest(changes=changes), self.assertRaises(BridgeError):
                self.poll()
            store = Store(self.state)
            try:
                self.assertIsNone(store.get_binding())
            finally:
                store.close()

    def test_timeout_is_safe_and_retains_pending_qr(self):
        self.start()
        self.client.poll_login.side_effect = BridgeError("network_timeout")
        result = self.poll()
        self.assertEqual(result["error"], "network_timeout")
        self.assertEqual(result["status"], "wait")
        self.assertNotIn("qrcode", result)
        self.assertIn("qrcode", self.private_state())

    def test_confirmed_database_survives_crash_before_qr_state_replacement(self):
        self.start()
        store = Store(self.state)
        try:
            store.import_binding({"id": CONFIRMED["ilink_bot_id"], "owner": CONFIRMED["ilink_user_id"],
                                  "token": CONFIRMED["bot_token"], "base_url": CONFIRMED["baseurl"]})
        finally:
            store.close()
        result = self.poll()
        self.assertEqual(result["status"], "already_bound")
        self.assertTrue(result["bound"])
        self.assertFalse(result["enabled"])
        self.assertNotIn("qrcode", self.private_state())
        self.client.poll_login.assert_not_called()

    def test_rejects_symlink_and_open_permissions(self):
        self.start()
        login_path = self.state / LOGIN_FILE
        os.chmod(login_path, 0o644)
        with self.assertRaises(BridgeError) as caught:
            self.poll()
        self.assertEqual(caught.exception.code, "unsafe_login_file")
        os.chmod(login_path, 0o600)
        login_path.unlink()
        unrelated = self.state / "unrelated"
        unrelated.write_text("fixture-private")
        login_path.symlink_to(unrelated)
        with self.assertRaises(BridgeError):
            self.poll()
        self.assertEqual(unrelated.read_text(), "fixture-private")

    def test_invalid_digits_and_poll_before_start_have_safe_errors(self):
        for code in [1234, "1234\n", "a123", "", "1" * 17]:
            with self.subTest(code=code), self.assertRaises(BridgeError) as caught:
                self.poll(code)
            self.assertEqual(caught.exception.code, "invalid_verify_code")
        with self.assertRaises(BridgeError) as caught:
            self.poll()
        self.assertEqual(caught.exception.code, "login_not_started")


class LoginProtocolTests(unittest.TestCase):
    def test_current_protocol_posts_qr_and_gets_status_with_encoded_digits(self):
        client = LoginClient()
        with patch.object(client, "_request", return_value=dict(QR)) as request:
            client.start_login()
            request.assert_called_with("https://ilinkai.weixin.qq.com", "POST",
                                       "/ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []})
        with patch.object(client, "_request", return_value={"status": "wait"}) as request:
            client.poll_login("fixture&private=qr", verify_code="1234")
            origin, method, path = request.call_args.args
            self.assertEqual(origin, "https://ilinkai.weixin.qq.com")
            self.assertEqual(method, "GET")
            self.assertEqual(parse_qs(urlsplit(path).query), {"qrcode": ["fixture&private=qr"], "verify_code": ["1234"]})

    def test_login_request_has_no_existing_authorization_token(self):
        from wechat_dot_bridge.net_transport import NetworkResponse
        for method in ["POST", "GET"]:
            with patch("wechat_dot_bridge.login._resolve_public", return_value="8.8.8.8"), \
                 patch("wechat_dot_bridge.login.request", return_value=NetworkResponse(200, b'{"status":"wait"}', ())) as request:
                LoginClient()._request("https://ilinkai.weixin.qq.com", method, "/fixture", {"local_token_list": []})
            headers = request.call_args.args[5]
            self.assertNotIn("Authorization", headers)
            if method == "GET":
                self.assertNotIn("AuthorizationType", headers)
                self.assertNotIn("X-WECHAT-UIN", headers)
            request.assert_called_once()

    def test_http_redirect_never_followed_and_errors_never_include_response(self):
        from wechat_dot_bridge.net_transport import NetworkResponse
        for status, raw, expected in [(302, b"", "endpoint_rejected"),
                                      (500, b"fixture-private-token", "http_error"),
                                      (200, b"fixture-private-invalid-json", "invalid_login_response")]:
            with patch("wechat_dot_bridge.login._resolve_public", return_value="8.8.8.8"), \
                 patch("wechat_dot_bridge.login.request", return_value=NetworkResponse(status, raw, ())) as request:
                with self.assertRaises(BridgeError) as caught:
                    LoginClient().start_login()
            self.assertEqual(str(caught.exception), expected)
            self.assertNotIn("fixture-private", str(caught.exception))
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
