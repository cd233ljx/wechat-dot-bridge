#!/usr/bin/env python3
"""Prepare a user service and private configuration. Does not start or enable it."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys


def private_dir(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("unsafe_path")
    if path in {Path.home(), Path.cwd(), Path("/"), Path("/srv/data"), Path("/srv/stacks")}:
        raise ValueError("unsafe_directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("unsafe_owner")
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError("unsafe_directory")
    return path


def new_file(path, text, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    Path(path).chmod(mode)


def validate_secret_file(path, missing_ok=False):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("unsafe_key_path")
    try:
        info = path.stat()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise ValueError("key_missing") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or not 20 <= info.st_size <= 1024):
        raise ValueError("unsafe_key_file")
    return True


def prepare(config_dir, state_dir, bin_dir, unit_dir, source_dir, tunnel_bin, tunnel_id, key_file, proxy=None):
    if not re.fullmatch(r"tunnel_[0-9a-f]{32}", tunnel_id):
        raise ValueError("invalid_tunnel_id")
    paths = [Path(p).absolute() for p in (config_dir, state_dir, bin_dir, unit_dir, source_dir, tunnel_bin, key_file)]
    config_dir, state_dir, bin_dir, unit_dir, source_dir, tunnel_bin, key_file = paths
    if any(any(c in str(p) for c in "\n\r\"%\\") for p in paths):
        raise ValueError("unsupported_path")
    wrapper = bin_dir / "wechat-dot-bridge"
    unit = unit_dir / "wechat-dot-bridge.service"
    bridge_config = config_dir / "bridge.json"
    tunnel_config = config_dir / "tunnel.yaml"
    for path in (wrapper, unit, bridge_config, tunnel_config):
        if path.exists() or path.is_symlink():
            raise ValueError("target_exists")
    if not (source_dir / "wechat_dot_bridge/__main__.py").is_file() or not tunnel_bin.is_file():
        raise ValueError("missing_program")
    if proxy is not None and not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", proxy):
        raise ValueError("invalid_proxy")
    key_ready = validate_secret_file(key_file, missing_ok=True)
    for path in (config_dir, state_dir, unit_dir):
        private_dir(path)
    for component in (bin_dir, *bin_dir.parents):
        if component.is_symlink():
            raise ValueError("unsafe_bin_dir")
    bin_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    if bin_dir.stat().st_uid != os.getuid() or bin_dir.stat().st_mode & 0o022:
        raise ValueError("unsafe_bin_dir")
    new_file(wrapper, "#!/bin/sh\ncd " + shlex.quote(str(source_dir)) + " || exit 1\nexec " +
             shlex.quote(sys.executable) + " -m wechat_dot_bridge \"$@\"\n", 0o755)
    new_file(bridge_config, json.dumps({"state_dir": str(state_dir), "callback_hosts": []}, indent=2) + "\n")
    command = shlex.join([str(wrapper), "--config", str(bridge_config), "serve"])
    yaml = ("config_version: 1\ncontrol_plane:\n  base_url: \"https://api.openai.com\"\n  tunnel_id: " +
            json.dumps(tunnel_id) + "\n  api_key: " + json.dumps("file:" + str(key_file)) + "\n")
    if proxy:
        yaml += "  http_proxy: " + json.dumps(proxy) + "\n"
    yaml += ("health:\n  listen_addr: \"127.0.0.1:0\"\n  url_file: " + json.dumps(str(state_dir / "health.url")) +
             "\nadmin_ui:\n  open_browser: false\nlog:\n  level: warn\n  format: json\nmcp:\n  commands:\n    - channel: main\n      command: " +
             json.dumps(command) + "\n")
    new_file(tunnel_config, yaml)
    service = ("[Unit]\nDescription=Private WeChat bridge for existing OpenAI dot\nAfter=network-online.target\nWants=network-online.target\n\n"
               "[Service]\nType=simple\nExecStart=\"" + str(tunnel_bin) + "\" run --profile-file \"" + str(tunnel_config) + "\"\n"
               "WorkingDirectory=" + str(source_dir) + "\nUMask=0077\nRestart=on-failure\nRestartSec=10\n"
               "KillMode=control-group\nTimeoutStopSec=45\nNoNewPrivileges=true\nPrivateTmp=true\n"
               "StandardOutput=null\nStandardError=null\n\n[Install]\nWantedBy=default.target\n")
    new_file(unit, service)
    return {"bridge_config": str(bridge_config), "tunnel_config": str(tunnel_config), "unit": str(unit), "key_ready": key_ready, "enabled": False, "started": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--bin-dir", default=str(Path.home() / ".local/bin"))
    parser.add_argument("--unit-dir", default=str(Path.home() / ".config/systemd/user"))
    parser.add_argument("--source-dir", default=str(Path(__file__).absolute().parent.parent))
    parser.add_argument("--tunnel-bin", required=True)
    parser.add_argument("--tunnel-id", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(prepare(**vars(args))))
        return 0
    except Exception:
        print(json.dumps({"error": "local_install_failed", "hint": "Check paths, permissions and pre-existing files; no files are overwritten."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
