"""Private, text-only Weixin transport; no model or agent backend lives here."""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import ssl
import stat
import threading
import time
import tomllib
from urllib.parse import urlsplit

from .errors import BridgeError


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 8192
MAX_CONTEXT_BYTES = 16384
MAX_CURSOR_BYTES = 65536
DNS_TIMEOUT_SECONDS = 10.0
_DNS_SLOTS = threading.BoundedSemaphore(4)
_ID = re.compile(r"[A-Za-z0-9@._-]{1,128}\Z")
_OWNER = re.compile(r"[A-Za-z0-9@._+-]{1,256}\Z")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json_object(raw: bytes, error="response_invalid") -> dict:
    try:
        result = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(result, dict):
            raise ValueError("not an object")
        return result
    except (UnicodeError, ValueError, RecursionError):
        raise BridgeError(error) from None


def _endpoint(base_url: str) -> tuple[str, str]:
    """Only Tencent Weixin HTTPS origins are usable, including imported origins."""
    try:
        if not isinstance(base_url, str) or any(c.isspace() for c in base_url):
            raise ValueError("invalid URL")
        parsed = urlsplit(base_url)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not host.isascii()
            or not host.endswith(".weixin.qq.com")
            or "\\" in base_url
        ):
            raise ValueError("disallowed URL")
        # Empty DNS labels, percent escapes and trailing dots are unnecessary here.
        if not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)+", host):
            raise ValueError("invalid hostname")
        return host, f"https://{host}"
    except (ValueError, TypeError):
        raise BridgeError("endpoint_rejected") from None


