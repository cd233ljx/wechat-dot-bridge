#!/usr/bin/env python3
"""Operator-only hidden input. Never accept a key in argv or stdin pipes."""
import argparse
import getpass
import os
from pathlib import Path
import re
import stat
import sys
import warnings


def save_key(path, key):
    if not isinstance(key, str) or not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,512}", key):
        raise ValueError("invalid_key_format")
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("unsafe_path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid():
        raise ValueError("unsafe_owner")
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise ValueError("unsafe_permissions")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unsafe_path")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as file:
            fd = None
            file.write(key + "\n")
            file.flush()
            os.fsync(file.fileno())
    finally:
        if fd is not None:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if not sys.stdin.isatty():
        print("需要交互式终端；不会从管道读取密钥。", file=sys.stderr)
        return 2
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("粘贴 Tunnel 运行密钥（隐藏输入），按回车保存：")
        save_key(args.path, key.strip())
        print("运行密钥已保存，仅当前用户可读。")
        return 0
    except FileExistsError:
        print("目标文件已经存在，未覆盖。", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("已取消，未保存密钥。", file=sys.stderr)
        return 2
    except Exception:
        print("未能保存；请检查密钥格式、目标权限及终端是否支持隐藏输入。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
