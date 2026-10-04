"""Owner diagnostics, offline administration, and bounded reply-linked notices."""
from contextlib import closing
import os
import sqlite3
from pathlib import Path
from .errors import BridgeError


def subscriptions(store):
    with store.lock:
        if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='subscriptions'").fetchone():
            return []
        return [dict(r) for r in store.db.execute("SELECT id,status,CASE WHEN expires_at=0 THEN NULL ELSE expires_at END AS refresh_before FROM subscriptions ORDER BY rowid")]


def queue(store):
    with store.lock:
        return [dict(r) for r in store.db.execute("SELECT client_id AS reply_id,message_id,status,error,created_at FROM replies WHERE status NOT IN ('accepted') ORDER BY rowid DESC LIMIT 100")]


def diagnostics(store, threshold=300):
    now = store.clock()
    with store.lock:
        issues = []
        binding = store.get_binding()
        scope = (binding['id'], binding['owner']) if binding else ('', '')
        for row in store.db.execute("""SELECT m.id,m.created_at,
            (w.read_at IS NOT NULL OR EXISTS(SELECT 1 FROM tool_calls t WHERE t.message_id=m.id AND t.name='read_weixin_message' AND t.outcome='ok')) AS was_read
            FROM messages m JOIN message_work w ON w.message_id=m.id WHERE w.state!='completed' AND m.created_at<?
            AND m.account=? AND m.peer=?
            AND NOT (w.state='active' AND COALESCE(w.lease_until,0)>?)
            ORDER BY m.rowid DESC LIMIT 50""", (now-threshold,*scope,now)):
            issues.append(dict(message_id=row['id'],code='processing_stalled' if row['was_read'] else 'message_unread',age_seconds=int(now-row['created_at'])))
        for row in store.db.execute("""SELECT r.message_id,r.status,r.error FROM replies r
            JOIN messages m ON m.id=r.message_id WHERE r.is_notice=0
            AND r.status IN ('failed','uncertain','context_required','auth_required')
            AND m.account=? AND m.peer=? ORDER BY r.rowid DESC LIMIT 50""", scope):
            issues.append(dict(message_id=row['message_id'],code='reply_'+row['status'],error=row['error']))
        if binding and binding['last_error']:
            issues.append(dict(code='channel_error',error=binding['last_error']))
        if binding and not any(s['status']=='active' and (s['refresh_before'] is None or s['refresh_before']>now) for s in subscriptions(store)):
            issues.append(dict(code='subscription_inactive'))
        from .maintenance import storage_status
        storage = storage_status(store)
        issues.extend({'code': code} for code in storage['warnings'])
        return {'healthy':not issues,'issues':issues}


def backup(store, destination):
    from .private_paths import _private_directory
    path=Path(destination).absolute()
    for item in (path,*path.parents):
        if item.is_symlink():
            raise BridgeError('unsafe_path')
    _private_directory(path.parent)
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    created = os.fstat(fd)
    os.close(fd)
    try:
        with store.lock, closing(sqlite3.connect(path)) as target:
            store.db.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise BridgeError('backup_invalid')
    except BaseException:
        # A failed partial snapshot must never count towards future rotation.
        # Preserve any independently replaced file at the same destination.
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                path.unlink()
        except OSError:
            pass
        raise
    return {'backup_created':True,'integrity':'ok'}


class HealthMonitor:
    def __init__(self,store):
        self.store=store
        self.next_check=0

    def tick(self):
        now=self.store.clock()
        if now<self.next_check:
            return
        self.next_check=now+30
        binding=self.store.get_binding()
        if not binding or not binding['enabled'] or binding['status']!='ready':
            return
        issue=diagnostics(self.store)
        for item in issue['issues']:
            mid=item.get('message_id')
            # Waiting age is an observation, not a dot execution failure.
            # Unlike cc-connect, this bridge does not own the agent event loop.
            if not mid or item['code'] not in {'reply_failed', 'reply_context_required', 'reply_auth_required'}:
                continue
            # Respect the channel's explicit flood-control response: another
            # status message would only add pressure to the same rate limit.
            # This failure remains visible in diagnostics and send status.
            if item.get('error') == 'rate_limited':
                continue
            with self.store.lock:
                message=self.store.db.execute('SELECT created_at FROM messages WHERE id=?',(mid,)).fetchone()
                if not message or now-message[0]>86400:
                    continue
                # A historical different failure for this message must not
                # bypass the rate-limit guard on a more recent failed reply.
                if self.store.db.execute("SELECT 1 FROM replies WHERE message_id=? AND is_notice=0 AND status='failed' AND error='rate_limited'", (mid,)).fetchone():
                    continue
                # At most one notice per inbound message, persisted across restarts.
                if self.store.db.execute('SELECT 1 FROM replies WHERE message_id=? AND is_notice=1',(mid,)).fetchone():
                    continue
            text='【桥接状态】这条来信的回复发送遇到了问题，请让原 dot 查询对应 reply_id 的发送状态。消息记录已保留。'
            try:
                self.store.queue_reply(mid,text,reply_key='bridge_status_notice',notice=True)
                break
            except BridgeError:
                pass


def restore(source,directory):
    import stat
    from .private_paths import _private_directory
    directory = _private_directory(directory)
    source=Path(source).absolute()
    for item in (source,*source.parents):
        if item.is_symlink(): raise BridgeError('unsafe_path')
    info=source.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise BridgeError('unsafe_backup')
    target=Path(directory)/'bridge.sqlite3'
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as original:
        if original.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise BridgeError('backup_invalid')
        tables={r[0] for r in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'messages','replies','binding'} <= tables: raise BridgeError('backup_invalid')
        fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        with closing(sqlite3.connect(target)) as recovered, recovered:
            original.backup(recovered)
            recovered.execute('UPDATE binding SET enabled=0')
    return {'restored':True,'started':False,'note':'Review pending and uncertain queues before enabling a sole receiver.'}
