"""Version-7 callback fixtures and transaction boundaries, never live state."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from wechat_dot_bridge import migrations
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.operations import backup, restore
from wechat_dot_bridge.store import Store


# Frozen pre-versioning event schema. Partial old startup could leave any
# prefix of these tables even after Store had committed schema version 7.
LEGACY_EVENT_SCHEMA = {
    "subscriptions": """CREATE TABLE subscriptions (
        id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL,
        arguments TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
        old_secret TEXT, rotate_until REAL, expires_at REAL NOT NULL,
        verified_until REAL NOT NULL, status TEXT NOT NULL, start_rowid INTEGER NOT NULL)""",
    "deliveries": """CREATE TABLE deliveries (
        event_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL,
        message_id TEXT NOT NULL, body BLOB NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
        error TEXT, UNIQUE(subscription_id,message_id))""",
    "event_metadata": "CREATE TABLE event_metadata (key TEXT PRIMARY KEY,value TEXT)",
}


class EventMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def legacy(self, name="state", event_tables=3):
        state = self.root / name
        state.mkdir(mode=0o700)
        path = state / "bridge.sqlite3"
        path.touch(mode=0o600)
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for upgrade in migrations.MIGRATIONS[:7]:
                upgrade(db, lambda: 100)
            db.execute("PRAGMA user_version=7")
            db.execute("COMMIT")
            for table in tuple(LEGACY_EVENT_SCHEMA)[:event_tables]:
                db.execute(LEGACY_EVENT_SCHEMA[table])
            db.execute("""INSERT INTO binding(id,owner,token,base_url,enabled)
                VALUES('fixture-account','fixture-owner','fixture-token',
                       'https://ilinkai.weixin.qq.com',1)""")
            for index, outcome in enumerate(("acknowledged", "retry", "sending")):
                mid = "fixture-message-" + str(index)
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                           (mid, "fixture-account", "fixture-owner", str(index),
                            "synthetic inbound", 101, "synthetic-context", "pending"))
                db.execute("""INSERT INTO message_work(message_id,run_id,state,updated_at)
                    VALUES(?,?,'waiting',101)""", (mid, "fixture-work-" + str(index)))
                if event_tables >= 2:
                    body = json.dumps({"id": "fixture-event-" + str(index),
                                       "data": {"message_id": mid}},
                                      separators=(",", ":")).encode()
                    db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?)",
                               ("fixture-event-" + str(index), "fixture-subscription",
                                mid, body, outcome, 2, 105,
                                "http_error" if outcome == "retry" else None))
            db.execute("""INSERT INTO replies(message_id,text,client_id,status,error,
                created_at,attempts) VALUES('fixture-message-2','synthetic reply',
                'fixture-reply','uncertain','network_timeout',102,1)""")
            db.execute("INSERT INTO reply_parts VALUES('fixture-reply',0,'accepted segment',1,'text')")
            db.execute("INSERT INTO reply_parts VALUES('fixture-reply',1,'uncertain segment',0,'text')")
            if event_tables >= 1:
                principal = json.dumps(["fixture-account", "fixture-owner"], separators=(",", ":"))
                db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("fixture-subscription", principal, "weixin.message", "{}",
                            "https://callback.example.com/synthetic", "synthetic-secret",
                            "synthetic-old-secret", 150, 0, 130, "active", 0))
            if event_tables == 3:
                db.execute("INSERT INTO event_metadata VALUES('nonexpiring_default:fixture-subscription','1')")
        return state

    def event_data(self, db):
        return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in LEGACY_EVENT_SCHEMA}

    def test_store_adopts_every_legacy_initialization_prefix_before_events(self):
        for count in range(4):
            with self.subTest(tables=count):
                state = self.legacy("state-" + str(count), count)
                with closing(sqlite3.connect(state / "bridge.sqlite3")) as db:
                    previous = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
                                for table in tuple(LEGACY_EVENT_SCHEMA)[:count]}
                with closing(Store(state)) as store:
                    self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], migrations.SCHEMA_VERSION)
                    data = self.event_data(store.db)
                    for table, rows in previous.items():
                        self.assertEqual(data[table], rows)
                    self.assertEqual(store.send_status("fixture-message-2")["status"], "uncertain")
                    self.assertEqual(store.send_status("fixture-message-2")["parts_accepted"], 1)
                    self.assertIsNone(store.claim_send())
                    self.assertFalse(store.db.execute("PRAGMA foreign_key_check").fetchall())

    def test_event_upgrade_failure_rolls_back_version_and_new_tables(self):
        state = self.legacy(event_tables=1)
        original = migrations.MIGRATIONS[7]

        def interrupted(db, clock):
            original(db, clock)
            raise sqlite3.OperationalError("synthetic migration interruption")

        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:7], interrupted)):
            with self.assertRaisesRegex(BridgeError, "database_unavailable"):
                Store(state)
        with closing(sqlite3.connect(state / "bridge.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='deliveries'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='event_metadata'").fetchone())
            self.assertEqual(db.execute("SELECT secret FROM subscriptions").fetchone()[0], "synthetic-secret")
        with closing(Store(state)) as store:
            self.assertEqual(set(self.event_data(store.db)), set(LEGACY_EVENT_SCHEMA))

    def test_read_only_events_does_not_commit_or_recover_callers_transaction(self):
        with closing(Store(self.legacy())) as store:
            original = self.event_data(store.db)
            with self.assertRaisesRegex(RuntimeError, "synthetic caller rollback"):
                with store._transaction():
                    store.db.execute("INSERT INTO event_metadata VALUES('uncommitted','1')")
                    Events(store, recover_inflight=False)
                    self.assertTrue(store.db.in_transaction)
                    self.assertEqual(self.event_data(store.db)["deliveries"], original["deliveries"])
                    raise RuntimeError("synthetic caller rollback")
            self.assertEqual(self.event_data(store.db), original)

    def test_recovering_events_refuses_nested_transaction_without_committing_it(self):
        with closing(Store(self.legacy())) as store:
            original = self.event_data(store.db)
            with store._transaction():
                with self.assertRaisesRegex(BridgeError, "database_unavailable"):
                    Events(store)
                self.assertTrue(store.db.in_transaction)
                self.assertEqual(self.event_data(store.db), original)

    def test_migration_and_backup_do_not_recover_claims_until_runtime_requests_it(self):
        state = self.legacy()
        with closing(sqlite3.connect(state / "bridge.sqlite3")) as db:
            original = self.event_data(db)
        with closing(Store(state)) as store:
            self.assertEqual(self.event_data(store.db), original)
            snapshot = self.root / "backup.sqlite3"
            backup(store, snapshot)
        recovered = self.root / "recovered"
        recovered.mkdir(mode=0o700)
        restore(snapshot, recovered)
        with closing(Store(recovered, clock=lambda: 200)) as store:
            self.assertFalse(store.get_binding()["enabled"])
            self.assertEqual(self.event_data(store.db), original)
            Events(store, recover_inflight=False)
            self.assertEqual(self.event_data(store.db), original)
            Events(store, clock=lambda: 200)
            current = self.event_data(store.db)
            self.assertEqual(current["subscriptions"], original["subscriptions"])
            self.assertEqual(current["event_metadata"], original["event_metadata"])
            self.assertEqual(current["deliveries"][:2], original["deliveries"][:2])
            self.assertEqual(current["deliveries"][2][:4], original["deliveries"][2][:4])
            self.assertEqual(current["deliveries"][2][4:], ("retry", 1, 200, "callback_interrupted"))
            Events(store, clock=lambda: 300)
            self.assertEqual(self.event_data(store.db), current)
            store.set_enabled(True)
            self.assertEqual(store.send_status("fixture-message-2")["status"], "uncertain")
            self.assertEqual(store.send_status("fixture-message-2")["parts_accepted"], 1)
            self.assertIsNone(store.claim_send())

    def test_recovery_failure_leaves_claims_and_connection_usable(self):
        with closing(Store(self.legacy())) as store:
            original = self.event_data(store.db)
            store.db.execute("""CREATE TRIGGER interrupt_recovery BEFORE UPDATE ON deliveries
                BEGIN SELECT RAISE(ABORT,'synthetic recovery failure'); END""")
            with self.assertRaisesRegex(BridgeError, "database_unavailable"):
                Events(store)
            self.assertFalse(store.db.in_transaction)
            self.assertEqual(self.event_data(store.db), original)
            store.db.execute("DROP TRIGGER interrupt_recovery")
            Events(store)
            self.assertEqual(store.db.execute("SELECT status FROM deliveries WHERE status='sending'").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
