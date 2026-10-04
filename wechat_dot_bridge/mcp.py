"""Minimal MCP tool and Events protocol dispatcher; stdio framing is separate."""

import base64
import json
import re

from .errors import BridgeError

# Keep the existing import surface for integrations using this module.
from .tool_contract import (
    COMPATIBLE_VERSIONS, MESSAGE_ID_SCHEMA, PROTOCOL, SERVER_INFO, WORK_ARGUMENTS,
    _schema, safe_reason, tool_definitions,
)


class MCPServer:
    def __init__(self, store, events, typing=None):
        self.store, self.events = store, events
        self.typing = typing
        self.wake_workers = None

    def _wake(self, *names):
        if self.wake_workers is not None:
            try:
                self.wake_workers(*names)
            except Exception:
                # Scheduling is best effort; periodic workers remain the
                # recovery path and cannot change a committed tool result.
                pass

    @staticmethod
    def _error(request_id, code, message, reason=None):
        error = {"code": code, "message": message}
        if reason:
            error["data"] = {"reason": reason}
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _safe_reason(value):
        return safe_reason(value)

    @staticmethod
    def _validate_arguments(name, arguments):
        if not isinstance(arguments, dict):
            raise BridgeError("invalid_params")
        definitions = {tool["name"]: tool["inputSchema"] for tool in tool_definitions()}
        if name not in definitions:
            raise BridgeError("unknown_tool")
        schema = definitions[name]
        from jsonschema import Draft202012Validator
        if not Draft202012Validator(schema).is_valid(arguments):
            raise BridgeError("invalid_params")
        if "text" in arguments:
            text = arguments["text"]
            try:
                valid = isinstance(text, str) and (bool(text.strip()) or name == "reply_weixin_attachment") and len(text.encode("utf-8")) <= 262144
            except UnicodeError:
                valid = False
            if not valid:
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
                    value = self.store.report_progress(arguments["message_id"],arguments["active"],
                        **{k:arguments[k] for k in (*WORK_ARGUMENTS, 'completed') if k in arguments})
                    if self.typing is not None:
                        self.typing.sync(arguments["message_id"])
                    self._wake("typing")
                elif name == "retry_weixin_operation":
                    if bool(arguments.get("attachment_id")) == bool(arguments.get("reply_id")):
                        raise BridgeError("choose_one_operation")
                    value = self.store.retry_attachment(arguments["attachment_id"]) if arguments.get("attachment_id") else self.store.retry_reply(arguments["reply_id"])
                    self._wake("media" if arguments.get("attachment_id") else "send")
                elif name == "read_weixin_message":
                    value = self.store.read_message(arguments["message_id"])
                    if self.typing is not None:
                        self.typing.sync(arguments["message_id"])
                    self._wake("typing")
                    for attachment in value["attachments"]:
                        if attachment["kind"] == "image" and attachment["status"] == "ready":
                            from .media import image_preview
                            data = self.store.read_attachment(attachment["attachment_id"])["data"]
                            extra_content.append({"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(image_preview(data)).decode()})
                            break
                elif name == "read_weixin_attachment":
                    from .attachment_content import read_content
                    value, extra_content = read_content(self.store, arguments)
                    for mid in self.store.touch_attachment_work(arguments["attachment_id"]):
                        if self.typing is not None:
                            self.typing.sync(mid)
                    self._wake("typing")
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
                    value = self.store.queue_reply(arguments["message_id"], arguments.get("text", ""), arguments.get("reply_key"), arguments["attachment_id"], final=arguments.get("final",True), **{k:arguments[k] for k in WORK_ARGUMENTS if k in arguments})
                    if self.typing is not None:
                        self.typing.sync(arguments["message_id"])
                    self._wake("send", "typing")
                elif name == "reply_weixin_message":
                    value = self.store.queue_reply(arguments["message_id"], arguments["text"], arguments.get("reply_key"), final=arguments.get("final",True), **{k:arguments[k] for k in WORK_ARGUMENTS if k in arguments})
                    if self.typing is not None:
                        self.typing.sync(arguments["message_id"])
                    self._wake("send", "typing")
                elif name == "get_weixin_send_status":
                    value = self.store.send_status(arguments["message_id"], arguments.get("reply_id"))
                else:
                    value = {"messages": self.store.list_pending(arguments.get("limit", 20))}
        except BridgeError as error:
            value = {"error": self._safe_reason(error.code)}
            return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value, "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}] + extra_content, "structuredContent": value, "isError": False}

    def handle(self, request):
        """Dispatch embedded requests through the official MCP SDK runtime."""
        from .sdk_adapter import SDKAdapter
        adapter = getattr(self, "_sdk_adapter", None)
        if adapter is None:
            adapter = self._sdk_adapter = SDKAdapter(self)
        return adapter.handle(request)
