"""Durable owner processing, independent from notifications and send acceptance."""

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from sqlite3 import Connection, Row

from .errors import BridgeError

LEASE_SECONDS = 300
UNFINISHED = ('waiting', 'active', 'interrupted', 'failed')


class WorkStore:
    """Mixin sharing Store's owner guards, connection, lock and transaction.

    Attachment ownership comes from the same AttachmentStore composition.
    Helpers without a transaction wrapper participate in the host transaction.
    """

    if TYPE_CHECKING:
        db: Connection
        lock: AbstractContextManager[object]
        clock: Callable[[], float]

        def _binding(self) -> Row: ...
        def _message(self, message_id: str, binding: Row) -> Row: ...
        def _attachment(self, attachment_id: str) -> Row: ...
        def _transaction(self) -> AbstractContextManager[None]: ...

    def _ensure_work(self, message_id):
        self.db.execute("""INSERT OR IGNORE INTO message_work
            (message_id,run_id,revision,state,updated_at)
            VALUES(?,?,1,'waiting',?)""", (message_id, 'work_' + uuid.uuid4().hex, self.clock()))
        return self.db.execute('SELECT * FROM message_work WHERE message_id=?', (message_id,)).fetchone()

    def _work_value(self, row):
        state = row['state']
        if state == 'active' and (row['lease_until'] or 0) <= self.clock():
            state = 'interrupted'
        return {'processing_id': row['run_id'], 'revision': row['revision'],
                'state': state, 'expires_at': row['lease_until']}

    def work_status(self, message_id):
        with self.lock:
            self._message(message_id, self._binding())
            row = self.db.execute('SELECT * FROM message_work WHERE message_id=?', (message_id,)).fetchone()
            return self._work_value(row) if row else None

    def _start_work(self, message_id):
        row = self._ensure_work(message_id)
        if row['state'] == 'completed':
            return self._work_value(row)
        now = self.clock()
        if row['state'] != 'active' or (row['lease_until'] or 0) <= now:
            # A resumed reader gets a new generation. Late writers from the old
            # generation cannot renew or complete the new processing attempt.
            run_id = 'work_' + uuid.uuid4().hex
            self.db.execute("""UPDATE message_work SET run_id=?,revision=revision+1,
                state='active',lease_until=?,updated_at=?,read_at=COALESCE(read_at,?)
                WHERE run_id=? AND state!='completed'""",
                (run_id, now + LEASE_SECONDS, now, now, row['run_id']))
            # A resumed grouped run updates every member, including the legacy
            # processing mirror used by existing operational diagnostics.
            self.db.execute("""INSERT INTO processing(message_id,expires)
                SELECT message_id,lease_until FROM message_work WHERE run_id=?
                ON CONFLICT(message_id) DO UPDATE SET expires=excluded.expires""", (run_id,))
        else:
            # Reading again is not a heartbeat; leave the finite lease intact.
            self.db.execute('UPDATE message_work SET read_at=COALESCE(read_at,?) WHERE message_id=?', (now, message_id))
        current = self.db.execute("SELECT lease_until FROM message_work WHERE message_id=?", (message_id,)).fetchone()[0]
        self.db.execute('INSERT INTO processing VALUES(?,?) ON CONFLICT(message_id) DO UPDATE SET expires=excluded.expires',
                        (message_id, current))
        return self._work_value(self.db.execute('SELECT * FROM message_work WHERE message_id=?', (message_id,)).fetchone())

    def begin_work(self, message_id):
        with self._transaction():
            self._message(message_id, self._binding())
            return self._start_work(message_id)

    def work_members(self, message_id):
        with self.lock:
            self._message(message_id, self._binding())
            row = self.db.execute('SELECT run_id FROM message_work WHERE message_id=?', (message_id,)).fetchone()
            return [r[0] for r in self.db.execute('SELECT message_id FROM message_work WHERE run_id=?', (row[0],))] if row else []

    def _work_members(self, message_id, message_ids=None):
        ids = [message_id] if message_ids is None else message_ids
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 128 or
                any(not isinstance(mid, str) for mid in ids) or len(set(ids)) != len(ids)
                or message_id not in ids):
            raise BridgeError('invalid_processing_messages')
        binding = self._binding()
        for mid in ids:
            self._message(mid, binding)
            row = self._ensure_work(mid)
            if mid != message_id and row['read_at'] is None:
                raise BridgeError('processing_message_unread')
        return ids

    def _transition_work(self, message_id, *, active, completed=False, message_ids=None,
                         processing_id=None, revision=None, new_reply=False):
        if type(active) is not bool or type(completed) is not bool or (active and completed):
            raise BridgeError('invalid_processing_state')
        if (processing_id is None) != (revision is None):
            raise BridgeError('processing_generation_required')
        ids = self._work_members(message_id, message_ids)
        row = self._ensure_work(message_id)
        if processing_id is not None:
            if (not isinstance(processing_id, str) or type(revision) is not int or
                    processing_id != row['run_id'] or revision != row['revision']):
                raise BridgeError('processing_generation_stale')
            if row['state'] == 'completed' and (active or new_reply):
                raise BridgeError('processing_already_completed')
            if row['state'] in ('interrupted', 'failed'):
                raise BridgeError('processing_generation_inactive')
            if row['state'] == 'active' and (row['lease_until'] or 0) <= self.clock():
                raise BridgeError('processing_lease_expired')
        members = [r[0] for r in self.db.execute('SELECT message_id FROM message_work WHERE run_id=?', (row['run_id'],))]
        if row['state'] == 'completed' and not new_reply:
            # Completion is sticky even for legacy clients without a version.
            # A late heartbeat cannot restart typing; a late stop/completion is
            # idempotent only for the already completed membership.
            if active or (message_ids is not None and set(ids) != set(members)):
                raise BridgeError('processing_already_completed')
            return self._work_value(row)
        now = self.clock()
        # An explicit group owns exactly the supplied, read messages. Never use
        # "recent reply in the chat" to silently consume another pending file.
        placeholders = ','.join('?' for _ in ids)
        if message_ids is None:
            # A fresh additional reply after completion starts with its own
            # primary message. Previous completed group members stay finished
            # unless the new reply explicitly names them again.
            ids = [message_id] if new_reply and row['state'] == 'completed' else members
            placeholders = ','.join('?' for _ in ids)
        rows = self.db.execute('SELECT * FROM message_work WHERE message_id IN (' + placeholders + ')', ids).fetchall()
        same_run = all(r['run_id'] == row['run_id'] for r in rows)
        same_members = set(ids) == set(members)
        generation = max(r['revision'] for r in rows)
        run_id = row['run_id']
        if (not same_run or not same_members or (new_reply and row['state'] == 'completed')
                or (active and (row['state'] != 'active' or (row['lease_until'] or 0) <= now))):
            run_id, generation = 'work_' + uuid.uuid4().hex, generation + 1
            # Changing a group's membership retires every source generation,
            # including tokens held by members excluded from the new scope.
            # Keep each source group's remainder together without renewing its
            # lease or changing completed/interrupted work into active work.
            for previous_run in {r['run_id'] for r in rows}:
                remainder = self.db.execute(
                    'SELECT 1 FROM message_work WHERE run_id=? AND message_id NOT IN (' + placeholders + ') LIMIT 1',
                    (previous_run, *ids)).fetchone()
                if remainder:
                    previous_revision = self.db.execute(
                        'SELECT max(revision) FROM message_work WHERE run_id=?', (previous_run,)).fetchone()[0]
                    self.db.execute(
                        'UPDATE message_work SET run_id=?,revision=?,updated_at=? WHERE run_id=? AND message_id NOT IN (' + placeholders + ')',
                        ('work_' + uuid.uuid4().hex, previous_revision + 1, now, previous_run, *ids))
        state = 'active' if active else 'completed' if completed else 'interrupted'
        for mid in ids:
            # A completed secondary message must not be reopened by a batch
            # heartbeat. A fresh per-message action can explicitly start again.
            current = self._ensure_work(mid)
            if active and not new_reply and mid != message_id and current['state'] == 'completed':
                raise BridgeError('processing_already_completed')
            self.db.execute("""UPDATE message_work SET run_id=?,revision=?,state=?,
                lease_until=?,updated_at=? WHERE message_id=?""",
                (run_id, generation, state, now + LEASE_SECONDS if active else None, now, mid))
            if active:
                self.db.execute('INSERT INTO processing VALUES(?,?) ON CONFLICT(message_id) DO UPDATE SET expires=excluded.expires',
                                (mid, now + LEASE_SECONDS))
            else:
                self.db.execute('DELETE FROM processing WHERE message_id=?', (mid,))
        return self._work_value(self._ensure_work(message_id))

    def report_progress(self, message_id, active=True, *, completed=False, message_ids=None,
                        processing_id=None, revision=None):
        with self._transaction():
            value = self._transition_work(message_id, active=active, completed=completed,
                message_ids=message_ids, processing_id=processing_id, revision=revision)
        return {'message_id': message_id, 'processing': active, 'work': value}

    def touch_attachment_work(self, attachment_id):
        """A successful read renews only an already active owner processing run."""
        with self._transaction():
            attachment = self._attachment(attachment_id)
            mids = [r[0] for r in self.db.execute('SELECT message_id FROM attachment_links WHERE attachment_id=?', (attachment_id,))]
            if attachment['message_id']:
                mids.append(attachment['message_id'])
            touched = []
            for mid in dict.fromkeys(mids):
                row = self.db.execute('SELECT * FROM message_work WHERE message_id=?', (mid,)).fetchone()
                if row and row['state'] == 'active' and (row['lease_until'] or 0) > self.clock():
                    self._transition_work(mid, active=True)
                    touched.extend(r[0] for r in self.db.execute('SELECT message_id FROM message_work WHERE run_id=?', (row['run_id'],)))
            return list(dict.fromkeys(touched))

    def recover_work(self):
        """Process restarts preserve unresolved work without pretending execution."""
        with self._transaction():
            self.db.execute("UPDATE message_work SET state='interrupted',lease_until=NULL,updated_at=? WHERE state='active'", (self.clock(),))
            self.db.execute('DELETE FROM processing')
