"""Optional local codecs; no model calls, URLs, or message text passed to processes."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave

from .errors import BridgeError

MAX_PCM = 24000 * 2 * 300


def ffmpeg_path():
    configured = os.environ.get('WECHAT_BRIDGE_FFMPEG')
    return configured or shutil.which('ffmpeg') or str(Path.home() / '.local/lib/wechat-dot-bridge-codecs/ffmpeg')


def available():
    helper = Path(__file__).resolve().parent / 'codecs'
    return bool(shutil.which('node') and (helper / 'node_modules/silk-wasm').is_dir() and os.access(ffmpeg_path(), os.X_OK))


def _run(command, data, limit):
    # File-backed stdout bounds Python memory; ffmpeg output is additionally duration-limited.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(command, input=data, stdout=output, stderr=subprocess.DEVNULL, timeout=45, check=False)
            if result.returncode or output.tell() > limit:
                raise BridgeError('media_audio_conversion_failed')
            output.seek(0)
            return output.read(limit + 1)
        except (OSError, subprocess.TimeoutExpired):
            raise BridgeError('media_audio_conversion_failed') from None


def _silk(mode, data):
    return _run([shutil.which('node') or 'node', str(Path(__file__).resolve().parent / 'codecs/audio.mjs'), mode], data, MAX_PCM)


def is_silk(data):
    return data.startswith((b'#!SILK_V3', b'\x02#!SILK_V3'))


def pcm(data):
    if not available():
        raise BridgeError('media_audio_codec_unavailable')
    result = _silk('decode', data) if is_silk(data) else _run([
        ffmpeg_path(), '-nostdin', '-v', 'error', '-protocol_whitelist', 'pipe', '-i', 'pipe:0',
        '-t', '301', '-vn', '-ac', '1', '-ar', '24000', '-f', 's16le', 'pipe:1'], data, MAX_PCM + 48000)
    if not result or len(result) > MAX_PCM:
        raise BridgeError('media_audio_duration_exceeded')
    return result


def to_silk(data):
    return _silk('encode', pcm(data))


def to_wav(data):
    raw = pcm(data)
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
        wav.writeframes(raw)
    return out.getvalue()
