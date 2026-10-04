import base64
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from wechat_dot_bridge.cli import load_config, main
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.maintenance import cleanup_history, managed_backup, storage_status
from wechat_dot_bridge.operations import diagnostics
from wechat_dot_bridge.store import Store


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.now = 10000000
        self.store = Store(self.directory / "state", clock=lambda: self.now)
        self.store.import_binding(dict(id="fixture", owner="owner", token="fixture-token",
                                       base_url="https://ilinkai.weixin.qq.com"))
        self.store.set_enabled(True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def message(self, source, accepted=False, attachment_id=None):
        message = dict(source_id=source, peer="owner", text="fixture-private-message",
                       created_at=self.now, context_token="fixture-context")
        self.store.ingest("fixture", [message], source)
        mid = self.store.db.execute("SELECT id FROM messages WHERE source_id=?", (source,)).fetchone()[0]
        if accepted:
            reply = self.store.queue_reply(mid, "fixture-reply", attachment_id=attachment_id)
            while (claimed := self.store.claim_send()) is not None:
                self.store.finish_send(claimed["message_id"], "accepted", reply_id=claimed["reply_id"])
        return mid, message

    def advance(self):
        self.now += 61 * 86400
        return self.message("latest")

    def test_preview_then_retire_and_deduplicate_after_restart(self):
        mid, original = self.message("old", accepted=True)
        self.advance()
        self.assertEqual(cleanup_history(self.store, days=30)["eligible_messages"], 1)
        self.assertTrue(self.store.read_message(mid))
        self.assertEqual(cleanup_history(self.store, days=30, apply=True)["retired_messages"], 1)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone())
        self.assertFalse(self.store.db.execute("PRAGMA foreign_key_check").fetchall())
        self.store.close()
        self.store = Store(self.directory / "state", clock=lambda: self.now)
        self.assertEqual(self.store.ingest("fixture", [original], "replayed-cursor"), 0)
        self.assertEqual(self.store.get_binding()["cursor"], "replayed-cursor")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM retired_messages").fetchone()[0], 1)

    def test_preserves_pending_all_unresolved_replies_and_progress(self):
        active, _ = self.message("active", accepted=True)
        pending, _ = self.message("pending")
        protected = [pending]
        for outcome in ("queued", "sending", "uncertain", "failed", "context_required", "auth_required"):
            mid, _ = self.message(outcome)
            reply = self.store.queue_reply(mid, "fixture-reply")
            self.store.db.execute("UPDATE replies SET status=? WHERE client_id=?", (outcome, reply["reply_id"]))
            protected.append(mid)
        self.advance()
        self.store.queue_reply(active, 'fixture-progress', final=False)
        protected.append(active)
        cleanup_history(self.store, days=30, apply=True)
        self.assertTrue(all(self.store.db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone()
                            for mid in protected))
        self.assertEqual(self.store.db.execute("SELECT status FROM replies WHERE message_id=(SELECT id FROM messages WHERE source_id='uncertain')").fetchone()[0], "uncertain")

    def test_preserves_latest_context_and_subscription_rowid_watermark(self):
        mid, _ = self.message("only", accepted=True)
        self.now += 61 * 86400
        cleanup_history(self.store, days=30, apply=True)
        self.assertTrue(self.store.read_message(mid))
        # A newer message without a context must not release the last usable context.
        self.store.ingest("fixture", [dict(source_id="new", peer="owner", text="fixture",
                         created_at=self.now)], "new")
        cleanup_history(self.store, days=30, apply=True)
        self.assertTrue(self.store.read_message(mid))

    def test_recent_reply_to_old_message_is_not_retired(self):
        mid, _ = self.message("old")
        self.advance()
        reply = self.store.queue_reply(mid, "recent reply")
        claimed = self.store.claim_send()
        self.store.finish_send(mid, "accepted", reply_id=claimed["reply_id"])
        self.assertEqual(reply["reply_id"], claimed["reply_id"])
        self.assertEqual(cleanup_history(self.store, days=30, apply=True)["retired_messages"], 0)

    def test_preserves_event_delivery_in_flight(self):
        mid, _ = self.message("old", accepted=True)
        Events(self.store)
        self.store.db.execute("INSERT INTO deliveries(event_id,subscription_id,message_id,body,status,next_attempt) VALUES('fixture-event','fixture-sub',?,?,'sending',0)", (mid, b"fixture"))
        self.advance()
        self.assertEqual(cleanup_history(self.store, days=30, apply=True)["retired_messages"], 0)
        self.store.db.execute("UPDATE deliveries SET status='acknowledged'")
        self.assertEqual(cleanup_history(self.store, days=30, apply=True)["retired_messages"], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_shared_attachment_preserves_owner_until_all_users_complete(self):
        owner, _ = self.message("owner", accepted=True)
        staged = self.store.stage_attachment(base64.b64encode(b"fixture-data").decode(), "file", "fixture.txt")
        aid = staged["attachment_id"]
        self.store.db.execute("UPDATE attachments SET message_id=? WHERE id=?", (owner, aid))
        user, _ = self.message("user")
        self.store.db.execute("INSERT INTO attachment_links VALUES(?,?)", (user, aid))
        self.advance()
        self.assertEqual(cleanup_history(self.store, days=30, apply=True)["retired_messages"], 0)
        self.store.db.execute("UPDATE messages SET state='reply_committed' WHERE id=?", (user,))
        self.store.db.execute("INSERT INTO replies(message_id,text,client_id,status,created_at) VALUES(?,'fixture','fixture-reply-id','accepted',?)", (user, self.now - 61 * 86400))
        self.store.db.execute("UPDATE message_work SET state='completed' WHERE message_id=?", (user,))
        self.assertEqual(cleanup_history(self.store, days=30, apply=True)["retired_messages"], 1)
        self.assertEqual(cleanup_history(self.store, days=30, apply=True)["retired_messages"], 1)
        self.assertFalse(self.store.db.execute("PRAGMA foreign_key_check").fetchall())

    def test_protected_shared_owner_cannot_starve_an_independent_batch(self):
        owner, _ = self.message("owner", accepted=True)
        aid = self.store.stage_attachment(base64.b64encode(b"fixture").decode(), "file", "fixture.txt")["attachment_id"]
        self.store.db.execute("UPDATE attachments SET message_id=? WHERE id=?", (owner, aid))
        independent, _ = self.message("independent", accepted=True)
        user, _ = self.message("user")
        self.store.db.execute("INSERT INTO attachment_links VALUES(?,?)", (user, aid))
        self.advance()
        with patch("wechat_dot_bridge.maintenance.HISTORY_BATCH", 1):
            self.assertEqual(cleanup_history(self.store, days=30, apply=True)["retired_messages"], 1)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM messages WHERE id=?", (independent,)).fetchone())
        self.assertTrue(self.store.read_message(owner))

    def test_cleanup_failure_rolls_back_records_and_tombstones(self):
        mid, _ = self.message("old", accepted=True)
        self.advance()
        self.store.db.execute("CREATE TRIGGER interrupt_retirement BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaisesRegex(BridgeError, "database_unavailable"):
            cleanup_history(self.store, days=30, apply=True)
        self.assertTrue(self.store.read_message(mid))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM retired_messages").fetchone()[0], 0)
        self.assertEqual(self.store.send_status(mid)["status"], "accepted")

    def test_default_disabled_and_policy_validation(self):
        self.message("old", accepted=True)
        self.advance()
        self.assertEqual(cleanup_history(self.store, apply=True)["retired_messages"], 0)
        from argparse import Namespace
        path = self.directory / "config.json"
        for policy in ({"history_days": True}, {"history_days": -1}, {"unknown": 1}, None):
            path.write_text(json.dumps({"maintenance_policy": policy}))
            with self.assertRaisesRegex(BridgeError, "config_invalid"):
                load_config(Namespace(config=str(path), state_dir=str(self.directory / "state")))
        path.write_text(json.dumps({"maintenance_policy": {"history_days": 30}}))
        args = Namespace(config=str(path), state_dir=str(self.directory / "state"))
        load_config(args)
        self.assertEqual(args.maintenance_policy["history_days"], 30)

    def test_storage_warnings_are_local_redacted_and_visible_in_diagnostics(self):
        self.message("fixture")
        with patch("wechat_dot_bridge.maintenance.shutil.disk_usage", return_value=shutil._ntuple_diskusage(1000, 999, 1)):
            result = storage_status(self.store)
            self.assertIn("disk_space_low", result["warnings"])
            self.assertIn("disk_space_low", [issue["code"] for issue in diagnostics(self.store)["issues"]])
        self.assertNotIn("fixture-private", json.dumps(result))
        self.assertNotIn(str(self.directory), json.dumps(result))

    def test_managed_backup_retains_new_snapshot_and_preserves_manual_files(self):
        directory = self.directory / "backups"
        for _ in range(4):
            result = managed_backup(self.store, directory, keep=2)
        manual = directory / "before-upgrade.sqlite3"
        manual.write_bytes(b"fixture-manual-backup")
        manual.chmod(0o600)
        result = managed_backup(self.store, directory, keep=2)
        self.assertEqual(result["retained"], 2)
        self.assertEqual(result["removed"], 1)
        self.assertTrue(manual.exists())
        for path in directory.glob("bridge-backup-*.sqlite3"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failed_backup_cannot_rotate_and_symlink_is_rejected(self):
        directory = self.directory / "backups"
        managed_backup(self.store, directory, keep=1)
        existing = list(directory.iterdir())
        with patch("wechat_dot_bridge.operations.backup", side_effect=BridgeError("backup_invalid")):
            with self.assertRaisesRegex(BridgeError, "backup_invalid"):
                managed_backup(self.store, directory, keep=1)
        self.assertEqual(list(directory.iterdir()), existing)
        symlink = directory / "bridge-backup-symlink.sqlite3"
        symlink.symlink_to(existing[0])
        with self.assertRaisesRegex(BridgeError, "unsafe_backup"):
            managed_backup(self.store, directory, keep=1)

    def test_partial_backup_failure_does_not_replace_a_verified_snapshot(self):
        directory = self.directory / "backups"
        managed_backup(self.store, directory, keep=2)
        original = list(directory.iterdir())[0]
        with patch("wechat_dot_bridge.operations.sqlite3.connect", side_effect=sqlite3.Error("fixture")):
            with self.assertRaises(sqlite3.Error):
                managed_backup(self.store, directory, keep=2)
        self.assertEqual(list(directory.iterdir()), [original])
        result = managed_backup(self.store, directory, keep=2)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(original.exists())

    def test_cleanup_preserves_subscription_watermark_for_next_event(self):
        self.message("retire", accepted=True)
        mid, _ = self.message("highest", accepted=True)
        # Only the older record has a context; the highest rowid has none.
        self.store.db.execute("UPDATE messages SET context_token=NULL WHERE id=?", (mid,))
        events = Events(self.store, ["callback.example.com"])
        params = {"name": "weixin.message", "arguments": {}, "delivery": {"mode": "webhook", "url": "https://callback.example.com/fixture"}}
        sid, principal, url, _ = events._identity(params, False)
        watermark = self.store.db.execute("SELECT max(rowid) FROM messages").fetchone()[0]
        secret = "whsec_" + base64.b64encode(b"0" * 32).decode()
        self.store.db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?,?,NULL,NULL,0,0,'active',?)", (sid, principal, "weixin.message", "{}", url, secret, watermark))
        self.now += 61 * 86400
        cleanup_history(self.store, days=30, apply=True)
        self.assertTrue(self.store.read_message(mid))
        self.message("new")
        events.post = lambda *args: (200, b"")
        self.assertTrue(events.deliver_once())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM deliveries WHERE status='acknowledged'").fetchone()[0], 1)

    def test_cli_cleanup_preview_is_read_only(self):
        from contextlib import redirect_stdout
        import io
        self.message("old", accepted=True)
        self.advance()
        # Exercise argument dispatch. The CLI uses a real clock, making this data old.
        output = io.StringIO()
        initial_umask = os.umask(0o077)
        try:
            with redirect_stdout(output):
                self.assertEqual(main(["--state-dir", str(self.directory / "state"), "cleanup-history", "--days", "30"]), 0)
        finally:
            os.umask(initial_umask)
        self.assertFalse(json.loads(output.getvalue())["applied"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
