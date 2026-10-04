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
import anyio

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
        if not isinstance(config, dict) or set(config) - {"state_dir", "callback_hosts", "lock_dir", "callback_proxy", "callback_dns", "media_proxy", "media_dns", "media_policy", "maintenance_policy"}:
            raise BridgeError("config_invalid")
    from .maintenance import validate_policy
    args.maintenance_policy = validate_policy(config.get("maintenance_policy", {}))
    policy = config.get("media_policy", {})
    bounds = {"cache_mib": (20, 65536), "retention_days": (1, 3650), "upload_hours": (1, 8760)}
    if not isinstance(policy,dict) or set(policy)-set(bounds):
        raise BridgeError("config_invalid")
    for key,value in policy.items():
        low,high = bounds[key]
        if type(value) is not int or not low <= value <= high:
            raise BridgeError("config_invalid")
    args.media_policy = policy
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
    dns = config.get("callback_dns", "system")
    if dns not in ("system", "google"):
        raise BridgeError("config_invalid")
    media_proxy, media_dns = config.get("media_proxy"), config.get("media_dns", "system")
    from .net_transport import NetworkFailure, proxy_port
    try:
        proxy_port(proxy)
        proxy_port(media_proxy)
    except NetworkFailure:
        raise BridgeError("config_invalid") from None
    if media_dns not in ("system", "google"):
        raise BridgeError("config_invalid")
    return Path(state).absolute(), hosts, Path(locks).absolute(), proxy, dns, media_proxy, media_dns


async def _run_runtime(store, server, weixin, events, media, monitor, typing):
    from .net_transport import cancel_all, reset_cancellation
    from .sdk_adapter import run_stdio
    from .maintenance import maintain

    reset_cancellation()
    wakes = {name: anyio.Event() for name in ("send", "event", "media", "typing")}

    def notify(*names):
        for name in names:
            wakes[name].set()

    def notify_from_thread(*names):
        # AnyIO events belong to this loop. MCP handlers run in AnyIO worker
        # threads and must marshal their notification back to the loop.
        anyio.from_thread.run_sync(notify, *names)

    previous_wake = getattr(server, "wake_workers", None)
    server.wake_workers = notify_from_thread
    previous_received = getattr(weixin, "on_received", None)

    def received(message_ids):
        # Store invokes this only after atomically committing genuinely new
        # messages. Registration is memory-only; the poll worker wakes I/O.
        typing.received(message_ids)
        if previous_received is not None:
            previous_received(message_ids)

    weixin.on_received = received

    async def wait_for_work(name, delay):
        if name not in wakes:
            await anyio.sleep(delay)
        elif delay > 0:
            event = wakes[name]
            with anyio.move_on_after(delay):
                await event.wait()
            # Replace atomically on the loop; a notification during the next
            # blocking call remains set for its subsequent wait.
            wakes[name] = anyio.Event()

    async def worker(name, function, interval=0.5):
        delay = interval if name == "cache" else 0
        next_send = 0
        while True:
            await wait_for_work(name, delay)
            if name == "send":
                # A wake requests a durable queue check; it cannot bypass the
                # minimum inter-send pace or WeixinWorker's rate-limit guard.
                await anyio.sleep(max(0, next_send - anyio.current_time()))
            started = anyio.current_time()
            try:
                # AnyIO waits for bounded calls to finish on cancellation. The
                # store and process locks stay open until all writers finish.
                result = await anyio.to_thread.run_sync(function)
                if name == "poll":
                    if result:
                        # Long-poll completion needs no extra gap. Immediately
                        # empty responses still get a 100 ms minimum cycle.
                        delay = max(0, 0.1 - (anyio.current_time() - started))
                        if weixin.poll_added:
                            notify("media", "event", "typing")
                    else:
                        delay = min(30, max(1, delay * 2))
                elif name == "send" and result:
                    next_send = anyio.current_time() + 0.1
                    delay = 0
                elif name == "event" and result:
                    delay = 0
                elif name == "media" and result:
                    notify("event")
                    delay = 0
                else:
                    delay = interval
            except Exception:
                delay = min(30, max(1, delay * 2)) if name == "poll" else interval
                if name not in ("typing", "cache"):
                    try:
                        if store.get_binding():
                            store.record_error(name + "_failed")
                    except Exception:
                        pass

    def presence():
        typing.prewarm()
        typing.tick()

    try:
        async with anyio.create_task_group() as tasks:
            async def signals():
                with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as receiver:
                    async for _ in receiver:
                        cancel_all()
                        weixin.client.cancel()
                        tasks.cancel_scope.cancel()
                        break

            tasks.start_soon(signals)
            for name, function in (("poll", weixin.poll_once), ("send", weixin.send_once),
                                   ("event", events.deliver_once), ("media", media.tick),
                                   ("monitor", monitor.tick), ("typing", presence)):
                tasks.start_soon(worker, name, function)
            tasks.start_soon(worker, "cache", lambda: maintain(store), 3600)
            try:
                await run_stdio(server)
            finally:
                # EOF and signals share the same shutdown path. A claimed send
                # finishes before the store closes; interruption stays uncertain.
                cancel_all()
                weixin.client.cancel()
                tasks.cancel_scope.cancel()
    finally:
        server.wake_workers = previous_wake
        weixin.on_received = previous_received
        # Protocol failures also release presence after every writer has exited.
        # Shield this cleanup from cancellation inherited from the caller.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(typing.close)


