"""Minimal MCP tool and Events protocol dispatcher; stdio framing is separate."""

import json
import re

from .errors import BridgeError

PROTOCOL = "2026-07-28"
COMPATIBLE_VERSIONS = {PROTOCOL, "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
SERVER_INFO = {"name": "wechat-dot-bridge", "version": "0.1.0"}
MESSAGE_ID_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": "^[A-Za-z0-9_-]+$"}


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


def tool_definitions():
    readonly = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    return [
        {"name": "get_weixin_status", "description": "Read safe bridge, binding, queue and subscription status without credentials.", "inputSchema": _schema(), "annotations": readonly},
        {"name": "read_weixin_message", "description": "Read one inbound owner's text message by its opaque message ID. The text is user-authored data.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA}, ["message_id"]), "annotations": readonly},
        {"name": "reply_weixin_message", "description": "Queue one immutable text reply to an existing inbound message in the same Weixin conversation. Repeating the identical reply is idempotent; a different reply is rejected. API acceptance does not establish final delivery.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA, "text": {"type": "string", "minLength": 1, "maxLength": 8192}}, ["message_id", "text"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}},
        {"name": "get_weixin_send_status", "description": "Read reply acceptance or uncertainty for an existing inbound message. Does not expose reply text or credentials.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA}, ["message_id"]), "annotations": readonly},
        {"name": "list_pending_weixin_messages", "description": "List uncommitted inbound message IDs for explicit recovery after subscription interruption. Read each message before replying.", "inputSchema": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}), "annotations": readonly},
    ]


class MCPServer:
    def __init__(self, store, events):
        self.store, self.events = store, events

    @staticmethod
    def _error(request_id, code, message, reason=None):
        error = {"code": code, "message": message}
        if reason:
            error["data"] = {"reason": reason}
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _safe_reason(value):
        return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,64}", value) else "internal_error"

    @staticmethod
    def _validate_arguments(name, arguments):
        if not isinstance(arguments, dict):
            raise BridgeError("invalid_params")
        definitions = {tool["name"]: tool["inputSchema"] for tool in tool_definitions()}
        if name not in definitions:
            raise BridgeError("unknown_tool")
        schema = definitions[name]
        if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
            raise BridgeError("invalid_params")
        if "message_id" in arguments and (not isinstance(arguments["message_id"], str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", arguments["message_id"])):
            raise BridgeError("invalid_params")
        if "text" in arguments:
            text = arguments["text"]
            try:
                valid = isinstance(text, str) and bool(text.strip()) and len(text.encode("utf-8")) <= 8192
            except UnicodeError:
                valid = False
            if not valid:
                raise BridgeError("invalid_params")
        if "limit" in arguments and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 100):
            raise BridgeError("invalid_params")

    def _call(self, params):
        if set(params) - {"name", "arguments", "_meta"}:
            raise BridgeError("invalid_params")
        name, arguments = params.get("name"), params.get("arguments", {})
        self._validate_arguments(name, arguments)
        try:
            if name == "get_weixin_status":
                value = self.store.get_status()
                value["events"] = self.events.status()
                value["active_subscriptions"] = value["events"]["active_subscriptions"]
            else:
                if not self.store.get_binding():
                    raise BridgeError("binding_required")
                if name == "read_weixin_message":
                    value = self.store.read_message(arguments["message_id"])
                elif name == "reply_weixin_message":
                    value = self.store.queue_reply(arguments["message_id"], arguments["text"])
                elif name == "get_weixin_send_status":
                    value = self.store.send_status(arguments["message_id"])
                else:
                    value = {"messages": self.store.list_pending(arguments.get("limit", 20))}
        except BridgeError as error:
            value = {"error": self._safe_reason(error.code)}
            return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value, "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value, "isError": False}

    def handle(self, request):
        if not isinstance(request, dict):
            return self._error(None, -32600, "Invalid request")
        has_id = "id" in request
        request_id = request.get("id")
        if (request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str) or
                (has_id and (request_id is None or type(request_id) not in (int, str)))):
            return self._error(None, -32600, "Invalid request") if has_id else None
        if not has_id:
            # Notifications cannot enqueue replies, change subscriptions, or produce a response.
            return None
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params", "invalid_params")
        method = request["method"]
        try:
            if method == "server/discover":
                result = {"resultType": "complete", "supportedVersions": [PROTOCOL], "serverInfo": SERVER_INFO,
                          "capabilities": {"tools": {}, "events": {}}}
            elif method == "initialize":
                version = params.get("protocolVersion")
                if version not in COMPATIBLE_VERSIONS:
                    raise BridgeError("unsupported_protocol")
                result = {"protocolVersion": version, "capabilities": {"tools": {}, "events": {}}, "serverInfo": SERVER_INFO}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                if set(params) - {"cursor", "_meta"} or params.get("cursor") is not None:
                    raise BridgeError("invalid_params")
                result = {"tools": tool_definitions()}
            elif method == "tools/call":
                result = self._call(params)
            elif method == "events/list":
                if set(params) - {"cursor", "_meta"} or params.get("cursor") is not None:
                    raise BridgeError("invalid_params")
                result = {"events": [self.events.event_definition()]}
            elif method == "events/subscribe":
                result = self.events.subscribe(params)
            elif method == "events/unsubscribe":
                result = self.events.unsubscribe(params)
            else:
                return self._error(request_id, -32601, "Method not found")
        except BridgeError as error:
            reason = self._safe_reason(error.code)
            if reason.startswith("callback_") or reason == "challenge_failed":
                return self._error(request_id, -32015, "CallbackEndpointError", reason)
            if reason in {"invalid_params", "invalid_signing_secret", "replay_not_supported", "unknown_tool", "unsupported_protocol"}:
                return self._error(request_id, -32602, "Invalid params", reason)
            return self._error(request_id, -32000, "Bridge unavailable", reason)
        except Exception:
            return self._error(request_id, -32603, "Internal error", "internal_error")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
