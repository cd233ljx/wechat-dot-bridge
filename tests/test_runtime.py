"""Coordinated shutdown keeps active sends uncertain and the store open."""

from pathlib import Path
import base64
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import anyio

from wechat_dot_bridge.cli import _run_runtime
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.media import MediaWorker
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import WeixinWorker


class InterruptedClient:
    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.posts = []

    def poll(self, binding):
        return {"msgs": [], "get_updates_buf": "fixture-cursor"}

    def send(self, binding, reply):
        self.posts.append(reply["client_id"])
        self.started.set()
        if not self.stopped.wait(5):
            raise AssertionError("runtime failed to cancel active send")
        raise BridgeError("network_cancelled")

    def cancel(self):
        self.stopped.set()


class RuntimeTests(unittest.TestCase):
    def check_shutdown(self, protocol_failure):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            store = Store(state)
            store.import_binding({"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token",
                                  "base_url": "https://ilinkai.weixin.qq.com"})
            store.set_enabled(True)
            store.ingest("fixture-account", [{"source_id": "1", "peer": "fixture-owner", "text": "fixture-input",
                                              "created_at": 1800000000, "context_token": "fixture-context"}], "1")
            mid = store.list_pending()[0]["message_id"]
            queued = store.queue_reply(mid, "fixture-output")
            client = InterruptedClient()
            worker = WeixinWorker(store, client=client)
            typing = Mock()
            def close_presence():
                # If cleanup runs before workers, this remains 'sending'. If
                # the store has already closed, reading it raises instead.
                self.assertEqual(store.send_status(mid)["status"], "uncertain")
                self.assertEqual(store.send_status(mid)["error"], "network_cancelled")
            typing.close.side_effect = close_presence

            async def stdio(server):
                self.assertTrue(await anyio.to_thread.run_sync(client.started.wait, 3))
                if protocol_failure:
                    raise RuntimeError("fixture protocol failure")
                # Returning represents stdio EOF; the runtime must cancel the
                # active network exchange and wait for its durable outcome.

            try:
                with patch("wechat_dot_bridge.sdk_adapter.run_stdio", stdio):
                    args = (store, Mock(), worker, Mock(), Mock(), Mock(), typing)
                    if protocol_failure:
                        with self.assertRaises(ExceptionGroup):
                            anyio.run(_run_runtime, *args)
                    else:
                        anyio.run(_run_runtime, *args)
                self.assertTrue(client.stopped.is_set())
                typing.close.assert_called_once()
                self.assertEqual(store.send_status(mid)["status"], "uncertain")
                self.assertEqual(store.send_status(mid)["reply_id"], queued["reply_id"])
                self.assertEqual(len(client.posts), 1)
            finally:
                store.close()
                from wechat_dot_bridge.net_transport import reset_cancellation
                reset_cancellation()
            restored = Store(state)
            try:
                restored.recover_inflight()
                self.assertEqual(restored.send_status(mid)["status"], "uncertain")
                self.assertIsNone(restored.claim_send())
                self.assertFalse(WeixinWorker(restored, client=client).send_once())
                self.assertEqual(len(client.posts), 1)
            finally:
                restored.close()

    def test_eof_waits_for_interrupted_send_before_closing_store(self):
        self.check_shutdown(False)

    def test_protocol_error_also_closes_typing_and_preserves_uncertain(self):
        self.check_shutdown(True)

    def test_eof_recovers_notification_and_download_without_resending_weixin(self):
        from wechat_dot_bridge.net_transport import cancellation_token, reset_cancellation

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            store = Store(state)
            store.import_binding({"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token",
                                  "base_url": "https://ilinkai.weixin.qq.com"})
            store.set_enabled(True)
            rows = [{"source_id": str(index), "peer": "fixture-owner", "text": "synthetic inbound",
                     "created_at": store.clock(), "context_token": "fixture-context"} for index in range(3)]
            rows[1]["attachments"] = [{"kind": "file", "name": "fixture.json", "url": "fixture-download"}]
            client = InterruptedClient()
            event_started, download_started = threading.Event(), threading.Event()

            def callback(url, body, headers):
                value = json.loads(body)
                if value.get("type") == "verification":
                    return 200, json.dumps({"challenge": value["challenge"]}).encode()
                event_started.set()
                if not cancellation_token().wait(5):
                    raise AssertionError("event did not receive runtime cancellation")
                raise BridgeError("callback_cancelled")

            def interrupted_download(descriptor):
                download_started.set()
                if not cancellation_token().wait(5):
                    raise AssertionError("download did not receive runtime cancellation")
                raise BridgeError("media_cancelled")

            events = Events(store, ["callback.example.com"], callback)
            secret = "whsec_" + base64.b64encode(b"fixture-signing-key-32-byte-data!").decode()
            events.subscribe({"name": "weixin.message", "arguments": {},
                              "delivery": {"mode": "webhook", "url": "https://callback.example.com/fixture", "secret": secret}})
            store.ingest("fixture-account", rows, "fixture-cursor")
            mids = [row["message_id"] for row in store.list_pending()]
            store.queue_reply(mids[0], "synthetic reply")
            aid = store.attachment_metadata(mids[1])[0]["attachment_id"]
            media = MediaWorker(store)

            async def stdio(server):
                for name, started in (("send", client.started), ("event", event_started), ("download", download_started)):
                    self.assertTrue(await anyio.to_thread.run_sync(started.wait, 3), name)

            try:
                with patch("wechat_dot_bridge.sdk_adapter.run_stdio", stdio), \
                        patch("wechat_dot_bridge.media.fetch", interrupted_download):
                    anyio.run(_run_runtime, store, Mock(), WeixinWorker(store, client), events,
                              media, Mock(), Mock())
                self.assertEqual(store.send_status(mids[0])["status"], "uncertain")
                self.assertEqual(store.send_status(mids[0])["error"], "network_cancelled")
                delivery = store.db.execute("SELECT status,attempts FROM deliveries WHERE message_id=?", (mids[2],)).fetchone()
                self.assertEqual(tuple(delivery), ("retry", 0))
                attachment = store.db.execute("SELECT status,attempts FROM attachments WHERE id=?", (aid,)).fetchone()
                self.assertEqual(tuple(attachment), ("pending", 0))
                self.assertEqual(len(client.posts), 1)
            finally:
                store.close()
                reset_cancellation()

            restored = Store(state)
            try:
                restored.recover_inflight()
                self.assertIsNone(restored.claim_send())
                recovered_events = Events(restored, ["callback.example.com"], lambda *args: (200, b"{}"))
                self.assertTrue(recovered_events.deliver_once())
                self.assertEqual(recovered_events.status()["deliveries"].get("acknowledged"), 1)
                with patch("wechat_dot_bridge.media.fetch", return_value=b"{}"):
                    MediaWorker(restored).tick()
                self.assertEqual(restored.read_attachment(aid)["data"], b"{}")
                self.assertTrue(recovered_events.deliver_once())
                self.assertEqual(recovered_events.status()["deliveries"].get("acknowledged"), 2)
                self.assertFalse(WeixinWorker(restored, client).send_once())
                self.assertEqual(len(client.posts), 1)
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