def serve(store, hosts, lock_dir, state_dir, callback_proxy=None, callback_dns="system", media_proxy=None, media_dns="system"):
    locks = []
    try:
        locks.append(acquire_lock(state_dir / "worker.lock"))
        binding = store.get_binding()
        if binding:
            identity = hashlib.sha256(binding["id"].encode()).hexdigest()
            locks.append(acquire_lock(lock_dir / ("account-" + identity + ".lock")))
        store.recover_inflight()
        store.recover_work()
        events = Events(store, allowed_hosts=hosts, proxy=callback_proxy, dns=callback_dns)
        typing = TypingIndicator(store)
        server = MCPServer(store, events, typing=typing)
        weixin = WeixinWorker(store)
        from .media import configure_network
        configure_network(media_proxy, media_dns)
        media = MediaWorker(store)
        from .operations import HealthMonitor
        monitor = HealthMonitor(store)
        anyio.run(_run_runtime, store, server, weixin, events, media, monitor, typing)
    finally:
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
    commands.add_parser("diagnose", help="Show redacted health and stuck work")
    commands.add_parser("subscriptions", help="List subscription IDs and lifetime, without callback credentials")
    commands.add_parser("queue", help="List unresolved reply IDs and fixed error categories")
    commands.add_parser("storage", help="Read storage sizes, row counts and fixed capacity warnings")
    cleanup = commands.add_parser("cleanup-history", help="Preview completed history retirement; stop service first")
    cleanup.add_argument("--days", type=int)
    cleanup.add_argument("--apply", action="store_true", help="Retire eligible completed history, preserving unresolved work and deduplication")
    managed = commands.add_parser("backup-managed", help="Create a verified private snapshot and rotate only managed backups; stop service first")
    managed.add_argument("directory")
    managed.add_argument("--keep", type=int, default=7)
    latency = commands.add_parser("latency", help="Show redacted per-message timing stages and tool server durations")
    latency.add_argument("--limit",type=int,default=10)
    latency.add_argument("--message-id")
    cancel = commands.add_parser("cancel-subscription", help="Cancel one subscription; stop service first")
    cancel.add_argument("subscription_id")
    retry = commands.add_parser("retry-send", help="Retry an explicitly failed reply; stop service first")
    retry.add_argument("reply_id")
    retry = commands.add_parser("retry-attachment", help="Retry a failed download; stop service first")
    retry.add_argument("attachment_id")
    backup = commands.add_parser("backup", help="Create a new private SQLite snapshot; stop service first")
    backup.add_argument("destination")
    restore = commands.add_parser("restore-backup", help="Restore only into an empty state directory; never overwrite live data")
    restore.add_argument("source")
    resolve = commands.add_parser("resolve-send", help="After checking WeChat, resolve the uncertain PART of a reply; stop service first")
    resolve.add_argument("reply_id")
    resolve.add_argument("--outcome",choices=["delivered","not-delivered"],required=True)
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
        state, hosts, lock_dir, proxy, dns, media_proxy, media_dns = load_config(args)
        state = private_directory(state)
        if args.command == "restore-backup":
            from .operations import restore
            with acquire_lock(state / "worker.lock"):
                print(canonical(restore(args.source,state)))
        elif args.command in ("login-start", "login-poll"):
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
        elif args.command in ("import-binding", "resume", "pause", "subscription-forever", "cancel-subscription", "retry-send", "retry-attachment", "backup", "resolve-send", "cleanup-history", "backup-managed"):
            # Configuration changes must not race a running tunnel child.
            with acquire_lock(state / "worker.lock"):
                store = Store(state)
                store.maintenance_policy = args.maintenance_policy
                if args.command == "cleanup-history":
                    from .maintenance import cleanup_history
                    result = cleanup_history(store, days=args.days, apply=args.apply)
                elif args.command == "backup-managed":
                    from .maintenance import managed_backup
                    result = managed_backup(store, args.directory, keep=args.keep)
                elif args.command == "resolve-send":
                    result = store.resolve_uncertain(args.reply_id,args.outcome=="delivered")
                elif args.command == "backup":
                    from .operations import backup
                    result = backup(store,args.destination)
                elif args.command == "retry-send":
                    result = store.retry_reply(args.reply_id)
                elif args.command == "retry-attachment":
                    result = store.retry_attachment(args.attachment_id)
                elif args.command == "cancel-subscription":
                    events = Events(store, allowed_hosts=hosts, proxy=proxy, dns=dns, recover_inflight=False)
                    result = events.cancel_by_id(args.subscription_id)
                elif args.command == "import-binding":
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
            store.media_policy = args.media_policy
            store.maintenance_policy = args.maintenance_policy
            if args.command == "serve":
                runtime_store = store
                store = None
                serve(runtime_store, hosts, lock_dir, state, proxy, dns, media_proxy, media_dns)
            else:
                from .operations import diagnostics, subscriptions, queue
                from .timing import report
                from .maintenance import storage_status
                result = storage_status(store) if args.command == "storage" else report(store,args.limit,args.message_id) if args.command=="latency" else diagnostics(store) if args.command == "diagnose" else subscriptions(store) if args.command == "subscriptions" else queue(store) if args.command == "queue" else store.get_status()
                print(canonical(result))
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
