#!/usr/bin/env python3
"""Render a newly requested private Weixin QR locally, without logging its data."""
import argparse
import json
import os
from pathlib import Path
import stat
import struct
import sys
import time
import zlib


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def render(state_dir, output):
    import qrcode
    state_dir, output = Path(state_dir).absolute(), Path(output).absolute()
    if output.parent != state_dir:
        raise ValueError("output_must_be_private_state")
    for path in (state_dir, *state_dir.parents, output):
        if path.is_symlink():
            raise ValueError("unsafe_path")
    directory = state_dir.stat()
    if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
        raise ValueError("unsafe_directory")
    fd = os.open(state_dir / "weixin-login.json", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 131072):
            raise ValueError("unsafe_login_file")
        login = json.load(file)
    if (login.get("status") not in {"wait", "scaned", "need_verifycode", "scaned_but_redirect"}
            or login.get("expires_at", 0) <= time.time()):
        raise ValueError("login_inactive")
    content = login.get("qrcode_img_content")
    if not isinstance(content, str) or not 1 <= len(content.encode("utf-8")) <= 4096:
        raise ValueError("invalid_qr_content")
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    code.add_data(content)
    code.make(fit=True)
    matrix = code.get_matrix()
    scale = max(4, min(10, 640 // len(matrix)))
    width = len(matrix) * scale
    rows = []
    for row in matrix:
        pixels = b"".join((b"\x00" if cell else b"\xff") * scale for cell in row)
        rows.extend([b"\x00" + pixels] * scale)
    png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, width, 8, 0, 0, 0, 0))
           + png_chunk(b"IDAT", zlib.compress(b"".join(rows))) + png_chunk(b"IEND", b""))
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(png)
    return {"qr_image": str(output), "expires_at": login["expires_at"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(render(args.state_dir, args.output)))
        return 0
    except Exception:
        print(json.dumps({"error": "qr_render_failed", "hint": "Use the setup environment with qrcode installed and an active login; choose a new PNG filename inside the private state directory."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
