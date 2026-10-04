"""Owner attachment presentation with separate byte and UTF-8 text boundaries.

The MCP transport owns response framing; this adapter owns only the content
types and the bridge's stateless byte pagination. Binary chunks remain exact.
Python's incremental UTF-8 codec supplies the text boundary state, including
replacement semantics for malformed input, without decoding the whole file.
"""

import base64
import codecs
import mimetypes

from .errors import BridgeError

CHUNK_BYTES = 131072
TEXT_SUFFIXES = (".txt", ".md", ".csv", ".json", ".py", ".log",
                 ".yaml", ".yml", ".xml", ".html")


def _text_boundary(data, boundary):
    """Defer an unfinished UTF-8 prefix to the next text chunk.

    UTF-8 buffers at most three prefix bytes. Looking behind a byte cursor is
    sufficient to recreate its incremental decoder state, even when that small
    window starts inside an earlier character. Its decoded output is discarded.
    EOF flushes all remaining bytes through normal replacement decoding.
    """
    if boundary >= len(data):
        return len(data)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoder.decode(data[max(0, boundary - 3):boundary], final=False)
    return boundary - len(decoder.getstate()[0])


def read_content(store, arguments):
    """Return metadata and MCP content without exposing private media routing.

    ``offset`` and ``next_offset`` always address the original bytes. Text may
    start before its byte chunk because the prior chunk deferred a partial
    character. ``text_offset`` / ``text_next_offset`` explicitly describe that
    interval; sequential reads using byte ``next_offset`` concatenate exactly
    as one UTF-8 decode with replacement, independently of read order or retry.
    """
    attachment = store.read_attachment(arguments["attachment_id"])
    data = attachment["data"]
    offset = arguments.get("offset", 0)
    value = {"attachment_id": attachment["id"], "kind": attachment["kind"],
             "name": attachment["name"], "size": len(data)}
    content = []
    if attachment["kind"] == "image":
        from .media import image_preview
        content.append({"type": "image", "mimeType": "image/jpeg",
                        "data": base64.b64encode(image_preview(data)).decode()})
        return value, content

    if attachment["kind"] == "voice" and offset == 0:
        from .audio import available, to_wav
        if available():
            try:
                content.append({"type": "audio", "mimeType": "audio/wav",
                                "data": base64.b64encode(to_wav(data)).decode()})
                value["audio_preview"] = True
            except BridgeError as error:
                value["audio_preview_error"] = error.code
    chunk = data[offset:offset + CHUNK_BYTES]
    end = min(offset + len(chunk), len(data))
    value.update(offset=offset, next_offset=end if end < len(data) else None)
    content.append({"type": "resource", "resource": {
        "uri": "wechat-attachment://" + attachment["id"] + "/" + str(offset),
        "mimeType": mimetypes.guess_type(attachment["name"])[0] or "application/octet-stream",
        "blob": base64.b64encode(chunk).decode()}})
    if attachment["name"].lower().endswith(TEXT_SUFFIXES):
        text_start = _text_boundary(data, min(offset, len(data)))
        text_end = _text_boundary(data, end)
        value.update(text_offset=text_start,
                     text_next_offset=text_end if end < len(data) else None,
                     text_encoding="utf-8")
        content.append({"type": "text",
                        "text": data[text_start:text_end].decode("utf-8", errors="replace")})
    return value, content
