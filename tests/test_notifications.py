"""Local HTTP boundary and durable notification outcomes with synthetic inputs."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.notification_api import serve_notifications
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import WeixinWorker
from test_store import ACCOUNT, binding, message


class NotificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = 1000
        self.store = Store(Path(directory.name) / "state", clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.store.import_binding(binding())
        self.store.set_enabled(True)

    def test_replay_conflict_expiry_and_recovery(self):
        submit = self.store.queue_notification
        with self.assertRaisesRegex(BridgeError, "^fresh_context_required$"):
            submit("job", "text")
        self.store.ingest(ACCOUNT, [message()], "cursor", context_only=True)
        self.assertEqual(self.store.list_pending(), [])
        with ThreadPoolExecutor(4) as pool:
            records = list(pool.map(lambda _: submit("event", "x" * 4000, 1010), range(8)))
        self.assertEqual(len({r["id"] for r in records}), 1)
        with self.assertRaisesRegex(BridgeError, "^key_conflict$"):
            submit("event", "changed", 1010)
        identifier = records[0]["id"]
        client = Mock()
        client.send.return_value = {}
        worker = WeixinWorker(self.store, client)
        self.assertTrue(worker.send_once())
        second_part = self.store.claim_send()
        self.assertEqual(second_part["reply_id"], identifier)
        self.assertNotEqual(client.send.call_args.args[1]["client_id"], second_part["client_id"])
        self.store.recover_inflight()
        self.assertEqual(submit("event", "x" * 4000, 1010)["status"], "uncertain")
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.resolve_uncertain(identifier, True)["status"], "accepted")
        expires = submit("other-event", "another", 1010)
        self.now = 1010
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.notification_status(expires["id"])["status"], "expired")

    def test_http_socket_to_worker_and_status(self):
        self.store.ingest(ACCOUNT, [message()], "cursor")
        path = self.store.directory / "notify.sock"
        def request(method, route, body=None):
            connection = http.client.HTTPConnection("localhost", timeout=3)
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(3)
            try:
                connection.sock.connect(str(path))
                connection.request(method, route, json.dumps(body) if body else None, {
                    "Content-Type": "application/json", "Idempotency-Key": "http-job"})
                response = connection.getresponse()
                return response.status, json.load(response)
            finally:
                connection.close()

        async def exercise():
            wake = Mock()
            task = asyncio.create_task(serve_notifications(self.store, wake))
            try:
                for _ in range(150):
                    try:
                        status, _ = await asyncio.to_thread(request, "GET", "/v1/notifications/missing")
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        await asyncio.sleep(0.01)
                else:
                    self.fail("notification listener unavailable")
                self.assertEqual(status, 404)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                route = "/v1/notifications"
                self.assertEqual((await asyncio.to_thread(request, "POST", route,
                                 {"text": "synthetic", "peer": "other"}))[0], 400)
                status, record = await asyncio.to_thread(request, "POST", route, {"text": "synthetic"})
                self.assertEqual(status, 202)
                wake.assert_called_once()
                client = Mock()
                client.send.return_value = {}
                self.assertTrue(WeixinWorker(self.store, client).send_once())
                self.assertEqual(client.send.call_args.args[1]["text"], "synthetic")
                status, result = await asyncio.to_thread(request, "GET", route + "/" + record["id"])
                self.assertEqual((status, result["status"]), (200, "accepted"))
                self.assertEqual(set(result), {"id", "status", "error"})
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(path.exists())
        asyncio.run(exercise())
