"""Owner-scoped text notifications submitted by local automation."""

import hashlib
import json
import re

from .errors import BridgeError
from .message_format import split_text

MAX_NOTIFICATION_BYTES = 65536


class NotificationStore:
    def queue_notification(self, key, text, expires_at=None):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise BridgeError("invalid_key")
        try:
            valid = isinstance(text, str) and text.strip() and len(text.encode()) <= MAX_NOTIFICATION_BYTES
        except UnicodeEncodeError:
            valid = False
        if not valid or (expires_at is not None and
                         (type(expires_at) is not int or not 0 < expires_at < 2**63)):
            raise BridgeError("invalid_notification")
        digest = hashlib.sha256(json.dumps([text, expires_at]).encode()).hexdigest()
        with self._transaction():
            binding = self._binding()
            scope = (binding["id"], binding["owner"])
            identity = json.dumps([*scope, key]).encode()
            identifier = "notify_" + hashlib.sha256(identity).hexdigest()
            row = self.db.execute("SELECT digest FROM notifications WHERE id=?", (identifier,)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise BridgeError("key_conflict")
            else:
                now = self.clock()
                if expires_at is not None and expires_at <= now:
                    raise BridgeError("notification_expired")
                if not binding["enabled"] or binding["status"] != "ready":
                    raise BridgeError("account_disabled")
                if self._latest_context(binding) is None:
                    raise BridgeError("fresh_context_required")
                self._expire_notifications(scope)
                pending = self.db.execute("""SELECT count(*) FROM notifications WHERE account=? AND peer=?
                    AND status IN ('queued','sending')""", scope).fetchone()[0]
                if pending >= 32:
                    raise BridgeError("send_queue_full")
                self.db.execute("""INSERT INTO notifications(id,account,peer,digest,expires_at,status,created_at)
                    VALUES(?,?,?,?,?,'queued',?)""", (identifier, *scope, digest, expires_at, now))
                self.db.executemany("INSERT INTO notification_parts(notification_id,part,text) VALUES(?,?,?)",
                                    ((identifier, i, chunk) for i, chunk in enumerate(split_text(text))))
        return self.notification_status(identifier)

    def notification_status(self, identifier):
        with self.lock:
            binding = self._binding()
            row = self.db.execute("""SELECT id,status,error FROM notifications WHERE id=?
                AND account=? AND peer=?""", (identifier, binding["id"], binding["owner"])).fetchone()
            if row is None:
                raise BridgeError("unknown_notification")
            return dict(row)

    def _expire_notifications(self, scope):
        self.db.execute("""UPDATE notifications SET status='expired',error='notification_expired'
            WHERE account=? AND peer=? AND status='queued' AND expires_at<=?""", (*scope, self.clock()))

    def _claim_notification(self, binding):
        scope = (binding["id"], binding["owner"])
        self._expire_notifications(scope)
        row = self.db.execute("""SELECT n.id,p.part,p.text FROM notifications n
            JOIN notification_parts p ON p.notification_id=n.id WHERE n.account=? AND n.peer=?
            AND n.status='queued' AND p.accepted=0 ORDER BY n.created_at,n.rowid,p.part LIMIT 1""", scope).fetchone()
        if row is None:
            return None
        context = self._latest_context(binding)
        if context is None:
            self.db.execute("UPDATE notifications SET status='failed',error='fresh_context_required' WHERE id=?",
                            (row["id"],))
            return None
        self.db.execute("UPDATE notifications SET status='sending' WHERE id=?", (row["id"],))
        return {"message_id": None, "reply_id": row["id"], "client_id": f"{row['id']}_{row['part']}",
                "text": row["text"], "peer": binding["owner"], "context_token": context["context_token"]}

    def _accept_notification_part(self, identifier):
        self.db.execute("""UPDATE notification_parts SET accepted=1 WHERE notification_id=? AND part=(
            SELECT min(part) FROM notification_parts WHERE notification_id=? AND accepted=0)""",
            (identifier, identifier))
        return self.db.execute("SELECT 1 FROM notification_parts WHERE notification_id=? AND accepted=0",
                               (identifier,)).fetchone() is not None

    def finish_notification(self, identifier, status, error):
        with self._transaction():
            if self.notification_status(identifier)["status"] != "sending":
                raise BridgeError("reply_not_sending")
            if status == "accepted" and self._accept_notification_part(identifier):
                status = "queued"
            self.db.execute("UPDATE notifications SET status=?,error=? WHERE id=?", (status, error, identifier))
            if status == "auth_required":
                self.db.execute("UPDATE binding SET status='auth_required',last_error='auth_required' WHERE id=?",
                                (self._binding()["id"],))

    def resolve_notification(self, identifier, delivered):
        with self._transaction():
            if self.notification_status(identifier)["status"] != "uncertain":
                raise BridgeError("uncertain_reply_required")
            if delivered:
                status = "partial" if self._accept_notification_part(identifier) else "accepted"
            else:
                status = "failed"
            self.db.execute("UPDATE notifications SET status=?,error=NULL WHERE id=?", (status, identifier))
        return self.notification_status(identifier)
