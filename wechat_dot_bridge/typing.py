"""Best-effort owner-only typing presence, never a reply or model inference."""

import threading
import time

from .errors import BridgeError
from .transport import IlinkClient


WAITING_SECONDS = 300
MAX_CANDIDATES = 128


class TypingIndicator:
    def __init__(self, store, client=None, clock=time.monotonic):
        self.store = store
        self.client = client or IlinkClient()
        self.clock = clock
        self.lock = threading.Lock()
        self.leases = {}
        self.lease_versions = {}
        self.waiting = {}
        self.waiting_seen = {}
        # CC Connect core/dedup.go excludes history before process startup,
        # allowing a two-second race at startup. Waiting itself is never durable.
        self.started_at = self.store.clock()
        self.ticket = None
        self.ticket_key = None
        self.ticket_until = 0
        self.active = None
        self.next_send = 0
        self.warm_after = 0
        self.warm_failures = 0
        self.closed = False
        self.failures = 0
        self.run_key = None
        self.outcomes = {}
        self.last_error = None
        self.last_attempt_at = None

    def _observe(self, outcome):
        # Fixed labels only: never retain exception text, tickets or routes.
        with self.lock:
            self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
            self.last_attempt_at = self.store.clock()
            if outcome.endswith('_failed'):
                self.last_error = outcome
            elif outcome == 'start_ok':
                self.last_error = None

    def status(self):
        with self.lock:
            return {'outcomes': dict(self.outcomes), 'last_error': self.last_error,
                    'last_attempt_at': self.last_attempt_at,
                    'live_leases': sum(expires > self.clock() for expires in self.leases.values()),
                    'waiting_leases': sum(expires > self.clock() for expires in self.waiting.values())}

    def received(self, message_ids):
        """Commit hook: finite presence while the bridge waits for dot to read."""
        if (not isinstance(message_ids, (tuple, list)) or len(message_ids) > 1000
                or any(not isinstance(mid, str) for mid in message_ids)):
            return
        now = self.clock()
        with self.lock:
            if self.closed:
                return
            self.waiting = {mid: expires for mid, expires in self.waiting.items() if expires > now}
            # Remember an expired unread candidate until its durable waiting
            # state ends, so a repeated hook cannot renew its five-minute lease.
            seen_waiting = self.store._waiting_typing_ids(tuple(self.waiting_seen))
            self.waiting_seen = {mid: expires for mid, expires in self.waiting_seen.items() if mid in seen_waiting}
            previous = tuple(self.waiting)
            candidates = tuple(reversed(dict.fromkeys(mid for mid in message_ids if mid not in self.waiting_seen)))
            # Validate state after commit: a reader may already have claimed the
            # message before this callback executes. Neither text nor media is
            # read; serial registration prevents concurrent hooks losing leases.
            valid_previous = self.store._waiting_typing_ids(previous)
            fresh_since = self.started_at - 2
            selected = []
            skipped_history = 0
            for offset in range(0, len(candidates), MAX_CANDIDATES):
                chunk = candidates[offset:offset + MAX_CANDIDATES]
                valid = self.store._waiting_typing_ids(chunk)
                fresh = self.store._waiting_typing_ids(chunk, created_since=fresh_since)
                skipped_history += len(valid - fresh)
                selected.extend(mid for mid in chunk if mid in fresh)
                if len(selected) >= MAX_CANDIDATES:
                    break
            if skipped_history:
                self.outcomes['waiting_history_skipped'] = self.outcomes.get('waiting_history_skipped', 0) + skipped_history
            self.waiting = {mid: expires for mid, expires in self.waiting.items()
                            if expires > now and mid in valid_previous}
            for mid in selected[:MAX_CANDIDATES]:
                if mid in self.waiting_seen or len(self.waiting) >= MAX_CANDIDATES:
                    continue
                self.waiting[mid] = now + WAITING_SECONDS
                if len(self.waiting_seen) >= MAX_CANDIDATES:
                    self.waiting_seen.pop(next(iter(self.waiting_seen)))
                self.waiting_seen[mid] = now + WAITING_SECONDS + 2

    def _target(self):
        now = self.clock()
        with self.lock:
            self.leases = {key: value for key, value in self.leases.items() if value > now}
            self.waiting = {key: value for key, value in self.waiting.items() if value > now}
            ids = [] if self.closed else list(self.leases)[:MAX_CANDIDATES]
            waiting_ids = () if self.closed else tuple(self.waiting)
        # Each query is bounded to 128 candidates. Actual processing takes
        # priority; waiting does not claim or renew a durable work generation.
        target = self.store.typing_target(ids)
        if target is not None:
            return target
        valid_waiting = self.store._waiting_typing_ids(waiting_ids)
        with self.lock:
            self.waiting = {mid: expires for mid, expires in self.waiting.items()
                            if expires > self.clock() and (mid not in waiting_ids or mid in valid_waiting)}
            current_ids = [] if self.closed else list(self.leases)[:MAX_CANDIDATES]
            waiting_ids = () if self.closed else tuple(self.waiting)
        # A committed arrival/read can race the old snapshot's SQL validation.
        # Keep new waiting members and recheck newly synchronized active work.
        if current_ids != ids:
            target = self.store.typing_target(current_ids)
            if target is not None:
                return target
        return self.store.typing_target((), waiting_ids=waiting_ids)

    def sync(self, message_id):
        # The durable run is the source of truth. Replaying a deduplicated
        # progress reply cannot create a new in-memory lease.
        for mid in self.store.work_members(message_id):
            value = self.store.work_status(mid)
            with self.lock:
                self.waiting.pop(mid, None)
                if not self.closed and value and value['state'] == 'active':
                    version = (value['processing_id'], value['revision'], value['expires_at'])
                    if self.lease_versions.get(mid) != version:
                        remaining = max(0, value['expires_at'] - self.store.clock())
                        self.leases[mid] = self.clock() + remaining
                        self.lease_versions[mid] = version
                else:
                    self.leases.pop(mid, None)
                    self.lease_versions.pop(mid, None)

    def begin(self, message_id):
        try:
            self.store.begin_work(message_id)
            self.sync(message_id)
        except BridgeError:
            return

    def progress(self, message_id, active=True):
        self.store.report_progress(message_id, active)
        self.sync(message_id)

    def prewarm(self):
        # This ticket cache warmup does not itself display presence. Only a
        # committed fresh waiting lease or dot's durable active run can do so.
        if self.closed or self._target() is not None:
            return
        binding = self.store.get_binding()
        if not binding or not binding['enabled'] or binding['status'] != 'ready':
            return
        with self.store.lock:
            context = self.store._latest_context(binding)
        if not context:
            return
        now = self.clock()
        key = (binding['id'], binding['owner'], binding['base_url'])
        if self.ticket_key == key and now < self.ticket_until:
            return
        if now < self.warm_after:
            return
        try:
            self.ticket = self.client.typing_ticket(binding, context['context_token'])
            self.ticket_key, self.ticket_until = key, self.clock() + 600
            self.warm_failures = 0
            self._observe('ticket_ok')
        except Exception:
            self._observe('ticket_failed')
            self.warm_failures = min(self.warm_failures + 1, 4)
            self.warm_after = self.clock() + min(30, 5 * 2 ** (self.warm_failures - 1))

    def _cancel(self, stopping=False):
        active, self.active = self.active, None
        if active:
            try:
                if stopping:
                    self.client.stop_typing(active[0], active[1])
                else:
                    self.client.send_typing(active[0], active[1], 2)
                self._observe('stop_ok')
            except Exception as error:
                # The ordinary stop shares runtime cancellation. Preserve its
                # target when shutdown interrupts a refresh, so close() can
                # still perform the isolated, bounded presence cleanup.
                if not stopping and isinstance(error, BridgeError) and error.code == 'network_cancelled':
                    self.active = active
                self._observe('stop_failed')

    def _run_changed(self, binding, pending):
        # Lease renewal does not start a new turn. Read the durable generation
        # rather than cached lease versions: regrouping also retires excluded
        # members whose own sync() was not part of that tool call.
        generations = []
        for mid in sorted(pending):
            work = self.store.work_status(mid)
            if work and work['state'] == 'active':
                generations.append(('active', mid, work['processing_id'], work['revision']))
            elif work and work['state'] == 'waiting':
                generations.append(('waiting', mid))
        key = ((binding['id'], binding['owner'], binding['base_url']), tuple(generations))
        changed = key != self.run_key
        self.run_key = key
        return changed

    def tick(self):
        now = self.clock()
        target = self._target()
        if target is None:
            self._cancel()
            self.next_send = 0
            self.failures = 0
            self.run_key = None
            return
        binding, context, pending = target
        if self._run_changed(binding, pending):
            # A fresh waiting member or read turn can recover from the old turn's
            # failure. Successful ongoing owner presence keeps its cadence.
            if self.failures or self.active is None:
                self.next_send = 0
            self.failures = 0
        if now < self.next_send:
            return
        self.next_send = now + 5
        # cc-connect caches a ticket per peer, not per inbound context token.
        key = (binding["id"], binding["owner"], binding["base_url"])
        stage = 'ticket'
        try:
            if self.ticket_key != key or now >= self.ticket_until:
                self.ticket = self.client.typing_ticket(binding, context)
                self.ticket_key, self.ticket_until = key, self.clock() + 600
                self._observe('ticket_ok')
            # Recheck both the lease and the store after a blocking request.
            if self._target() is None:
                self._cancel()
                return
            stage = 'start'
            self.active = (binding, self.ticket)
            self.client.send_typing(binding, self.ticket, 1)
            self._observe('start_ok')
            self.failures = 0
        except Exception:
            # Presence failures must never fail a read or delay a queued reply.
            self._observe(stage + '_failed')
            self._cancel()
            if stage == 'ticket':
                self.ticket_until = 0
            self.failures = min(self.failures + 1, 4)
            self.next_send = self.clock() + min(30, 5 * 2 ** (self.failures - 1))

    def close(self):
        with self.lock:
            self.closed = True
            self.leases.clear()
            self.lease_versions.clear()
            self.waiting.clear()
            self.waiting_seen.clear()
        self._cancel(stopping=True)
