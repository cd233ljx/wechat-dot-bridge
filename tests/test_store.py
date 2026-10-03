"""Offline persistence tests using synthetic accounts and temporary files."""

import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.store import MAX_PENDING, MAX_QUEUED_REPLIES, Store


ACCOUNT = "synthetic-bot"
OWNER = "synthetic-owner"
TOKEN = "fixture-not-a-real-token"


def binding(**changes):
    value = {"id": ACCOUNT, "owner": OWNER, "token": TOKEN,
             "base_url": "https://ilinkai.weixin.qq.com", "cursor": "initial-fixture-cursor"}
    value.update(changes)
    return value


def message(source="source-1", text="原文\n第二行", created_at=100, context_token="fixture-context", **changes):
    value = {"source_id": source, "peer": OWNER, "text": text,
             "created_at": created_at, "context_token": context_token}
    value.update(changes)
    return value


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "private-state"
        self.store = Store(self.directory, clock=lambda: 200)
        self.store.import_binding(binding())
        self.store.set_enabled(True)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def expect_error(self, code, operation, *args, **kwargs):
        with self.assertRaises(BridgeError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def add(self, value=None):
        self.store.ingest(ACCOUNT, [value or message()], "next-fixture-cursor")
        source = (value or message())["source_id"]
        with self.store.lock:
            return self.store.db.execute("SELECT id FROM messages WHERE source_id=?", (source,)).fetchone()[0]

    def reopen(self):
        self.store.close()
        self.store = Store(self.directory, clock=lambda: 200)

    def test_private_files_and_import_starts_disabled(self):
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.directory / "bridge.sqlite3").stat().st_mode), 0o600)
        for suffix in ("-wal", "-shm"):
            path = self.directory / ("bridge.sqlite3" + suffix)
            if path.exists():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        other = Store(Path(self.temporary.name) / "new-state")
        try:
            result = other.import_binding(binding())
            self.assertFalse(result["enabled"])
            self.assertIsNone(other.claim_send())
            self.expect_error("account_disabled", other.ingest, ACCOUNT, [message()], "ignored")
            self.assertEqual(other.get_binding()["cursor"], "initial-fixture-cursor")
        finally:
            other.close()

    def test_inbound_is_lossless_and_ids_persist_across_restart(self):
        identifier = self.add()
        self.assertEqual(self.store.read_message(identifier)["text"], "原文\n第二行")
        self.assertEqual(self.store.get_binding()["cursor"], "next-fixture-cursor")
        self.reopen()
        self.assertEqual(self.store.list_pending()[0]["message_id"], identifier)
        self.assertEqual(self.store.ingest(ACCOUNT, [message()], "duplicate-cursor"), 0)
        self.assertEqual(self.store.get_binding()["cursor"], "duplicate-cursor")
        self.assertEqual(len(self.store.list_pending()), 1)

    def test_batch_and_cursor_rollback_together_on_bad_owner(self):
        self.expect_error("owner_identity_mismatch", self.store.ingest, ACCOUNT,
                          [message("good"), message("wrong", peer="someone-else")], "must-not-advance")
        self.assertEqual(self.store.list_pending(), [])
        self.assertEqual(self.store.get_binding()["cursor"], "initial-fixture-cursor")

    def test_database_write_failure_rolls_back_inbox_context_and_cursor(self):
        with self.store.lock:
            self.store.db.execute(
                "CREATE TRIGGER fixture_failure BEFORE UPDATE OF cursor ON binding "
                "WHEN NEW.cursor='fail-fixture' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        self.expect_error("database_unavailable", self.store.ingest, ACCOUNT, [message()], "fail-fixture")
        self.assertEqual(self.store.list_pending(), [])
        self.assertEqual(self.store.get_binding()["cursor"], "initial-fixture-cursor")
        with self.store.lock:
            self.store.db.execute("DROP TRIGGER fixture_failure")
        self.assertEqual(self.store.ingest(ACCOUNT, [message()], "retry-fixture"), 1)

    def test_account_mismatch_and_invalid_input_do_not_advance_cursor(self):
        self.expect_error("account_identity_mismatch", self.store.ingest, "other-account", [message()], "ignored")
        for malformed in (message(text=""), message(text="\ud800"),
                          message(context_token=""), message(created_at=float("nan")),
                          message(created_at=True), message(source_id="\ud800"), message(text="a" * 65537)):
            with self.subTest(malformed_field=list(malformed)):
                self.expect_error("invalid_inbound_message", self.store.ingest, ACCOUNT, [malformed], "ignored")
        self.assertEqual(self.store.list_pending(), [])
        self.assertEqual(self.store.get_binding()["cursor"], "initial-fixture-cursor")

    def test_pending_capacity_rolls_back_whole_batch_and_duplicates_are_free(self):
        batch = [message(str(number)) for number in range(MAX_PENDING + 1)]
        self.expect_error("inbox_full", self.store.ingest, ACCOUNT, batch, "oversized-batch-cursor")
        self.assertEqual(self.store.get_status()["pending"], 0)
        self.assertEqual(self.store.get_binding()["cursor"], "initial-fixture-cursor")
        self.assertEqual(self.store.ingest(ACCOUNT, batch[:-1], "at-capacity"), MAX_PENDING)
        self.assertEqual(self.store.ingest(ACCOUNT, batch[:-1], "duplicate-at-capacity"), 0)
        self.expect_error("inbox_full", self.store.ingest, ACCOUNT, [batch[-1]], "over-capacity")
        self.assertEqual(self.store.get_binding()["cursor"], "duplicate-at-capacity")
        identifier = self.store.list_pending()[0]["message_id"]
        self.store.queue_reply(identifier, "回答")
        self.assertEqual(self.store.ingest(ACCOUNT, [batch[-1]], "freed-slot"), 1)

    def test_reply_is_immutable_idempotent_and_client_id_is_stable(self):
        identifier = self.add()
        first = self.store.queue_reply(identifier, "准确回复", "answer")
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first, self.store.queue_reply(identifier, "准确回复", "answer"))
        self.expect_error("reply_key_conflict", self.store.queue_reply, identifier, "另一个答案", "answer")
        self.assertEqual(self.store.list_pending(), [])
        self.reopen()
        outgoing = self.store.claim_send()
        self.assertEqual(outgoing["message_id"], identifier)
        self.assertEqual(outgoing["text"], "准确回复")
        self.assertEqual(outgoing["peer"], OWNER)
        self.assertIsNone(self.store.claim_send())
        self.store.finish_send(identifier, "context_required", "context_required")
        self.add(message("fresh", created_at=101, context_token="new-context"))
        second = self.store.claim_send()
        self.assertEqual(second["client_id"], outgoing["client_id"])
        self.assertEqual(second["context_token"], "new-context")

    def test_progress_and_completion_are_independent_replies_across_restart(self):
        identifier = self.add()
        progress = self.store.queue_reply(identifier, "正在处理")
        outgoing = self.store.claim_send()
        self.store.finish_send(identifier, "accepted", reply_id=outgoing["reply_id"])
        self.reopen()
        completion = self.store.queue_reply(identifier, "处理完成")
        self.assertNotEqual(progress["reply_id"], completion["reply_id"])
        final = self.store.claim_send()
        self.store.finish_send(identifier, "accepted", reply_id=final["reply_id"])
        self.assertEqual(self.store.queue_reply(identifier, "正在处理")["status"], "accepted")
        self.assertEqual(self.store.queue_reply(identifier, "处理完成")["status"], "accepted")
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.get_status()["reply_status"], {"accepted": 2})

    def test_uncertain_progress_does_not_block_completion_or_change_its_status(self):
        identifier = self.add()
        first = self.store.queue_reply(identifier, "进度", "progress")
        self.store.claim_send()
        self.store.finish_send(identifier, "uncertain", "network_timeout", reply_id=first["reply_id"])
        second = self.store.queue_reply(identifier, "完成", "done")
        self.store.claim_send()
        self.store.finish_send(identifier, "accepted", reply_id=second["reply_id"])
        self.assertEqual(self.store.send_status(identifier, first["reply_id"])["status"], "uncertain")
        self.assertEqual(self.store.send_status(identifier, second["reply_id"])["status"], "accepted")
        self.assertEqual(self.store.queue_reply(identifier, "进度", "progress")["status"], "uncertain")
        self.assertIsNone(self.store.claim_send())

    def test_explicit_keys_allow_intentional_identical_replies(self):
        identifier = self.add()
        first = self.store.queue_reply(identifier, "已完成", "part1")
        second = self.store.queue_reply(identifier, "已完成", "part2")
        self.assertNotEqual(first["reply_id"], second["reply_id"])
        self.assertEqual(self.store.queue_reply(identifier, "已完成", "part1"), first)

    def test_migration_preserves_legacy_accepted_reply_and_allows_append(self):
        identifier = self.add()
        self.store.db.execute("DROP TABLE replies")
        self.store.db.execute("""CREATE TABLE replies(message_id TEXT PRIMARY KEY REFERENCES messages(id),
            text TEXT NOT NULL,client_id TEXT UNIQUE NOT NULL,status TEXT NOT NULL,error TEXT,
            created_at REAL NOT NULL,context_id TEXT,attempts INTEGER NOT NULL DEFAULT 0)""")
        self.store.db.execute("INSERT INTO replies(message_id,text,client_id,status,created_at) VALUES(?,?,?,'accepted',?)",
                              (identifier, "已发送的旧回复", "legacy-id", 100))
        self.reopen()
        self.assertEqual(self.store.queue_reply(identifier, "已发送的旧回复")["reply_id"], "legacy-id")
        self.store.queue_reply(identifier, "追加完成消息")
        self.assertEqual(self.store.claim_send()["text"], "追加完成消息")
        self.assertEqual(self.store.send_status(identifier, "legacy-id")["status"], "accepted")

    def test_latest_owner_context_used_and_older_messages_cannot_replace_it(self):
        identifier = self.add(message("first", created_at=100, context_token="first-context"))
        self.add(message("latest", created_at=110, context_token="latest-context"))
        self.add(message("late-arriving-old", created_at=90, context_token="old-context"))
        self.store.queue_reply(identifier, "回答")
        self.reopen()
        self.assertEqual(self.store.claim_send()["context_token"], "latest-context")

    def test_missing_context_waits_and_fresh_context_resumes(self):
        identifier = self.add(message(context_token=None))
        self.store.queue_reply(identifier, "回答")
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.send_status(identifier)["status"], "context_required")
        self.assertIsNone(self.store.claim_send())
        self.add(message("fresh", created_at=101, context_token="new-context"))
        self.assertEqual(self.store.claim_send()["message_id"], identifier)

    def test_context_rejection_is_not_retried_until_new_context(self):
        identifier = self.add()
        self.store.queue_reply(identifier, "回答")
        self.store.claim_send()
        self.store.finish_send(identifier, "context_required", "context_required")
        self.assertIsNone(self.store.claim_send())
        self.store.ingest(ACCOUNT, [message()], "repeated-inbound")
        self.assertIsNone(self.store.claim_send())
        self.add(message("older", created_at=90, context_token="older-context"))
        self.assertIsNone(self.store.claim_send())
        self.add(message("fresh", created_at=101, context_token="new-context"))
        self.assertIsNotNone(self.store.claim_send())

    def test_context_received_during_post_can_resume_safely_rejected_send(self):
        identifier = self.add()
        self.store.queue_reply(identifier, "回答")
        self.store.claim_send()
        self.add(message("fresh", created_at=101, context_token="new-context"))
        self.store.finish_send(identifier, "context_required", "context_required")
        self.assertEqual(self.store.send_status(identifier)["status"], "queued")
        self.assertEqual(self.store.claim_send()["context_token"], "new-context")

    def test_context_retries_are_bounded(self):
        identifier = self.add()
        self.store.queue_reply(identifier, "回答")
        for attempt in range(3):
            self.assertIsNotNone(self.store.claim_send())
            self.store.finish_send(identifier, "context_required", "context_required")
            self.add(message("fresh-" + str(attempt), created_at=101 + attempt,
                             context_token="new-context-" + str(attempt)))
        self.assertEqual(self.store.send_status(identifier)["status"], "failed")
        self.assertEqual(self.store.send_status(identifier)["error"], "retry_limit")
        self.assertIsNone(self.store.claim_send())

    def test_sending_restart_becomes_uncertain_and_never_resends(self):
        identifier = self.add()
        self.store.queue_reply(identifier, "回答")
        original = self.store.claim_send()
        self.reopen()
        self.assertEqual(self.store.send_status(identifier)["status"], "sending")
        self.store.recover_inflight()
        self.assertEqual(self.store.send_status(identifier)["status"], "uncertain")
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.queue_reply(identifier, "回答")["status"], "uncertain")
        self.store.import_binding(binding(token="replacement-fixture-token"))
        self.assertIsNone(self.store.claim_send())
        self.assertTrue(original["client_id"])

    def test_timeout_uncertain_and_accepted_never_claim_again(self):
        for source, outcome in (("uncertain", "uncertain"), ("accepted", "accepted")):
            identifier = self.add(message(source))
            self.store.queue_reply(identifier, "回答")
            self.store.claim_send()
            error = "network_timeout" if outcome == "uncertain" else None
            self.store.finish_send(identifier, outcome, error)
            self.store.finish_send(identifier, outcome, error)
            self.assertEqual(self.store.send_status(identifier)["status"], outcome)
            self.assertIsNone(self.store.send_status(identifier)["delivered"])
            self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.get_status()["reply_status"], {"accepted": 1, "uncertain": 1})

    def test_binding_identity_is_immutable_and_refresh_preserves_cursor(self):
        identifier = self.add()
        self.store.queue_reply(identifier, "回答")
        self.store.claim_send()
        self.store.finish_send(identifier, "auth_required", "auth_required")
        self.store.set_enabled(False)
        self.expect_error("binding_identity_changed", self.store.import_binding, binding(owner="other-owner"))
        self.expect_error("binding_identity_changed", self.store.import_binding, binding(id="other-bot"))
        replacement = binding(token="replacement-fixture-token")
        del replacement["cursor"]
        self.store.import_binding(replacement)
        self.assertEqual(self.store.get_binding()["cursor"], "next-fixture-cursor")
        self.assertFalse(self.store.get_binding()["enabled"])
        self.assertEqual(self.store.send_status(identifier)["status"], "queued")
        self.assertIsNone(self.store.claim_send())
        self.store.set_enabled(True)
        self.assertIsNotNone(self.store.claim_send())

    def test_only_owner_scoped_records_are_read_or_claimed(self):
        identifier = self.add()
        with self.store.lock:
            self.store.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                                  ("foreign-id", "other-bot", "other-owner", "foreign-source",
                                   "private-foreign-text", 1, "foreign-context", "pending"))
            self.store.db.execute("INSERT INTO replies(message_id,text,client_id,status,created_at) VALUES(?,?,?,?,?)",
                                  ("foreign-id", "private-foreign-reply", "foreign-client", "queued", 1))
        self.expect_error("unknown_owner_message", self.store.read_message, "foreign-id")
        self.expect_error("unknown_owner_message", self.store.send_status, "foreign-id")
        self.expect_error("unknown_owner_message", self.store.queue_reply, "foreign-id", "回答")
        self.assertEqual([row["message_id"] for row in self.store.list_pending()], [identifier])
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.get_status()["pending"], 1)
        self.assertEqual(self.store.get_status()["reply_status"], {})

    def test_reply_capacity_and_utf8_byte_limit(self):
        values = [message(str(index)) for index in range(MAX_QUEUED_REPLIES + 1)]
        self.store.ingest(ACCOUNT, values, "synthetic-cursor")
        identifiers = [record["message_id"] for record in self.store.list_pending(100)]
        for identifier in identifiers[:-1]:
            self.store.queue_reply(identifier, "回答")
        self.expect_error("send_queue_full", self.store.queue_reply, identifiers[-1], "回答")
        # Retrying an already committed message is valid even at capacity.
        self.assertEqual(self.store.queue_reply(identifiers[0], "回答")["status"], "queued")
        for text in ("", " ", "\ud800", "中" * 90000):
            self.expect_error("invalid_reply_text", self.store.queue_reply, identifiers[-1], text)

    def test_maximum_utf8_reply_is_preserved_without_chunking(self):
        identifier = self.add()
        text = "中" * 2730 + "ab"
        self.assertEqual(len(text.encode("utf-8")), 8192)
        self.store.queue_reply(identifier, text)
        self.assertEqual(self.store.claim_send()["text"], text)

    def test_disabled_store_reads_existing_records_but_will_not_consume_or_send(self):
        identifier = self.add()
        self.store.queue_reply(identifier, "回答")
        self.store.set_enabled(False)
        self.assertEqual(self.store.read_message(identifier)["text"], message()["text"])
        self.assertIsNone(self.store.claim_send())
        self.expect_error("account_disabled", self.store.ingest, ACCOUNT, [message("new")], "ignored")
        self.assertEqual(self.store.queue_reply(identifier, "回答")["status"], "queued")
        self.store.set_enabled(True)
        self.assertIsNotNone(self.store.claim_send())

    def test_public_status_and_error_labels_never_expose_routing_or_text(self):
        identifier = self.add()
        self.store.queue_reply(identifier, "私密回答")
        self.store.claim_send()
        self.store.finish_send(identifier, "uncertain", "this-is-an-external-secret")
        self.store.record_error("external-token-should-not-be-stored")
        public = json.dumps([self.store.get_status(), self.store.send_status(identifier), self.store.list_pending()], ensure_ascii=False)
        for private in (TOKEN, OWNER, ACCOUNT, "fixture-context", "私密回答", "原文", "external-token", "external-secret"):
            self.assertNotIn(private, public)
        self.assertEqual(self.store.send_status(identifier)["error"], "operation_failed")
        self.assertEqual(self.store.get_binding()["last_error"], "operation_failed")

    def test_error_status_and_auth_requires_explicit_import_before_enable(self):
        self.store.record_error("network_timeout")
        self.assertEqual(self.store.get_status()["account_status"], "ready")
        self.store.record_error("auth_required")
        self.store.set_enabled(False)
        self.expect_error("account_not_ready", self.store.set_enabled, True)
        self.store.import_binding(binding())
        self.store.set_enabled(True)
        self.store.record_error(None)
        self.assertNotIn("last_error", self.store.get_status())

    def test_multiple_ingest_threads_do_not_overlap_transactions(self):
        errors = []
        def ingest(prefix):
            try:
                for index in range(20):
                    self.store.ingest(ACCOUNT, [message(prefix + str(index))], prefix + "cursor")
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=ingest, args=(prefix,)) for prefix in ("a-", "b-")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(self.store.get_status()["pending"], 40)

    def test_validation_does_not_accept_bool_limits_or_insecure_binding(self):
        for limit in (0, 101, True, 1.5):
            self.expect_error("invalid_limit", self.store.list_pending, limit)
        for url in ("http://ilinkai.weixin.qq.com", "https://ilinkai.weixin.qq.com.evil.example",
                    "https://user:secret@ilinkai.weixin.qq.com", "https://127.0.0.1",
                    "https://ilinkai.weixin.qq.com/private"):
            self.expect_error("invalid_binding", self.store.import_binding, binding(base_url=url))
        self.expect_error("invalid_enabled", self.store.set_enabled, 1)
        self.expect_error("invalid_binding", self.store.import_binding, binding(cursor="\ud800"))

    def test_foreign_directory_owner_or_public_permissions_are_rejected(self):
        public = Path(self.temporary.name) / "public-state"
        public.mkdir(mode=0o755)
        self.expect_error("unsafe_state_directory", Store, public)
        with patch("wechat_dot_bridge.store.os.geteuid", return_value=os.geteuid() + 1):
            self.expect_error("unsafe_state_directory", Store, self.directory)

    def test_symlink_directory_and_database_never_modify_target(self):
        alias = Path(self.temporary.name) / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        self.expect_error("unsafe_state_directory", Store, alias)
        self.expect_error("unsafe_state_directory", Store, alias / "sub-state")
        unsafe = Path(self.temporary.name) / "symlink-db-state"
        unsafe.mkdir(mode=0o700)
        target = Path(self.temporary.name) / "sentinel.txt"
        target.write_text("do-not-change", encoding="utf-8")
        (unsafe / "bridge.sqlite3").symlink_to(target)
        self.expect_error("unsafe_database_file", Store, unsafe)
        self.assertEqual(target.read_text(encoding="utf-8"), "do-not-change")

    def test_database_world_access_or_symlink_journal_is_rejected(self):
        unsafe = Path(self.temporary.name) / "unsafe-db-state"
        unsafe.mkdir(mode=0o700)
        database = unsafe / "bridge.sqlite3"
        connection = sqlite3.connect(database)
        connection.close()
        database.chmod(0o644)
        self.expect_error("unsafe_database_file", Store, unsafe)
        database.chmod(0o600)
        (unsafe / "bridge.sqlite3-wal").symlink_to(Path(self.temporary.name) / "not-present")
        self.expect_error("unsafe_database_file", Store, unsafe)


if __name__ == "__main__":
    unittest.main()
