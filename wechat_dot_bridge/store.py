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
from .migrations import migrate
from .lifecycle import WorkStore


MAX_PENDING = 128
MAX_QUEUED_REPLIES = 32
MAX_INBOUND_BYTES = 65536
MAX_REPLY_BYTES = 262144
SEND_OUTCOMES = frozenset(
    {"accepted", "uncertain", "failed", "context_required", "auth_required"}
)
# Diagnostics are labels, never remote response text. Unknown labels collapse
# into one fixed category so callers cannot inadvertently persist credentials.
SAFE_ERRORS = frozenset({
    "auth_required", "context_required", "fresh_context_required",
    "network_timeout", "network_error", "network_cancelled", "api_timeout", "api_network_error",
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


from .attachment_store import AttachmentStore, MEDIA_ERRORS
SAFE_ERRORS = SAFE_ERRORS | MEDIA_ERRORS


class Store(AttachmentStore, WorkStore):
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
            migrate(self.db, clock=self.clock)
        except BaseException as error:
            if hasattr(self, "db"):
                self.db.close()
            if isinstance(error, sqlite3.Error):
                raise BridgeError("database_unavailable") from None
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
            self.db.execute("DELETE FROM session_pause WHERE account=?", (binding["id"],))

    def pause_session(self):
        # Tencent's documented stale-session cooldown; explicit operator disable cancels it.
        with self._transaction():
            binding = self._binding()
            self.db.execute("UPDATE binding SET enabled=0,status='auth_required',last_error='auth_required' WHERE id=?", (binding["id"],))
            self.db.execute("INSERT OR REPLACE INTO session_pause VALUES(?,?)", (binding["id"],self.clock()+3600))

    def resume_session(self):
        with self._transaction():
            rows = self.db.execute("SELECT account FROM session_pause WHERE resume_at<=?", (self.clock(),)).fetchall()
            for row in rows:
                self.db.execute("UPDATE binding SET enabled=1,status='ready' WHERE id=?", (row["account"],))
                self.db.execute("DELETE FROM session_pause WHERE account=?", (row["account"],))

    def record_error(self, code):
        diagnostic = None if code is None else _safe_error(code)
        with self._transaction():
            binding = self._binding()
            status = "auth_required" if diagnostic == "auth_required" else "ready" if diagnostic is None else binding["status"]
            self.db.execute("UPDATE binding SET last_error=?,status=? WHERE id=?",
                            (diagnostic, status, binding["id"]))

    def ingest(self, account_id, messages, cursor, *, after_commit=None):
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
            refs = message.get("attachments", [])
            if not isinstance(refs, list) or len(refs) > 8 or any(not isinstance(r, dict) or r.get("kind") not in ("image", "file", "voice", "video") for r in refs):
                raise BridgeError("invalid_inbound_message")
            quotes = message.get("quotes", [])
            if not isinstance(quotes, list) or len(quotes) > 32 or any(not isinstance(q, dict) for q in quotes):
                raise BridgeError("invalid_inbound_message")
            aliases=message.get("aliases",[])
            from .message_format import reference_id
            if not isinstance(aliases,list) or len(aliases)>33:
                raise BridgeError("invalid_inbound_message")
            aliases=[rid for value in aliases if (rid:=reference_id(value))]
            received_at=message.get("received_at",self.clock())
            if not _timestamp(received_at):raise BridgeError("invalid_inbound_message")
            normalized.append((source, peer, text, float(created_at), context, refs, quotes, aliases,received_at))
        new_ids = []
        with self._transaction():
            binding = self._binding()
            if account_id != binding["id"]:
                raise BridgeError("account_identity_mismatch")
            if not binding["enabled"] or binding["status"] != "ready":
                raise BridgeError("account_disabled")
            previous_context = self._latest_context(binding)
            count = self.db.execute(
                "SELECT count(*) FROM messages m JOIN message_work w ON w.message_id=m.id WHERE account=? AND peer=? AND w.state!='completed'",
                (binding["id"], binding["owner"])).fetchone()[0]
            added = 0
            for source, peer, text, created_at, context, refs, quotes, aliases,received_at in normalized:
                if peer != binding["owner"]:
                    raise BridgeError("owner_identity_mismatch")
                identifier = _opaque_id(binding["id"], peer, source)
                if self.db.execute("SELECT 1 FROM messages WHERE id=? UNION ALL "
                                   "SELECT 1 FROM retired_messages WHERE id=? LIMIT 1",
                                   (identifier, identifier)).fetchone():
                    continue
                if count + added >= MAX_PENDING:
                    raise BridgeError("inbox_full")
                from .message_format import partial_quote
                cached_aids, quote_states = [], []
                from .message_format import reference_id
                for position,quote in enumerate(quotes):
                    sid=reference_id(quote.get("source_id"))
                    status="inline" if quote.get("inline") else "not_cached" if sid else "reference_not_provided"
                    resolved=None
                    cached=None
                    if sid:
                        cached=self.db.execute("SELECT m.id,m.text FROM messages m LEFT JOIN message_aliases a ON a.message_id=m.id WHERE m.account=? AND m.peer=? AND (a.reference_id=? OR m.source_id IN (?,?)) LIMIT 1", (binding["id"],peer,sid,"message:"+sid,"message:item:"+sid)).fetchone()
                    quote_text=""
                    if cached:
                        resolved=cached["id"];status="cached_inbound";quote_text=cached["text"]
                        cached_aids.extend(r[0] for r in self.db.execute("SELECT id FROM attachments WHERE message_id=? UNION SELECT attachment_id FROM attachment_links WHERE message_id=?", (resolved,resolved)))
                    elif sid:
                        sent=self.db.execute("SELECT text,attachment_id FROM sent_quote_cache WHERE account=? AND peer=? AND source_id=?", (binding["id"],peer,sid)).fetchone()
                        if sent:
                            status="cached_outbound";quote_text=sent["text"]
                            if sent["attachment_id"]: cached_aids.append(sent["attachment_id"])
                    shape=quote.get("shape",{})
                    safe_shape={k:[f for f in v if isinstance(f,str) and re.fullmatch(r"[a-zA-Z_]{1,64}",f)][:32] for k,v in shape.items() if k in ("reference_fields","item_fields") and isinstance(v,list)} if isinstance(shape,dict) else {}
                    quote_states.append((position,int(bool(sid)),status,resolved,json.dumps(safe_shape)))
                    quoted=partial_quote(quote_text,quote.get("partial"))
                    budget=max(0,MAX_INBOUND_BYTES-len(text.encode())-64)
                    quoted=quoted.encode()[:budget].decode("utf-8",errors="ignore")
                    if quoted:
                        text="[引用缓存]\n"+quoted+"\n[/引用缓存]\n"+text
                self.db.execute(
                    "INSERT INTO messages(id,account,peer,source_id,text,created_at,context_token) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (identifier, binding["id"], peer, source, text, created_at, context))
                self._ensure_work(identifier)
                if source.startswith("message:"):
                    original=source[len("message:item:"):] if source.startswith("message:item:") else source[len("message:"):]
                    if reference_id(original): aliases.append(reference_id(original))
                for alias in aliases:
                    self.db.execute("INSERT OR IGNORE INTO message_aliases VALUES(?,?,?,?)", (binding["id"],peer,alias,identifier))
                for position,has_id,status,resolved,shape in quote_states:
                    self.db.execute("INSERT INTO message_quotes VALUES(?,?,?,?,?,?)", (identifier,position,has_id,status,resolved,shape))
                from .timing import record as trace
                trace(self,identifier,"received",at=received_at)
                # Inline quote media and a cache hit may describe the same bytes.
                from .attachment_store import media_identity
                inline_refs={media_identity(ref) for ref in refs if media_identity(ref) is not None}
                cached_refs=set()
                reusable_aids=[]
                for aid in dict.fromkeys(cached_aids):
                    cached_attachment=self.db.execute("SELECT descriptor,status FROM attachments WHERE id=?",(aid,)).fetchone()
                    if cached_attachment is None:continue
                    identity=media_identity(json.loads(cached_attachment["descriptor"]))
                    if cached_attachment["status"] in ("ready","pending","downloading"):
                        if identity is not None:cached_refs.add(identity)
                    elif identity in inline_refs:
                        # Fresh inline metadata can recover an expired/failed cache.
                        continue
                    reusable_aids.append(aid)
                cached_aids=reusable_aids
                self._ingest_attachments(binding, identifier, [ref for ref in refs if media_identity(ref) is None or media_identity(ref) not in cached_refs])
                for aid in cached_aids:
                    self.db.execute("INSERT OR IGNORE INTO attachment_links VALUES(?,?)", (identifier,aid))
                added += 1
                new_ids.append(identifier)
            latest_context = self._latest_context(binding)
            if latest_context and (previous_context is None or latest_context["id"] != previous_context["id"]):
                self.db.execute(
                    "UPDATE replies SET status='queued',error=NULL WHERE status='context_required' AND attempts<3 "
                    "AND (context_id IS NULL OR context_id!=?) "
                    "AND message_id IN (SELECT id FROM messages WHERE account=? AND peer=?)",
                    (latest_context["id"], binding["id"], binding["owner"]))
            self.db.execute("UPDATE binding SET cursor=?,last_error=NULL WHERE id=?", (cursor, binding["id"]))
        # A best-effort presence hook receives identifiers only after the whole
        # batch and cursor commit. Failure cannot turn a committed poll into a
        # failed receive, and duplicates/tombstones never reach the hook.
        if new_ids and after_commit is not None:
            try:
                after_commit(tuple(new_ids))
            except Exception:
                pass
        return added

    def remember_sent_quote(self, source_id, reply):
        from .message_format import reference_id
        source_id=reference_id(source_id)
        if not source_id:
            return
        with self._transaction():
            attachment = reply.get("attachment")
            binding=self._binding()
            if reply.get("message_id"):
                self._message(reply["message_id"],binding)
            self.db.execute("INSERT OR REPLACE INTO sent_quote_cache VALUES(?,?,?,?,?)",
                            (binding["id"],binding["owner"],source_id,reply["text"],attachment["id"] if attachment else None))

    def read_message(self, message_id):
        with self._transaction():
            message = self._message(message_id, self._binding())
            work = self._start_work(message_id)
            return {"processing": work, "message_id": message["id"], "text": message["text"], "created_at": message["created_at"],
                    "attachments": self.attachment_metadata(message_id),
                    "quotes":[dict(has_reference_id=bool(r["has_reference_id"]),status=r["status"],resolved_message_id=r["resolved_message_id"],shape=json.loads(r["shape"])) for r in self.db.execute("SELECT has_reference_id,status,resolved_message_id,shape FROM message_quotes WHERE message_id=? ORDER BY position",(message_id,))]}

    def _waiting_typing_ids(self, message_ids, *, created_since=None):
        """Validate bounded in-memory waiting candidates without reading text."""
        if not message_ids or len(message_ids) > 128:
            return set()
        with self.lock:
            binding = self.get_binding()
            if not binding or not binding['enabled'] or binding['status'] != 'ready':
                return set()
            placeholders = ','.join('?' for _ in message_ids)
            query = ("SELECT m.id FROM messages m JOIN message_work w ON w.message_id=m.id "
                     "WHERE m.id IN (" + placeholders + ") AND m.account=? AND m.peer=? "
                     "AND w.state='waiting' AND w.read_at IS NULL")
            parameters = (*message_ids, binding['id'], binding['owner'])
            if created_since is not None:
                query += ' AND m.created_at>=?'
                parameters += (created_since,)
            return {row['id'] for row in self.db.execute(query, parameters)}

    def typing_target(self, message_ids, *, waiting_ids=()):
        """Private presence route; durable active work takes priority over waiting."""
        if not message_ids and not waiting_ids or len(message_ids) + len(waiting_ids) > 128:
            return None
        with self.lock:
            binding = self.get_binding()
            if not binding or not binding["enabled"] or binding["status"] != "ready":
                return None
            active_ids = set()
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                active_ids = {row['id'] for row in self.db.execute(
                    "SELECT m.id FROM messages m JOIN message_work w ON w.message_id=m.id "
                    "WHERE m.id IN (" + placeholders + ") AND m.account=? AND m.peer=? "
                    "AND w.state='active' AND w.lease_until>?",
                    (*message_ids, binding["id"], binding["owner"],self.clock()))}
            selected = active_ids or self._waiting_typing_ids(waiting_ids)
            context = self._latest_context(binding)
            if not selected or not context:
                return None
            return binding, context["context_token"], selected

    def list_pending(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BridgeError("invalid_limit")
        with self.lock:
            binding = self._binding()
            rows = self.db.execute(
                "SELECT m.id,m.created_at FROM messages m JOIN message_work w ON w.message_id=m.id "
                "WHERE account=? AND peer=? AND w.state!='completed' "
                "ORDER BY m.created_at,m.rowid LIMIT ?", (binding["id"], binding["owner"], limit)).fetchall()
            return [{"message_id": row["id"], "created_at": row["created_at"]} for row in rows]

    def queue_reply(self, message_id, text, reply_key=None, attachment_id=None, final=True, notice=False, *, message_ids=None, processing_id=None, revision=None):
        if type(final) is not bool:
            raise BridgeError("invalid_processing_state")
        size = _text_bytes(text)
        if size is None or size > MAX_REPLY_BYTES or (not text.strip() and attachment_id is None):
            raise BridgeError("invalid_reply_text")
        with self._transaction():
            binding = self._binding()
            self._message(message_id, binding)
            if reply_key is not None and (not isinstance(reply_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reply_key)):
                raise BridgeError("invalid_reply_key")
            if attachment_id is not None:
                self.read_attachment(attachment_id)
            if message_ids is not None:
                self._work_members(message_id, message_ids)
            work_scope = json.dumps(sorted(message_ids)) if message_ids is not None else None
            identity = json.dumps([message_id, reply_key if reply_key is not None else text, reply_key is not None] + ([attachment_id] if attachment_id and reply_key is None else []), ensure_ascii=False)
            client_id = "dot_reply_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            if reply_key is None:
                existing = self.db.execute("SELECT text,client_id,attachment_id,final,work_scope FROM replies WHERE message_id=? AND text=? AND attachment_id IS ? ORDER BY rowid LIMIT 1", (message_id, text, attachment_id)).fetchone()
            else:
                existing = self.db.execute("SELECT text,client_id,attachment_id,final,work_scope FROM replies WHERE message_id=? AND client_id=?", (message_id, client_id)).fetchone()
            if existing:
                if existing["text"] != text or existing["attachment_id"] != attachment_id:
                    raise BridgeError("reply_key_conflict")
                if bool(existing["final"]) != final or existing['work_scope'] != work_scope:
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
                    "INSERT INTO replies(message_id,text,client_id,status,created_at,attachment_id,is_notice,final,work_scope) VALUES(?,?,?,'queued',?,?,?,?,?)",
                    (message_id, text, client_id, self.clock(), attachment_id,int(notice),int(final),work_scope))
                from .message_format import split_text
                for part, chunk in enumerate(split_text(text) if text else []):
                    self.db.execute("INSERT INTO reply_parts(reply_id,part,text) VALUES(?,?,?)", (client_id, part, chunk))
                if attachment_id:
                    self.db.execute("INSERT INTO reply_parts(reply_id,part,text,kind) VALUES(?,?,'','media')", (client_id,len(split_text(text)) if text else 0))
                if not notice:
                    self._transition_work(message_id, active=not final, completed=final, message_ids=message_ids, processing_id=processing_id, revision=revision, new_reply=True)
                    self.db.execute("UPDATE messages SET state='reply_committed' WHERE id=?", (message_id,))
                    from .timing import record as trace
                    trace(self,message_id,"reply_queued",reply_id=client_id)
        return self.send_status(message_id, client_id)

    def send_status(self, message_id, reply_id=None):
        with self.lock:
            self._message(message_id, self._binding())
            if reply_id is None:
                reply = self.db.execute("SELECT client_id,status,error FROM replies WHERE message_id=? AND is_notice=0 ORDER BY rowid DESC LIMIT 1", (message_id,)).fetchone()
            else:
                reply = self.db.execute("SELECT client_id,status,error FROM replies WHERE message_id=? AND client_id=?", (message_id, reply_id)).fetchone()
                if reply is None:
                    raise BridgeError("unknown_reply")
            result = {"message_id": message_id, "status": reply["status"] if reply else "no_reply", "delivered": None}
            work = self.db.execute('SELECT * FROM message_work WHERE message_id=?', (message_id,)).fetchone()
            if work:
                result['work'] = self._work_value(work)
            if reply:
                result["reply_id"] = reply["client_id"]
            if reply:
                parts = self.db.execute("SELECT count(*),COALESCE(SUM(accepted),0) FROM reply_parts WHERE reply_id=?", (reply["client_id"],)).fetchone()
                if parts[0] > 1:
                    result.update(parts_total=parts[0], parts_accepted=parts[1])
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
            part = self.db.execute("SELECT part,text,kind FROM reply_parts WHERE reply_id=? AND accepted=0 ORDER BY part LIMIT 1", (row["client_id"],)).fetchone()
            remaining = self.db.execute("SELECT count(*) FROM reply_parts WHERE reply_id=? AND accepted=0", (row["client_id"],)).fetchone()[0]
            if not row["is_notice"]:
                from .timing import record as trace
                trace(self,row["message_id"],"send_start",reply_id=row["client_id"])
            return {"message_id": row["message_id"], "reply_id": row["client_id"], "text": part["text"] if part else row["text"],
                    "client_id": row["client_id"] + ("_part_" + str(part["part"]) if part and part["part"] else ""), "peer": row["peer"],
                    "context_token": context["context_token"],
                    **({"attachment": self.read_attachment(row["attachment_id"])} if row["attachment_id"] and (not part or part["kind"] == "media") else {})}

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
            if not self.db.execute("SELECT is_notice FROM replies WHERE client_id=?",(reply["client_id"],)).fetchone()[0]:
                from .timing import record as trace
                trace(self,message_id,"weixin_accepted" if status=="accepted" else "send_result",outcome=status,reply_id=reply["client_id"])
            # A fresh inbound context may arrive during the POST that rejected
            # the previous context. Resume only that safely rejected request.
            if status == "context_required":
                context = self._latest_context(binding)
                if reply["attempts"] >= 3:
                    status, diagnostic = "failed", "retry_limit"
                elif context and context["id"] != reply["context_id"]:
                    status, diagnostic = "queued", None
            if status == "accepted":
                part = self.db.execute("SELECT part FROM reply_parts WHERE reply_id=? AND accepted=0 ORDER BY part LIMIT 1", (reply["client_id"],)).fetchone()
                if part:
                    self.db.execute("UPDATE reply_parts SET accepted=1 WHERE reply_id=? AND part=?", (reply["client_id"], part["part"]))
                    if self.db.execute("SELECT 1 FROM reply_parts WHERE reply_id=? AND accepted=0", (reply["client_id"],)).fetchone():
                        status = "queued"
                        self.db.execute("UPDATE replies SET attempts=0 WHERE client_id=?", (reply["client_id"],))
            self.db.execute("UPDATE replies SET status=?,error=? WHERE client_id=?", (status, diagnostic, reply["client_id"]))
            if status == "accepted" and not self.db.execute("SELECT is_notice FROM replies WHERE client_id=?", (reply["client_id"],)).fetchone()[0]:
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

    def resolve_uncertain(self, reply_id, delivered):
        if type(delivered) is not bool:
            raise BridgeError("invalid_resolution")
        with self._transaction():
            binding=self._binding()
            row=self.db.execute("SELECT r.* FROM replies r JOIN messages m ON m.id=r.message_id WHERE r.client_id=? AND m.account=? AND m.peer=?", (reply_id,binding["id"],binding["owner"])).fetchone()
            if row is None or row["status"]!="uncertain":
                raise BridgeError("uncertain_reply_required")
            if delivered:
                part=self.db.execute("SELECT part FROM reply_parts WHERE reply_id=? AND accepted=0 ORDER BY part LIMIT 1", (reply_id,)).fetchone()
                if part:
                    self.db.execute("UPDATE reply_parts SET accepted=1 WHERE reply_id=? AND part=?", (reply_id,part[0]))
            remaining=self.db.execute("SELECT 1 FROM reply_parts WHERE reply_id=? AND accepted=0", (reply_id,)).fetchone()
            status="queued" if remaining or not delivered else "accepted"
            self.db.execute("UPDATE replies SET status=?,error=NULL,attempts=0 WHERE client_id=?", (status,reply_id))
            if status=="accepted" and not row["is_notice"]:
                self.db.execute("UPDATE messages SET state='replied' WHERE id=?", (row["message_id"],))
        return self.send_status(row["message_id"],reply_id)

    def retry_reply(self, reply_id):
        with self._transaction():
            binding = self._binding()
            row = self.db.execute("SELECT r.* FROM replies r JOIN messages m ON m.id=r.message_id WHERE r.client_id=? AND m.account=? AND m.peer=?", (reply_id,binding["id"],binding["owner"])).fetchone()
            if not row:
                raise BridgeError("unknown_reply")
            if row["status"] == "uncertain":
                raise BridgeError("delivery_uncertain_review_required")
            if row["status"] in ('failed','context_required','auth_required'):
                self.db.execute("UPDATE replies SET status='queued',attempts=0,error=NULL WHERE client_id=?", (reply_id,))
            mid = row["message_id"]
        return self.send_status(mid,reply_id)

    def record_tool_call(self, name, message_id, outcome, started_at=None, elapsed_ms=None):
        with self._transaction():
            if message_id and not self.db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
                message_id = None
            self.db.execute("INSERT INTO tool_calls(name,message_id,outcome,created_at,started_at,elapsed_ms) VALUES(?,?,?,?,?,?)", (name,message_id,outcome,self.clock(),started_at,elapsed_ms))
            if message_id and started_at is not None:
                from .timing import record as trace
                if name == "read_weixin_message":
                    trace(self,message_id,"read_start",at=started_at,outcome=outcome)
                    trace(self,message_id,"read_result",outcome=outcome,elapsed_ms=elapsed_ms)
                elif name in ("reply_weixin_message","reply_weixin_attachment"):
                    trace(self,message_id,"reply_tool_result",outcome=outcome,elapsed_ms=elapsed_ms)
            self.db.execute("DELETE FROM tool_calls WHERE id <= (SELECT COALESCE(MAX(id),0)-1000 FROM tool_calls)")

    def get_status(self):
        with self.lock:
            binding = self.db.execute("SELECT * FROM binding LIMIT 1").fetchone()
            pending, counts = 0, {}
            if binding:
                pending = self.db.execute(
                    "SELECT count(*) FROM messages m JOIN message_work w ON w.message_id=m.id WHERE account=? AND peer=? AND w.state!='completed'",
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
            scope = (binding['id'], binding['owner']) if binding else ('', '')
            result["processing_status"] = {r[0]: r[1] for r in self.db.execute("""SELECT
                CASE WHEN w.state='active' AND COALESCE(w.lease_until,0)<=? THEN 'interrupted' ELSE w.state END AS effective_state,
                count(*) FROM message_work w JOIN messages m ON m.id=w.message_id
                WHERE m.account=? AND m.peer=? GROUP BY effective_state""", (self.clock(), *scope))}
            result["recent_tool_calls"] = [dict(r) for r in self.db.execute("SELECT name,outcome,created_at FROM tool_calls ORDER BY id DESC LIMIT 10")]
            pause = self.db.execute("SELECT resume_at FROM session_pause LIMIT 1").fetchone()
            result["session_retry_at"] = pause[0] if pause else None
            result["pending_processing"] = [{"message_id": r["id"], "read_observed": bool(r["was_read"]),
                "age_seconds": max(0,int(self.clock()-r["created_at"])), "work": self._work_value(r)}
                for r in self.db.execute("""SELECT m.id,m.created_at,w.*,
                (w.read_at IS NOT NULL OR EXISTS(SELECT 1 FROM tool_calls t WHERE t.message_id=m.id AND t.name='read_weixin_message' AND t.outcome='ok')) AS was_read
                FROM messages m JOIN message_work w ON w.message_id=m.id
                WHERE w.state!='completed' AND m.account=? AND m.peer=? ORDER BY m.rowid LIMIT 20""", scope)]
            from .operations import diagnostics
            result["diagnostics"] = diagnostics(self)
            result["attachments"] = {row[0]: row[1] for row in self.db.execute("SELECT status,count(*) FROM attachments GROUP BY status")}
            if binding and binding["last_error"]:
                result["last_error"] = _safe_error(binding["last_error"])
            return result
