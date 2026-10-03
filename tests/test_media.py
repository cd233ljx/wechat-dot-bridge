import base64
import io
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.media import (CDN_BASE, MAX_STAGE, MediaWorker, cdn_parts, cdn_request,
                                     crypt, descriptor, fetch, image_preview, key_bytes, upload_item)
from wechat_dot_bridge.mcp import MCPServer
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, WeixinWorker, _normalize_message


def picture():
    out = io.BytesIO()
    Image.new("RGB", (48, 32), "blue").save(out, "PNG")
    return out.getvalue()


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.binding = {"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-private-token",
                        "base_url": "https://ilinkai.weixin.qq.com"}
        self.store.import_binding(self.binding)
        self.store.set_enabled(True)
        self.server = MCPServer(self.store, Events(self.store))
        self.key = bytes(range(16))
        self.data = picture()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def inbound(self, kind=2, extra=None):
        kinds = {2: "image", 3: "voice", 4: "file", 5: "video"}
        body = {"media": {"encrypt_query_param": "fixture-private-cdn", "aes_key": base64.b64encode(self.key).decode()}, **(extra or {})}
        wire = {"message_id": 1, "from_user_id": "fixture-owner", "message_type": 1, "message_state": 2,
                "context_token": "fixture-private-context", "item_list": [{"type": kind, kinds[kind] + "_item": body}]}
        normalized = _normalize_message(wire, self.binding, 100)
        self.store.ingest("fixture-account", [normalized], "cursor-1")
        return self.store.list_pending()[0]["message_id"]

    def call(self, name, args):
        return self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}})["result"]

    def test_aes_known_vector_and_both_key_encodings(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        encrypted = crypt(plain, key)
        self.assertEqual(encrypted[:16].hex(), "3ad77bb40d7a3660a89ecaf32466ef97")
        self.assertEqual(len(encrypted), 32)
        self.assertEqual(crypt(encrypted, key, True), plain)
        self.assertEqual(key_bytes(base64.b64encode(key).decode()), key)
        self.assertEqual(key_bytes(base64.b64encode(key.hex().encode()).decode()), key)
        self.assertEqual(key_bytes(key.hex(), True), key)
        with self.assertRaises(BridgeError):
            crypt(encrypted[:-1], key, True)

    def test_key_precedence_and_private_urls_never_reach_network(self):
        item = {"type": 2, "image_item": {"aeskey": self.key.hex(), "media": {
            "aes_key": "invalid", "encrypt_query_param": "fixture"}}}
        self.assertEqual(descriptor(item)["key"], self.key.hex())
        for url in ["https://127.0.0.1/c2c/download", "https://evil.example/c2c/download",
                    CDN_BASE.replace("https", "http") + "/download", CDN_BASE + "/unexpected",
                    "https://novac2c.cdn.weixin.qq.com.evil.example/c2c/download"]:
            with self.subTest(url=url), patch("wechat_dot_bridge.events._public_addresses") as dns:
                with self.assertRaises(BridgeError):
                    cdn_request(url)
                dns.assert_not_called()

    def test_persist_reference_cursor_restart_download_and_native_image_tool(self):
        mid = self.inbound()
        aid = self.store.read_message(mid)["attachments"][0]["attachment_id"]
        self.assertEqual(self.store.get_binding()["cursor"], "cursor-1")
        self.store.close()
        self.store = Store(self.directory.name)
        self.server = MCPServer(self.store, Events(self.store))
        worker = MediaWorker(self.store)
        with patch("wechat_dot_bridge.media.cdn_request", return_value=(crypt(self.data, self.key), None)):
            worker.tick()
        result = self.call("read_weixin_message", {"message_id": mid})
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][1]["type"], "image")
        self.assertEqual(result["structuredContent"]["attachments"][0]["status"], "ready")
        output = json.dumps(result)
        for secret in ["fixture-private-cdn", self.key.hex(), "fixture-private-context", "fixture-private-token"]:
            self.assertNotIn(secret, output)
        self.assertEqual(self.call("read_weixin_attachment", {"attachment_id": aid})["content"][1]["mimeType"], "image/jpeg")

    def test_failed_attachment_keeps_message_visible_and_reports_safe_error(self):
        mid = self.inbound()
        worker = MediaWorker(self.store)
        with patch("wechat_dot_bridge.media.cdn_request", return_value=(b"invalid", None)):
            worker.tick()
        metadata = self.store.read_message(mid)["attachments"][0]
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["error"], "media_decryption_failed")
        self.assertEqual(len(self.store.list_pending()), 1)

    def test_download_retries_bounded_and_duplicate_poll_does_not_duplicate_media(self):
        mid = self.inbound()
        self.inbound()
        self.assertEqual(len(self.store.attachment_metadata(mid)), 1)
        worker = MediaWorker(self.store)
        with patch("wechat_dot_bridge.media.fetch", side_effect=BridgeError("media_network_failed")) as fetcher:
            for _ in range(4):
                self.store.db.execute("UPDATE attachments SET next_attempt=0")
                worker.tick()
            self.assertEqual(fetcher.call_count, 3)
        self.assertEqual(self.store.attachment_metadata(mid)[0]["status"], "failed")

    def test_stage_reply_upload_encrypt_same_owner_and_append_text(self):
        mid = self.inbound(3, {"text": "微信转写"})
        self.assertEqual(self.store.read_message(mid)["text"], "微信转写")
        staged = self.call("stage_weixin_attachment", {"data_base64": base64.b64encode(self.data).decode(), "kind": "image", "name": "picture.png"})["structuredContent"]
        args = {"message_id": mid, "attachment_id": staged["attachment_id"], "text": "这张图片", "reply_key": "image1"}
        first = self.call("reply_weixin_attachment", args)["structuredContent"]
        self.assertEqual(first, self.call("reply_weixin_attachment", args)["structuredContent"])
        client = IlinkClient()
        posts = []

        def api(binding, path, body, timeout, **kwargs):
            posts.append((path, body))
            return {"upload_param": "fixture-upload"} if path.endswith("getuploadurl") else {}

        client._post = api
        uploads = []

        def cdn(url, data=None):
            uploads.append(data)
            return b"", "fixture-download"

        with patch("wechat_dot_bridge.media.cdn_request", side_effect=cdn):
            self.assertTrue(WeixinWorker(self.store, client).send_once())
        request = posts[0][1]
        self.assertEqual(crypt(uploads[0], bytes.fromhex(request["aeskey"]), True), self.data)
        msg = posts[-1][1]["msg"]
        self.assertEqual(msg["to_user_id"], "fixture-owner")
        self.assertEqual([x["type"] for x in msg["item_list"]], [1, 2])
        self.assertEqual(self.store.send_status(mid, first["reply_id"])["status"], "accepted")
        self.assertEqual(self.store.queue_reply(mid, "完成")["status"], "queued")

    def test_file_chunks_and_invalid_images(self):
        raw = b"file-content" * 16000
        mid = self.inbound(4, {"file_name": "../../report.txt"})
        aid = self.store.attachment_metadata(mid)[0]["attachment_id"]
        self.store.finish_attachment(aid, raw)
        first = self.call("read_weixin_attachment", {"attachment_id": aid})
        self.assertEqual(first["structuredContent"]["name"], "report.txt")
        offset = first["structuredContent"]["next_offset"]
        last = self.call("read_weixin_attachment", {"attachment_id": aid, "offset": offset})
        recovered = b"".join(base64.b64decode(r["content"][1]["resource"]["blob"]) for r in [first, last])
        self.assertEqual(recovered, raw)
        with self.assertRaisesRegex(BridgeError, "media_image_invalid"):
            image_preview(b"not an image")
        with self.assertRaises(BridgeError):
            self.store.stage_attachment(base64.b64encode(b"x" * (MAX_STAGE + 1)).decode(), "file", "large.bin")

    def test_attachment_unavailable_and_cross_owner_ids_fail_without_disclosure(self):
        mid = self.inbound()
        aid = self.store.attachment_metadata(mid)[0]["attachment_id"]
        result = self.call("read_weixin_attachment", {"attachment_id": aid})
        self.assertEqual(result["structuredContent"]["error"], "attachment_pending")
        self.store.db.execute("UPDATE attachments SET peer='other-owner' WHERE id=?", (aid,))
        result = self.call("read_weixin_attachment", {"attachment_id": aid})
        self.assertEqual(result["structuredContent"]["error"], "unknown_owner_attachment")
        self.assertNotIn("fixture-private", json.dumps(result))

    def test_cdn_proxy_keeps_pinned_host_and_never_sends_weixin_auth(self):
        from wechat_dot_bridge.media import configure_network
        configure_network('http://127.0.0.1:7890', 'google')
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'fixture'
        response.getheader.return_value = None
        try:
            with patch('wechat_dot_bridge.events._google_addresses', return_value=['fixture-ip']) as resolve, patch('wechat_dot_bridge.events._PinnedHTTPSConnection', return_value=connection) as factory:
                self.assertEqual(cdn_request(CDN_BASE + '/download?encrypted_query_param=fixture')[0], b'fixture')
                resolve.assert_called_once_with('novac2c.cdn.weixin.qq.com', 'http://127.0.0.1:7890')
                factory.assert_called_once_with('novac2c.cdn.weixin.qq.com', ['fixture-ip'], 'http://127.0.0.1:7890')
                headers = connection.request.call_args.kwargs['headers']
                self.assertNotIn('Authorization', headers)
                self.assertEqual(headers['Host'], 'novac2c.cdn.weixin.qq.com')
        finally:
            configure_network()

    def test_upload_retries_server_failures_but_not_client_rejection(self):
        client = Mock()
        client._post.return_value = {"upload_param": "fixture"}
        attachment = {"data": self.data, "kind": "image", "name": "fixture.png"}
        with patch("wechat_dot_bridge.media.cdn_request", side_effect=[BridgeError("media_http_error"), (b"", "fixture")]) as upload, patch("wechat_dot_bridge.media.time.sleep"):
            self.assertEqual(upload_item(client, self.binding, attachment)["type"], 2)
            self.assertEqual(upload.call_count, 2)
        with patch("wechat_dot_bridge.media.cdn_request", side_effect=BridgeError("media_http_rejected")) as upload:
            with self.assertRaisesRegex(BridgeError, "media_http_rejected"):
                upload_item(client, self.binding, attachment)
            self.assertEqual(upload.call_count, 1)


if __name__ == "__main__":
    unittest.main()
