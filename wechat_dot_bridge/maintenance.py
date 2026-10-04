"""Local storage accounting and conservative, opt-in history retirement."""

import os
import shutil
import stat
import time
import uuid

from .errors import BridgeError

DEFAULT_POLICY = {
    "history_days": 0,
    "disk_warning_mib": 512,
    "database_warning_mib": 1024,
}
BOUNDS = {
    "history_days": (0, 3650),
    "disk_warning_mib": (1, 1048576),
    "database_warning_mib": (1, 1048576),
}
HISTORY_BATCH = 500
QUOTE_CACHE_LIMIT = 10000


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) - set(BOUNDS):
        raise BridgeError("config_invalid")
    for name, value in policy.items():
        low, high = BOUNDS[name]
        if type(value) is not int or not low <= value <= high:
            raise BridgeError("config_invalid")
    return dict(DEFAULT_POLICY, **policy)


def _table(store, name):
    return bool(store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def storage_status(store):
    """Return sizes and counts only; never include filesystem paths or contents."""
    policy = validate_policy(getattr(store, "maintenance_policy", {}))
    with store.lock:
        try:
            disk = shutil.disk_usage(store.directory)
            database_bytes = 0
            for suffix in ("", "-wal", "-shm", "-journal"):
                path = store.directory / ("bridge.sqlite3" + suffix)
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise BridgeError("unsafe_database_file")
                database_bytes += info.st_size
        except OSError:
            raise BridgeError("storage_unavailable") from None
        page_size = store.db.execute("PRAGMA page_size").fetchone()[0]
        page_count = store.db.execute("PRAGMA page_count").fetchone()[0]
        free_pages = store.db.execute("PRAGMA freelist_count").fetchone()[0]
        tables = ("messages", "replies", "attachments", "retired_messages",
                  "message_timeline", "deliveries", "completed_uploads")
        rows = {name: store.db.execute("SELECT count(*) FROM " + name).fetchone()[0]
                for name in tables if _table(store, name)}
        attachment_bytes = store.db.execute(
            "SELECT coalesce(sum(length(data)),0) FROM attachments"
        ).fetchone()[0]
    warnings = []
    if disk.free < policy["disk_warning_mib"] * 1048576:
        warnings.append("disk_space_low")
    if database_bytes > policy["database_warning_mib"] * 1048576:
        warnings.append("database_large")
    return {
        "database_bytes": database_bytes,
        "database_allocated_bytes": page_count * page_size,
        "database_reusable_bytes": free_pages * page_size,
        "attachment_bytes": attachment_bytes,
        "disk_free_bytes": disk.free,
        "rows": rows,
        "warnings": warnings,
        "history_days": policy["history_days"],
        "automatic_history_cleanup": policy["history_days"] > 0,
    }


def _candidates(store, cutoff):
    # Keep the highest rowid so a new message cannot reuse subscription watermarks.
    # Keep the current context and any unresolved outgoing work, including failures.
    event_filter = ""
    if _table(store, "deliveries"):
        event_filter = """AND NOT EXISTS (SELECT 1 FROM deliveries d
            WHERE d.message_id=m.id AND d.status IN ('pending','retry','sending'))"""
    rows = store.db.execute("""SELECT m.id FROM messages m
        WHERE EXISTS (SELECT 1 FROM message_work w WHERE w.message_id=m.id AND w.state='completed') AND m.created_at<?
        AND m.rowid!=(SELECT max(rowid) FROM messages)
        AND m.id NOT IN (SELECT id FROM messages c WHERE c.context_token IS NOT NULL
            AND c.rowid=(SELECT rowid FROM messages c2 WHERE c2.account=c.account
                AND c2.peer=c.peer AND c2.context_token IS NOT NULL
                ORDER BY c2.created_at DESC,c2.rowid DESC LIMIT 1))
        AND EXISTS (SELECT 1 FROM replies r WHERE r.message_id=m.id AND r.is_notice=0)
        AND NOT EXISTS (SELECT 1 FROM replies r WHERE r.message_id=m.id
            AND (r.status!='accepted' OR r.created_at>=?))
        AND NOT EXISTS (SELECT 1 FROM processing p WHERE p.message_id=m.id AND p.expires>?)
        AND NOT EXISTS (SELECT 1 FROM message_timeline t WHERE t.message_id=m.id AND t.at>=?)
        AND NOT EXISTS (SELECT 1 FROM attachments a WHERE a.message_id=m.id AND
            (a.status IN ('pending','downloading')
             OR EXISTS(SELECT 1 FROM attachment_links l WHERE l.attachment_id=a.id AND l.message_id!=m.id)
             OR EXISTS(SELECT 1 FROM replies r WHERE r.attachment_id=a.id AND r.message_id!=m.id)))
        """ + event_filter + " ORDER BY m.rowid LIMIT ?",
        (cutoff, cutoff, store.clock(), cutoff, HISTORY_BATCH)).fetchall()
    # Filter protected owners before LIMIT: an old shared attachment must not
    # repeatedly occupy the whole batch and starve unrelated completed history.
    # A shared owner can be retired in the next batch after its users are retired.
    return [row[0] for row in rows]


def cleanup_history(store, days=None, apply=False):
    policy = validate_policy(getattr(store, "maintenance_policy", {}))
    days = policy["history_days"] if days is None else days
    if type(days) is not int or not 0 <= days <= 3650:
        raise BridgeError("invalid_retention_days")
    if not days:
        return {"applied": False, "history_days": 0, "eligible_messages": 0,
                "retired_messages": 0}
    cutoff = store.clock() - days * 86400
    with store._transaction():
        identifiers = _candidates(store, cutoff)
        result = {"applied": bool(apply), "history_days": days,
                  "eligible_messages": len(identifiers), "retired_messages": 0,
                  "batch_limit": HISTORY_BATCH}
        if not apply:
            return result
        # Remove all users in the batch before deleting shared attachment owners.
        for mid in identifiers:
            store.db.execute("INSERT OR IGNORE INTO retired_messages VALUES(?,?)",
                             (mid, store.clock()))
            store.db.execute("DELETE FROM reply_parts WHERE reply_id IN "
                             "(SELECT client_id FROM replies WHERE message_id=?)", (mid,))
            store.db.execute("DELETE FROM replies WHERE message_id=?", (mid,))
            store.db.execute("DELETE FROM attachment_links WHERE message_id=?", (mid,))
        for mid in identifiers:
            # Candidate selection has already excluded owners with retained users.
            store.db.execute("UPDATE sent_quote_cache SET attachment_id=NULL WHERE "
                             "attachment_id IN (SELECT id FROM attachments WHERE message_id=?)", (mid,))
            store.db.execute("DELETE FROM completed_uploads WHERE attachment_id IN "
                             "(SELECT id FROM attachments WHERE message_id=?)", (mid,))
            store.db.execute("DELETE FROM attachments WHERE message_id=?", (mid,))
            for table in ("message_timeline", "tool_calls", "processing", "message_aliases", "message_quotes"):
                store.db.execute("DELETE FROM " + table + " WHERE message_id=?", (mid,))
            store.db.execute("UPDATE message_quotes SET resolved_message_id=NULL,status='cache_expired' "
                             "WHERE resolved_message_id=?", (mid,))
            if _table(store, "deliveries"):
                store.db.execute("DELETE FROM deliveries WHERE message_id=?", (mid,))
            store.db.execute("DELETE FROM messages WHERE id=?", (mid,))
        # Outbound quotes lack a historical timestamp in old databases. Bound this
        # auxiliary cache by count, retaining the newest entries and live media.
        store.db.execute("""DELETE FROM sent_quote_cache WHERE rowid NOT IN
            (SELECT rowid FROM sent_quote_cache ORDER BY rowid DESC LIMIT ?)
            AND (attachment_id IS NULL OR NOT EXISTS (SELECT 1 FROM replies r
                WHERE r.attachment_id=sent_quote_cache.attachment_id AND r.status!='accepted'))""",
            (QUOTE_CACHE_LIMIT,))
        store.db.execute("DELETE FROM sent_quotes")
        store.db.execute("DELETE FROM processing WHERE expires<=?", (store.clock(),))
        # Drop no-longer-usable upload receipts together with orphan metadata.
        store.db.execute("""DELETE FROM completed_uploads WHERE attachment_id IN
            (SELECT a.id FROM attachments a WHERE a.message_id IS NULL
             AND a.created_at<? AND a.status IN ('expired','failed')
             AND NOT EXISTS(SELECT 1 FROM replies r WHERE r.attachment_id=a.id)
             AND NOT EXISTS(SELECT 1 FROM attachment_links l WHERE l.attachment_id=a.id)
             AND NOT EXISTS(SELECT 1 FROM sent_quote_cache q WHERE q.attachment_id=a.id))""", (cutoff,))
        store.db.execute("""DELETE FROM attachments WHERE message_id IS NULL
            AND created_at<? AND status IN ('expired','failed')
            AND NOT EXISTS(SELECT 1 FROM replies r WHERE r.attachment_id=attachments.id)
            AND NOT EXISTS(SELECT 1 FROM attachment_links l WHERE l.attachment_id=attachments.id)
            AND NOT EXISTS(SELECT 1 FROM sent_quote_cache q WHERE q.attachment_id=attachments.id)""", (cutoff,))
        result["retired_messages"] = len(identifiers)
        return result


def maintain(store):
    store.cleanup_media()
    return cleanup_history(store, apply=True)


def managed_backup(store, directory, keep=7):
    """Snapshot before rotation; manage only files created by this command."""
    from .operations import backup
    from .private_paths import _private_directory
    if type(keep) is not int or not 1 <= keep <= 1000:
        raise BridgeError("invalid_backup_keep")
    directory = _private_directory(directory)
    pattern = "bridge-backup-*.sqlite3"
    previous = []
    for path in directory.glob(pattern):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise BridgeError("unsafe_backup")
        previous.append((info.st_mtime_ns, path))
    name = "bridge-backup-" + str(time.time_ns()) + "-" + uuid.uuid4().hex + ".sqlite3"
    destination = directory / name
    backup(store, destination)
    # The new verified snapshot always survives, regardless of filesystem clocks.
    retained = sorted(previous, key=lambda item: (item[0], item[1].name), reverse=True)
    removed = 0
    for _, path in retained[keep - 1:]:
        path.unlink()
        removed += 1
    return {"backup_created": True, "integrity": "ok", "retained": min(len(previous) + 1, keep),
            "removed": removed}
