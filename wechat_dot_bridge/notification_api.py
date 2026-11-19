"""HTTP over an owner-only Unix socket. The existing worker owns all Weixin I/O."""

import asyncio
from contextlib import nullcontext
import json
import os
import socket
import stat

import anyio
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from .errors import BridgeError
from .notifications import MAX_NOTIFICATION_BYTES


def application(store, wake):
    async def submit(request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return JSONResponse({"error": "json_required"}, 415)
        body = bytearray()
        try:
            with anyio.fail_after(5):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_NOTIFICATION_BYTES * 6 + 1024:
                        return JSONResponse({"error": "body_too_large"}, 413)
                    body.extend(chunk)
            value = json.loads(body)
            if not isinstance(value, dict) or set(value) - {"text", "expires_at"}:
                raise ValueError
            result = await anyio.to_thread.run_sync(store.queue_notification,
                request.headers.get("idempotency-key"), value.get("text"), value.get("expires_at"))
        except (ValueError, RecursionError, ClientDisconnect):
            return JSONResponse({"error": "invalid_request"}, 400)
        except TimeoutError:
            return JSONResponse({"error": "request_timeout"}, 408)
        except BridgeError as exc:
            return failure(exc)
        if result["status"] == "queued":
            wake()
        return JSONResponse(result, 202 if result["status"] in {"queued", "sending"} else 200)

    async def status(request):
        try:
            result = await anyio.to_thread.run_sync(store.notification_status, request.path_params["identifier"])
        except BridgeError as exc:
            return failure(exc)
        return JSONResponse(result)

    return Starlette(routes=[Route("/v1/notifications", submit, methods=["POST"]),
                             Route("/v1/notifications/{identifier}", status)])


def failure(exc):
    codes = {"invalid_key": 400, "invalid_notification": 400, "key_conflict": 409,
             "unknown_notification": 404, "notification_expired": 410, "send_queue_full": 429,
             "not_bound": 503, "account_disabled": 503, "fresh_context_required": 409,
             "database_unavailable": 503}
    if exc.code in codes:
        return JSONResponse({"error": exc.code}, codes[exc.code])
    return JSONResponse({"error": "operation_failed"}, 500)


class LocalServer(uvicorn.Server):
    # The outer AnyIO task group owns SIGTERM/SIGINT and worker shutdown.
    def capture_signals(self):
        return nullcontext()


async def serve_notifications(store, wake):
    path = store.directory / "notify.sock"
    # The caller already holds the exclusive worker lock in the private directory.
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise BridgeError("unsafe_path")
        path.unlink()
    config = uvicorn.Config(application(store, wake), http="h11", ws="none", lifespan="off",
                            access_log=False, log_config=None, proxy_headers=False,
                            limit_concurrency=16, timeout_graceful_shutdown=6)
    server = LocalServer(config)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        try:
            path.chmod(0o600)
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                await asyncio.shield(task)
            finally:
                with anyio.CancelScope(shield=True):
                    server.should_exit = True
                    await task
        finally:
            path.unlink(missing_ok=True)
