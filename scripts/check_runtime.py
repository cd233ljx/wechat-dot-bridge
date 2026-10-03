#!/usr/bin/env python3
"""Read only safe status from local files and the official loopback health check."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request

from install_local import validate_secret_file


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--command", default=str(Path.home() / ".local/bin/wechat-dot-bridge"))
    parser.add_argument("--key-only", action="store_true", help="Validate only secret file metadata before starting the client")
    args = parser.parse_args()
    result = {"key_ready": False, "tunnel_healthy": False, "tunnel_ready": False}
    try:
        result["key_ready"] = validate_secret_file(args.key_file)
    except ValueError as error:
        result["key_error"] = str(error)
    if args.key_only:
        print(json.dumps({key: value for key, value in result.items() if key.startswith("key_")}))
        return 0 if result["key_ready"] else 1
    try:
        health_file = Path(args.state_dir) / "health.url"
        if health_file.is_symlink() or health_file.stat().st_uid != os.getuid() or health_file.stat().st_mode & 0o077:
            raise ValueError()
        url = health_file.read_text().strip()
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", url):
            raise ValueError()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        for endpoint, field in (("healthz", "tunnel_healthy"), ("readyz", "tunnel_ready")):
            with opener.open(url + "/" + endpoint, timeout=3) as response:
                result[field] = response.status == 200
    except Exception:
        pass
    try:
        process = subprocess.run([args.command, "--config", args.config, "status"], capture_output=True, timeout=5)
        if process.returncode == 0:
            result["weixin"] = json.loads(process.stdout)
    except Exception:
        pass
    print(json.dumps(result))
    return 0 if result["key_ready"] and result["tunnel_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
