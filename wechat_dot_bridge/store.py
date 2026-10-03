"""Private, owner-scoped persistence for the text bridge.

This module has no network or model client. Transactions commit an inbound batch
and its poll cursor together. A reply is an immutable promise, separate from the
upstream acceptance of its eventual POST.
"""

from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit

from .errors import BridgeError


MAX_PENDING = 128
MAX_QUEUED_REPLIES = 32
MAX_INBOUND_BYTES = 65536
MAX_REPLY_BYTES = 8192
SEND_OUTCOMES = frozenset(
    {"accepted", "uncertain", "failed", "context_required", "auth_required"}
)
# Diagnostics are labels, never remote response text. Unknown labels collapse
# into one fixed category so callers cannot inadvertently persist credentials.
SAFE_ERRORS = frozenset({
    "auth_required", "context_required", "fresh_context_required",
    "network_timeout", "network_error", "api_timeout", "api_network_error",
    "api_http_error", "invalid_api_response", "invalid_api_json",
    "response_too_large", "api_response_too_large", "poll_rejected",
    "send_rejected", "rate_limited", "restart_during_send",
    "invalid_send_response", "invalid_send_status", "invalid_destination",
    "invalid_api_destination", "non_public_api_address", "http_error",
    "operation_failed", "account_disabled", "context_expired", "retry_limit",
    "endpoint_rejected", "dns_rejected", "response_invalid", "binding_invalid",
    "binding_ambiguous", "import_failed", "gateway_error", "invalid_reply",
})


def _safe_error(value):
    return value if isinstance(value, str) and value in SAFE_ERRORS else "operation_failed"


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and _text_bytes(value) is not None and not any(
        ord(character) < 32 for character in value
    )


def _text_bytes(value):
    if not isinstance(value, str):
        return None
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        return None


def _timestamp(value):
    try:
        return type(value) in (float, int) and math.isfinite(value) and value >= 0
    except (OverflowError, ValueError):
        return False


