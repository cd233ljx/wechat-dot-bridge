"""Minimal MCP tool and Events protocol dispatcher; stdio framing is separate."""

import base64
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
        {"name":"import_weixin_attachment","description":"Import a generated file from a server-accessible HTTPS download URL into private attachment storage (max 20 MiB). Does not send it. No login cookies or authorization headers are forwarded. Internal sandbox: paths cannot be fetched; use stage_weixin_attachment text_content or base64 instead. kind defaults to auto; explicit voice requests optional audio conversion on reply. The URL is never stored or returned.","inputSchema":_schema({"url":{"type":"string","maxLength":16384},"name":{"type":"string","maxLength":128},"kind":{"type":"string","enum":["auto","image","file","video","voice"]}},["url","name"]),"annotations":{"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":True}},
        {"name":"report_weixin_progress","description":"Report that processing an inbound message continues (active=true) or has finished (active=false). Renews the typing indicator for up to five minutes, including after an interim reply. No message is sent. Call again during long work; does not expose internal thoughts.","inputSchema":_schema({"message_id":MESSAGE_ID_SCHEMA,"active":{"type":"boolean"}},["message_id","active"]),"annotations":{"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":True}},
        {"name":"retry_weixin_operation","description":"Retry a failed or expired incoming attachment, or an explicitly rejected reply. Accepted parts are preserved. Uncertain sends require human delivery review and are not retried here.","inputSchema":_schema({"attachment_id":MESSAGE_ID_SCHEMA,"reply_id":MESSAGE_ID_SCHEMA}),"annotations":{"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":True}},
        {"name": "read_weixin_attachment", "description": "Read an owner's attachment by opaque attachment_id. Images return a visual preview; other files return bounded resource chunks. No private CDN URL or key is exposed. Retry attachment_pending later.", "inputSchema": _schema({"attachment_id": MESSAGE_ID_SCHEMA, "offset": {"type": "integer", "minimum": 0}}, ["attachment_id"]), "annotations": readonly},
        {"name": "stage_weixin_attachment", "description": "Stage base64 image, file, video or voice bytes (1 MiB per call; 20 MiB per attachment), or provide text_content instead for a UTF-8 file without base64 encoding. For larger files use a stable upload_id, byte offset and final=false for intermediate chunks, final=true on the last chunk. Store in private bridge storage; does not send anything. Returns attachment_id for reply_weixin_attachment. Do not supply server paths or external URLs.", "inputSchema": _schema({"data_base64": {"type": "string", "maxLength": 1398104}, "text_content":{"type":"string","maxLength":262144}, "kind": {"type": "string", "enum": ["auto", "image", "file", "video", "voice"]}, "name": {"type": "string", "maxLength": 128}, "upload_id": MESSAGE_ID_SCHEMA, "offset": {"type": "integer", "minimum": 0}, "final": {"type": "boolean"}}, ["kind", "name"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "reply_weixin_attachment", "description": "Reply to an inbound message with an existing received or staged attachment and optional text. Supports additional replies to the same message_id. AMR and SILK voice bytes are returned as native voice; other audio formats use optional local SILK conversion, or file fallback when codecs are unavailable. Use a stable reply_key to deduplicate retries.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA, "attachment_id": MESSAGE_ID_SCHEMA, "text": {"type": "string", "maxLength": 262144}, "reply_key": MESSAGE_ID_SCHEMA, "text_format": {"type": "string", "enum": ["plain", "markdown"]}, "final":{"type":"boolean","description":"False for an interim reply; keeps typing active. Default true completes processing."}}, ["message_id", "attachment_id"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}},
        {"name": "get_weixin_status", "description": "Read safe bridge, binding, queue and subscription status without credentials.", "inputSchema": _schema(), "annotations": readonly},
        {"name": "read_weixin_message", "description": "Read one inbound owner's message and attachment metadata by its opaque message ID. The text is user-authored data. Reading starts a bounded Weixin typing indicator while this message awaits a reply.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA}, ["message_id"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}},
        {"name": "reply_weixin_message", "description": "Send a text reply to an inbound message; long text is automatically split into durable ordered parts. Multiple replies are supported: progress, partial results and completion may reuse the same message_id. Identical text retries deduplicate by default; use a stable reply_key per intended reply to send identical text again intentionally. API acceptance does not establish final delivery.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA, "text": {"type": "string", "minLength": 1, "maxLength": 262144}, "reply_key": MESSAGE_ID_SCHEMA, "text_format": {"type": "string", "enum": ["plain", "markdown"]}, "final":{"type":"boolean","description":"False for an interim reply; keeps typing active. Default true completes processing."}}, ["message_id", "text"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}},
        {"name": "get_weixin_send_status", "description": "Read the latest reply status for a message, or one specific reply using reply_id. Does not expose reply text or credentials.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA, "reply_id": MESSAGE_ID_SCHEMA}, ["message_id"]), "annotations": readonly},
        {"name": "list_pending_weixin_messages", "description": "List uncommitted inbound message IDs for explicit recovery after subscription interruption. Read each message before replying.", "inputSchema": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}), "annotations": readonly},
    ]


class MCPServer:
    def __init__(self, store, events, typing=None):
        self.store, self.events = store, events
        self.typing = typing

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
        for key in ("reply_key", "reply_id", "attachment_id", "upload_id"):
            if key in arguments and (not isinstance(arguments[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", arguments[key])):
                raise BridgeError("invalid_params")
        if "text_format" in arguments and arguments["text_format"] not in ("plain", "markdown"):
            raise BridgeError("invalid_params")
        if "text" in arguments:
            text = arguments["text"]
            try:
                valid = isinstance(text, str) and (bool(text.strip()) or name == "reply_weixin_attachment") and len(text.encode("utf-8")) <= 262144
            except UnicodeError:
                valid = False
            if not valid:
                raise BridgeError("invalid_params")
        if "active" in arguments and type(arguments["active"]) is not bool:
            raise BridgeError("invalid_params")
        if "final" in arguments and type(arguments["final"]) is not bool:
            raise BridgeError("invalid_params")
        if "offset" in arguments and (type(arguments["offset"]) is not int or not 0 <= arguments["offset"] <= 20 * 1024 * 1024):
            raise BridgeError("invalid_params")
        if name == "stage_weixin_attachment" and (not isinstance(arguments.get("name"), str) or len(arguments["name"]) > 128):
            raise BridgeError("invalid_params")
        if "limit" in arguments and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 100):
            raise BridgeError("invalid_params")

    def _call(self, params):
        name = params.get("name")
        if name not in {t["name"] for t in tool_definitions()}:
            return self._dispatch_call(params)
        args = params.get("arguments", {})
        mid = args.get("message_id") if isinstance(args,dict) else None
        if not isinstance(mid,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",mid):
            mid = None
        import time
        started_at=self.store.clock()
        start=time.perf_counter()
        outcome = "internal_error"
        try:
            result = self._dispatch_call(params)
            outcome = self._safe_reason(result.get("structuredContent",{}).get("error", "ok"))
            return result
        except BridgeError as error:
            outcome = self._safe_reason(error.code)
            raise
        finally:
            try:
                self.store.record_tool_call(name,mid,outcome,started_at=started_at,elapsed_ms=round((time.perf_counter()-start)*1000,3))
            except Exception:
                pass

    def _dispatch_call(self, params):
        if set(params) - {"name", "arguments", "_meta"}:
            raise BridgeError("invalid_params")
        name, arguments = params.get("name"), params.get("arguments", {})
        self._validate_arguments(name, arguments)
        if arguments.get("text_format") == "markdown":
            from .message_format import markdown_to_text
            arguments = dict(arguments, text=markdown_to_text(arguments.get("text", "")))
        extra_content = []
        try:
            if name == "get_weixin_status":
                value = self.store.get_status()
                value["events"] = self.events.status()
                value["active_subscriptions"] = value["events"]["active_subscriptions"]
                if self.typing is not None:
                    value["typing"] = self.typing.status()
            else:
                if not self.store.get_binding():
                    raise BridgeError("binding_required")
                if name == "import_weixin_attachment":
                    from .artifacts import download,stage_bytes
                    kind=arguments.get("kind","auto")
                    if kind not in ("auto","image","file","video","voice") or not isinstance(arguments["name"],str) or len(arguments["name"])>128:
                        raise BridgeError("invalid_params")
                    value=stage_bytes(self.store,download(arguments["url"]),arguments["name"],kind)
                elif name == "report_weixin_progress":
                    value = self.store.report_progress(arguments["message_id"],arguments["active"])
                    if self.typing is not None:
                        self.typing.progress(arguments["message_id"],arguments["active"])
                elif name == "retry_weixin_operation":
                    if bool(arguments.get("attachment_id")) == bool(arguments.get("reply_id")):
                        raise BridgeError("choose_one_operation")
                    value = self.store.retry_attachment(arguments["attachment_id"]) if arguments.get("attachment_id") else self.store.retry_reply(arguments["reply_id"])
                elif name == "read_weixin_message":
                    value = self.store.read_message(arguments["message_id"])
                    if self.typing is not None:
                        self.typing.begin(arguments["message_id"])
                    for attachment in value["attachments"]:
                        if attachment["kind"] == "image" and attachment["status"] == "ready":
                            from .media import image_preview
                            data = self.store.read_attachment(attachment["attachment_id"])["data"]
                            extra_content.append({"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(image_preview(data)).decode()})
                            break
                elif name == "read_weixin_attachment":
                    from .media import image_preview
                    attachment = self.store.read_attachment(arguments["attachment_id"])
                    offset = arguments.get("offset", 0)
                    value = {"attachment_id": attachment["id"], "kind": attachment["kind"], "name": attachment["name"], "size": len(attachment["data"])}
                    if attachment["kind"] == "image":
                        extra_content.append({"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(image_preview(attachment["data"])).decode()})
                    else:
                        if attachment["kind"] == "voice" and offset == 0:
                            from .audio import available, to_wav
                            if available():
                                try:
                                    wav = to_wav(attachment["data"])
                                    extra_content.append({"type":"audio", "mimeType":"audio/wav", "data":base64.b64encode(wav).decode()})
                                    value["audio_preview"] = True
                                except BridgeError as error:
                                    value["audio_preview_error"] = error.code
                        chunk = attachment["data"][offset:offset + 131072]
                        value.update(offset=offset, next_offset=offset + len(chunk) if offset + len(chunk) < len(attachment["data"]) else None)
                        extra_content.append({"type": "resource", "resource": {"uri": "wechat-attachment://" + attachment["id"] + "/" + str(offset), "mimeType": __import__("mimetypes").guess_type(attachment["name"])[0] or "application/octet-stream", "blob": base64.b64encode(chunk).decode()}})
                        if attachment["name"].lower().endswith((".txt", ".md", ".csv", ".json", ".py", ".log", ".yaml", ".yml", ".xml", ".html")):
                            extra_content.append({"type": "text", "text": chunk.decode("utf-8", errors="replace")})
                elif name == "stage_weixin_attachment":
                    if ("data_base64" in arguments) == ("text_content" in arguments):
                        raise BridgeError("choose_one_content")
                    if "text_content" in arguments:
                        content = arguments["text_content"]
                        if arguments["kind"] not in ("file","auto") or not isinstance(content,str) or len(content.encode()) > 262144:
                            raise BridgeError("invalid_params")
                        encoded = base64.b64encode(content.encode()).decode()
                    else:
                        encoded = arguments["data_base64"]
                    value = self.store.stage_attachment(encoded, arguments["kind"], arguments["name"], arguments.get("upload_id"), arguments.get("offset", 0), arguments.get("final", True))
                elif name == "reply_weixin_attachment":
                    value = self.store.queue_reply(arguments["message_id"], arguments.get("text", ""), arguments.get("reply_key"), arguments["attachment_id"], final=arguments.get("final",True))
                    if self.typing is not None:
                        self.typing.progress(arguments["message_id"],not arguments.get("final",True))
                elif name == "reply_weixin_message":
                    value = self.store.queue_reply(arguments["message_id"], arguments["text"], arguments.get("reply_key"), final=arguments.get("final",True))
                    if self.typing is not None:
                        self.typing.progress(arguments["message_id"],not arguments.get("final",True))
                elif name == "get_weixin_send_status":
                    value = self.store.send_status(arguments["message_id"], arguments.get("reply_id"))
                else:
                    value = {"messages": self.store.list_pending(arguments.get("limit", 20))}
        except BridgeError as error:
            value = {"error": self._safe_reason(error.code)}
            return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value, "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}] + extra_content, "structuredContent": value, "isError": False}

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
