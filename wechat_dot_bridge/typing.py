"""Best-effort owner-only typing presence, never a reply or model inference."""

import threading
import time

from .transport import IlinkClient


class TypingIndicator:
    def __init__(self, store, client=None, clock=time.monotonic):
        self.store = store
        self.client = client or IlinkClient()
        self.clock = clock
        self.lock = threading.Lock()
        self.leases = {}
        self.ticket = None
        self.ticket_key = None
        self.ticket_until = 0
        self.active = None
        self.next_send = 0
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

    def begin(self, message_id):
        # No network on the MCP thread. Repeated reads cannot extend a lease.
        with self.lock:
            if not self.closed and len(self.leases) < 128:
                self.leases.setdefault(message_id, self.clock() + 300)

    def progress(self, message_id, active=True):
        with self.lock:
            if active and not self.closed:
                self.leases[message_id] = self.clock() + 300
            else:
                self.leases.pop(message_id, None)

    def _cancel(self):
        active, self.active = self.active, None
        if active:
            try:
                self.client.send_typing(active[0], active[1], 2)
                self._observe('stop_ok')
            except Exception:
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
        self._cancel()
