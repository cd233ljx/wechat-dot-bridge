"""Official MCP SDK runtime with the bridge's webhook and wire compatibility.

The SDK owns JSON-RPC parsing, request dispatch, cancellation and MCP surface
validation. This module only adapts the existing Tunnel's bare modern requests,
the OpenAI webhook Events methods, bounded frames and private error reporting.
"""

from collections import deque
from contextlib import contextmanager
import logging
import sys
import threading
import time

import anyio
from jsonschema import Draft202012Validator
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import _claim_fd, _open_stdin_diversion, stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from pydantic import ConfigDict, ValidationError

from .errors import BridgeError
from .tool_contract import COMPATIBLE_VERSIONS, PROTOCOL, SERVER_INFO, safe_reason, tool_definitions

MAX_RPC_BYTES = 2 * 1024 * 1024
_FRAME_TOO_LARGE = "\x00bridge_request_too_large\n"
_INITIALIZE_COMPAT = "bridge/initializeCompatibility"
_LOG_LOCK = threading.RLock()
_LOG_USERS = 0
_LOG_SAVED = None


def _error(code, message, reason=None):
    return types.ErrorData(code=code, message=message, data={"reason": reason} if reason else None)


def _bridge_error(error):
    # Reuse the business error vocabulary, never an exception's text or URL.
    reason = error.code
    reason = safe_reason(reason)
    if reason.startswith("callback_") or reason == "challenge_failed":
        return _error(-32015, "CallbackEndpointError", reason)
    if reason in {"invalid_params", "invalid_signing_secret", "replay_not_supported", "unknown_tool", "unsupported_protocol"}:
        return _error(-32602, "Invalid params", reason)
    return _error(-32000, "Bridge unavailable", reason)


@contextmanager
def _private_sdk_logging():
    # SDK diagnostics can include invalid client input and exception messages.
    # Bridge diagnostics already record only fixed categories in private state.
    global _LOG_USERS, _LOG_SAVED
    with _LOG_LOCK:
        if not _LOG_USERS:
            loggers = [logging.getLogger("mcp")]
            loggers.extend(logger for name, logger in logging.Logger.manager.loggerDict.items()
                           if name.startswith("mcp.") and isinstance(logger, logging.Logger))
            parent = loggers[0]
            _LOG_SAVED = ([(logger, logger.disabled) for logger in loggers], parent.handlers, parent.propagate)
            parent.handlers, parent.propagate = [logging.NullHandler()], False
            for logger in loggers:
                logger.disabled = True
        _LOG_USERS += 1
    try:
        yield
    finally:
        with _LOG_LOCK:
            _LOG_USERS -= 1
            if not _LOG_USERS:
                saved, old_handlers, old_propagate = _LOG_SAVED
                parent = logging.getLogger("mcp")
                parent.handlers, parent.propagate = old_handlers, old_propagate
                for logger, disabled in saved:
                    logger.disabled = disabled
                _LOG_SAVED = None


class _EventParams(types.RequestParams):
    # Webhook Events have an external protocol distinct from SDK subscriptions.
    # Events.subscribe/unsubscribe perform their existing complete validation.
    model_config = ConfigDict(extra="allow")


class _BoundedInput:
    """Bound raw bytes before the SDK's UTF-8/JSON parser allocates a frame."""

    def __init__(self, stream):
        self.stream = stream
        self.errors = deque()

    async def __aiter__(self):
        while True:
            line = await anyio.to_thread.run_sync(self.stream.readline, MAX_RPC_BYTES + 1, abandon_on_cancel=True)
            if not line:
                return
            if len(line) > MAX_RPC_BYTES:
                while line and not line.endswith(b"\n"):
                    line = await anyio.to_thread.run_sync(self.stream.readline, MAX_RPC_BYTES + 1, abandon_on_cancel=True)
                self.errors.append("request_too_large")
                yield _FRAME_TOO_LARGE
            else:
                # Invalid UTF-8 stays a fixed parse error rather than replacement
                # characters entering user data or protocol parameters.
                try:
                    yield line.decode("utf-8")
                except UnicodeError:
                    self.errors.append("parse_error")
                    yield _FRAME_TOO_LARGE


