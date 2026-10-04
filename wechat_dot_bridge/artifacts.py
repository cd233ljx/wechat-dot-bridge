"""Import caller-supplied public HTTPS artifacts without forwarding credentials."""
import base64
import hashlib
import socket
import threading
from urllib.parse import urlsplit,urljoin
from .errors import BridgeError
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
    from .events import _public_addresses,_google_addresses,_PinnedHTTPSConnection
    for hop in range(4):
        p=url_parts(url)
        connection=None
        timer=None
        expired=threading.Event()
        try:
            addresses=_google_addresses(p.hostname,media.NETWORK_PROXY) if media.NETWORK_DNS=='google' else _public_addresses(p.hostname)
            connection=_PinnedHTTPSConnection(p.hostname,addresses,media.NETWORK_PROXY)
            connection._request_expired=expired
            def abort():
                expired.set()
                if connection.sock:
                    try: connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    connection.sock.close()
            timer=threading.Timer(20,abort);timer.daemon=True;timer.start()
            connection.connect()
            if expired.is_set(): raise BridgeError('artifact_timeout')
            request_path=(p.path or '/')+('?' + p.query if p.query else '')
            connection.request('GET',request_path,headers={'Host':p.hostname})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                target=response.getheader('Location')
                if not target: raise BridgeError('artifact_download_failed')
                url=urljoin(url,target)
                continue  # Validate and pin public addresses again; never forward authorization.
            if response.status!=200: raise BridgeError('artifact_http_rejected')
            length=response.getheader('Content-Length')
            if length and (not length.isdigit() or int(length)>MAX_MEDIA):
                raise BridgeError('media_too_large')
            data=response.read(MAX_MEDIA+1)
            if not data or len(data)>MAX_MEDIA: raise BridgeError('media_too_large')
            if expired.is_set(): raise BridgeError('artifact_timeout')
            return data
        except BridgeError as error:
            if error.code.startswith(('artifact_','media_')): raise
            raise BridgeError('artifact_endpoint_rejected') from None
        except Exception:
            raise BridgeError('artifact_download_failed') from None
        finally:
            if timer: timer.cancel()
            if connection: connection.close()
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
