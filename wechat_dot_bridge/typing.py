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
            except Exception:
                pass

    def tick(self):
        now = self.clock()
        with self.lock:
            ids = [key for key, expires in self.leases.items() if expires > now]
            self.leases = {key: value for key, value in self.leases.items() if value > now}
            if self.closed:
                ids = []
        target = self.store.typing_target(ids)
        if target is None:
            self._cancel()
            return
        binding, context, pending = target
        if now < self.next_send:
            return
        self.next_send = now + 5
        key = (binding["id"], binding["owner"], context)
        try:
            if self.ticket_key != key or now >= self.ticket_until:
                self.ticket = self.client.typing_ticket(binding, context)
                self.ticket_key, self.ticket_until = key, now + 600
            # A reply may have been queued while obtaining the ticket.
            if self.store.typing_target(list(pending)) is None:
                self._cancel()
                return
            self.active = (binding, self.ticket)
            self.client.send_typing(binding, self.ticket, 1)
        except Exception:
            # Presence failures must never fail a read or delay a queued reply.
            self._cancel()
            self.ticket_until = 0
            self.next_send = now + 30

    def close(self):
        with self.lock:
            self.closed = True
            self.leases.clear()
        self._cancel()
