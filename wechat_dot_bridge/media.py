"""Tencent CDN media protocol, isolated from model and callback transports.

Protocol reference: Tencent/openclaw-weixin docs/protocol.md and src/cdn/.
AES-128-ECB with PKCS7 is required by that wire protocol, not a storage cipher.
"""

import base64
import hashlib
import io
import json
import secrets
import threading
import time
import warnings
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from .errors import BridgeError
from .net_transport import NetworkFailure, request, cancellation_token, wait_for_retry, proxy_port, resolve_addresses

CDN_HOST = "novac2c.cdn.weixin.qq.com"
CDN_BASE = "https://" + CDN_HOST + "/c2c"
MAX_MEDIA = 20 * 1024 * 1024
MAX_STAGE = 1024 * 1024
MAX_CACHE = 256 * 1024 * 1024
NETWORK_PROXY = None
NETWORK_DNS = "system"


def configure_network(proxy=None, dns="system"):
    try:
        proxy_port(proxy)
    except NetworkFailure:
        raise BridgeError("media_network_invalid") from None
    if dns not in ("system", "google"):
        raise BridgeError("media_network_invalid")
    global NETWORK_PROXY, NETWORK_DNS
    NETWORK_PROXY, NETWORK_DNS = proxy, dns


KINDS = {2: "image", 3: "voice", 4: "file", 5: "video"}


def safe_name(value):
    if not isinstance(value, str):
        return "attachment.bin"
    return value.replace("\\", "/").split("/")[-1].replace("\x00", "")[:128] or "attachment.bin"


def key_bytes(value, hexadecimal=False):
    try:
        decoded = bytes.fromhex(value) if hexadecimal else base64.b64decode(value, validate=True)
        if len(decoded) == 32:
            decoded = bytes.fromhex(decoded.decode("ascii"))
        if len(decoded) != 16:
            raise ValueError()
        return decoded
    except (ValueError, TypeError, UnicodeError):
        raise BridgeError("media_key_invalid") from None


def crypt(data, key, decrypt=False):
    try:
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        if decrypt:
            worker = cipher.decryptor()
            unpad = padding.PKCS7(128).unpadder()
            plain = worker.update(data) + worker.finalize()
            return unpad.update(plain) + unpad.finalize()
        pad = padding.PKCS7(128).padder()
        padded = pad.update(data) + pad.finalize()
        worker = cipher.encryptor()
        return worker.update(padded) + worker.finalize()
    except ValueError:
        raise BridgeError("media_decryption_failed") from None


def cdn_parts(url):
    try:
        p = urlsplit(url)
        if (not isinstance(url, str) or len(url) > 16384 or any(ord(c) <= 32 for c in url)
                or p.scheme != "https" or p.hostname != CDN_HOST or p.username is not None
                or p.password is not None or p.port not in (None, 443) or p.fragment
                or p.path not in ("/c2c/download", "/c2c/upload") or "\\" in url):
            raise ValueError()
        return p
    except (ValueError, TypeError, AttributeError):
        raise BridgeError("media_endpoint_rejected") from None


def cdn_request(url, data=None):
    # Caller authorizes the exact CDN before shared DNS or connection I/O.
    p = cdn_parts(url)
    if data is not None and len(data) > MAX_MEDIA + 16:
        raise BridgeError("media_too_large")
    try:
        addresses = resolve_addresses(CDN_HOST, dns=NETWORK_DNS, proxy=NETWORK_PROXY)
        limit = MAX_MEDIA + 16 if data is None else 65536
        response = request(CDN_HOST, addresses, "POST" if data is not None else "GET",
                           p.path + ("?" + p.query if p.query else ""), data,
                           {"Content-Type": "application/octet-stream"}, timeout=30,
                           max_response=limit, proxy=NETWORK_PROXY, body_statuses={200})
        if response.status != 200:
            raise BridgeError("media_http_rejected" if 400 <= response.status < 500 else "media_http_error")
        return response.body, response.header("x-encrypted-param")
    except NetworkFailure as error:
        category = {"timeout": "media_timeout", "response_too_large": "media_too_large", "cancelled": "media_cancelled"}.get(error.category, "media_network_failed")
        raise BridgeError(category) from None


