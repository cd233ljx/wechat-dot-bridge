"""Versioned, atomic upgrades for the private bridge database.

``PRAGMA user_version`` is the committed schema version. Version 0 includes
every database written before versioned migrations, including interrupted old
startup upgrades. Apply all pending DDL and backfills in one write transaction;
never use ``executescript`` here because it implicitly commits a transaction.

Migration functions are append-only once released. They do not send messages,
change send outcomes, or recreate accepted reply parts.
"""

import time

from .errors import BridgeError


def _columns(db, table):
    # Table names are module constants, never external input.
    return {row[1]: row for row in db.execute("PRAGMA table_info(" + table + ")")}


def _core(db, clock):
    db.execute("""CREATE TABLE IF NOT EXISTS binding (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT NOT NULL,
        base_url TEXT NOT NULL, cursor TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
        status TEXT NOT NULL DEFAULT 'ready', last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, peer TEXT NOT NULL,
        source_id TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
        context_token TEXT, state TEXT NOT NULL DEFAULT 'pending',
        UNIQUE(account,peer,source_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS replies (
        message_id TEXT NOT NULL REFERENCES messages(id), text TEXT NOT NULL,
        client_id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT,
        created_at REAL NOT NULL, context_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0)""")
    columns = _columns(db, "replies")
    if columns["message_id"][5]:
        # A reply, not its inbound message, is the unit of idempotency. Preserve
        # optional media columns too when upgrading an intermediate old schema.
        optional = [name for name in ("attachment_id", "is_notice") if name in columns]
        extra = "".join(
            ", attachment_id TEXT" if name == "attachment_id"
            else ", is_notice INTEGER NOT NULL DEFAULT 0" for name in optional
        )
        db.execute("""CREATE TABLE replies_v2 (
            message_id TEXT NOT NULL REFERENCES messages(id), text TEXT NOT NULL,
            client_id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT,
            created_at REAL NOT NULL, context_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0""" + extra + ")")
        names = "message_id,text,client_id,status,error,created_at,context_id,attempts"
        if optional:
            names += "," + ",".join(optional)
        db.execute("INSERT INTO replies_v2 (" + names + ") SELECT " + names + " FROM replies")
        db.execute("DROP TABLE replies")
        db.execute("ALTER TABLE replies_v2 RENAME TO replies")
    db.execute("CREATE INDEX IF NOT EXISTS message_pending_scope ON messages(account,peer,state,created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS reply_dispatch ON replies(status,created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS reply_message ON replies(message_id)")


def _inbound_quotes(db, clock):
    db.execute("""CREATE TABLE IF NOT EXISTS message_aliases (
        account TEXT NOT NULL, peer TEXT NOT NULL, reference_id TEXT NOT NULL,
        message_id TEXT NOT NULL REFERENCES messages(id),
        PRIMARY KEY(account,peer,reference_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS message_quotes (
        message_id TEXT NOT NULL REFERENCES messages(id), position INTEGER NOT NULL,
        has_reference_id INTEGER NOT NULL, status TEXT NOT NULL,
        resolved_message_id TEXT, PRIMARY KEY(message_id,position))""")
    if "shape" not in _columns(db, "message_quotes"):
        db.execute("ALTER TABLE message_quotes ADD COLUMN shape TEXT NOT NULL DEFAULT '{}'")
    db.execute("""INSERT OR IGNORE INTO message_aliases
        SELECT account,peer,CASE WHEN source_id LIKE 'message:item:%'
        THEN substr(source_id,14) ELSE substr(source_id,9) END,id
        FROM messages WHERE source_id LIKE 'message:%'""")