class _WireState:
    def __init__(self):
        self.legacy = None
        self.compatible_ids = set()

    def normalize(self, item):
        message = item.message
        if isinstance(message, types.JSONRPCNotification) and message.method == "notifications/cancelled":
            request_id = (message.params or {}).get("requestId")
            if type(request_id) in (str, int):
                self.compatible_ids.discard(request_id)
        if not isinstance(message, types.JSONRPCRequest):
            return item
        params = dict(message.params or {})
        original_meta = params.get("_meta")
        enveloped = isinstance(original_meta, dict) and types.PROTOCOL_VERSION_META_KEY in original_meta
        version = params.get("protocolVersion") if message.method == "initialize" else None
        if self.legacy is None:
            self.legacy = message.method == "initialize" and version in COMPATIBLE_VERSIONS - {PROTOCOL}
        if not enveloped:
            self.compatible_ids.add(message.id)
        else:
            self.compatible_ids.discard(message.id)
        if self.legacy:
            if message.method == "initialize":
                params.setdefault("capabilities", {})
                params.setdefault("clientInfo", {"name": "bridge-compatible-client", "version": "1"})
            return SessionMessage(message.model_copy(update={"params": params}), item.metadata)
        method = message.method
        if method == "initialize" and not enveloped:
            # The former bridge accepted a modern-version initialize handshake.
            # Keep that contract without teaching the SDK a second handshake.
            method = _INITIALIZE_COMPAT
        if not enveloped:
            if original_meta is not None and not isinstance(original_meta, dict):
                return item
            params["_meta"] = dict(original_meta or {}, **{
                types.PROTOCOL_VERSION_META_KEY: PROTOCOL,
                types.CLIENT_CAPABILITIES_META_KEY: {},
            })
        return SessionMessage(message.model_copy(update={"method": method, "params": params}), item.metadata)


class _PrivateWriteStream:
    def __init__(self, stream, state):
        self.stream, self.state = stream, state

    async def send(self, item):
        message = item.message
        if isinstance(message, types.JSONRPCError):
            error = message.error
            safe_data = error.data if (isinstance(error.data, dict) and set(error.data) == {"reason"}) else None
            if safe_data:
                safe_data = {"reason": safe_reason(safe_data["reason"])}
            # Errors raised by SDK classifiers must not return the supplied
            # method, malformed parameter values or callback addresses.
            messages = {-32600: "Invalid request", -32601: "Method not found", -32602: "Invalid params",
                        -32603: "Internal error", -32700: "parse_error", -32015: "CallbackEndpointError",
                        -32000: "Bridge unavailable", -32022: "Unsupported protocol version"}
            code = error.code if error.code in messages else -32603
            text = "request_too_large" if error.message == "request_too_large" else messages[code]
            message = message.model_copy(update={"error": types.ErrorData(code=code, message=text, data=safe_data)})
        elif isinstance(message, types.JSONRPCResponse) and message.id in self.state.compatible_ids:
            result = dict(message.result)
            result.pop("_meta", None)
            if "supportedVersions" not in result:
                result.pop("resultType", None)
            message = message.model_copy(update={"result": result})
        if isinstance(message, (types.JSONRPCError, types.JSONRPCResponse)):
            self.state.compatible_ids.discard(message.id)
        await self.stream.send(SessionMessage(message, item.metadata))

    async def aclose(self):
        await self.stream.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


