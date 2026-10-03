"""Private durable attachment metadata and bounded content, in the owner database."""

import base64
import hashlib
import json
import re

from .errors import BridgeError
from .media import MAX_CACHE, MAX_MEDIA, MAX_STAGE, image_preview, safe_name

MEDIA_ERRORS = {"media_key_invalid", "media_invalid", "media_endpoint_rejected", "media_too_large",
                "media_timeout", "media_http_error", "media_network_failed", "media_decryption_failed",
                "media_image_invalid", "media_cache_full", "media_failed", "media_upload_failed"}


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
        if "attachment_id" not in {r["name"] for r in self.db.execute("PRAGMA table_info(replies)")}:
            self.db.execute("ALTER TABLE replies ADD COLUMN attachment_id TEXT")

    def _cache_room(self, size):
        total = self.db.execute("SELECT COALESCE(SUM(length(data)),0) FROM attachments").fetchone()[0]
        total += self.db.execute("SELECT COALESCE(SUM(length(data)),0) FROM attachment_uploads").fetchone()[0]
        if size > MAX_MEDIA or total + size > MAX_CACHE:
            raise BridgeError("media_cache_full")

    def _ingest_attachments(self, binding, message_id, refs):
        for index, ref in enumerate(refs):
            aid = "att_" + hashlib.sha256((message_id + ":" + str(index)).encode()).hexdigest()
            error = ref.get("error")
            error = error if error in MEDIA_ERRORS else "media_invalid" if error else None
            self.db.execute("""INSERT INTO attachments
                (id,account,peer,message_id,kind,name,descriptor,status,error)
                VALUES(?,?,?,?,?,?,?,?,?)""", (aid, binding["id"], binding["owner"], message_id,
                ref["kind"], safe_name(ref.get("name")), json.dumps(ref), "failed" if error else "pending", error))

    def attachment_metadata(self, message_id):
        with self.lock:
            self._message(message_id, self._binding())
            return [dict(r) for r in self.db.execute(
                "SELECT id AS attachment_id,kind,name,status,error,length(data) AS size FROM attachments WHERE message_id=? ORDER BY rowid", (message_id,))]

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
                raise BridgeError(row["error"] or "attachment_pending")
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
        if kind not in ("image", "file", "video", "voice") or not isinstance(data_base64, str) or len(data_base64) > ((MAX_STAGE + 2) // 3) * 4:
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
                    return {"attachment_id": existing["id"], "kind": kind, "name": name, "size": len(existing["data"])}
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
            if kind == "image":
                image_preview(data)
            material = json.dumps([binding["id"], binding["owner"], kind, name]).encode() + data
            aid = "att_" + hashlib.sha256(material).hexdigest()
            if not self.db.execute("SELECT 1 FROM attachments WHERE id=?", (aid,)).fetchone():
                self._cache_room(len(data))
                self.db.execute("""INSERT INTO attachments
                    (id,account,peer,kind,name,descriptor,data,status) VALUES(?,?,?,?,?,'{}',?,'ready')""",
                    (aid, binding["id"], binding["owner"], kind, name, data))
            if upload_id is not None:
                self.db.execute("INSERT INTO completed_uploads VALUES(?,?,?,?,?,?)", (uid, kind, name, offset, final_digest, aid))
        return {"attachment_id": aid, "kind": kind, "name": name, "size": len(data)}