def descriptor(item):
    kind = KINDS.get(item.get("type"))
    body = item.get((kind or "") + "_item")
    if kind is None or not isinstance(body, dict):
        raise BridgeError("media_invalid")
    ref = body.get("media")
    if not isinstance(ref, dict):
        raise BridgeError("media_invalid")
    url = ref.get("full_url")
    if not url:
        param = ref.get("encrypt_query_param")
        if not isinstance(param, str) or not 0 < len(param) <= 8192:
            raise BridgeError("media_invalid")
        url = CDN_BASE + "/download?" + urlencode({"encrypted_query_param": param})
    cdn_parts(url)
    key = None
    if kind == "image" and body.get("aeskey"):
        try:
            key = key_bytes(body["aeskey"], True)
        except BridgeError:
            # cc-connect prefers valid image hex keys, then the CDN media key.
            if not ref.get("aes_key"):raise
            key = key_bytes(ref["aes_key"])
    elif ref.get("aes_key"):
        key = key_bytes(ref["aes_key"])
    elif kind != "image":
        raise BridgeError("media_key_invalid")
    return {"kind": kind, "name": safe_name(body.get("file_name", kind + {"image": ".jpg", "voice": ".silk", "video": ".mp4", "file": ".bin"}[kind])),
            "url": url, "key": key.hex() if key else None}


def image_preview(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                if im.width * im.height > 20_000_000 or im.format not in ("JPEG", "PNG", "GIF", "WEBP"):
                    raise ValueError()
                im.seek(0)
                im.thumbnail((1600, 1600))
                out = io.BytesIO()
                im.convert("RGB").save(out, "JPEG", quality=80)
                return out.getvalue()
    except Exception:
        raise BridgeError("media_image_invalid") from None


def fetch(ref):
    raw, _ = cdn_request(ref["url"])
    data = crypt(raw, bytes.fromhex(ref["key"]), True) if ref.get("key") else raw
    if not data or len(data) > MAX_MEDIA:
        raise BridgeError("media_too_large")
    if ref["kind"] == "image":
        image_preview(data)  # Reject corrupt images and decompression bombs.
    return data


def upload_item(client, binding, attachment):
    data, kind = attachment["data"], attachment["kind"]
    # Optional local conversion follows the upstream SILK codec implementation.
    if kind == "voice":
        if data.startswith(b"#!AMR\n"):
            voice_encoding = 0
        elif data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
            voice_encoding = 1
        else:
            from .audio import available, to_silk
            if available():
                data, voice_encoding = to_silk(data), 1
            else:
                kind = "file"
    key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
    encrypted = crypt(data, key)
    result = client._post(binding, "/ilink/bot/getuploadurl", {
        "filekey": filekey, "media_type": {"image": 1, "video": 2, "file": 3, "voice": 3}[kind],
        "to_user_id": binding["owner"], "rawsize": len(data),
        "rawfilemd5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "filesize": len(encrypted), "no_need_thumb": True, "aeskey": key.hex(),
        "base_info": client._base_info()}, 15)
    from .transport import _application_error
    error = _application_error(result)
    if error:
        raise BridgeError(error)
    url = result.get("upload_full_url")
    if not url:
        param = result.get("upload_param")
        if not isinstance(param, str) or not param:
            raise BridgeError("media_upload_failed")
        url = CDN_BASE + "/upload?" + urlencode({"encrypted_query_param": param, "filekey": filekey})
    retry_token = cancellation_token()
    for attempt in range(3):
        if retry_token.is_set():
            raise BridgeError("media_cancelled")
        try:
            _, download = cdn_request(url, encrypted)
            if not isinstance(download, str) or not 0 < len(download) <= 8192:
                raise BridgeError("media_upload_failed")
            break
        except BridgeError as error:
            if error.code not in {"media_http_error", "media_timeout", "media_network_failed", "media_upload_failed"} or attempt == 2:
                raise
            try:
                wait_for_retry(2 ** attempt, retry_token)
            except NetworkFailure:
                raise BridgeError("media_cancelled") from None
    body = {"media": {"encrypt_query_param": download,
                      "aes_key": base64.b64encode(key.hex().encode()).decode(), "encrypt_type": 1}}
    if kind == "image":
        body["mid_size"] = len(encrypted)
    elif kind == "video":
        body["video_size"] = len(encrypted)
    elif kind == "voice":
        body["encode_type"] = voice_encoding
    else:
        body.update(file_name=attachment["name"], len=str(len(data)))
    return {"type": {"image": 2, "file": 4, "video": 5, "voice": 3}[kind], kind + "_item": body}


class MediaWorker:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.execute("""UPDATE attachments SET status='pending',
                attempts=MAX(0,attempts-1),error='media_interrupted',next_attempt=?
                WHERE status='downloading'""", (store.clock(),))

    def tick(self):
        row = self.store.next_attachment()
        if row is None:
            return False
        try:
            self.store.finish_attachment(row["id"], fetch(json.loads(row["descriptor"])))
        except BridgeError as error:
            self.store.fail_attachment(row["id"], error.code)
        except Exception:
            self.store.fail_attachment(row["id"], "media_failed")
        return True