def _resolve_public(host: str) -> str:
    """Resolve once per connection and reject a DNS answer with any private IP."""
    # libc getaddrinfo has no per-call Python timeout. Keep no more than four
    # outstanding daemon resolvers; timed-out callers never join a stuck DNS
    # thread or create an unbounded number of replacements.
    slots = _DNS_SLOTS
    if not slots.acquire(blocking=False):
        raise BridgeError("dns_rejected")
    resolved = queue.Queue(maxsize=1)

    def resolve():
        try:
            answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            resolved.put((True, answers))
        except Exception:
            resolved.put((False, None))
        finally:
            slots.release()

    try:
        threading.Thread(target=resolve, name="weixin-dns", daemon=True).start()
    except (RuntimeError, OSError):
        slots.release()
        raise BridgeError("dns_rejected") from None
    try:
        succeeded, answers = resolved.get(timeout=DNS_TIMEOUT_SECONDS)
    except queue.Empty:
        raise BridgeError("network_timeout") from None
    if not succeeded:
        raise BridgeError("dns_rejected")
    try:
        if not isinstance(answers, (list, tuple)) or not 0 < len(answers) <= 16:
            raise ValueError("invalid answer count")
        addresses = []
        for family, _kind, _protocol, _name, address in answers:
            if family not in (socket.AF_INET, socket.AF_INET6):
                raise ValueError("unsupported address")
            parsed = ipaddress.ip_address(address[0])
            if not parsed.is_global or parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified:
                raise ValueError("nonpublic address")
            if parsed.version == 6 and (parsed.ipv4_mapped is not None or parsed.is_site_local
                                       or parsed.sixtofour is not None or parsed.teredo is not None):
                raise ValueError("mapped address")
            if str(parsed) not in addresses:
                addresses.append(str(parsed))
        if not addresses:
            raise ValueError("no addresses")
        return addresses[0]
    except (OSError, ValueError, TypeError, IndexError, OverflowError):
        raise BridgeError("dns_rejected") from None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self):
        # Connect to the validated IP without resolving the hostname again. The
        # original hostname remains the TLS identity, SNI and HTTP Host header.
        raw_socket = socket.create_connection((self._pinned_address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


def _checked_string(value, maximum, error="binding_invalid") -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise BridgeError(error)
    return value


def _binding(binding: dict) -> dict:
    if not isinstance(binding, dict):
        raise BridgeError("binding_invalid")
    account = binding.get("id")
    owner = binding.get("owner")
    if not isinstance(account, str) or not _ID.fullmatch(account):
        raise BridgeError("binding_invalid")
    if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        raise BridgeError("binding_invalid")
    token = _checked_string(binding.get("token"), 8192).strip()
    if not token or not token.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token) or "${" in token:
        raise BridgeError("binding_invalid")
    _host, origin = _endpoint(binding.get("base_url", DEFAULT_BASE_URL))
    cursor = binding.get("cursor", "")
    if not isinstance(cursor, str) or len(cursor.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise BridgeError("binding_invalid")
    return {"id": account, "owner": owner, "token": token, "base_url": origin, "cursor": cursor}


class IlinkClient:
    """Direct HTTPS client: no environment proxy, redirects, or response logging."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.poll_timeout = 40.0
        self.connections = set()
        self.connection_lock = __import__("threading").Lock()

    def cancel(self):
        with self.connection_lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                if connection.sock:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                connection.close()
            except OSError:
                pass

    def _post(self, binding: dict, path: str, body: dict, timeout: float, allow_empty=False) -> dict:
        binding = _binding(binding)
        host, _origin = _endpoint(binding["base_url"])
        address = _resolve_public(host)
        connection = None
        try:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "AuthorizationType": "ilink_bot_token",
                "Authorization": "Bearer " + binding["token"],
                "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode("ascii")).decode("ascii"),
                "iLink-App-Id": "bot",
                "iLink-App-ClientVersion": "256",
            }
            connection = _PinnedHTTPSConnection(host, address, timeout)
            with self.connection_lock:
                self.connections.add(connection)
            connection.request("POST", path, body=encoded, headers=headers)
            response = connection.getresponse()
            if response.status in (401, 403):
                raise BridgeError("auth_required")
            if 300 <= response.status < 400:
                raise BridgeError("endpoint_rejected")
            if response.status == 429:
                raise BridgeError("rate_limited")
            if response.status != 200:
                raise BridgeError("http_error")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise BridgeError("response_too_large")
            return {} if allow_empty and not raw.strip() else _json_object(raw)
        except BridgeError:
            raise
        except (TimeoutError, socket.timeout):
            raise BridgeError("network_timeout") from None
        except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
            raise BridgeError("network_error") from None
        finally:
            if connection is not None:
                with self.connection_lock:
                    self.connections.discard(connection)
                try:
                    connection.close()
                except OSError:
                    pass

    @staticmethod
    def _base_info() -> dict:
        return {"channel_version": "0.1.0", "bot_agent": "WechatDotBridge/0.1.0"}

    def poll(self, binding: dict) -> dict:
        checked = _binding(binding)
        response = self._post(checked, "/ilink/bot/getupdates", {
            "get_updates_buf": checked["cursor"], "base_info": self._base_info(),
        }, self.poll_timeout)
        timeout = response.get("longpolling_timeout_ms")
        if type(timeout) is int and 0 < timeout <= 120000:
            self.poll_timeout = timeout / 1000 + 5
        return response

    def typing_ticket(self, binding, context_token):
        checked = _binding(binding)
        context = _checked_string(context_token, MAX_CONTEXT_BYTES, "context_required")
        result = self._post(checked, "/ilink/bot/getconfig", {
            "ilink_user_id": checked["owner"], "context_token": context,
            "base_info": self._base_info()}, 5.0)
        error = _application_error(result)
        if error:
            raise BridgeError(error)
        ticket = result.get("typing_ticket")
        return _checked_string(ticket, MAX_CONTEXT_BYTES, "typing_unavailable")

    def send_typing(self, binding, ticket, status):
        checked = _binding(binding)
        if type(status) is not int or status not in (1, 2):
            raise BridgeError("invalid_typing_status")
        ticket = _checked_string(ticket, MAX_CONTEXT_BYTES, "typing_unavailable")
        result = self._post(checked, "/ilink/bot/sendtyping", {
            "ilink_user_id": checked["owner"], "typing_ticket": ticket,
            "status": status, "base_info": self._base_info()}, 5.0, allow_empty=True)
        error = _application_error(result)
        if error:
            raise BridgeError(error)

    def send(self, binding: dict, reply: dict) -> dict:
        checked = _binding(binding)
        if not isinstance(reply, dict) or reply.get("peer") != checked["owner"]:
            raise BridgeError("invalid_reply")
        text = reply.get("text", "")
        attachment = reply.get("attachment")
        if text or attachment is None:
            text = _checked_string(text, MAX_TEXT_BYTES, "invalid_reply")
        items = [{"type": 1, "text_item": {"text": text}}] if text else []
        if attachment is not None:
            from .media import upload_item
            items.append(upload_item(self, checked, attachment))
        context = _checked_string(reply.get("context_token"), MAX_CONTEXT_BYTES, "context_required")
        client_id = _checked_string(reply.get("client_id"), 256, "invalid_reply")
        return self._post(checked, "/ilink/bot/sendmessage", {
            "msg": {
                "from_user_id": "", "to_user_id": checked["owner"],
                "client_id": client_id, "message_type": 2, "message_state": 2,
                "context_token": context,
                "item_list": items,
            }, "base_info": self._base_info(),
        }, 15.0)


def _application_error(response: dict) -> str | None:
    if not isinstance(response, dict):
        return "response_invalid"
    known_fields = {"upload_full_url", "upload_param", "typing_ticket", "ret", "errcode", "errmsg", "message_id", "msgs", "get_updates_buf", "longpolling_timeout_ms"}
    if response and not known_fields.intersection(response):
        return "response_invalid"
    codes = []
    for name in ("ret", "errcode"):
        if name not in response:
            continue
        value = response[name]
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            return "response_invalid"
        codes.append(value)
    if -14 in codes:
        return "auth_required"
    if -2 in codes and "prepare failed" in str(response.get("errmsg", "")).lower():
        return "rate_limited"
    if any(code != 0 for code in codes):
        # No inferred undocumented error-code mapping. A named context-token
        # rejection can be classified without retaining the remote error text.
        message = response.get("errmsg", "")
        if isinstance(message, str) and any(x in message.lower() for x in ("context_token", "context token", "上下文")):
            return "context_required"
        return "gateway_error"
    return None


def _numeric_identifier(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 0 <= value < 2 ** 64 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        integer = int(value)
        return str(integer) if integer < 2 ** 64 else None
    return None


def _normalize_message(message: dict, binding: dict, now: float) -> dict | None:
    if not isinstance(message, dict):
        return None
    if (message.get("from_user_id") != binding["owner"] or message.get("group_id")
            or message.get("room_id") or message.get("delete_time_ms")):
        return None
    accounts = {binding["id"]}
    if binding["id"].endswith("-im-bot"):
        accounts.add(binding["id"][:-7] + "@im.bot")
    elif binding["id"].endswith("@im.bot"):
        accounts.add(binding["id"][:-7] + "-im-bot")
    if "to_user_id" in message and (not isinstance(message["to_user_id"], str) or message["to_user_id"] not in accounts):
        return None
    if type(message.get("message_type")) is not int or message["message_type"] != 1:
        return None
    if type(message.get("message_state")) is not int or message["message_state"] != 2:
        return None
    items = message.get("item_list")
    if not isinstance(items, list) or not items or len(items) > 32:
        return None
    parts, attachments, quotes = [], [], []
    from .media import descriptor, KINDS
    for item in items:
        if not isinstance(item, dict) or type(item.get("type")) is not int:
            return None
        if item.get("ref_msg"):
            from .message_format import quote_content
            ref = item["ref_msg"]
            quoted, quoted_media = quote_content(ref)
            if quoted_media:
                quoted_body=quoted_media.get(KINDS[quoted_media["type"]]+"_item")
                if not isinstance(quoted_body,dict) or not isinstance(quoted_body.get("media"),dict):
                    quoted_media=None
            if isinstance(ref, dict):
                from .message_format import reference_id
                nested=ref.get("message_item")
                rid=reference_id(ref.get("svr_id")) or reference_id(nested.get("msg_id") if isinstance(nested,dict) else None)
                inline=bool(quoted_media or (quoted and quoted!="[引用内容暂不可用]"))
                quotes.append({"source_id":rid,"partial":ref.get("partial_text"),"inline":inline,
                    "shape":{"reference_fields":[k for k in ref if re.fullmatch(r"[a-zA-Z_]{1,64}",k)][:32],
                             "item_fields":[k for k in nested if re.fullmatch(r"[a-zA-Z_]{1,64}",k)][:32] if isinstance(nested,dict) else []}})
                if rid and quoted=="[引用内容暂不可用]":
                    quoted=""
            if quoted:
                parts.append("[引用]\n" + quoted + "\n[/引用]")
            if quoted_media:
                try:
                    attachments.append(descriptor(quoted_media))
                except BridgeError as error:
                    attachments.append({"kind": KINDS[quoted_media["type"]], "name": "quoted", "error": error.code})
        if item["type"] == 1:
            text_item = item.get("text_item")
            if not isinstance(text_item, dict) or not isinstance(text_item.get("text"), str):
                return None
            parts.append(text_item["text"])
        elif item["type"] in KINDS:
            if item["type"] == 3 and isinstance(item.get("voice_item"), dict) and isinstance(item["voice_item"].get("text"), str) and item["voice_item"]["text"].strip():
                parts.append(item["voice_item"]["text"])
                if not isinstance(item["voice_item"].get("media"), dict):
                    continue
            if item["type"] in KINDS:
                try:
                    attachments.append(descriptor(item))
                except BridgeError as error:
                    attachments.append({"kind": KINDS[item["type"]], "name": KINDS[item["type"]], "error": error.code})
        else:
            return None
    if len(attachments) > 8:
        attachments = [{"kind": "file", "name": "attachments", "error": "media_too_large"}]
    text = "\n".join(parts)
    if not text.strip() and attachments:
        text = "[微信附件]"
    try:
        if not text.strip() or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            return None
    except UnicodeError:
        return None
    identifier = _numeric_identifier(message.get("message_id"))
    if identifier is None and isinstance(message.get("message_id"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}",message["message_id"]) and not message["message_id"].isdigit():
        identifier = message["message_id"]
    if identifier == "0":
        identifier = None
    if identifier is None:
        for item in items:
            item_id = item.get("msg_id")
            if isinstance(item_id, str) and item_id.strip() and len(item_id) <= 256:
                identifier = "item:" + item_id.strip()
                break
    if identifier is not None:
        source_id = "message:" + identifier
    else:
        client_id = message.get("client_id")
        if not isinstance(client_id, str) or not client_id or len(client_id) > 256:
            client_id = None
        seq = _numeric_identifier(message.get("seq"))
        if client_id is None and seq is None:
            return None
        timestamp = message.get("create_time_ms")
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            timestamp = None
        identity = json.dumps([binding["id"], binding["owner"], client_id, seq, timestamp], separators=(",", ":"), ensure_ascii=True)
        source_id = "fallback:" + hashlib.sha256(identity.encode("ascii")).hexdigest()
    created = message.get("create_time_ms")
    if type(created) not in (int, float) or not math.isfinite(created) or created < 0:
        created_at = now
    else:
        created_at = created / 1000.0
    context = message.get("context_token", "")
    try:
        if not isinstance(context, str) or len(context.encode("utf-8")) > MAX_CONTEXT_BYTES:
            return None
    except UnicodeError:
        return None
    from .message_format import reference_id
    aliases=list(dict.fromkeys(r for value in [message.get("message_id"), *(item.get("msg_id") for item in items)] if (r:=reference_id(value))))
    return {"aliases":aliases,"source_id": source_id, "peer": binding["owner"], "text": text,
            "created_at": created_at, "context_token": context or None, "attachments": attachments, "quotes": quotes}


class WeixinWorker:
    def __init__(self, store, client=None):
        self.store = store
        self.client = client if client is not None else IlinkClient()
        self.send_after = 0

    def _record_error(self, code: str, stale=False):
        self.store.record_error(code)
        if code == "auth_required":
            self.store.set_enabled(False)
            if stale and hasattr(self.store, "pause_session"):
                self.store.pause_session()
        elif code == "rate_limited":
            self.send_after = time.monotonic() + 30

    def poll_once(self) -> bool:
        if hasattr(self.store,"resume_session"):
            self.store.resume_session()
        binding = self.store.get_binding()
        if not binding or not binding.get("enabled"):
            return False
        try:
            response = self.client.poll(binding)
            error = _application_error(response)
            if error:
                self._record_error(error, stale=response.get("ret") == -14 or response.get("errcode") == -14)
                return False
            messages = response.get("msgs", [])
            cursor = response.get("get_updates_buf", binding.get("cursor", ""))
            if not isinstance(messages, list) or len(messages) > 1000:
                raise BridgeError("response_invalid")
            if not isinstance(cursor, str) or len(cursor.encode("utf-8")) > MAX_CURSOR_BYTES:
                raise BridgeError("response_invalid")
            clock = getattr(self.client, "clock", time.time)
            normalized = [item for message in messages if (item := _normalize_message(message, binding, clock())) is not None]
            # Store commits messages, owner context and cursor as one transaction.
            self.store.ingest(binding["id"], normalized, cursor or binding.get("cursor", ""))
            self.store.record_error(None)
            return True
        except BridgeError as error:
            self._record_error(error.code)
            return False
        except (UnicodeError, ValueError, TypeError, OverflowError):
            self._record_error("response_invalid")
            return False

    def send_once(self) -> bool:
        if time.monotonic() < self.send_after:
            return False
        binding = self.store.get_binding()
        if not binding or not binding.get("enabled"):
            return False
        reply = self.store.claim_send()
        if reply is None:
            return False
        message_id = reply["message_id"]
        try:
            response = self.client.send(binding, reply)
            error = _application_error(response)
            if error:
                status = error if error in ("auth_required", "context_required") else (
                    "uncertain" if error == "response_invalid" else "failed")
                self.store.finish_send(message_id, status, error, reply_id=reply.get("reply_id"))
                self._record_error(error, stale=response.get("ret") == -14 or response.get("errcode") == -14)
            else:
                self.store.finish_send(message_id, "accepted", reply_id=reply.get("reply_id"))
                if response.get("message_id") and hasattr(self.store, "remember_sent_quote"):
                    try:
                        self.store.remember_sent_quote(str(response["message_id"]), reply)
                    except BridgeError:
                        pass  # Quote cache cannot change an accepted send result.
                self.store.record_error(None)
        except BridgeError as error:
            status = error.code if error.code in ("auth_required", "context_required") else (
                "failed" if error.code in ("endpoint_rejected", "dns_rejected", "binding_invalid", "invalid_reply", "rate_limited") or error.code.startswith("media_") else "uncertain")
            self.store.finish_send(message_id, status, error.code, reply_id=reply.get("reply_id"))
            self._record_error(error.code)
        except (UnicodeError, ValueError, TypeError, OverflowError):
            self.store.finish_send(message_id, "uncertain", "response_invalid", reply_id=reply.get("reply_id"))
            self._record_error("response_invalid")
        return True


def _read_import_file(path: Path) -> bytes:
    descriptor = None
    try:
        absolute = Path(os.path.abspath(path))
        for component in (absolute, *absolute.parents):
            metadata = component.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BridgeError("binding_invalid")
        descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BridgeError("binding_invalid")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            raw = source.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            raise BridgeError("binding_invalid")
        return raw
    except OSError:
        raise BridgeError("import_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def load_existing_binding(source_path, account_id=None) -> dict:
    """Read only an explicitly authorized file and its documented sync sibling.

    No directory traversal, credential discovery, environment interpolation or
    automatic QR login is performed. The caller must never print this return value.
    """
    try:
        path = Path(source_path).expanduser()
        if account_id is not None and (not isinstance(account_id, str) or not _ID.fullmatch(account_id)):
            raise BridgeError("binding_invalid")
        raw = _read_import_file(path)
        if path.suffix.lower() == ".json":
            data = _json_object(raw, "binding_invalid")
            account = account_id or path.stem
            if not account_id and account in ("credentials", "accounts"):
                raise BridgeError("binding_invalid")
            binding = {"id": account, "owner": data.get("userId"), "token": data.get("token"),
                       "base_url": data.get("baseUrl") or DEFAULT_BASE_URL, "cursor": ""}
            sibling = path.with_name(path.stem + ".sync.json")
            try:
                sync_raw = _read_import_file(sibling)
            except BridgeError as error:
                if error.code != "import_failed" or sibling.exists():
                    raise
            else:
                binding["cursor"] = _json_object(sync_raw, "binding_invalid").get("get_updates_buf", "")
            return _binding(binding)
        if path.suffix.lower() != ".toml":
            raise BridgeError("binding_invalid")
        try:
            config = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise BridgeError("binding_invalid") from None
        candidates = []
        projects = config.get("projects", [])
        if not isinstance(projects, list):
            raise BridgeError("binding_invalid")
        for project in projects:
            if not isinstance(project, dict) or not isinstance(project.get("platforms", []), list):
                raise BridgeError("binding_invalid")
            for platform in project.get("platforms", []):
                if not isinstance(platform, dict) or platform.get("type") != "weixin":
                    continue
                options = platform.get("options")
                if not isinstance(options, dict):
                    raise BridgeError("binding_invalid")
                if account_id is None or options.get("account_id", "default") == account_id:
                    candidates.append(options)
        if len(candidates) != 1:
            raise BridgeError("binding_ambiguous" if len(candidates) > 1 else "binding_invalid")
        options = candidates[0]
        allow_from = options.get("allow_from")
        if isinstance(allow_from, str):
            owners = [owner.strip() for owner in allow_from.split(",") if owner.strip()]
        elif isinstance(allow_from, list):
            owners = allow_from
        else:
            owners = []
        if len(owners) != 1 or options.get("route_tag"):
            raise BridgeError("binding_invalid")
        return _binding({"id": options.get("account_id", "default"), "owner": owners[0],
                         "token": options.get("token"), "base_url": options.get("base_url") or DEFAULT_BASE_URL,
                         "cursor": ""})
    except BridgeError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BridgeError("binding_invalid") from None
