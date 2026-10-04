import base64
import random
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_dot_bridge.attachment_content import CHUNK_BYTES, read_content
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.mcp import MCPServer
from wechat_dot_bridge.store import Store


class AttachmentContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state")
        self.store.import_binding(dict(id="fixture-bot", owner="fixture-owner", token="fixture-token",
                                       base_url="https://ilinkai.weixin.qq.com"))
        self.server = MCPServer(self.store, Events(self.store))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def stage(self, data, name="fixture.json"):
        return self.store.stage_attachment(base64.b64encode(data).decode(), "file", name)["attachment_id"]

    def read_all(self, aid):
        offset, metadata, binary, text = 0, [], [], []
        while offset is not None:
            result = self.server._call({"name": "read_weixin_attachment",
                                        "arguments": {"attachment_id": aid, "offset": offset}})
            self.assertFalse(result["isError"])
            value = result["structuredContent"]
            metadata.append(value)
            binary.append(base64.b64decode(next(item["resource"]["blob"] for item in result["content"]
                                                if item["type"] == "resource")))
            text.append(result["content"][-1]["text"])
            offset = value["next_offset"]
        return metadata, b"".join(binary), "".join(text)

    def test_json_utf8_roundtrip_with_every_multibyte_boundary(self):
        for character in ("é", "中", "😀"):
            for partial in range(1, len(character.encode())):
                with self.subTest(character=character, partial=partial):
                    source = "a" * (CHUNK_BYTES - partial) + character + "中文😀" * 5000
                    raw = source.encode()
                    aid = self.stage(raw)
                    metadata, binary, text = self.read_all(aid)
                    self.assertEqual(binary, raw)
                    self.assertEqual(text, source)
                    self.assertEqual(metadata[0]["next_offset"], CHUNK_BYTES)
                    self.assertEqual(metadata[0]["text_next_offset"], CHUNK_BYTES - partial)
                    self.assertEqual(metadata[1]["text_offset"], CHUNK_BYTES - partial)
                    self.assertEqual(metadata[1]["offset"], CHUNK_BYTES)
                    self.assertNotIn("\ufffd", text)

    def test_retry_and_out_of_order_reads_are_stateless(self):
        raw = ("a" * (CHUNK_BYTES - 1) + "中" + "剩余内容").encode()
        aid = self.stage(raw)
        arguments = {"attachment_id": aid, "offset": CHUNK_BYTES}
        later = read_content(self.store, arguments)
        first = read_content(self.store, {"attachment_id": aid})
        self.assertEqual(later, read_content(self.store, arguments))
        self.assertEqual(first[1][-1]["text"] + later[1][-1]["text"], raw.decode())
        self.assertEqual(base64.b64decode(later[1][0]["resource"]["blob"]), raw[CHUNK_BYTES:])

    def test_invalid_utf8_matches_one_decode_without_changing_binary(self):
        rng = random.Random(20261004)
        raw = bytes(rng.randrange(256) for _ in range(1000)) + b"\xf0\x9f\x92"
        aid = self.stage(raw, "fixture.txt")
        for chunk_size in (1, 2, 3, 7, 31):
            with self.subTest(chunk_size=chunk_size), patch("wechat_dot_bridge.attachment_content.CHUNK_BYTES", chunk_size):
                metadata, binary, text = self.read_all(aid)
                self.assertEqual(binary, raw)
                self.assertEqual(text, raw.decode("utf-8", errors="replace"))
                self.assertIsNone(metadata[-1]["next_offset"])

    def test_empty_and_past_end_reads_keep_compatible_byte_cursors(self):
        raw = "中文".encode()
        aid = self.stage(raw)
        value, content = read_content(self.store, {"attachment_id": aid, "offset": len(raw) + 10})
        self.assertEqual(value["offset"], len(raw) + 10)
        self.assertIsNone(value["next_offset"])
        self.assertEqual(content[-1]["text"], "")
        self.assertEqual(base64.b64decode(content[0]["resource"]["blob"]), b"")

    def test_nontext_files_return_exact_binary_resource(self):
        raw = b"\x00\xff\x01\x80"
        aid = self.stage(raw, "fixture.bin")
        value, content = read_content(self.store, {"attachment_id": aid})
        self.assertNotIn("text_encoding", value)
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["resource"]["mimeType"], "application/octet-stream")
        self.assertEqual(base64.b64decode(content[0]["resource"]["blob"]), raw)
