"""Weixin text and quoted-message handling; protocol reference Tencent MIT source."""
import hashlib


def split_text(text, max_chars=3800, max_bytes=8192):
    chunks, current, size = [], [], 0
    for char in text:
        count = len(char.encode('utf-8'))
        if current and (len(current) >= max_chars or size + count > max_bytes):
            chunks.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += count
    if current:
        chunks.append(''.join(current))
    return chunks or ['']


def partial_quote(text, partial):
    """Both observed end-index conventions, verified by protocol MD5 when given."""
    if not isinstance(partial, dict):
        return text
    def nth(needle, index, start=0):
        if not isinstance(needle, str) or not needle or type(index) is not int or not 0 <= index <= len(text):
            return -1
        for _ in range(index + 1):
            pos = text.find(needle, start)
            if pos < 0:
                return -1
            start = pos + len(needle)
        return pos
    start = nth(partial.get('start'), partial.get('startindex'))
    if start < 0:
        return text
    for origin in (0, start + len(partial['start'])):
        end = nth(partial.get('end'), partial.get('endindex'), origin)
        if end < start:
            continue
        candidate = text[start:end + len(partial['end'])]
        digest = partial.get('quotemd5')
        if not digest or (isinstance(digest, str) and hashlib.md5(candidate.encode(), usedforsecurity=False).hexdigest() == digest.lower()):
            return candidate
    return text


def quote_content(ref):
    if not isinstance(ref, dict):
        return '', None
    item = ref.get('message_item')
    text = ref.get('title') if isinstance(ref.get('title'), str) else ''
    media = None
    if isinstance(item, dict):
        body = item.get('text_item') if item.get('type') == 1 else item.get('voice_item') if item.get('type') == 3 else None
        if isinstance(body, dict) and isinstance(body.get('text'), str):
            text = '\n'.join(filter(None, [text, partial_quote(body['text'], ref.get('partial_text'))]))
        elif item.get('type') in (2, 3, 4, 5):
            media = item
    if not text and not media:
        text = '[引用内容暂不可用]'
    return text[:16384], media


def markdown_to_text(text):
    """Use the upstream Weixin-compatible parser when Node is installed."""
    import shutil
    from pathlib import Path
    from .audio import _run
    from .errors import BridgeError
    node = shutil.which("node")
    if node:
        try:
            return _run([node, str(Path(__file__).parent / "codecs/markdown.mjs")], text.encode(), 262144).decode()
        except BridgeError:
            pass
    return _plain_markdown(text)


def _plain_markdown(text):
    """Plain Weixin rendering with code contents and link destinations preserved."""
    import re
    output, fenced = [], False
    for line in text.splitlines(keepends=True):
        if re.match(r'^\s*(```|~~~)', line):
            fenced = not fenced
            continue
        if not fenced:
            line = re.sub(r'^ {0,3}#{1,6}\s+', '', line)
            line = re.sub(r'!?\[([^\]\n]*)\]\(([^)\n]+)\)', lambda m: m[1] + ' (' + m[2] + ')', line)
            line = re.sub(r'(`+)([^`\n]+)\1', r'\2', line)
            line = re.sub(r'(\*\*|__|~~)(.+?)\1', r'\2', line)
            line = re.sub(r'(?<!\w)\*([^*\n]+)\*(?!\w)', r'\1', line)
            if re.match(r'^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$', line):
                continue
        output.append(line)
    return ''.join(output)
