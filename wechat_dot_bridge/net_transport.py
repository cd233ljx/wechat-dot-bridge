"""Bounded HTTPS on HTTPCore, with private routing supplied by each caller.

HTTPCore owns HTTP framing, CONNECT and response parsing. The network backend
only adds public-address pinning, original TLS identity and interruptible total
deadlines, which its default backend does not provide. Nothing is retried.
"""

from dataclasses import dataclass
import ipaddress
import json
import logging
import queue
import re
import socket
import ssl
import threading
import time

import httpcore


class NetworkFailure(Exception):
    """Only fixed categories cross this transport boundary."""

    def __init__(self, category):
        self.category = category
        super().__init__(category)


class NetworkAbort(NetworkFailure):
    def __init__(self):
        super().__init__("cancelled")


_active_lock = threading.Lock()
_active_requests = set()
_cancellation = threading.Event()
DNS_SLOTS = threading.BoundedSemaphore(4)


def cancellation_token():
    with _active_lock:
        return _cancellation


def cancel_all():
    """Stop this runtime's exchanges, including requests after a DNS deadline."""
    with _active_lock:
        _cancellation.set()
        requests = list(_active_requests)
    for active in requests:
        if not active._presence_cleanup:
            active.cancel()


def reset_cancellation():
    """Begin a new local runtime after its previous workers have fully stopped."""
    global _cancellation
    with _active_lock:
        if _active_requests:
            raise NetworkAbort()
        _cancellation = threading.Event()


def wait_for_retry(seconds, token):
    if token.wait(seconds):
        raise NetworkAbort()


def proxy_port(proxy):
    if proxy is None:
        return None
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", proxy) if isinstance(proxy, str) else None
    if not match or not 1 <= int(match[1]) <= 65535:
        raise NetworkFailure("proxy_invalid")
    return int(match[1])


def public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        raise NetworkFailure("dns_blocked") from None
    if (not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified
            or (address.version == 6 and (address.ipv4_mapped is not None or address.is_site_local
                                         or address.sixtofour is not None or address.teredo is not None))):
        raise NetworkFailure("dns_blocked")
    return address


def checked_addresses(entries):
    try:
        if not isinstance(entries, (list, tuple)) or not 0 < len(entries) <= 16:
            raise NetworkFailure("dns_failed")
        result = []
        for family, kind, protocol, *tail in entries:
            endpoint = tail[-1]
            if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM:
                raise NetworkFailure("dns_failed")
            ip = public_ip(endpoint[0])
            if (ip.version == 4) != (family == socket.AF_INET) or endpoint[1] != 443:
                raise NetworkFailure("dns_failed")
            # Discard resolver-provided flow/scope identifiers for global IPv6.
            address = (str(ip), 443) if ip.version == 4 else (str(ip), 443, 0, 0)
            item = (family, kind, socket.IPPROTO_TCP, address)
            if item not in result:
                result.append(item)
        return result
    except NetworkFailure:
        raise
    except (ValueError, TypeError, IndexError, OverflowError):
        raise NetworkFailure("dns_failed") from None


def resolve_public_addresses(host, timeout, slots):
    """Bound libc DNS calls without accumulating unlimited stuck resolver threads."""
    return _resolve_public_addresses(host, timeout, slots, cancellation_token())


def _resolve_public_addresses(host, timeout, slots, token, *, deadline=None):
    """The private resolver also serves the restricted presence-stop cleanup."""
    if token.is_set():
        raise NetworkAbort()
    deadline = time.monotonic() + timeout if deadline is None else deadline
    if time.monotonic() >= deadline:
        raise NetworkFailure("timeout")
    if not slots.acquire(blocking=False):
        raise NetworkFailure("dns_failed")
    resolved = queue.Queue(maxsize=1)

    def resolve():
        try:
            entries = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            resolved.put((True, entries))
        except Exception:
            resolved.put((False, None))
        finally:
            slots.release()

    try:
        threading.Thread(target=resolve, name="bridge-dns", daemon=True).start()
    except (RuntimeError, OSError):
        slots.release()
        raise NetworkFailure("dns_failed") from None
    while True:
        if token.is_set():
            raise NetworkAbort()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkFailure("timeout")
        try:
            ok, entries = resolved.get(timeout=min(0.1, remaining))
            break
        except queue.Empty:
            continue
    if token.is_set():
        raise NetworkAbort()
    if time.monotonic() >= deadline:
        raise NetworkFailure("timeout")
    if not ok:
        raise NetworkFailure("dns_failed")
    return checked_addresses(entries)


_private_request = threading.local()


class _NoPrivateTrace(logging.Filter):
    def filter(self, record):
        # HTTPCore DEBUG traces include response headers. Suppress them only for
        # this bridge's private exchanges, including library exception details.
        return not getattr(_private_request, "active", False)


