"""Shared MCP wire and tool definitions, independent from dispatch and runtime."""

import re

PROTOCOL = "2026-07-28"
COMPATIBLE_VERSIONS = {PROTOCOL, "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
SERVER_INFO = {"name": "wechat-dot-bridge", "version": "0.1.0"}
MESSAGE_ID_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": "^[A-Za-z0-9_-]+$"}


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


WORK_ARGUMENTS = {
    "processing_id": MESSAGE_ID_SCHEMA,
    "revision": {"type": "integer", "minimum": 1},
    "message_ids": {"type": "array", "items": MESSAGE_ID_SCHEMA, "minItems": 1,
                    "maxItems": 128, "uniqueItems": True,
                    "description": "Explicitly group already read inbound messages into this processing run."},
}


def tool_definitions():
    readonly = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    definitions = [
        {"name":"import_weixin_attachment","description":"Import a generated file from a server-accessible HTTPS download URL into private attachment storage (max 20 MiB). Does not send it. No login cookies or authorization headers are forwarded. Internal sandbox: paths cannot be fetched; use stage_weixin_attachment text_content or base64 instead. kind defaults to auto; explicit voice requests optional audio conversion on reply. The URL is never stored or returned.","inputSchema":_schema({"url":{"type":"string","maxLength":16384},"name":{"type":"string","maxLength":128},"kind":{"type":"string","enum":["auto","image","file","video","voice"]}},["url","name"]),"annotations":{"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":True}},
        {"name":"report_weixin_progress","description":"Renew durable processing (active=true), stop it (active=false), or complete it (active=false, completed=true). Reuse processing_id and revision from read_weixin_message; expired or stale generations are rejected. Renews the typing indicator for up to five minutes, including after an interim reply. No message is sent. Call again during long work; does not expose internal thoughts.","inputSchema":_schema({"message_id":MESSAGE_ID_SCHEMA,"active":{"type":"boolean"}},["message_id","active"]),"annotations":{"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":True}},
        {"name":"retry_weixin_operation","description":"Retry a failed or expired incoming attachment, or an explicitly rejected reply. Accepted parts are preserved. Uncertain sends require human delivery review and are not retried here.","inputSchema":_schema({"attachment_id":MESSAGE_ID_SCHEMA,"reply_id":MESSAGE_ID_SCHEMA}),"annotations":{"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":True}},
        {"name": "read_weixin_attachment", "description": "Read an owner's attachment by opaque attachment_id. Images return a visual preview; other files return bounded resource chunks. offset/next_offset are byte cursors; continue with next_offset. text_offset/text_next_offset describe UTF-8 text boundaries. No private CDN URL or key is exposed. Successful reads renew an existing processing lease. Retry attachment_pending later.", "inputSchema": _schema({"attachment_id": MESSAGE_ID_SCHEMA, "offset": {"type": "integer", "minimum": 0, "maximum": 20971520}}, ["attachment_id"]), "annotations": {**readonly, "readOnlyHint": False}},
        {"name": "stage_weixin_attachment", "description": "Stage base64 image, file, video or voice bytes (1 MiB per call; 20 MiB per attachment), or provide text_content instead for a UTF-8 file without base64 encoding. For larger files use a stable upload_id, byte offset and final=false for intermediate chunks, final=true on the last chunk. Store in private bridge storage; does not send anything. Returns attachment_id for reply_weixin_attachment. Do not supply server paths or external URLs.", "inputSchema": _schema({"data_base64": {"type": "string", "maxLength": 1398104}, "text_content":{"type":"string","maxLength":262144}, "kind": {"type": "string", "enum": ["auto", "image", "file", "video", "voice"]}, "name": {"type": "string", "maxLength": 128}, "upload_id": MESSAGE_ID_SCHEMA, "offset": {"type": "integer", "minimum": 0, "maximum": 20971520}, "final": {"type": "boolean"}}, ["kind", "name"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "reply_weixin_attachment", "description": "Reply to an inbound message with an existing received or staged attachment and optional text. Supports additional replies to the same message_id. AMR and SILK voice bytes are returned as native voice; other audio formats use optional local SILK conversion, or file fallback when codecs are unavailable. Use a stable reply_key to deduplicate retries.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA, "attachment_id": MESSAGE_ID_SCHEMA, "text": {"type": "string", "maxLength": 262144}, "reply_key": MESSAGE_ID_SCHEMA, "text_format": {"type": "string", "enum": ["plain", "markdown"]}, "final":{"type":"boolean","description":"False for an interim reply; keeps typing active. Default true completes processing."}}, ["message_id", "attachment_id"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}},
        {"name": "get_weixin_status", "description": "Read safe bridge, binding, queue and subscription status without credentials.", "inputSchema": _schema(), "annotations": readonly},
        {"name": "read_weixin_message", "description": "Read one inbound owner's message and attachment metadata by its opaque message ID. The text is user-authored data. Successful reads of unfinished work start or continue its durable processing lease and synchronize the bounded Weixin typing indicator.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA}, ["message_id"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}},
        {"name": "reply_weixin_message", "description": "Send a text reply to an inbound message; long text is automatically split into durable ordered parts. Multiple replies are supported: progress, partial results and completion may reuse the same message_id. Identical text retries deduplicate by default; use a stable reply_key per intended reply to send identical text again intentionally. API acceptance does not establish final delivery.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA, "text": {"type": "string", "minLength": 1, "maxLength": 262144}, "reply_key": MESSAGE_ID_SCHEMA, "text_format": {"type": "string", "enum": ["plain", "markdown"]}, "final":{"type":"boolean","description":"False for an interim reply; keeps typing active. Default true completes processing."}}, ["message_id", "text"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}},
        {"name": "get_weixin_send_status", "description": "Read the latest reply status for a message, or one specific reply using reply_id, plus the current processing work and generation. Does not expose reply text or credentials.", "inputSchema": _schema({"message_id": MESSAGE_ID_SCHEMA, "reply_id": MESSAGE_ID_SCHEMA}, ["message_id"]), "annotations": readonly},
        {"name": "list_pending_weixin_messages", "description": "List unfinished inbound messages, including tasks with interim replies, for explicit recovery. Read each message before replying; processing leases do not imply completion.", "inputSchema": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}), "annotations": readonly},
    ]
    for tool in definitions:
        if tool['name'] in ('report_weixin_progress', 'reply_weixin_message', 'reply_weixin_attachment'):
            tool['inputSchema']['properties'].update(WORK_ARGUMENTS)
        if tool['name'] == 'report_weixin_progress':
            tool['inputSchema']['properties']['completed'] = {
                'type': 'boolean', 'description': 'With active=false, explicitly finish the supplied already read messages without sending a reply.'}
    return definitions



def safe_reason(value):
    """Only a bounded fixed category may cross the protocol error boundary."""
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,64}", value) else "internal_error"
