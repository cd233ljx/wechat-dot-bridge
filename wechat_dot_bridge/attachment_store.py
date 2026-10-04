"""Private durable attachment metadata and bounded content, in the owner database."""

import base64
import hashlib
import json
import re

from .errors import BridgeError
from .media import MAX_CACHE, MAX_MEDIA, MAX_STAGE, image_preview, safe_name

MEDIA_ERRORS = {"media_key_invalid", "media_invalid", "media_endpoint_rejected", "media_too_large",
                "media_timeout", "media_http_error", "media_http_rejected", "media_network_failed", "media_decryption_failed",
                "media_image_invalid", "media_cache_full", "media_failed", "media_upload_failed"}


def media_identity(ref):
    if ref.get("error") or not ref.get("url"):
        return None
    return (ref.get("kind"),ref.get("url"),ref.get("key"),safe_name(ref.get("name")))


class AttachmentStore:
    def _init_media(self):
        self.db.execute("""CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, peer TEXT NOT NULL,
            message_id TEXT REFERENCES messages(id), kind TEXT NOT NULL, name TEXT NOT NULL,
            descriptor TEXT NOT NULL, data BLOB, status TEXT NOT NULL, error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS attachment_uploads (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, peer TEXT NOT NULL,
            kind TEXT NOT NULL, name TEXT NOT NULL, data BLOB NOT NULL, updated REAL NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS completed_uploads (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
            offset INTEGER NOT NULL, digest TEXT NOT NULL, attachment_id TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS attachment_links (
            message_id TEXT NOT NULL REFERENCES messages(id), attachment_id TEXT NOT NULL REFERENCES attachments(id),
            PRIMARY KEY(message_id,attachment_id))""")
        if "created_at" not in {r["name"] for r in self.db.execute("PRAGMA table_info(attachments)")}:
            self.db.execute("ALTER TABLE attachments ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
            self.db.execute("UPDATE attachments SET created_at=? WHERE created_at=0", (self.clock(),))
        if "attachment_id" not in {r["name"] for r in self.db.execute("PRAGMA table_info(replies)")}:
            self.db.execute("ALTER TABLE replies ADD COLUMN attachment_id TEXT")

    def _cache_room(self, size):
        total = self.db.execute("SELECT COALESCE(SUM(length(data)),0) FROM attachments").fetchone()[0]
        total += self.db.execute("SELECT COALESCE(SUM(length(data)),0) FROM attachment_uploads").fetchone()[0]
        if size > MAX_MEDIA or total + size > getattr(self,"media_policy",{}).get("cache_mib",MAX_CACHE//1048576)*1048576:
            raise BridgeError("media_cache_full")

    def _ingest_attachments(self, binding, message_id, refs):
        seen=set()
        for index, ref in enumerate(refs):
            identity=media_identity(ref)
            if identity is not None:
                if identity in seen:continue
                seen.add(identity)
            aid = "att_" + hashlib.sha256((message_id + ":" + str(index)).encode()).hexdigest()
            error = ref.get("error")
            error = error if error in MEDIA_ERRORS else "media_invalid" if error else None
            self.db.execute("""INSERT INTO attachments
                (id,account,peer,message_id,kind,name,descriptor,status,error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (aid, binding["id"], binding["owner"], message_id,
                ref["kind"], safe_name(ref.get("name")), json.dumps(ref), "failed" if error else "pending", error,self.clock()))

    def attachment_metadata(self, message_id):
        with self.lock:
            self._message(message_id, self._binding())
            return [dict(r) for r in self.db.execute(
                "SELECT id AS attachment_id,kind,name,status,error,length(data) AS size FROM attachments WHERE message_id=? OR id IN (SELECT attachment_id FROM attachment_links WHERE message_id=?) ORDER BY rowid", (message_id,message_id))]

    def _attachment(self, attachment_id):
        binding = self._binding()
        row = self.db.execute("SELECT * FROM attachments WHERE id=? AND account=? AND peer=?",
                              (attachment_id, binding["id"], binding["owner"])).fetchone()
        if row is None:
            raise BridgeError("unknown_owner_attachment")
        return row

    def read_attachment(self, attachment_id):
        with self.lock:
            row = self._attachment(attachment_id)
            if row["status"] != "ready":
                raise BridgeError(row["error"] or ("attachment_expired" if row["status"] == "expired" else "attachment_pending"))
            return {key: row[key] for key in ("id", "kind", "name", "data")}

    def next_attachment(self):
        with self._transaction():
            binding = self._binding()
            if not binding["enabled"] or binding["status"] != "ready":
                return None
            row = self.db.execute("""SELECT a.id,a.descriptor FROM attachments a
                WHERE a.account=? AND a.peer=? AND a.status='pending' AND a.attempts<3
                AND a.next_attempt<=? ORDER BY a.rowid LIMIT 1""",
                (binding["id"], binding["owner"], self.clock())).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE attachments SET attempts=attempts+1,status='downloading' WHERE id=?", (row["id"],))
            return dict(row)

    def finish_attachment(self, attachment_id, data):
        with self._transaction():
            self._attachment(attachment_id)
            self._cache_room(len(data))
            self.db.execute("UPDATE attachments SET data=?,status='ready',error=NULL WHERE id=?", (data, attachment_id))

    def fail_attachment(self, attachment_id, error):
        error = error if error in MEDIA_ERRORS else "media_failed"
        with self._transaction():
            row = self._attachment(attachment_id)
            retry = error in {"media_timeout", "media_network_failed", "media_http_error"} and row["attempts"] < 3
            self.db.execute("UPDATE attachments SET status=?,error=?,next_attempt=? WHERE id=?",
                            ("pending" if retry else "failed", error, self.clock() + 5 * 2 ** row["attempts"], attachment_id))

    def stage_attachment(self, data_base64, kind, name, upload_id=None, offset=0, final=True):
        requested_kind = kind
        if kind not in ("auto", "image", "file", "video", "voice") or not isinstance(data_base64, str) or len(data_base64) > ((MAX_STAGE + 2) // 3) * 4:
            raise BridgeError("media_invalid")
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (ValueError, TypeError):
            raise BridgeError("media_invalid") from None
        if not data or len(data) > MAX_STAGE:
            raise BridgeError("media_too_large")
        name = safe_name(name)
        with self._transaction():
            binding = self._binding()
            if upload_id is not None:
                if not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", upload_id) or type(offset) is not int or offset < 0 or type(final) is not bool:
                    raise BridgeError("media_invalid")
                uid = hashlib.sha256(json.dumps([binding["id"], binding["owner"], upload_id]).encode()).hexdigest()
                completed = self.db.execute("SELECT * FROM completed_uploads WHERE id=?", (uid,)).fetchone()
                if completed:
                    if not final or completed["kind"] != kind or completed["name"] != name or completed["offset"] != offset or completed["digest"] != hashlib.sha256(data).hexdigest():
                        raise BridgeError("upload_conflict")
                    existing = self.read_attachment(completed["attachment_id"])
                    return {"attachment_id": existing["id"], "kind": existing["kind"], "name": name, "size": len(existing["data"])}
                final_digest = hashlib.sha256(data).hexdigest()
                row = self.db.execute("SELECT * FROM attachment_uploads WHERE id=?", (uid,)).fetchone()
                previous = row["data"] if row else b""
                if row and (row["kind"] != kind or row["name"] != name):
                    raise BridgeError("upload_conflict")
                if offset > len(previous) or offset + len(data) > MAX_MEDIA:
                    raise BridgeError("upload_offset_invalid")
                if offset < len(previous):
                    if previous[offset:offset + len(data)] != data:
                        raise BridgeError("upload_conflict")
                    combined = previous
                else:
                    self._cache_room(len(data))
                    combined = previous + data
                self.db.execute("INSERT INTO attachment_uploads VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,updated=excluded.updated",
                                (uid, binding["id"], binding["owner"], kind, name, combined, self.clock()))
                if not final:
                    return {"upload_id": upload_id, "next_offset": len(combined), "complete": False}
                data = combined
                self.db.execute("DELETE FROM attachment_uploads WHERE id=?", (uid,))
            elif offset or not final:
                raise BridgeError("upload_id_required")
            if kind == "auto":
                from .artifacts import detect_kind
                kind = detect_kind(data,name)
            if kind == "image":
                image_preview(data)
            material = json.dumps([binding["id"], binding["owner"], kind, name]).encode() + data
            aid = "att_" + hashlib.sha256(material).hexdigest()
            if not self.db.execute("SELECT 1 FROM attachments WHERE id=?", (aid,)).fetchone():
                self._cache_room(len(data))
                self.db.execute("""INSERT INTO attachments
                    (id,account,peer,kind,name,descriptor,data,status,created_at) VALUES(?,?,?,?,?,'{}',?,'ready',?)""",
                    (aid, binding["id"], binding["owner"], kind, name, data,self.clock()))
            else:
                existing = self.db.execute("SELECT status FROM attachments WHERE id=?", (aid,)).fetchone()
                if existing["status"] == "expired":
                    self._cache_room(len(data))
                    self.db.execute("UPDATE attachments SET data=?,status='ready',error=NULL,created_at=? WHERE id=?", (data,self.clock(),aid))
            if upload_id is not None:
                self.db.execute("INSERT INTO completed_uploads VALUES(?,?,?,?,?,?)", (uid, requested_kind, name, offset, final_digest, aid))
        return {"attachment_id": aid, "kind": kind, "name": name, "size": len(data)}

    def cleanup_media(self, retention_days=None, upload_hours=None):
        policy = getattr(self,"media_policy",{})
        retention_days = retention_days if retention_days is not None else policy.get("retention_days",7)
        upload_hours = upload_hours if upload_hours is not None else policy.get("upload_hours",24)
        with self._transaction():
            self.db.execute("DELETE FROM attachment_uploads WHERE updated<?", (self.clock()-upload_hours*3600,))
            changed = self.db.execute("""UPDATE attachments SET data=NULL,status='expired',error=NULL
                WHERE status='ready' AND created_at<?
                AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.state='pending' AND
                    (m.id=attachments.message_id OR EXISTS(SELECT 1 FROM attachment_links l WHERE l.message_id=m.id AND l.attachment_id=attachments.id)))
                AND NOT EXISTS(SELECT 1 FROM replies r WHERE r.attachment_id=attachments.id
                    AND r.status IN ('queued','sending','uncertain','context_required','auth_required'))""", (self.clock()-retention_days*86400,)).rowcount
            return changed

    def retry_attachment(self, attachment_id):
        with self._transaction():
            row = self._attachment(attachment_id)
            if row["status"] in ('ready','pending','downloading'):
                return {"attachment_id":attachment_id,"status":row["status"]}
            if not json.loads(row["descriptor"]).get("url"):
                raise BridgeError("attachment_source_unavailable")
            self.db.execute("UPDATE attachments SET status='pending',attempts=0,error=NULL,next_attempt=0,created_at=? WHERE id=?", (self.clock(),attachment_id))
            return {"attachment_id":attachment_id,"status":"pending"}
