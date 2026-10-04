"""Standard Webhooks reference signing, preserving the bridge wire contract."""

import base64
from datetime import datetime, timezone

from standardwebhooks import Webhook

from .errors import BridgeError


def _key(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 100:
        raise BridgeError("invalid_signing_secret")
    try:
        decoded = base64.b64decode(secret[6:], validate=True)
    except (ValueError, TypeError):
        raise BridgeError("invalid_signing_secret") from None
    if not 24 <= len(decoded) <= 64:
        raise BridgeError("invalid_signing_secret")
    return decoded


def signed_headers(event_id, body, secret, signed_at, subscription_id, old_secret=None):
    """Strict UTF-8 JSON roundtrips exactly through the reference str API."""
    try:
        payload = body.decode("utf-8")
        event_id.encode("ascii")
        timestamp = str(int(signed_at))
        when = datetime.fromtimestamp(int(signed_at), timezone.utc)
        signatures = [Webhook(_key(key)).sign(event_id, when, payload)
                      for key in (secret, old_secret) if key]
    except BridgeError:
        raise
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise BridgeError("invalid_signing_payload") from None
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": timestamp, "webhook-signature": " ".join(signatures),
            "X-MCP-Subscription-Id": subscription_id}
