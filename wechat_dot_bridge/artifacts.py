"""Import caller-supplied public HTTPS artifacts without forwarding credentials."""
import base64
import hashlib
from urllib.parse import urlsplit,urljoin
from .errors import BridgeError
from .net_transport import NetworkFailure, request, resolve_addresses
from .media import MAX_MEDIA, MAX_STAGE, safe_name


def detect_kind(data,name):
    head=data[:32]
    if head.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff',b'GIF87a',b'GIF89a',b'BM')) or (head[:4]==b'RIFF' and head[8:12]==b'WEBP'):
        return 'image'
    if head.startswith((b'#!AMR\n',b'#!SILK_V3',b'\x02#!SILK_V3')):
        return 'voice'
    # Ordinary audio remains a file unless the caller asks for native voice.
    if len(head)>=12 and head[4:8]==b'ftyp':
        return 'file' if head[8:12] in (b'M4A ',b'M4B ') else 'video'
    import mimetypes
    mime=mimetypes.guess_type(name)[0] or ''
    return 'image' if mime.startswith('image/') else 'video' if mime.startswith('video/') else 'file'


def url_parts(url):
    try:
        p=urlsplit(url)
        if (not isinstance(url,str) or len(url)>16384 or any(ord(c)<=32 for c in url) or '\\' in url
                or p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443) or p.fragment):
            raise ValueError()
        return p
    except (ValueError,TypeError,AttributeError):
        raise BridgeError('artifact_url_invalid') from None


def download(url):
    from . import media
    for hop in range(4):
        p = url_parts(url)
        try:
            addresses = resolve_addresses(p.hostname, dns=media.NETWORK_DNS, proxy=media.NETWORK_PROXY)
            response = request(p.hostname, addresses, 'GET', (p.path or '/') + ('?' + p.query if p.query else ''),
                               None, {}, timeout=20, max_response=MAX_MEDIA,
                               proxy=media.NETWORK_PROXY, body_statuses={200})
            # Artifact imports explicitly retain bounded redirects. Each hop
            # is reauthorized and resolved; the shared library never follows.
            if response.status in (301, 302, 303, 307, 308):
                target = response.header('location')
                if not target:
                    raise BridgeError('artifact_download_failed')
                url = urljoin(url, target)
                continue
            if response.status != 200:
                raise BridgeError('artifact_http_rejected')
            length = response.header('content-length')
            if length and (not length.isdigit() or int(length) > MAX_MEDIA):
                raise BridgeError('media_too_large')
            if not response.body:
                raise BridgeError('media_too_large')
            return response.body
        except NetworkFailure as error:
            category = {'timeout': 'artifact_timeout', 'response_too_large': 'media_too_large', 'cancelled': 'artifact_cancelled',
                        'dns_blocked': 'artifact_endpoint_rejected', 'dns_failed': 'artifact_endpoint_rejected'}.get(error.category, 'artifact_download_failed')
            raise BridgeError(category) from None
        except BridgeError as error:
            if error.code.startswith(('artifact_', 'media_')):
                raise
            raise BridgeError('artifact_endpoint_rejected') from None
    raise BridgeError('artifact_redirect_limit')


def stage_bytes(store,data,name,kind='auto'):
    if not data or len(data)>MAX_MEDIA: raise BridgeError('media_too_large')
    name=safe_name(name)
    uid='import_'+__import__('secrets').token_hex(16)
    result=None
    for offset in range(0,len(data),MAX_STAGE):
        chunk=data[offset:offset+MAX_STAGE]
        result=store.stage_attachment(base64.b64encode(chunk).decode(),kind,name,uid,offset,offset+len(chunk)==len(data))
    return dict(result,sha256=hashlib.sha256(data).hexdigest())
