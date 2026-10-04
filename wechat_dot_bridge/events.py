"""Owner-scoped MCP Events with durable, bounded webhook delivery.

Protocol reference: https://developers.openai.com/plugins/build/mcp-events
No user text or Weixin routing credential is placed in event payloads.
"""

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import queue
import re
import secrets
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from .errors import BridgeError

EVENT_NAME = "weixin.message"
MAX_BODY = 262144
MAX_RESPONSE = 65536
MAX_DNS_RESPONSE = 32768
MAX_ATTEMPTS = 5
ROTATION_SECONDS = 300
VERIFICATION_SECONDS = 300
DEFAULT_TTL_SECONDS = 3600
# Zero is an internal no-expiration sentinel compatible with existing NOT NULL rows.
# Explicit ttlMs:null grants this; a local owner policy pins it until cancellation.
NO_EXPIRATION = 0
MAX_TTL_SECONDS = 86400
NETWORK_TIMEOUT = 10
DNS_TIMEOUT = 10
_DNS_SLOTS = threading.BoundedSemaphore(4)


def _proxy_port(proxy):
    """Only an explicitly configured, literal IPv4 loopback HTTP proxy is valid."""
    if proxy is None:
        return None
    if not isinstance(proxy, str):
        raise BridgeError("invalid_callback_proxy")
    matched = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", proxy)
    if not matched or not 1 <= int(matched[1]) <= 65535:
        raise BridgeError("invalid_callback_proxy")
    return int(matched[1])


def _public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        raise BridgeError("callback_dns_blocked") from None
    embedded = isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)
    if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved or embedded:
        raise BridgeError("callback_dns_blocked")
    return ip


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 100:
        raise BridgeError("invalid_signing_secret")
    try:
        decoded = base64.b64decode(secret[6:], validate=True)
    except (ValueError, TypeError):
        raise BridgeError("invalid_signing_secret") from None
    if not 24 <= len(decoded) <= 64:
        raise BridgeError("invalid_signing_secret")
    return decoded


def signed_headers(event_id, body, secret, signed_at, subscription_id, old_secret=None):
    """Standard Webhooks signs the exact bytes, with dual signatures on rotation."""
    timestamp = str(int(signed_at))
    signed = event_id.encode("ascii") + b"." + timestamp.encode("ascii") + b"." + body
    signatures = []
    for key in (secret, old_secret):
        if key:
            signature = hmac.new(_signing_key(key), signed, hashlib.sha256).digest()
            signatures.append("v1," + base64.b64encode(signature).decode("ascii"))
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": timestamp, "webhook-signature": " ".join(signatures),
            "X-MCP-Subscription-Id": subscription_id}