def _opaque_id(account, peer, source):
    material = json.dumps([account, peer, source], ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    return "msg_" + hashlib.sha256(material).hexdigest()


def _private_directory(path):
    directory = Path(os.path.abspath(os.fspath(path)))
    try:
        # Reject symlinks before creating anything; resolve() would hide them.
        for component in reversed((directory, *directory.parents)):
            try:
                metadata = component.lstat()
            except FileNotFoundError:
                component.mkdir(mode=0o700)
                metadata = component.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BridgeError("unsafe_state_directory")
        metadata = directory.lstat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise BridgeError("unsafe_state_directory")
    except BridgeError:
        raise
    except OSError:
        raise BridgeError("state_directory_unavailable") from None
    return directory


def _private_database_file(directory):
    path = directory / "bridge.sqlite3"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                raise BridgeError("unsafe_database_file")
        finally:
            os.close(fd)
        for suffix in ("-wal", "-shm", "-journal"):
            auxiliary = Path(str(path) + suffix)
            if auxiliary.exists() or auxiliary.is_symlink():
                metadata = auxiliary.lstat()
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                    raise BridgeError("unsafe_database_file")
    except BridgeError:
        raise
    except OSError:
        raise BridgeError("unsafe_database_file") from None
    return path


class Store:
    def __init__(self, state_dir, clock=time.time):
        self.directory = _private_directory(state_dir)
        self.state_dir = self.directory
        self.clock = clock
        self.lock = threading.RLock()
        self._closed = False
        database = _private_database_file(self.directory)
        try:
            self.db = sqlite3.connect(database, isolation_level=None,
                                      check_same_thread=False, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS binding (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    token TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    cursor TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                    status TEXT NOT NULL DEFAULT 'ready',
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    peer TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    context_token TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(account, peer, source_id)
                );
                CREATE TABLE IF NOT EXISTS replies (
                    message_id TEXT PRIMARY KEY REFERENCES messages(id),
                    text TEXT NOT NULL,
                    client_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at REAL NOT NULL,
                    context_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS message_pending_scope
                    ON messages(account,peer,state,created_at);
                CREATE INDEX IF NOT EXISTS reply_dispatch
                    ON replies(status,created_at);
            """)
            self._migrate_reply_identity()
            if self.db.execute("SELECT count(*) FROM binding").fetchone()[0] > 1:
                raise BridgeError("invalid_binding_state")
        except (sqlite3.Error, BridgeError) as error:
            if hasattr(self, "db"):
                self.db.close()
            if isinstance(error, BridgeError):
                raise
            raise BridgeError("database_unavailable") from None

    def _migrate_reply_identity(self):
        columns = {r["name"]: r for r in self.db.execute("PRAGMA table_info(replies)")}
        if not columns["message_id"]["pk"]:
            return
        # Preserve every existing client id and send outcome. A reply, not an
        # inbound message, is now the unit of idempotency and uncertainty.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("""CREATE TABLE replies_v2 (
                message_id TEXT NOT NULL REFERENCES messages(id), text TEXT NOT NULL,
                client_id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT,
                created_at REAL NOT NULL, context_id TEXT, attempts INTEGER NOT NULL DEFAULT 0)""")
            self.db.execute("INSERT INTO replies_v2 SELECT message_id,text,client_id,status,error,created_at,context_id,attempts FROM replies")
            self.db.execute("DROP TABLE replies")
            self.db.execute("ALTER TABLE replies_v2 RENAME TO replies")
            self.db.execute("CREATE INDEX reply_dispatch ON replies(status,created_at)")
            self.db.execute("CREATE INDEX reply_message ON replies(message_id)")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    @contextmanager
    def _transaction(self):
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                raise BridgeError("database_unavailable") from None
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException as error:
                if self.db.in_transaction:
                    try:
                        self.db.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                if isinstance(error, sqlite3.Error):
                    raise BridgeError("database_unavailable") from None
                raise

    def close(self):
        with self.lock:
            if not self._closed:
                self.db.close()
                self._closed = True

    def _binding(self):
        row = self.db.execute("SELECT * FROM binding LIMIT 1").fetchone()
        if row is None:
            raise BridgeError("not_bound")
        return row

    def _message(self, message_id, binding):
        if not _identity(message_id):
            raise BridgeError("invalid_message_id")
        row = self.db.execute(
            "SELECT * FROM messages WHERE id=? AND account=? AND peer=?",
            (message_id, binding["id"], binding["owner"])).fetchone()
        if row is None:
            raise BridgeError("unknown_owner_message")
        return row

    def _latest_context(self, binding):
        return self.db.execute(
            "SELECT id,context_token FROM messages "
            "WHERE account=? AND peer=? AND context_token IS NOT NULL "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (binding["id"], binding["owner"])).fetchone()

    def get_binding(self):
        """Internal routing/transport access; callers must never log this dict."""
        with self.lock:
            row = self.db.execute("SELECT * FROM binding LIMIT 1").fetchone()
            if row is None:
                return None
            result = dict(row)
            result["enabled"] = bool(result["enabled"])
            return result

    def import_binding(self, binding):
        if not isinstance(binding, dict):
            raise BridgeError("invalid_binding")
        account, owner = binding.get("id"), binding.get("owner")
        token, base_url = binding.get("token"), binding.get("base_url")
        if (not _identity(account) or not _identity(owner) or not isinstance(token, str)
                or not token or len(token) > 8192 or any(c.isspace() for c in token)):
            raise BridgeError("invalid_binding")
        try:
            parsed = urlsplit(base_url)
            host = parsed.hostname
            if (not isinstance(base_url, str) or len(base_url) > 2048
                    or parsed.scheme != "https" or not host
                    or not (host == "weixin.qq.com" or host.endswith(".weixin.qq.com"))
                    or parsed.port not in (None, 443) or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                    or any(character in base_url for character in "\r\n\x00")):
                raise ValueError
            base_url = "https://" + host
        except (TypeError, ValueError, AttributeError):
            raise BridgeError("invalid_binding") from None
        cursor = binding.get("cursor", "")
        if not isinstance(cursor, str) or _text_bytes(cursor) is None or _text_bytes(cursor) > 65536:
            raise BridgeError("invalid_binding")
        with self._transaction():
            previous = self.db.execute("SELECT * FROM binding LIMIT 1").fetchone()
            if previous:
                if previous["id"] != account or previous["owner"] != owner:
                    raise BridgeError("binding_identity_changed")
                if "cursor" not in binding:
                    cursor = previous["cursor"]
                self.db.execute(
                    "UPDATE binding SET token=?,base_url=?,cursor=?,status='ready',last_error=NULL WHERE id=?",
                    (token, base_url, cursor, account))
            else:
                self.db.execute(
                    "INSERT INTO binding(id,owner,token,base_url,cursor) VALUES(?,?,?,?,?)",
                    (account, owner, token, base_url, cursor))
            # A new confirmed credential may resume previously rejected auth.
            self.db.execute(
                "UPDATE replies SET status='queued',error=NULL WHERE status='auth_required' "
                "AND message_id IN (SELECT id FROM messages WHERE account=? AND peer=?)",
                (account, owner))
        return {"bound": True, "enabled": self.get_binding()["enabled"], "account_status": "ready"}

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise BridgeError("invalid_enabled")
        with self._transaction():
            binding = self._binding()
            if enabled and binding["status"] != "ready":
                raise BridgeError("account_not_ready")
            self.db.execute("UPDATE binding SET enabled=? WHERE id=?", (int(enabled), binding["id"]))

    def record_error(self, code):
        diagnostic = None if code is None else _safe_error(code)
        with self._transaction():
            binding = self._binding()
            status = "auth_required" if diagnostic == "auth_required" else "ready" if diagnostic is None else binding["status"]
            self.db.execute("UPDATE binding SET last_error=?,status=? WHERE id=?",
                            (diagnostic, status, binding["id"]))

    def ingest(self, account_id, messages, cursor):
        if (not isinstance(messages, list) or len(messages) > 1000
                or not isinstance(cursor, str) or _text_bytes(cursor) is None
                or _text_bytes(cursor) > 65536):
            raise BridgeError("invalid_inbound_batch")
        normalized = []
        for message in messages:
            if not isinstance(message, dict):
                raise BridgeError("invalid_inbound_message")
            source, peer = message.get("source_id"), message.get("peer")
            text, created_at = message.get("text"), message.get("created_at")
            context = message.get("context_token")
            size = _text_bytes(text)
            if (not _identity(source) or not _identity(peer) or size is None
                    or not 0 < size <= MAX_INBOUND_BYTES or not _timestamp(created_at)
                    or (context is not None and (not isinstance(context, str)
                        or not 0 < len(context) <= 16384))):
                raise BridgeError("invalid_inbound_message")
            normalized.append((source, peer, text, float(created_at), context))
        with self._transaction():
            binding = self._binding()
            if account_id != binding["id"]:
                raise BridgeError("account_identity_mismatch")
            if not binding["enabled"] or binding["status"] != "ready":
                raise BridgeError("account_disabled")
            previous_context = self._latest_context(binding)
            count = self.db.execute(
                "SELECT count(*) FROM messages WHERE account=? AND peer=? AND state='pending'",
                (binding["id"], binding["owner"])).fetchone()[0]
            added = 0
            for source, peer, text, created_at, context in normalized:
                if peer != binding["owner"]:
                    raise BridgeError("owner_identity_mismatch")
                identifier = _opaque_id(binding["id"], peer, source)
                if self.db.execute("SELECT 1 FROM messages WHERE id=?", (identifier,)).fetchone():
                    continue
                if count + added >= MAX_PENDING:
                    raise BridgeError("inbox_full")
                self.db.execute(
                    "INSERT INTO messages(id,account,peer,source_id,text,created_at,context_token) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (identifier, binding["id"], peer, source, text, created_at, context))
                added += 1
            latest_context = self._latest_context(binding)
            if latest_context and (previous_context is None or latest_context["id"] != previous_context["id"]):
                self.db.execute(
                    "UPDATE replies SET status='queued',error=NULL WHERE status='context_required' AND attempts<3 "
                    "AND (context_id IS NULL OR context_id!=?) "
                    "AND message_id IN (SELECT id FROM messages WHERE account=? AND peer=?)",
                    (latest_context["id"], binding["id"], binding["owner"]))
            self.db.execute("UPDATE binding SET cursor=?,last_error=NULL WHERE id=?", (cursor, binding["id"]))
        return added

    def read_message(self, message_id):
        with self.lock:
            message = self._message(message_id, self._binding())
            return {"message_id": message["id"], "text": message["text"], "created_at": message["created_at"]}

    def typing_target(self, message_ids):
        """Internal route only for pending owner messages dot has actually read."""
        if not message_ids or len(message_ids) > 128:
            return None
        with self.lock:
            binding = self.get_binding()
            if not binding or not binding["enabled"] or binding["status"] != "ready":
                return None
            placeholders = ",".join("?" for _ in message_ids)
            rows = self.db.execute(
                "SELECT m.id FROM messages m WHERE m.id IN (" + placeholders + ") "
                "AND m.account=? AND m.peer=? AND m.state='pending' "
                "AND NOT EXISTS(SELECT 1 FROM replies r WHERE r.message_id=m.id)",
                (*message_ids, binding["id"], binding["owner"])).fetchall()
            context = self._latest_context(binding)
            if not rows or not context:
                return None
            return binding, context["context_token"], {row["id"] for row in rows}

    def list_pending(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BridgeError("invalid_limit")
        with self.lock:
            binding = self._binding()
            rows = self.db.execute(
                "SELECT id,created_at FROM messages m WHERE account=? AND peer=? AND state='pending' "
                "AND NOT EXISTS(SELECT 1 FROM replies r WHERE r.message_id=m.id) "
                "ORDER BY created_at,rowid LIMIT ?", (binding["id"], binding["owner"], limit)).fetchall()
            return [{"message_id": row["id"], "created_at": row["created_at"]} for row in rows]

    def queue_reply(self, message_id, text, reply_key=None):
        size = _text_bytes(text)
        if size is None or not text.strip() or not 0 < size <= MAX_REPLY_BYTES:
            raise BridgeError("invalid_reply_text")
        with self._transaction():
            binding = self._binding()
            self._message(message_id, binding)
            if reply_key is not None and (not isinstance(reply_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reply_key)):
                raise BridgeError("invalid_reply_key")
            identity = json.dumps([message_id, reply_key if reply_key is not None else text, reply_key is not None], ensure_ascii=False)
            client_id = "dot_reply_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            if reply_key is None:
                existing = self.db.execute("SELECT text,client_id FROM replies WHERE message_id=? AND text=? ORDER BY rowid LIMIT 1", (message_id, text)).fetchone()
            else:
                existing = self.db.execute("SELECT text,client_id FROM replies WHERE message_id=? AND client_id=?", (message_id, client_id)).fetchone()
            if existing:
                if existing["text"] != text:
                    raise BridgeError("reply_key_conflict")
                client_id = existing["client_id"]
            else:
                if not binding["enabled"] or binding["status"] != "ready":
                    raise BridgeError("account_disabled")
                queued = self.db.execute(
                    "SELECT count(*) FROM replies r JOIN messages m ON m.id=r.message_id "
                    "WHERE r.status IN ('queued','sending','context_required','auth_required') "
                    "AND m.account=? AND m.peer=?", (binding["id"], binding["owner"])).fetchone()[0]
                if queued >= MAX_QUEUED_REPLIES:
                    raise BridgeError("send_queue_full")
                self.db.execute(
                    "INSERT INTO replies(message_id,text,client_id,status,created_at) VALUES(?,?,?,'queued',?)",
                    (message_id, text, client_id, self.clock()))
                self.db.execute("UPDATE messages SET state='reply_committed' WHERE id=?", (message_id,))
        return self.send_status(message_id, client_id)

    def send_status(self, message_id, reply_id=None):
        with self.lock:
            self._message(message_id, self._binding())
            if reply_id is None:
                reply = self.db.execute("SELECT client_id,status,error FROM replies WHERE message_id=? ORDER BY rowid DESC LIMIT 1", (message_id,)).fetchone()
            else:
                reply = self.db.execute("SELECT client_id,status,error FROM replies WHERE message_id=? AND client_id=?", (message_id, reply_id)).fetchone()
                if reply is None:
                    raise BridgeError("unknown_reply")
            result = {"message_id": message_id, "status": reply["status"] if reply else "no_reply", "delivered": None}
            if reply:
                result["reply_id"] = reply["client_id"]
            if reply and reply["error"]:
                result["error"] = _safe_error(reply["error"])
            return result

    def claim_send(self):
        with self._transaction():
            binding = self.db.execute("SELECT * FROM binding LIMIT 1").fetchone()
            if not binding or not binding["enabled"] or binding["status"] != "ready":
                return None
            row = self.db.execute(
                "SELECT r.*,m.peer FROM replies r JOIN messages m ON m.id=r.message_id "
                "WHERE r.status='queued' AND m.account=? AND m.peer=? "
                "ORDER BY r.created_at,r.rowid LIMIT 1", (binding["id"], binding["owner"])).fetchone()
            if row is None:
                return None
            if row["attempts"] >= 3:
                self.db.execute("UPDATE replies SET status='failed',error='retry_limit' WHERE client_id=?", (row["client_id"],))
                return None
            context = self._latest_context(binding)
            if context is None:
                self.db.execute(
                    "UPDATE replies SET status='context_required',error='fresh_context_required' WHERE client_id=?",
                    (row["client_id"],))
                return None
            self.db.execute(
                "UPDATE replies SET status='sending',error=NULL,context_id=?,attempts=attempts+1 WHERE client_id=?",
                (context["id"], row["client_id"]))
            return {"message_id": row["message_id"], "reply_id": row["client_id"], "text": row["text"],
                    "client_id": row["client_id"], "peer": row["peer"],
                    "context_token": context["context_token"]}

    def finish_send(self, message_id, status, error=None, reply_id=None):
        if not isinstance(status, str) or status not in SEND_OUTCOMES:
            raise BridgeError("invalid_send_outcome")
        diagnostic = None if error is None else _safe_error(error)
        with self._transaction():
            binding = self._binding()
            self._message(message_id, binding)
            if reply_id is None:
                reply = self.db.execute("SELECT client_id,status,error,context_id,attempts FROM replies WHERE message_id=? ORDER BY (status='sending') DESC,rowid DESC LIMIT 1", (message_id,)).fetchone()
            else:
                reply = self.db.execute("SELECT client_id,status,error,context_id,attempts FROM replies WHERE message_id=? AND client_id=?", (message_id, reply_id)).fetchone()
            if reply is None:
                raise BridgeError("reply_not_committed")
            if reply["status"] != "sending":
                if reply["status"] == status and reply["error"] == diagnostic:
                    return
                raise BridgeError("reply_not_sending")
            # A fresh inbound context may arrive during the POST that rejected
            # the previous context. Resume only that safely rejected request.
            if status == "context_required":
                context = self._latest_context(binding)
                if reply["attempts"] >= 3:
                    status, diagnostic = "failed", "retry_limit"
                elif context and context["id"] != reply["context_id"]:
                    status, diagnostic = "queued", None
            self.db.execute("UPDATE replies SET status=?,error=? WHERE client_id=?", (status, diagnostic, reply["client_id"]))
            if status == "accepted":
                self.db.execute("UPDATE messages SET state='replied' WHERE id=?", (message_id,))
            if status == "auth_required":
                self.db.execute("UPDATE binding SET status='auth_required',last_error='auth_required' WHERE id=?", (binding["id"],))

    def recover_inflight(self):
        """Only the daemon uses this; status/read operations never resend."""
        with self._transaction():
            binding = self.db.execute("SELECT id,owner FROM binding LIMIT 1").fetchone()
            if binding:
                self.db.execute(
                    "UPDATE replies SET status='uncertain',error='restart_during_send' WHERE status='sending' "
                    "AND message_id IN (SELECT id FROM messages WHERE account=? AND peer=?)",
                    (binding["id"], binding["owner"]))

    def get_status(self):
        with self.lock:
            binding = self.db.execute("SELECT * FROM binding LIMIT 1").fetchone()
            pending, counts = 0, {}
            if binding:
                pending = self.db.execute(
                    "SELECT count(*) FROM messages WHERE account=? AND peer=? AND state='pending'",
                    (binding["id"], binding["owner"])).fetchone()[0]
                rows = self.db.execute(
                    "SELECT r.status,count(*) AS n FROM replies r JOIN messages m ON m.id=r.message_id "
                    "WHERE m.account=? AND m.peer=? GROUP BY r.status", (binding["id"], binding["owner"])).fetchall()
                counts = {row["status"]: row["n"] for row in rows}
            active = 0
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscriptions'").fetchone():
                # Events owns the table, including its expiration column.
                columns = {row["name"] for row in self.db.execute("PRAGMA table_info(subscriptions)")}
                expiration = "expires" if "expires" in columns else "expires_at" if "expires_at" in columns else None
                if expiration:
                    active_filter = " AND status='active'" if "status" in columns else ""
                    active = self.db.execute("SELECT count(*) FROM subscriptions WHERE (" + expiration + "=0 OR " + expiration + ">?)" + active_filter, (self.clock(),)).fetchone()[0]
            result = {"bound": bool(binding), "enabled": bool(binding and binding["enabled"]),
                      "account_status": binding["status"] if binding else "not_bound",
                      "pending": pending, "reply_status": counts, "active_subscriptions": active}
            if binding and binding["last_error"]:
                result["last_error"] = _safe_error(binding["last_error"])
            return result