for _logger_name in ("httpcore.connection", "httpcore.http11", "httpcore.proxy"):
    logging.getLogger(_logger_name).addFilter(_NoPrivateTrace())


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, sock, backend):
        self.sock, self.backend = sock, backend

    def read(self, max_bytes, timeout=None):
        self.sock.settimeout(self.backend.remaining(timeout))
        return self.sock.recv(max_bytes)

    def write(self, buffer, timeout=None):
        self.sock.settimeout(self.backend.remaining(timeout))
        self.sock.sendall(buffer)

    def close(self):
        self.backend.release(self.sock)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        numeric = self.backend.addresses[0][3][0]
        if server_hostname not in (self.backend.hostname, numeric, "[" + numeric + "]"):
            raise NetworkFailure("tls")
        # HTTPProxy 1.0.9 ignores the sni_hostname extension after CONNECT.
        # The private backend restores the caller-authorized TLS identity.
        server_hostname = self.backend.hostname
        self.sock.settimeout(self.backend.remaining(timeout))
        try:
            tls = ssl_context.wrap_socket(self.sock, server_hostname=server_hostname,
                                          do_handshake_on_connect=False)
        except BaseException:
            self.close()
            raise
        # Publish before handshake. Cancellation in wrap_socket's descriptor
        # detach window is detected at registration and closes the new socket.
        self.backend.replace(self.sock, tls)
        stream = _DeadlineStream(tls, self.backend)
        try:
            tls.settimeout(self.backend.remaining(timeout))
            tls.do_handshake()
            self.backend.remaining(timeout)
            return stream
        except BaseException:
            stream.close()
            raise

    def get_extra_info(self, info):
        if info == "ssl_object":
            return getattr(self.sock, "_sslobj", None)
        if info == "socket":
            return self.sock
        return None


class _PinnedBackend(httpcore.NetworkBackend):
    def __init__(self, hostname, addresses, proxy, timeout):
        self.hostname, self.addresses = hostname, checked_addresses(addresses)
        self.proxy_port = proxy_port(proxy)
        self.deadline = time.monotonic() + timeout
        self.expired = threading.Event()
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.sockets = set()

    def remaining(self, timeout=None):
        if self.cancelled.is_set():
            raise NetworkAbort()
        remaining = self.deadline - time.monotonic()
        if self.expired.is_set() or remaining <= 0:
            raise TimeoutError()
        return remaining if timeout is None else min(remaining, timeout)

    def register(self, sock):
        with self.lock:
            self.sockets.add(sock)
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            self.release(sock)
            raise TimeoutError()

    def replace(self, raw, tls):
        with self.lock:
            self.sockets.discard(raw)
            self.sockets.add(tls)
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            self.release(tls)
            raise TimeoutError()

    def release(self, sock):
        with self.lock:
            self.sockets.discard(sock)
        try:
            sock.close()
        except OSError:
            pass

    def abort(self, cancelled=False):
        if cancelled:
            self.cancelled.set()
        self.expired.set()
        with self.lock:
            sockets = list(self.sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.release(sock)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if self.proxy_port is not None:
            if host != "127.0.0.1" or port != self.proxy_port:
                raise NetworkFailure("network")
            addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (host, port))]
        else:
            if host != self.addresses[0][3][0] or port != 443:
                raise NetworkFailure("network")
            addresses = self.addresses
        # Address fallback only occurs inside connect_tcp before any HTTP bytes.
        # Neither a TLS failure nor a completed CONNECT/application write retries.
        for family, kind, protocol, address in addresses:
            raw = socket.socket(family, kind, protocol)
            self.register(raw)
            try:
                raw.settimeout(self.remaining(timeout))
                raw.connect(address)
                self.remaining(timeout)
                return _DeadlineStream(raw, self)
            except TimeoutError:
                self.release(raw)
                raise
            except OSError:
                self.release(raw)
        raise NetworkFailure("network")


@dataclass(frozen=True, repr=False)
class NetworkResponse:
    status: int
    body: bytes
    headers: tuple

    def header(self, name):
        target = name.lower().encode("ascii")
        for key, value in self.headers:
            if key.lower() == target:
                return value.decode("latin-1")
        return None


