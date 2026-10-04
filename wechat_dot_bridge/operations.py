"""Owner diagnostics, offline administration, and bounded reply-linked notices."""
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
        for row in store.db.execute("""SELECT m.id,m.created_at,
            EXISTS(SELECT 1 FROM tool_calls t WHERE t.message_id=m.id AND t.name='read_weixin_message' AND t.outcome='ok') AS was_read
            FROM messages m WHERE m.state='pending' AND m.created_at<?
            AND NOT EXISTS(SELECT 1 FROM processing p WHERE p.message_id=m.id AND p.expires>?)
            ORDER BY m.rowid DESC LIMIT 50""", (now-threshold,now)):
            issues.append(dict(message_id=row['id'],code='processing_stalled' if row['was_read'] else 'message_unread',age_seconds=int(now-row['created_at'])))
        for row in store.db.execute("SELECT message_id,status,error FROM replies WHERE is_notice=0 AND status IN ('failed','uncertain','context_required','auth_required') ORDER BY rowid DESC LIMIT 50"):
            issues.append(dict(message_id=row['message_id'],code='reply_'+row['status'],error=row['error']))
        binding=store.get_binding()
        if binding and binding['last_error']:
            issues.append(dict(code='channel_error',error=binding['last_error']))
        if binding and not any(s['status']=='active' and (s['refresh_before'] is None or s['refresh_before']>now) for s in subscriptions(store)):
            issues.append(dict(code='subscription_inactive'))
        return {'healthy':not issues,'issues':issues}


def backup(store, destination):
    path=Path(destination).absolute()
    for item in (path,*path.parents):
        if item.is_symlink():
            raise BridgeError('unsafe_path')
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    os.close(fd)
    with store.lock, sqlite3.connect(path) as target:
        store.db.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise BridgeError('backup_invalid')
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
            if not mid or item['code']=='reply_uncertain':
                continue
            with self.store.lock:
                message=self.store.db.execute('SELECT created_at FROM messages WHERE id=?',(mid,)).fetchone()
                if not message or now-message[0]>86400:
                    continue
                # At most one notice per inbound message, persisted across restarts.
                if self.store.db.execute('SELECT 1 FROM replies WHERE message_id=? AND is_notice=1',(mid,)).fetchone():
                    continue
            text=('【桥接状态】这条来信等待超过五分钟，dot 尚未读取。消息已保留，当前没有微信回复发送记录。'
                  if item['code']=='message_unread' else
                  '【桥接状态】dot 已读取这条来信，但尚未提交微信回复。尚未进入微信回复发送阶段，消息仍在待处理列表。'
                  if item['code']=='processing_stalled' else
                  '【桥接状态】这条来信的回复发送遇到了问题，请让原 dot 查询对应 reply_id 的发送状态。消息记录已保留。')
            try:
                self.store.queue_reply(mid,text,reply_key='bridge_status_notice',notice=True)
                break
            except BridgeError:
                pass


def restore(source,directory):
    import stat
    source=Path(source).absolute()
    for item in (source,*source.parents):
        if item.is_symlink(): raise BridgeError('unsafe_path')
    info=source.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise BridgeError('unsafe_backup')
    target=Path(directory)/'bridge.sqlite3'
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as original:
        if original.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise BridgeError('backup_invalid')
        tables={r[0] for r in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'messages','replies','binding'} <= tables: raise BridgeError('backup_invalid')
        fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        with sqlite3.connect(target) as recovered:
            original.backup(recovered)
            recovered.execute('UPDATE binding SET enabled=0')
    return {'restored':True,'started':False,'note':'Review pending and uncertain queues before enabling a sole receiver.'}