class _PrivateReadStream:
    def __init__(self, stream, output, state, frame_errors=None):
        self.stream, self.output, self.state = stream, output, state
        self.frame_errors = frame_errors if frame_errors is not None else deque()

    @property
    def last_context(self):
        return getattr(self.stream, "last_context", None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def receive(self):
        while True:
            item = await self.stream.receive()
            if isinstance(item, Exception):
                reason = self.frame_errors.popleft() if self.frame_errors else None
                code = -32600 if reason == "request_too_large" else -32700
                if isinstance(item, ValidationError) and reason is None:
                    code = -32700 if any(error["type"] == "json_invalid" for error in item.errors()) else -32600
                await self.output.send(SessionMessage(types.JSONRPCError(
                    jsonrpc="2.0", id=None, error=_error(code, reason or ("parse_error" if code == -32700 else "Invalid request")))))
                continue
            return self.state.normalize(item)

    async def aclose(self):
        await self.stream.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


class SDKAdapter:
    def __init__(self, bridge):
        self.bridge = bridge
        self.server = Server(SERVER_INFO["name"], version=SERVER_INFO["version"],
                             on_list_tools=self._list_tools, on_call_tool=self._call_tool)
        # Remove SDK telemetry; it can record exception text. No exporter or
        # model execution is needed for this private protocol bridge.
        self.server.middleware[:] = [self._middleware]
        for method in ("events/list", "events/subscribe", "events/unsubscribe"):
            self.server.add_request_handler(method, _EventParams, self._events)
        self.server.add_request_handler(_INITIALIZE_COMPAT, _EventParams, self._initialize_compat)

    async def _middleware(self, ctx, call_next):
        try:
            if ctx.method == "ping" and ctx.protocol_version == PROTOCOL:
                return {}
            result = await call_next(ctx)
            if ctx.method in {"initialize", "server/discover"}:
                result["capabilities"]["events"] = {}
                if ctx.method == "server/discover":
                    result["supportedVersions"] = [PROTOCOL]
            return result
        except BridgeError as error:
            raise MCPError.from_error_data(_bridge_error(error)) from None
        except (MCPError, ValidationError):
            raise
        except Exception:
            raise MCPError.from_error_data(_error(-32603, "Internal error", "internal_error")) from None

    async def _list_tools(self, ctx, params):
        raw = ctx.params or {}
        if set(raw) - {"cursor", "_meta"} or raw.get("cursor") is not None:
            raise BridgeError("invalid_params")
        return types.ListToolsResult(tools=[types.Tool.model_validate(tool) for tool in tool_definitions()])

    async def _call_tool(self, ctx, params):
        raw = dict(ctx.params or {})
        if set(raw) - {"name", "arguments", "_meta"}:
            raise BridgeError("invalid_params")
        definitions = {tool["name"]: tool["inputSchema"] for tool in tool_definitions()}
        if params.name not in definitions:
            raise BridgeError("unknown_tool")
        arguments = raw.get("arguments", {})
        if not Draft202012Validator(definitions[params.name]).is_valid(arguments):
            raise BridgeError("invalid_params")
        # These calls persist atomically under Store's lock. Running in a worker
        # thread lets protocol cancellation/stdio stay responsive to media work.
        if params.name == "read_weixin_message":
            dispatched_at, queued_at = self.bridge.store.clock(), time.perf_counter()
            result = await anyio.to_thread.run_sync(self._read_tool, raw, dispatched_at, queued_at)
        else:
            result = await anyio.to_thread.run_sync(self.bridge._call, raw)
        return types.CallToolResult.model_validate(result)

    def _read_tool(self, raw, dispatched_at, queued_at):
        # Capture thread entry before acquiring the Store lock. Persist both
        # timestamps here, never on the event loop or in another worker thread.
        thread_started = time.perf_counter()
        try:
            from .timing import record
            store, mid = self.bridge.store, raw["arguments"]["message_id"]
            thread_started_at = store.clock()
            # Commit the pair together so concurrent reads and status queries
            # cannot combine one request's dispatch with another thread entry.
            with store._transaction():
                record(store, mid, "read_dispatch", at=dispatched_at)
                record(store, mid, "read_thread_start", at=thread_started_at,
                       elapsed_ms=round((thread_started - queued_at) * 1000, 3))
        except Exception:
            # Diagnostics cannot fail a read or change its returned content.
            pass
        return self.bridge._call(raw)

    async def _events(self, ctx, params):
        raw = dict(ctx.params or {})
        raw.pop("_meta", None)
        if ctx.method == "events/list":
            if set(raw) - {"cursor"} or raw.get("cursor") is not None:
                raise BridgeError("invalid_params")
            return {"events": [self.bridge.events.event_definition()]}
        function = self.bridge.events.subscribe if ctx.method == "events/subscribe" else self.bridge.events.unsubscribe
        return await anyio.to_thread.run_sync(function, raw)

    async def _initialize_compat(self, ctx, params):
        version = (ctx.params or {}).get("protocolVersion")
        if version not in COMPATIBLE_VERSIONS:
            raise BridgeError("unsupported_protocol")
        return {"protocolVersion": version, "capabilities": {"tools": {}, "events": {}}, "serverInfo": SERVER_INFO}

    async def run_stdio(self):
        # The SDK has no frame limit on its default stdin iterator. Reuse its
        # pinned 2.3 stdio descriptor guard while supplying our bounded reader;
        # handler subprocesses still see EOF rather than consuming MCP frames.
        stdin_buffer, restore = _claim_fd(0, sys.stdin, "rb", _open_stdin_diversion)
        bounded = _BoundedInput(stdin_buffer)
        try:
            async with stdio_server(stdin=bounded) as (read_stream, write_stream):
                await self.run_streams(read_stream, write_stream, bounded.errors)
        finally:
            if restore is not None:
                restore()

    async def run_streams(self, read_stream, write_stream, frame_errors=None):
        """Serve typed SDK streams, also used for offline cancellation contracts."""
        state = _WireState()
        output = _PrivateWriteStream(write_stream, state)
        incoming = _PrivateReadStream(read_stream, output, state, frame_errors)
        with _private_sdk_logging():
            await self.server.run(incoming, output, self.server.create_initialization_options())

    def handle(self, request):
        """Synchronous embedding compatibility, using the same SDK dispatcher."""
        try:
            message = types.jsonrpc_message_adapter.validate_python(request, by_name=False)
        except (ValidationError, TypeError, ValueError):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        if not isinstance(message, types.JSONRPCRequest):
            return None
        return anyio.run(self._handle_once, message)

    async def _handle_once(self, message):
        send, receive = anyio.create_memory_object_stream(1)
        output, response = anyio.create_memory_object_stream(1)
        async with send, response, anyio.create_task_group() as tasks:
            tasks.start_soon(self.run_streams, receive, output)
            await send.send(SessionMessage(message))
            item = await response.receive()
            tasks.cancel_scope.cancel()
        return item.message.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)


async def run_stdio(bridge):
    await SDKAdapter(bridge).run_stdio()