def _callback_parts(url, allowed_hosts):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 for c in url) or "\\" in url:
        raise BridgeError("callback_invalid")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except (ValueError, TypeError):
        raise BridgeError("callback_invalid") from None
    if (parts.scheme != "https" or not host or parts.username is not None or
            parts.password is not None or parts.fragment or port not in (None, 443) or
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) or
            ".." in host or host.endswith(".") or "%" in parts.netloc):
        raise BridgeError("callback_invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise BridgeError("callback_invalid")
    if host not in allowed_hosts:
        raise BridgeError("callback_not_allowed")
    return parts, host


def _public_addresses(host):
    # libc resolution has no Python timeout. A small bounded pool of daemon
    # resolvers keeps the delivery worker responsive even if the resolver hangs.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise BridgeError("callback_dns_failed")
    resolved = queue.Queue(maxsize=1)

    def resolve():
        try:
            resolved.put((True, socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        except Exception:
            resolved.put((False, None))
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, name="callback-dns", daemon=True).start()
    try:
        ok, entries = resolved.get(timeout=DNS_TIMEOUT)
    except queue.Empty:
        raise BridgeError("callback_timeout") from None
    if not ok:
        raise BridgeError("callback_dns_failed") from None
    addresses = []
    for family, socktype, protocol, _, address in entries:
        _public_ip(address[0])
        entry = (family, socktype, protocol, address)
        if entry not in addresses:
            addresses.append(entry)
    if not addresses or len(addresses) > 16:
        raise BridgeError("callback_dns_failed")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a previously checked IP while verifying TLS against the hostname."""

    def __init__(self, host, addresses, proxy=None):
        super().__init__(host, 443, timeout=NETWORK_TIMEOUT, context=ssl.create_default_context())
        self.addresses = addresses
        self.proxy_port = _proxy_port(proxy)
        self._request_expired = None

    def connect(self):
        deadline = time.monotonic() + NETWORK_TIMEOUT
        for family, socktype, protocol, address in self.addresses:
            ip = _public_ip(address[0])
            raw = socket.socket(socket.AF_INET if self.proxy_port else family, socktype, protocol)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                raw.settimeout(remaining)
                if self.proxy_port is None:
                    raw.connect(address)
                else:
                    # The proxy receives a numeric CONNECT target and therefore
                    # cannot re-resolve the approved callback host. TLS remains
                    # end-to-end and is verified against the original hostname.
                    numeric = str(ip)
                    target = "[" + numeric + "]" if ip.version == 6 else numeric
                    self.set_tunnel(target, 443, headers={"Host": target + ":443"})
                    raw.connect(("127.0.0.1", self.proxy_port))
                    self.sock = raw
                    try:
                        self._tunnel()
                    finally:
                        # http.client otherwise emits the numeric tunnel target
                        # as HTTP Host on the subsequent application request.
                        self._tunnel_host = None
                        self._tunnel_port = None
                        self._tunnel_headers = {}
                # Publish the TLS socket before the blocking handshake: wrapping
                # detaches raw's descriptor, which would hide it from the timer.
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host,
                                                     do_handshake_on_connect=False)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (self._request_expired is not None and self._request_expired.is_set()):
                    raise TimeoutError()
                self.sock.settimeout(remaining)
                self.sock.do_handshake()
                if (time.monotonic() >= deadline or
                        (self._request_expired is not None and self._request_expired.is_set())):
                    raise TimeoutError()
                return
            except ssl.SSLError:
                self.close()
                raw.close()
                raise
            except TimeoutError:
                self.close()
                raw.close()
                raise
            except OSError:
                self.close()
                raw.close()
        raise OSError("callback connection failed")


def _https_request(host, addresses, method, target, body, headers, proxy=None, max_response=MAX_RESPONSE):
    """One bounded TLS exchange, without redirects or ambient proxy variables."""
    connection = _PinnedHTTPSConnection(host, addresses, proxy=proxy)
    request_headers = dict(headers)
    request_headers["Host"] = host
    # A socket read timeout alone permits a peer to trickle bytes forever.
    # Shutdown establishes an overall HTTPS exchange deadline as well.
    timed_out = threading.Event()
    connection._request_expired = timed_out

    def abort():
        timed_out.set()
        sock = connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    deadline = threading.Timer(NETWORK_TIMEOUT, abort)
    deadline.daemon = True
    deadline.start()
    try:
        connection.connect()
        if timed_out.is_set():
            raise BridgeError("callback_timeout")
        connection.request(method, target, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read(max_response + 1)
        if timed_out.is_set():
            raise BridgeError("callback_timeout")
        if len(data) > max_response:
            raise BridgeError("callback_response_too_large")
        if 300 <= response.status < 400:
            raise BridgeError("callback_redirect")
        return response.status, data
    except (TimeoutError, socket.timeout):
        raise BridgeError("callback_timeout") from None
    except ssl.SSLError:
        if timed_out.is_set():
            raise BridgeError("callback_timeout") from None
        raise BridgeError("callback_tls_failed") from None
    except (OSError, ValueError, http.client.HTTPException):
        if timed_out.is_set():
            raise BridgeError("callback_timeout") from None
        raise BridgeError("callback_network_failed") from None
    finally:
        deadline.cancel()
        connection.close()


def _google_addresses(host, proxy=None):
    """Explicit Google DoH: fixed public IP, provider TLS, only the approved name."""
    _proxy_port(proxy)
    google_ip = "8.8.8.8"
    _public_ip(google_ip)
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (google_ip, 443))]
    target = "/resolve?" + urlencode({"name": host, "type": "A"})
    status, body = _https_request("dns.google", addresses, "GET", target, None,
                                  {"Accept": "application/dns-json"}, proxy, MAX_DNS_RESPONSE)
    if status != 200:
        raise BridgeError("callback_dns_failed")
    try:
        value = json.loads(body)
    except (ValueError, TypeError, UnicodeError):
        raise BridgeError("callback_dns_failed") from None
    if (not isinstance(value, dict) or type(value.get("Status")) is not int or value["Status"] != 0 or
            value.get("TC") is True or not isinstance(value.get("Answer"), list) or
            not 1 <= len(value["Answer"]) <= 16):
        raise BridgeError("callback_dns_failed")
    result = []
    for record in value["Answer"]:
        if (not isinstance(record, dict) or type(record.get("type")) is not int or
                record["type"] not in (1, 5) or not isinstance(record.get("data"), str)):
            raise BridgeError("callback_dns_failed")
        if record["type"] == 5:
            # DNS aliases may accompany the final A records. Never connect to
            # or separately resolve the alias; TLS still verifies the approved host.
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}\.?", record["data"]) or ".." in record["data"]:
                raise BridgeError("callback_dns_failed")
            continue
        ip = _public_ip(record["data"])
        if ip.version != 4:
            raise BridgeError("callback_dns_failed")
        entry = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (str(ip), 443))
        if entry not in result:
            result.append(entry)
    if not result:
        raise BridgeError("callback_dns_failed")
    return result


def secure_post(url, body, headers, allowed_hosts, proxy=None, dns="system"):
    """Pin an approved callback through direct TLS or an explicit loopback proxy."""
    # Callback authorization precedes DNS or proxy I/O. No callback path, token,
    # headers or request body is disclosed to the optional DNS provider.
    parts, host = _callback_parts(url, allowed_hosts)
    _proxy_port(proxy)
    if not isinstance(dns, str) or dns not in {"system", "google"}:
        raise BridgeError("invalid_callback_dns")
    if len(body) > MAX_BODY:
        raise BridgeError("event_too_large")
    addresses = _public_addresses(host) if dns == "system" else _google_addresses(host, proxy)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return _https_request(host, addresses, "POST", target, body, headers, proxy)


class Events:
    def __init__(self, store, allowed_hosts=(), post=None, clock=time.time, proxy=None, dns="system", recover_inflight=True):
        self.store, self.clock = store, clock
        self.allowed_hosts = frozenset(str(host).lower() for host in allowed_hosts)
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", host) or ".." in host or host.endswith(".")
               for host in self.allowed_hosts):
            raise BridgeError("invalid_callback_hosts")
        _proxy_port(proxy)
        if not isinstance(dns, str) or dns not in {"system", "google"}:
            raise BridgeError("invalid_callback_dns")
        self.post = post or (lambda url, body, headers: secure_post(url, body, headers, self.allowed_hosts, proxy, dns))
        # Serializes verification, delivery, refresh and cancellation within one owner process.
        self._network_lock = threading.RLock()
        with self.store.lock:
            self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL,
                    arguments TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
                    old_secret TEXT, rotate_until REAL, expires_at REAL NOT NULL,
                    verified_until REAL NOT NULL, status TEXT NOT NULL, start_rowid INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, body BLOB NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
                    error TEXT, UNIQUE(subscription_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS event_metadata (key TEXT PRIMARY KEY,value TEXT);
            """)
            if recover_inflight:
                self.store.db.execute("UPDATE deliveries SET status='retry' WHERE status='sending'")

    def _begin(self):
        self.store.db.execute("BEGIN IMMEDIATE")

    def _end(self):
        self.store.db.execute("COMMIT")

    def _expire(self, now):
        self.store.db.execute("UPDATE subscriptions SET status='expired' WHERE status='active' AND expires_at!=0 AND expires_at<=?", (now,))
        self.store.db.execute("""UPDATE deliveries SET status='cancelled',error='subscription_inactive'
            WHERE status IN ('pending','retry','sending') AND subscription_id IN
            (SELECT id FROM subscriptions WHERE status!='active')""")
        self.store.db.execute("UPDATE subscriptions SET old_secret=NULL,rotate_until=NULL WHERE rotate_until<=?", (now,))

    def _metadata(self, key, value):
        self.store.db.execute("INSERT INTO event_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def event_definition(self):
        return {"name": EVENT_NAME, "description": "A new message from the bound owner: text, image, file, video or voice. Read the message by its ID and process its attachment metadata; follow the owner request to reply or return attachments.",
                "delivery": ["webhook"], "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                "payloadSchema": {"type": "object", "properties": {"message_id": {"type": "string"}},
                                  "required": ["message_id"], "additionalProperties": False}}

    def _identity(self, params, with_secret):
        if not isinstance(params, dict) or set(params) - {"name", "arguments", "delivery", "cursor", "ttlMs", "_meta"}:
            raise BridgeError("invalid_params")
        if params.get("name") != EVENT_NAME or params.get("arguments", {}) != {}:
            raise BridgeError("invalid_params")
        if params.get("cursor") is not None:
            raise BridgeError("replay_not_supported")
        delivery = params.get("delivery")
        allowed = {"mode", "url", "secret"} if with_secret else {"mode", "url"}
        if not isinstance(delivery, dict) or set(delivery) - allowed or delivery.get("mode") != "webhook":
            raise BridgeError("invalid_params")
        url = delivery.get("url")
        try:
            _callback_parts(url, self.allowed_hosts)
        except BridgeError as error:
            # Record only a syntactically safe host; never persist rejected callback paths or secrets.
            if error.code == "callback_not_allowed":
                host = (urlsplit(url).hostname or "").lower()
                with self.store.lock:
                    self._metadata("observed_host", host)
                    self._metadata("last_error", error.code)
            raise
        binding = self.store.get_binding()
        if not binding:
            raise BridgeError("binding_required")
        principal = _json_bytes([binding["id"], binding["owner"]]).decode("utf-8")
        identity = {"principal": principal, "url": url, "name": EVENT_NAME, "arguments": {}}
        subscription_id = "sub_" + hashlib.sha256(_json_bytes(identity)).hexdigest()
        if with_secret:
            _signing_key(delivery.get("secret"))
        return subscription_id, principal, url, delivery.get("secret")

    def set_current_subscription_nonexpiring(self):
        """Local owner command, under the CLI worker lock; never an MCP tool.

        Changes only the currently active owner's subscription. Cancellation
        clears this policy; a different callback cannot inherit it.
        """
        binding = self.store.get_binding()
        if not binding:
            raise BridgeError("binding_required")
        principal = _json_bytes([binding["id"], binding["owner"]]).decode("utf-8")
        with self._network_lock, self.store.lock:
            self._expire(self.clock())
            rows = self.store.db.execute(
                "SELECT id FROM subscriptions WHERE status='active' AND principal=?", (principal,)).fetchall()
            if len(rows) != 1:
                raise BridgeError("active_subscription_required")
            subscription_id = rows[0][0]
            self._begin()
            try:
                self.store.db.execute("UPDATE subscriptions SET expires_at=0 WHERE id=?", (subscription_id,))
                self._metadata("nonexpiring_default:" + subscription_id, "1")
                self._end()
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
        return {"active_subscriptions": 1, "refreshBefore": None,
                "scope": "bridge_subscription", "platform_task_lifetime_verified": False}

    def subscribe(self, params):
        subscription_id, principal, url, secret = self._identity(params, True)
        with self.store.lock:
            local_policy = self.store.db.execute(
                "SELECT value FROM event_metadata WHERE key=?", ("nonexpiring_default:" + subscription_id,)).fetchone()
        default_ttl = None if local_policy and local_policy[0] == "1" else DEFAULT_TTL_SECONDS * 1000
        requested = params.get("ttlMs", default_ttl)
        if requested is not None and (type(requested) is not int or requested <= 0):
            raise BridgeError("invalid_params")
        owner_forever = bool(local_policy and local_policy[0] == "1")
        ttl = None if requested is None or owner_forever else min(MAX_TTL_SECONDS, max(60, requested / 1000))
        with self._network_lock:
            now = self.clock()
            with self.store.lock:
                self._expire(now)
                existing = self.store.db.execute("SELECT secret,old_secret,rotate_until,verified_until,status,start_rowid FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
                active = self.store.db.execute("SELECT id FROM subscriptions WHERE status='active'").fetchone()
                if active and active[0] != subscription_id:
                    raise BridgeError("subscription_conflict")
                cached = bool(existing and existing[0] == secret and existing[3] > now and existing[4] == "active")
            if not cached:
                challenge = secrets.token_urlsafe(32)
                body = _json_bytes({"type": "verification", "challenge": challenge})
                verification_id = "verify_" + secrets.token_hex(16)
                headers = signed_headers(verification_id, body, secret, now, subscription_id)
                try:
                    status, response = self.post(url, body, headers)
                    value = json.loads(response)
                except BridgeError as error:
                    with self.store.lock:
                        self._metadata("last_error", error.code if error.code.startswith("callback_") else "challenge_failed")
                    raise
                except (ValueError, TypeError, OSError):
                    with self.store.lock:
                        self._metadata("last_error", "challenge_failed")
                    raise BridgeError("challenge_failed") from None
                if (not 200 <= status < 300 or not isinstance(value, dict) or
                        not isinstance(value.get("challenge"), str) or
                        self.clock() > now + 30 or
                        not hmac.compare_digest(value["challenge"].encode("utf-8"), challenge.encode("utf-8"))):
                    with self.store.lock:
                        self._metadata("last_error", "challenge_failed")
                    raise BridgeError("challenge_failed")
            expires = NO_EXPIRATION if ttl is None else self.clock() + ttl
            with self.store.lock:
                self._begin()
                try:
                    current = self.store.db.execute("SELECT secret,old_secret,rotate_until,status,start_rowid FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
                    continuous = bool(current and current[3] == "active")
                    # An explicit refresh resumes the same application's outstanding
                    # inbox. This is not a protocol replay cursor and never resumes
                    # a message that has an immutable reply committed to it.
                    boundary = current[4] if current else self.store.db.execute("SELECT COALESCE(MAX(rowid),0) FROM messages").fetchone()[0]
                    old_secret = current[0] if continuous and current[0] != secret else (current[1] if continuous else None)
                    rotation = self.clock() + ROTATION_SECONDS if continuous and current[0] != secret else (current[2] if continuous else None)
                    if current:
                        self.store.db.execute("""UPDATE deliveries SET status='pending',attempts=0,next_attempt=?,error=NULL
                            WHERE subscription_id=? AND (status IN ('failed','cancelled','retry') OR
                            (status='acknowledged' AND ?)) AND message_id IN
                            (SELECT m.id FROM messages m WHERE m.state='pending' AND NOT EXISTS
                            (SELECT 1 FROM replies r WHERE r.message_id=m.id AND r.is_notice=0))""", (self.clock(), subscription_id, int(not continuous)))
                    self.store.db.execute("""INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,old_secret=excluded.old_secret,
                        rotate_until=excluded.rotate_until,expires_at=excluded.expires_at,verified_until=excluded.verified_until,
                        status='active',start_rowid=excluded.start_rowid""",
                        (subscription_id, principal, EVENT_NAME, "{}", url, secret, old_secret, rotation,
                         expires, self.clock() + VERIFICATION_SECONDS, "active", boundary))
                    if requested is not None and not owner_forever:
                        self.store.db.execute("DELETE FROM event_metadata WHERE key=?", ("nonexpiring_default:" + subscription_id,))
                    self._metadata("last_error", "")
                    self._end()
                except Exception:
                    self.store.db.execute("ROLLBACK")
                    raise
        return {"id": subscription_id, "refreshBefore": None if expires == NO_EXPIRATION else _iso(expires), "cursor": None, "truncated": False}

    def cancel_by_id(self, subscription_id):
        with self._network_lock, self.store.lock:
            row = self.store.db.execute("SELECT url FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if not row:
                raise BridgeError("unknown_subscription")
            params = {"name":EVENT_NAME,"arguments":{},"delivery":{"mode":"webhook","url":row[0]}}
            if self._identity(params,False)[0] != subscription_id:
                raise BridgeError("subscription_identity_mismatch")
            self.unsubscribe(params)
        return {"subscription_id":subscription_id,"status":"cancelled"}

    def unsubscribe(self, params):
        subscription_id, _, _, _ = self._identity(params, False)
        with self._network_lock, self.store.lock:
            self._begin()
            try:
                self.store.db.execute("UPDATE subscriptions SET status='cancelled',secret='',old_secret=NULL,rotate_until=NULL WHERE id=?", (subscription_id,))
                self.store.db.execute("DELETE FROM event_metadata WHERE key=?", ("nonexpiring_default:" + subscription_id,))
                self.store.db.execute("UPDATE deliveries SET status='cancelled',error='subscription_inactive' WHERE subscription_id=? AND status IN ('pending','retry','sending')", (subscription_id,))
                self._end()
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
        return {}

    def deliver_once(self):
        binding = self.store.get_binding()
        if not binding or not binding["enabled"]:
            return False
        with self._network_lock:
            now = self.clock()
            with self.store.lock:
                self._begin()
                try:
                    self._expire(now)
                    subscription = self.store.db.execute("SELECT id,url,secret,old_secret,rotate_until,start_rowid,principal FROM subscriptions WHERE status='active'").fetchone()
                    principal = _json_bytes([binding["id"], binding["owner"]]).decode("utf-8")
                    if not subscription or subscription[6] != principal:
                        self._end()
                        return False
                    self.store.db.execute("""UPDATE deliveries SET status='cancelled',error='reply_committed'
                        WHERE status IN ('pending','retry') AND message_id IN (SELECT message_id FROM replies WHERE is_notice=0)""")
                    rows = self.store.db.execute("""SELECT id,created_at FROM messages m WHERE rowid>? AND account=? AND peer=?
                        AND state='pending' AND NOT EXISTS (SELECT 1 FROM replies r WHERE r.message_id=m.id AND r.is_notice=0)
                        AND NOT EXISTS (SELECT 1 FROM attachments a WHERE (a.message_id=m.id OR EXISTS(SELECT 1 FROM attachment_links l WHERE l.message_id=m.id AND l.attachment_id=a.id)) AND a.status IN ('pending','downloading'))
                        AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.subscription_id=? AND d.message_id=m.id)
                        ORDER BY rowid LIMIT 100""", (subscription[5], binding["id"], binding["owner"], subscription[0])).fetchall()
                    for message_id, created_at in rows:
                        event_id = "evt_" + hashlib.sha256((subscription[0] + ":" + message_id).encode()).hexdigest()
                        body = _json_bytes({"eventId": event_id, "name": EVENT_NAME, "timestamp": _iso(created_at),
                                            "data": {"message_id": message_id}, "cursor": None})
                        self.store.db.execute("INSERT OR IGNORE INTO deliveries(event_id,subscription_id,message_id,body,status,next_attempt) VALUES(?,?,?,?,'pending',?)", (event_id, subscription[0], message_id, body, now))
                        from .timing import record as trace
                        trace(self.store,message_id,"event_enqueued")
                    delivery = self.store.db.execute("""SELECT event_id,body,attempts FROM deliveries
                        WHERE subscription_id=? AND status IN ('pending','retry') AND next_attempt<=?
                        ORDER BY next_attempt,event_id LIMIT 1""", (subscription[0], now)).fetchone()
                    if not delivery:
                        self._end()
                        return False
                    if delivery[2] >= MAX_ATTEMPTS:
                        self.store.db.execute("UPDATE deliveries SET status='failed',error='retry_exhausted' WHERE event_id=?", (delivery[0],))
                        self._metadata("last_error", "retry_exhausted")
                        self._end()
                        return True
                    attempts = delivery[2] + 1
                    self.store.db.execute("UPDATE deliveries SET status='sending',attempts=? WHERE event_id=?", (attempts, delivery[0]))
                    self._end()
                except Exception:
                    self.store.db.execute("ROLLBACK")
                    raise
            old = subscription[3] if subscription[4] and subscription[4] > now else None
            headers = signed_headers(delivery[0], delivery[1], subscription[2], self.clock(), subscription[0], old)
            from .timing import record as trace
            mid=json.loads(delivery[1])["data"]["message_id"]
            trace(self.store,mid,"event_start")
            callback_start=time.perf_counter()
            try:
                status, _ = self.post(subscription[1], delivery[1], headers)
                if 200 <= status < 300:
                    state, error = "acknowledged", None
                elif status == 410:
                    state, error = "failed", "callback_gone"
                elif status == 413:
                    state, error = "failed", "event_too_large"
                elif status in (408, 429) or 500 <= status < 600:
                    state, error = "retry", "callback_transient"
                else:
                    state, error = "failed", "callback_rejected"
            except BridgeError as failure:
                error = failure.code if failure.code in {"callback_timeout", "callback_dns_failed", "callback_network_failed", "callback_dns_blocked", "callback_tls_failed", "callback_redirect", "callback_response_too_large", "callback_not_allowed", "callback_invalid"} else "callback_failed"
                state = "retry" if error in {"callback_timeout", "callback_dns_failed", "callback_network_failed"} else "failed"
            except Exception:
                state, error = "failed", "callback_failed"
            trace(self.store,mid,"event_result",outcome=state,elapsed_ms=round((time.perf_counter()-callback_start)*1000,3))
            if state == "retry" and attempts >= MAX_ATTEMPTS:
                state, error = "failed", "retry_exhausted"
            with self.store.lock:
                self._begin()
                try:
                    self.store.db.execute("UPDATE deliveries SET status=?,error=?,next_attempt=? WHERE event_id=?", (state, error, self.clock() + min(300, 2 ** attempts), delivery[0]))
                    self._metadata("last_error", error or "")
                    if error == "callback_gone":
                        self.store.db.execute("UPDATE subscriptions SET status='cancelled',secret='',old_secret=NULL WHERE id=?", (subscription[0],))
                        self._expire(self.clock())
                    self._end()
                except Exception:
                    self.store.db.execute("ROLLBACK")
                    raise
            return True

    def status(self):
        with self.store.lock:
            self._expire(self.clock())
            subscriptions = self.store.db.execute("SELECT id,status,expires_at FROM subscriptions ORDER BY id").fetchall()
            counts = dict(self.store.db.execute("SELECT status,COUNT(*) FROM deliveries GROUP BY status").fetchall())
            metadata = dict(self.store.db.execute("SELECT key,value FROM event_metadata").fetchall())
        return {"active_subscriptions": sum(row[1] == "active" for row in subscriptions),
                "subscriptions": [{"id": row[0], "status": row[1], "refreshBefore": None if row[2] == NO_EXPIRATION else _iso(row[2])} for row in subscriptions],
                "deliveries": counts, "last_error": metadata.get("last_error") or None,
                "observed_callback_host": metadata.get("observed_host"),
                "callback_hosts_configured": bool(self.allowed_hosts), "replay_supported": False,
                "recovery_tool": "list_pending_weixin_messages"}
