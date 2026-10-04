"""Explicit, private QR binding using the current Tencent Weixin protocol.

The returned metadata never includes a QR payload, token, owner ID, or phone
verification code. Only the caller's explicitly selected bridge state is read.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from urllib.parse import urlencode
import uuid

from .errors import BridgeError
from .net_transport import NetworkFailure, request
from .store import Store
from .transport import (
    DEFAULT_BASE_URL, MAX_RESPONSE_BYTES, _pinned_address, _binding,
    _endpoint, _json_object, _resolve_public,
)


LOGIN_TTL_SECONDS = 300
LOGIN_FILE = "weixin-login.json"
MAX_LOGIN_BYTES = 131072
_ACTIVE = frozenset({"wait", "scaned", "need_verifycode", "scaned_but_redirect"})
_TERMINAL = frozenset({"confirmed", "expired", "verify_code_blocked", "already_bound"})


def _private_file(fd):
    metadata = os.fstat(fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
        raise BridgeError("unsafe_login_file")


@contextmanager
def _login_lock(directory: Path):
    fd = None
    try:
        fd = os.open(directory / ".weixin-login.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        _private_file(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("login_busy") from None
        yield
    except BridgeError:
        raise
    except OSError:
        raise BridgeError("login_state_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _read_state(directory: Path) -> dict | None:
    fd = None
    try:
        fd = os.open(directory / LOGIN_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        _private_file(fd)
        with os.fdopen(fd, "rb") as source:
            fd = None
            raw = source.read(MAX_LOGIN_BYTES + 1)
        if len(raw) > MAX_LOGIN_BYTES:
            raise BridgeError("invalid_login_state")
        result = _json_object(raw, "invalid_login_state")
        if (type(result.get("version")) is not int or result["version"] != 1
                or not isinstance(result.get("status"), str) or result["status"] not in _ACTIVE | _TERMINAL
                or ("bound" in result and type(result["bound"]) is not bool)
                or ("enabled" in result and type(result["enabled"]) is not bool)):
            raise BridgeError("invalid_login_state")
        return result
    except FileNotFoundError:
        return None
    except BridgeError:
        raise
    except OSError:
        raise BridgeError("login_state_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _write_state(directory: Path, data: dict):
    temporary = directory / (".weixin-login-" + uuid.uuid4().hex + ".tmp")
    fd = None
    try:
        # Refuse an existing unsafe destination instead of following/replacing it.
        existing = None
        try:
            existing = os.open(directory / LOGIN_FILE, os.O_RDONLY | os.O_NOFOLLOW)
            _private_file(existing)
        except FileNotFoundError:
            pass
        finally:
            if existing is not None:
                os.close(existing)
        raw = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(raw) > MAX_LOGIN_BYTES:
            raise BridgeError("invalid_login_state")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as destination:
            fd = None
            destination.write(raw)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, directory / LOGIN_FILE)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BridgeError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise BridgeError("login_state_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _text(value, maximum):
    try:
        if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum
                or any(ord(character) < 32 or ord(character) == 127 for character in value)):
            raise ValueError
        return value
    except (UnicodeError, ValueError):
        raise BridgeError("invalid_login_response") from None


def _metadata(directory: Path, state: dict) -> dict:
    result = {"status": state["status"], "bound": state.get("bound", state["status"] == "confirmed"),
              "enabled": state.get("enabled", False), "requires_phone_verification": state["status"] == "need_verifycode"}
    if state["status"] in _ACTIVE:
        result["expires_at"] = state["expires_at"]
        result["qr_state_file"] = str(directory / LOGIN_FILE)
    return result


def _terminal(directory: Path, status: str, bound=None, enabled=False):
    # Remove QR contents from the state file after success/expiry/blocking.
    state = {"version": 1, "status": status}
    if bound is not None:
        state["bound"] = bound
        state["enabled"] = enabled
    _write_state(directory, state)
    return _metadata(directory, state)


def _active_state_valid(state: dict, now: float) -> bool:
    started, expires = state.get("started_at"), state.get("expires_at")
    try:
        if (type(started) not in (int, float) or type(expires) not in (int, float)
                or not math.isfinite(started) or not math.isfinite(expires)
                or started < 0 or expires != started + LOGIN_TTL_SECONDS):
            raise ValueError
    except (ValueError, OverflowError):
        raise BridgeError("invalid_login_state")
    return started <= now < expires


class LoginClient:
    """Unauthenticated login requests use the same HTTPS pinning as transport."""

    def _request(self, base_url, method, path, body=None):
        host, _origin = _endpoint(base_url)
        address = _resolve_public(host)
        try:
            headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": "256"}
            encoded = None
            if method == "POST":
                encoded = json.dumps(body, separators=(",", ":")).encode("ascii")
                # Official QR POST has no bearer or existing bot-token list.
                headers.update({"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token"})
                headers["X-WECHAT-UIN"] = base64.b64encode(str(secrets.randbits(32)).encode("ascii")).decode("ascii")
            response = request(host, _pinned_address(address), method, path, encoded, headers,
                               timeout=40.0, max_response=MAX_RESPONSE_BYTES, body_statuses={200})
            if 300 <= response.status < 400:
                raise BridgeError("endpoint_rejected")
            if response.status != 200:
                raise BridgeError("http_error")
            result = _json_object(response.body, "invalid_login_response")
            for name in ("ret", "errcode"):
                if name in result and result[name] is not None and (type(result[name]) is not int or result[name] != 0):
                    raise BridgeError("login_rejected")
            return result
        except BridgeError:
            raise
        except NetworkFailure as error:
            category = {"timeout": "network_timeout", "response_too_large": "response_too_large", "cancelled": "network_cancelled"}.get(error.category, "network_error")
            raise BridgeError(category) from None
        except (UnicodeError, ValueError, TypeError):
            raise BridgeError("network_error") from None

    def start_login(self):
        # Current official client uses POST, not the older GET QR endpoint.
        return self._request(DEFAULT_BASE_URL, "POST", "/ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []})

    def poll_login(self, qrcode, base_url=DEFAULT_BASE_URL, verify_code=None):
        parameters = {"qrcode": _text(qrcode, 8192)}
        if verify_code is not None:
            if not isinstance(verify_code, str) or not re.fullmatch(r"[0-9]{1,16}", verify_code):
                raise BridgeError("invalid_verify_code")
            parameters["verify_code"] = verify_code
        return self._request(base_url, "GET", "/ilink/bot/get_qrcode_status?" + urlencode(parameters))


def start_login(state_dir, client=None, clock=time.time) -> dict:
    """Create or reuse one pending QR; only safe local metadata is returned."""
    store = Store(state_dir, clock=clock)
    try:
        with _login_lock(store.directory):
            binding = store.get_binding()
            if binding is not None:
                raise BridgeError("login_already_bound")
            state = _read_state(store.directory)
            now = clock()
            if state and state["status"] in _ACTIVE and _active_state_valid(state, now):
                return _metadata(store.directory, state)
            response = (client or LoginClient()).start_login()
            if not isinstance(response, dict):
                raise BridgeError("invalid_login_response")
            state = {"version": 1, "status": "wait", "started_at": now,
                     "expires_at": now + LOGIN_TTL_SECONDS, "base_url": DEFAULT_BASE_URL,
                     "qrcode": _text(response.get("qrcode"), 8192),
                     "qrcode_img_content": _text(response.get("qrcode_img_content"), 65536)}
            _write_state(store.directory, state)
            return _metadata(store.directory, state)
    finally:
        store.close()


def poll_login(state_dir, verify_code=None, client=None, clock=time.time) -> dict:
    """One bounded status poll; phone digits are neither logged nor persisted."""
    if verify_code is not None and (not isinstance(verify_code, str) or not re.fullmatch(r"[0-9]{1,16}", verify_code)):
        raise BridgeError("invalid_verify_code")
    store = Store(state_dir, clock=clock)
    try:
        with _login_lock(store.directory):
            state = _read_state(store.directory)
            if state is None:
                raise BridgeError("login_not_started")
            if state["status"] in _TERMINAL:
                return _metadata(store.directory, state)
            if not _active_state_valid(state, clock()):
                return _terminal(store.directory, "expired")
            binding = store.get_binding()
            if binding is not None:
                # A previous confirmation can commit the DB before the state
                # file is replaced. Do not issue another credential request.
                return _terminal(store.directory, "already_bound", bound=True, enabled=binding["enabled"])
            _host, origin = _endpoint(state.get("base_url"))
            qrcode = _text(state.get("qrcode"), 8192)
            if verify_code is not None and state["status"] != "need_verifycode":
                raise BridgeError("verify_code_not_requested")
            try:
                response = (client or LoginClient()).poll_login(qrcode, origin, verify_code)
            except BridgeError as error:
                if error.code == "network_timeout":
                    result = _metadata(store.directory, state)
                    result["error"] = "network_timeout"
                    return result
                raise
            if not isinstance(response, dict):
                raise BridgeError("invalid_login_response")
            status = response.get("status")
            if status == "confirmed":
                binding = _binding({"id": response.get("ilink_bot_id"), "owner": response.get("ilink_user_id"),
                                    "token": response.get("bot_token"),
                                    "base_url": response.get("baseurl") or origin, "cursor": ""})
                store.import_binding(binding)
                store.set_enabled(False)
                return _terminal(store.directory, "confirmed")
            if status == "binded_redirect":
                # Without credentials this server status is not a usable binding.
                return _terminal(store.directory, "already_bound")
            if status in ("expired", "verify_code_blocked"):
                return _terminal(store.directory, status)
            if status == "scaned_but_redirect":
                redirected = _text(response.get("redirect_host"), 253)
                _host, state["base_url"] = _endpoint("https://" + redirected)
            elif status not in ("wait", "scaned", "need_verifycode"):
                raise BridgeError("invalid_login_response")
            state["status"] = status
            _write_state(store.directory, state)
            return _metadata(store.directory, state)
    finally:
        store.close()