class PinnedRequest:
    """One cancellable, bounded exchange; created after caller authorization/DNS."""

    def __init__(self, hostname, addresses, timeout, proxy=None):
        self.backend = _PinnedBackend(hostname, addresses, proxy, timeout)
        self.timeout, self.hostname, self.proxy = timeout, hostname, proxy
        self.cancellation = cancellation_token()
        self._presence_cleanup = False

    def cancel(self):
        self.backend.abort(cancelled=True)

    def execute(self, method, target, body, headers, max_response, body_statuses=None):
        timer = threading.Timer(self.timeout, self.backend.abort)
        timer.daemon = True
        timer.start()
        previous = getattr(_private_request, "active", False)
        _private_request.active = True
        with _active_lock:
            _active_requests.add(self)
        try:
            if self.cancellation.is_set():
                raise NetworkAbort()
            self.backend.remaining()
            numeric = self.backend.addresses[0][3][0]
            # HTTPCore builds CONNECT from the origin; IPv6 needs brackets.
            wire_host = "[" + numeric + "]" if ":" in numeric and self.proxy else numeric
            url = httpcore.URL(scheme=b"https", host=wire_host.encode("ascii"), port=443,
                               target=target.encode("ascii"))
            request_headers = {key: value for key, value in headers.items() if key.lower() != "host"}
            request_headers["Host"] = self.hostname
            request_headers["Accept-Encoding"] = "identity"
            kwargs = {"ssl_context": ssl.create_default_context(), "network_backend": self.backend,
                      "max_connections": 1, "max_keepalive_connections": 0, "http2": False, "retries": 0}
            pool = httpcore.HTTPProxy(self.proxy, **kwargs) if self.proxy else httpcore.ConnectionPool(**kwargs)
            with pool, pool.stream(method, url, headers=request_headers, content=body,
                                   extensions={"sni_hostname": self.hostname,
                                               "timeout": {key: self.timeout for key in ("connect", "read", "write", "pool")}}) as response:
                data = bytearray()
                if body_statuses is None or response.status in body_statuses:
                    for key, value in response.headers:
                        if key.lower() == b"content-length" and (not value.isdigit() or int(value) > max_response):
                            raise NetworkFailure("response_too_large")
                    for chunk in response.iter_stream():
                        self.backend.remaining()
                        if len(data) + len(chunk) > max_response:
                            raise NetworkFailure("response_too_large")
                        data.extend(chunk)
                self.backend.remaining()
                return NetworkResponse(response.status, bytes(data), tuple(response.headers))
        except NetworkFailure:
            raise
        except (TimeoutError, httpcore.TimeoutException):
            if self.backend.cancelled.is_set():
                raise NetworkAbort() from None
            raise NetworkFailure("timeout") from None
        except ssl.SSLError:
            if self.backend.cancelled.is_set():
                raise NetworkAbort() from None
            raise NetworkFailure("timeout" if self.backend.expired.is_set() else "tls") from None
        except (OSError, httpcore.NetworkError, httpcore.ProtocolError, httpcore.ProxyError,
                ValueError, TypeError, UnicodeError):
            if self.backend.cancelled.is_set():
                raise NetworkAbort() from None
            raise NetworkFailure("timeout" if self.backend.expired.is_set() else "network") from None
        finally:
            _private_request.active = previous
            with _active_lock:
                _active_requests.discard(self)
            timer.cancel()
            with self.backend.lock:
                sockets = list(self.backend.sockets)
            for sock in sockets:
                self.backend.release(sock)


def request(hostname, addresses, method, target, body, headers, *, timeout,
            max_response, proxy=None, body_statuses=None):
    return PinnedRequest(hostname, addresses, timeout, proxy).execute(
        method, target, body, headers, max_response, body_statuses)


def presence_stop(hostname, body, headers):
    """Only a shutdown typing-stop gets an independent, total three-second budget.

    This is a constrained operation rather than a cancellation override on the
    general request API: method, endpoint, status, payload shape and Tencent
    hostname are checked here. Business requests remain under cancel_all().
    """
    try:
        value = json.loads(body)
        if (not isinstance(hostname, str) or not hostname.endswith('.weixin.qq.com')
                or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', hostname)
                or '..' in hostname or not isinstance(body, bytes) or len(body) > 32768
                or not isinstance(value, dict)
                or set(value) != {'ilink_user_id', 'typing_ticket', 'status', 'base_info'}
                or type(value['status']) is not int or value['status'] != 2
                or not isinstance(value['ilink_user_id'], str) or not value['ilink_user_id']
                or not isinstance(value['typing_ticket'], str) or not value['typing_ticket']):
            raise NetworkFailure('presence_stop_invalid')
    except NetworkFailure:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NetworkFailure('presence_stop_invalid') from None
    deadline = time.monotonic() + 3
    cleanup_signal = threading.Event()
    addresses = _resolve_public_addresses(hostname, deadline - time.monotonic(), DNS_SLOTS,
                                         cleanup_signal, deadline=deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NetworkFailure('timeout')
    exchange = PinnedRequest(hostname, addresses, remaining)
    # This private capability exists only after validating the fixed stop
    # operation. Neither request() nor IlinkClient._post accepts an override.
    exchange.cancellation = cleanup_signal
    exchange._presence_cleanup = True
    exchange.backend.deadline = deadline
    return exchange.execute('POST', '/ilink/bot/sendtyping', body, headers, 1024 * 1024, {200})
