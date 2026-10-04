"""Best-effort owner-only typing presence, never a reply or model inference."""

import threading
import time

from .errors import BridgeError
from .transport import IlinkClient


class TypingIndicator:
    def __init__(self, store, client=None, clock=time.monotonic):
        self.store = store
        self.client = client or IlinkClient()
        self.clock = clock
        self.lock = threading.Lock()
        self.leases = {}
        self.lease_versions = {}
        self.ticket = None
        self.ticket_key = None
        self.ticket_until = 0
        self.active = None
        self.next_send = 0
        self.warm_after = 0
        self.warm_failures = 0
        self.closed = False
        self.failures = 0
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
                    'live_leases': sum(expires > self.clock() for expires in self.leases.values())}

    def _target(self):
        now = self.clock()
        with self.lock:
            self.leases = {key: value for key, value in self.leases.items() if value > now}
            ids = [] if self.closed else list(self.leases)
        return self.store.typing_target(ids)

    def sync(self, message_id):
        # The durable run is the source of truth. Replaying a deduplicated
        # progress reply cannot create a new in-memory lease.
        for mid in self.store.work_members(message_id):
            value = self.store.work_status(mid)
            with self.lock:
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
        # Incoming context only warms a cached ticket. It never shows typing
        # before dot has actually read a message and claimed processing.
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

    def tick(self):
        now = self.clock()
        target = self._target()
        if target is None:
            self._cancel()
            self.next_send = 0
            self.failures = 0
            return
        binding, context, pending = target
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
            self.ticket_until = 0
            self.failures = min(self.failures + 1, 4)
            self.next_send = self.clock() + min(30, 5 * 2 ** (self.failures - 1))

    def close(self):
        with self.lock:
            self.closed = True
            self.leases.clear()
            self.lease_versions.clear()
        self._cancel(stopping=True)