def _media(db, clock):
    db.execute("""CREATE TABLE IF NOT EXISTS attachments (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, peer TEXT NOT NULL,
        message_id TEXT REFERENCES messages(id), kind TEXT NOT NULL, name TEXT NOT NULL,
        descriptor TEXT NOT NULL, data BLOB, status TEXT NOT NULL, error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS attachment_uploads (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, peer TEXT NOT NULL,
        kind TEXT NOT NULL, name TEXT NOT NULL, data BLOB NOT NULL, updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS completed_uploads (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
        offset INTEGER NOT NULL, digest TEXT NOT NULL, attachment_id TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS attachment_links (
        message_id TEXT NOT NULL REFERENCES messages(id),
        attachment_id TEXT NOT NULL REFERENCES attachments(id),
        PRIMARY KEY(message_id,attachment_id))""")
    if "created_at" not in _columns(db, "attachments"):
        db.execute("ALTER TABLE attachments ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
    # Old startup could stop after ADD COLUMN and before its backfill. A schema
    # column's existence is not evidence that its data migration committed.
    db.execute("UPDATE attachments SET created_at=? WHERE created_at=0", (clock(),))
    if "attachment_id" not in _columns(db, "replies"):
        db.execute("ALTER TABLE replies ADD COLUMN attachment_id TEXT")


def _outbound(db, clock):
    if "is_notice" not in _columns(db, "replies"):
        db.execute("ALTER TABLE replies ADD COLUMN is_notice INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE IF NOT EXISTS sent_quotes (
        source_id TEXT PRIMARY KEY, text TEXT NOT NULL, attachment_id TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS sent_quote_cache (
        account TEXT NOT NULL, peer TEXT NOT NULL, source_id TEXT NOT NULL,
        text TEXT NOT NULL, attachment_id TEXT, PRIMARY KEY(account,peer,source_id))""")
    # Retained history must prove the legacy unscoped cache belongs to the sole
    # binding. Repeat a possibly interrupted backfill, preserving newer entries.
    scopes = db.execute("SELECT DISTINCT account,peer FROM messages").fetchall()
    bindings = db.execute("SELECT id,owner FROM binding").fetchall()
    if len(scopes) == 1 and len(bindings) == 1 and tuple(scopes[0]) == tuple(bindings[0]):
        db.execute("""INSERT OR IGNORE INTO sent_quote_cache
            SELECT ?,?,source_id,text,attachment_id FROM sent_quotes""", tuple(scopes[0]))
    db.execute("""CREATE TABLE IF NOT EXISTS reply_parts (
        reply_id TEXT NOT NULL, part INTEGER NOT NULL, text TEXT NOT NULL,
        accepted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(reply_id,part))""")
    if "kind" not in _columns(db, "reply_parts"):
        db.execute("ALTER TABLE reply_parts ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")
    for row in db.execute("""SELECT client_id FROM replies WHERE attachment_id IS NOT NULL
            AND status IN ('queued','sending','uncertain','failed','context_required','auth_required')""").fetchall():
        reply_id = row[0]
        if db.execute("SELECT 1 FROM reply_parts WHERE reply_id=? AND kind='media'", (reply_id,)).fetchone():
            continue
        last = db.execute("SELECT part,text,accepted FROM reply_parts WHERE reply_id=? ORDER BY part DESC LIMIT 1", (reply_id,)).fetchone()
        if last and not last[1]:
            db.execute("UPDATE reply_parts SET kind='media' WHERE reply_id=? AND part=?", (reply_id,last[0]))
        else:
            db.execute("INSERT INTO reply_parts(reply_id,part,text,kind) VALUES(?,?,'','media')", (reply_id,last[0]+1 if last else 0))
    db.execute("CREATE TABLE IF NOT EXISTS session_pause (account TEXT PRIMARY KEY, resume_at REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS processing (message_id TEXT PRIMARY KEY REFERENCES messages(id), expires REAL NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS tool_calls (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, message_id TEXT,
        outcome TEXT NOT NULL, created_at REAL NOT NULL)""")


def _timing(db, clock):
    db.execute("""CREATE TABLE IF NOT EXISTS message_timeline (
        id INTEGER PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
        stage TEXT NOT NULL, at REAL NOT NULL, outcome TEXT,
        elapsed_ms REAL, reply_id TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS timeline_message ON message_timeline(message_id,id)")
    columns = _columns(db, "tool_calls")
    for name in ("started_at", "elapsed_ms"):
        if name not in columns:
            db.execute("ALTER TABLE tool_calls ADD COLUMN " + name + " REAL")


def _retired_messages(db, clock):
    # Only the existing opaque digest survives history cleanup. No original
    # message identifiers, bodies, account identities or credentials are kept.
    db.execute("""CREATE TABLE IF NOT EXISTS retired_messages (
        id TEXT PRIMARY KEY, retired_at REAL NOT NULL)""")


def _processing_lifecycle(db, clock):
    db.execute("""CREATE TABLE IF NOT EXISTS message_work (
        message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL CHECK(state IN ('waiting','active','completed','failed','interrupted')),
        lease_until REAL, updated_at REAL NOT NULL, read_at REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS work_run ON message_work(run_id)")
    db.execute("CREATE INDEX IF NOT EXISTS work_unfinished ON message_work(state,updated_at)")
    if 'final' not in _columns(db, 'replies'):
        db.execute("ALTER TABLE replies ADD COLUMN final INTEGER NOT NULL DEFAULT 1 CHECK(final IN (0,1))")
    if 'work_scope' not in _columns(db, 'replies'):
        db.execute("ALTER TABLE replies ADD COLUMN work_scope TEXT")
    # Old replies lacked final semantics. A surviving explicit processing lease
    # is evidence of interim work; otherwise preserve the historical completion.
    db.execute("""UPDATE replies SET final=0 WHERE is_notice=0 AND message_id IN
        (SELECT message_id FROM processing)""")
    db.execute("""INSERT OR IGNORE INTO message_work(message_id,run_id,revision,state,lease_until,updated_at,read_at)
        SELECT m.id,'work_' || substr(m.id,5),1,
        CASE WHEN EXISTS(SELECT 1 FROM processing p WHERE p.message_id=m.id) THEN 'interrupted'
             WHEN EXISTS(SELECT 1 FROM replies r WHERE r.message_id=m.id AND r.is_notice=0) THEN 'completed'
             ELSE 'waiting' END,NULL,?,
        (SELECT max(t.created_at) FROM tool_calls t WHERE t.message_id=m.id
            AND t.name='read_weixin_message' AND t.outcome='ok') FROM messages m""", (clock(),))
    db.execute('DELETE FROM processing')


def _events(db, clock):
    # These tables used to be created lazily by Events, after Store's versioned
    # upgrade had already committed. Adopt any complete or interrupted legacy
    # initialization without changing subscriptions, immutable payloads or
    # delivery outcomes. Runtime claim recovery is separate from schema work.
    db.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL,
        arguments TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
        old_secret TEXT, rotate_until REAL, expires_at REAL NOT NULL,
        verified_until REAL NOT NULL, status TEXT NOT NULL, start_rowid INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        event_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL,
        message_id TEXT NOT NULL, body BLOB NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
        error TEXT, UNIQUE(subscription_id,message_id)
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS event_metadata (key TEXT PRIMARY KEY,value TEXT)")


MIGRATIONS = (_core, _inbound_quotes, _media, _outbound, _timing, _retired_messages,
              _processing_lifecycle, _events)
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(db, *, clock=time.time):
    """Apply pending versions atomically; return the committed schema version.

    Caller owns connection setup/error categorization and must pass a connection
    outside a transaction. BEGIN IMMEDIATE serializes concurrent startup, then
    rechecks the version under the write lock. All DDL, backfills and version
    updates commit together, including adoption of an unversioned legacy file.
    """
    if db.in_transaction:
        raise BridgeError("database_migration_in_transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < 0 or version > SCHEMA_VERSION:
            raise BridgeError("database_schema_unsupported")
        for target, upgrade in enumerate(MIGRATIONS, start=1):
            if target > version:
                upgrade(db, clock)
                db.execute("PRAGMA user_version=" + str(target))
        if db.execute("SELECT count(*) FROM binding").fetchone()[0] > 1:
            raise BridgeError("invalid_binding_state")
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION
