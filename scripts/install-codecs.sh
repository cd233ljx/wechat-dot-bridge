#!/usr/bin/env bash
# Optional, local-only audio codecs. No authentication or service configuration changes.
set -euo pipefail
bridge_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
codec_root="${WECHAT_BRIDGE_CODEC_ROOT:-$HOME/.local/lib/wechat-dot-bridge-codecs}"
command -v node >/dev/null
command -v npm >/dev/null
command -v uv >/dev/null
npm ci --ignore-scripts --prefix "$bridge_root/wechat_dot_bridge/codecs"
uv venv --allow-existing "$codec_root/venv"
uv pip install --python "$codec_root/venv/bin/python" imageio-ffmpeg==0.6.0
"$codec_root/venv/bin/python" - "$codec_root" <<'PY'
import imageio_ffmpeg, pathlib, sys
link=pathlib.Path(sys.argv[1])/'ffmpeg'
if link.exists() or link.is_symlink():
    if link.resolve() != pathlib.Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve():
        raise SystemExit('Existing ffmpeg differs; select WECHAT_BRIDGE_FFMPEG explicitly.')
else:
    link.symlink_to(imageio_ffmpeg.get_ffmpeg_exe())
print('Local codecs installed; Node 24+ recommended.')
PY
