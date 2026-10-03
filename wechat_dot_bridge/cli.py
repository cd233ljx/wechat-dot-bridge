"""Local operator commands and bounded stdio runtime."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading

from .errors import BridgeError
from .store import Store
from .transport import WeixinWorker, load_existing_binding
from .events import Events
from .mcp import MCPServer
from .typing import TypingIndicator
from .media import MediaWorker

MAX_RPC_BYTES = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def private_directory(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise BridgeError("unsafe_path")
    if path in {Path.home(), Path.cwd(), Path("/"), Path("/srv/data"), Path("/srv/stacks")}:
        raise BridgeError("unsafe_path")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise BridgeError("unsafe_owner")
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise BridgeError("unsafe_permissions")
    return path


def acquire_lock(path):
    path = Path(path)
    private_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or not stat.S_ISREG(metadata.st_mode):
            raise BridgeError("unsafe_owner")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return os.fdopen(fd, "r+")
    except BaseException:
        os.close(fd)
        raise


def load_config(args):
    config = {}
    if args.config:
        path = Path(args.config)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise BridgeError("config_invalid")
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            raise BridgeError("config_invalid") from None
        if not isinstance(config, dict) or set(config) - {"state_dir", "callback_hosts", "lock_dir", "callback_proxy", "callback_dns"}:
            raise BridgeError("config_invalid")
    state = args.state_dir or config.get("state_dir") or str(Path.home() / ".local/share/wechat-dot-bridge")
    hosts = config.get("callback_hosts", [])
    if not isinstance(hosts, list) or len(hosts) > 3:
        raise BridgeError("config_invalid")
    for host in hosts:
        if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) or ".." in host:
            raise BridgeError("config_invalid")
    locks = config.get("lock_dir") or str(Path.home() / ".local/state/wechat-dot-bridge/locks")
    if not isinstance(state, str) or not isinstance(locks, str):
        raise BridgeError("config_invalid")
    proxy = config.get("callback_proxy")
    if proxy is not None:
        match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", proxy) if isinstance(proxy, str) else None
        if not match or not 1 <= int(match[1]) <= 65535:
            raise BridgeError("config_invalid")
    dns = config.get("callback_dns", "system")
    if dns not in ("system", "google"):
        raise BridgeError("config_invalid")
    return Path(state).absolute(), hosts, Path(locks).absolute(), proxy, dns


def rpc_error(code, message):
    return {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}


def serve(store, hosts, lock_dir, state_dir, callback_proxy=None, callback_dns="system"):
    locks = []
    stop = threading.Event()
    threads = []
    old_handlers = {}
    try:
        locks.append(acquire_lock(state_dir / "worker.lock"))
        binding = store.get_binding()
        if binding:
            identity = hashlib.sha256(binding["id"].encode()).hexdigest()
            locks.append(acquire_lock(lock_dir / ("account-" + identity + ".lock")))
        store.recover_inflight()
        events = Events(store, allowed_hosts=hosts, proxy=callback_proxy, dns=callback_dns)
        typing = TypingIndicator(store)
        server = MCPServer(store, events, typing=typing)
        weixin = WeixinWorker(store)
        media = MediaWorker(store)

        def run_worker(name, function):
            while not stop.wait(0.5):
                try:
                    function()
                except Exception:
                    # Never print network exceptions, request bodies or credentials.
                    try:
                        if store.get_binding():
                            store.record_error(name + "_failed")
                    except Exception:
                        pass

        for name, function in (("poll", weixin.poll_once), ("send", weixin.send_once), ("event", events.deliver_once), ("media", media.tick)):
            thread = threading.Thread(target=run_worker, args=(name, function), daemon=True, name=name)
            thread.start()
            threads.append(thread)

        def run_typing():
            try:
                while not stop.wait(0.5):
                    try:
                        typing.tick()
                    except Exception:
                        pass
            finally:
                typing.close()

        thread = threading.Thread(target=run_typing, daemon=True, name="typing")
        thread.start()
        threads.append(thread)

        def terminate(signum, frame):
            stop.set()
            raise SystemExit(0)

        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, terminate)

        while True:
            line = sys.stdin.buffer.readline(MAX_RPC_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_RPC_BYTES:
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(MAX_RPC_BYTES + 1)
                response = rpc_error(-32600, "request_too_large")
            else:
                try:
                    request = json.loads(line)
                    response = server.handle(request)
                except (ValueError, UnicodeError):
                    response = rpc_error(-32700, "parse_error")
                except Exception:
                    response = rpc_error(-32603, "internal_error")
            if response is not None:
                sys.stdout.write(canonical(response) + "\n")
                sys.stdout.flush()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=1)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        # A still-running bounded network thread is terminated with the process;
        # do not close its SQLite connection while it might finish a transaction.
        if not any(thread.is_alive() for thread in threads):
            store.close()
        for lock in reversed(locks):
            lock.close()


def parser():
    result = argparse.ArgumentParser(prog="wechat-dot-bridge")
    result.add_argument("--state-dir")
    result.add_argument("--config")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Start the private stdio MCP server")
    commands.add_parser("status", help="Read redacted local status")
    commands.add_parser("pause", help="Disable WeChat polling and sending")
    commands.add_parser("subscription-forever", help="Keep the current owner subscription until cancellation; stop the service first")
    enable = commands.add_parser("resume", help="Enable the imported single-owner binding")
    enable.add_argument("--confirm-single-consumer", action="store_true", required=True)
    importer = commands.add_parser("import-binding", help="Import an existing binding without changing its source")
    importer.add_argument("source")
    importer.add_argument("--account-id")
    commands.add_parser("login-start", help="Request a new private Weixin QR login; leaves polling disabled")
    login = commands.add_parser("login-poll", help="Check QR login confirmation without printing credentials")
    login.add_argument("--verification-code", action="store_true", help="Read any required verification code with hidden input")
    return result


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    store = None
    try:
        state, hosts, lock_dir, proxy, dns = load_config(args)
        state = private_directory(state)
        if args.command in ("login-start", "login-poll"):
            from .login import start_login, poll_login
            with acquire_lock(state / "worker.lock"):
                if args.command == "login-start":
                    result = start_login(state)
                else:
                    code = None
                    if args.verification_code:
                        import getpass
                        import warnings
                        with warnings.catch_warnings():
                            warnings.simplefilter("error", getpass.GetPassWarning)
                            code = getpass.getpass("Weixin verification code (hidden): ")
                    result = poll_login(state, verify_code=code)
                print(canonical(result))
        elif args.command in ("import-binding", "resume", "pause", "subscription-forever"):
            # Configuration changes must not race a running tunnel child.
            with acquire_lock(state / "worker.lock"):
                store = Store(state)
                if args.command == "import-binding":
                    binding = load_existing_binding(args.source, args.account_id)
                    result = store.import_binding(binding)
                elif args.command == "subscription-forever":
                    events = Events(store, allowed_hosts=hosts, proxy=proxy, dns=dns)
                    result = events.set_current_subscription_nonexpiring()
                else:
                    store.set_enabled(args.command == "resume")
                    result = store.get_status()
                print(canonical(result))
        else:
            store = Store(state)
            if args.command == "serve":
                runtime_store = store
                store = None
                serve(runtime_store, hosts, lock_dir, state, proxy, dns)
            else:
                print(canonical(store.get_status()))
        return 0
    except BlockingIOError:
        print(canonical({"error": "already_running"}), file=sys.stderr)
        return 2
    except BridgeError as error:
        print(canonical({"error": error.code}), file=sys.stderr)
        return 2
    except Exception:
        print(canonical({"error": "operation_failed"}), file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()
