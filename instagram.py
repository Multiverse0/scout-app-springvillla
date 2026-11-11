"""Official Instagram Messaging API helpers for inbound conversations and replies."""
import datetime
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request


def settings():
    version = os.environ.get("INSTAGRAM_GRAPH_VERSION", "v22.0").strip()
    return {
        "account_id": os.environ.get("INSTAGRAM_ACCOUNT_ID", "").strip(),
        "access_token": os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip(),
        "app_secret": os.environ.get("INSTAGRAM_APP_SECRET", "").strip(),
        "verify_token": os.environ.get("INSTAGRAM_VERIFY_TOKEN", "").strip(),
        "graph_version": version,
    }


def status():
    cfg = settings()
    missing = [label for key, label in (
        ("account_id", "INSTAGRAM_ACCOUNT_ID"),
        ("access_token", "INSTAGRAM_ACCESS_TOKEN"),
        ("app_secret", "INSTAGRAM_APP_SECRET"),
        ("verify_token", "INSTAGRAM_VERIFY_TOKEN"),
    ) if not cfg[key]]
    if cfg["account_id"] and (not cfg["account_id"].isdigit() or len(cfg["account_id"]) > 64):
        missing.append("INSTAGRAM_ACCOUNT_ID (must be numeric)")
    if not re.fullmatch(r"v\d+\.\d+", cfg["graph_version"]):
        missing.append("INSTAGRAM_GRAPH_VERSION")
    return {
        "configured": not missing,
        "missing": missing,
        "account_id": cfg["account_id"],
        "graph_version": cfg["graph_version"],
    }


def verify_signature(raw_body, signature, app_secret=None):
    secret = app_secret if app_secret is not None else settings()["app_secret"]
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_challenge(mode, token, verify_token):
    return bool(mode == "subscribe" and verify_token and
                hmac.compare_digest(str(token or ""), verify_token))


def _message_text(message):
    text = str(message.get("text") or "").strip()
    if text:
        return text[:4000]
    attachments = message.get("attachments") or []
    kinds = sorted({str(a.get("type") or "file") for a in attachments if isinstance(a, dict)})
    if kinds:
        return "Received Instagram attachment: " + ", ".join(kinds) + "."
    return ""


def parse_events(payload, account_id):
    """Return user-initiated text/attachment messages for this Instagram account."""
    messages = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for event in entry.get("messaging") or []:
            if not isinstance(event, dict) or not isinstance(event.get("message"), dict):
                continue
            sender = event.get("sender") or {}
            recipient = event.get("recipient") or {}
            if not isinstance(sender, dict) or not isinstance(recipient, dict):
                continue
            sender_id = str(sender.get("id") or "")
            recipient_id = str(recipient.get("id") or entry.get("id") or "")
            if (event["message"].get("is_echo") or not sender_id or
                    not sender_id.isdigit() or len(sender_id) > 64 or sender_id == account_id or
                    (recipient_id and recipient_id != account_id)):
                continue
            text = _message_text(event["message"])
            if not text:
                continue
            timestamp = event.get("timestamp")
            try:
                received = datetime.datetime.fromtimestamp(
                    float(timestamp) / 1000 if float(timestamp) > 10**12 else float(timestamp),
                    tz=datetime.timezone.utc,
                )
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            messages.append({
                "id": str(event["message"].get("mid") or ""),
                "sender_id": sender_id,
                "username": str(sender.get("username") or "")[:100],
                "text": text,
                "at": received.isoformat(timespec="seconds"),
            })
    return messages


def can_reply(conversation, now=None):
    last_inbound = conversation.get("last_inbound_at")
    if not last_inbound:
        return False
    try:
        received = datetime.datetime.fromisoformat(last_inbound.replace("Z", "+00:00"))
        if received.tzinfo is None:
            received = received.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return False
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    return datetime.timedelta(0) <= current - received < datetime.timedelta(hours=24)


def send_message(recipient_id, text):
    cfg = settings()
    current = status()
    if not current["configured"]:
        raise ValueError("Instagram Messaging API is not configured: " + ", ".join(current["missing"]))
    if not recipient_id.isdigit():
        raise ValueError("Invalid Instagram recipient")
    if not text.strip() or len(text) > 1000:
        raise ValueError("Instagram replies must contain 1-1000 characters")

    url = f"https://graph.facebook.com/{cfg['graph_version']}/{cfg['account_id']}/messages"
    request = urllib.request.Request(
        url,
        data=json.dumps({
            "recipient": {"id": recipient_id},
            "message": {"text": text.strip()},
        }).encode(),
        headers={
            "Authorization": "Bearer " + cfg["access_token"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read() or b"{}")
            message = body.get("error", {}).get("message") or "Meta rejected the message"
        except (ValueError, AttributeError):
            message = "Meta rejected the message"
        raise RuntimeError(f"Instagram send failed (HTTP {error.code}): {str(message)[:300]}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach the Meta Graph API: {error.reason}") from None
