"""
Gmail inbox fetching for the email monitor tool.
Fetches inbox messages newer than a given timestamp. Email bodies are
returned for in-memory triage only — callers must never persist the
'body' field to disk (see data-architecture.md).
"""
import base64
import logging
import re
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr

from googleapiclient.discovery import build

from .auth import get_credentials

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 500
DEFAULT_LOOKBACK_MINUTES = 3  # defensive fallback only — callers should pass an explicit since_iso


def _build_service():
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_part(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace").strip()


def _strip_html(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html,
                  flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<(br|p|div|tr|li)[^>]*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", "", html)
    for entity, char in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        html = html.replace(entity, char)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text from a Gmail message payload, preferring text/plain."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return _decode_part(data)

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return _strip_html(_decode_part(data))

    if mime_type.startswith("multipart/"):
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                result = _extract_body(part)
                if result:
                    return result
        for part in parts:
            if part.get("mimeType") == "text/html":
                result = _extract_body(part)
                if result:
                    return result
        for part in parts:
            result = _extract_body(part)
            if result:
                return result

    return ""


def _strip_quoted_content(body: str) -> str:
    """Keep only the most recent message — strip everything from the first quote marker down."""
    lines = body.splitlines()
    cutoff = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">"):
            cutoff = i
            break
        if re.match(r"^On .{5,100}wrote:\s*$", stripped, re.IGNORECASE):
            cutoff = i
            break
        if re.match(r"^-{3,}.*original message.*-{3,}", stripped, re.IGNORECASE):
            cutoff = i
            break
        if stripped.startswith("From:") and i + 1 < len(lines) and lines[i + 1].strip().startswith("Sent:"):
            cutoff = i
            break
    return "\n".join(lines[:cutoff]).strip()


def _parse_sender(from_header: str) -> tuple:
    name, addr = parseaddr(from_header)
    return (name or addr), addr


def fetch_new_emails(since_iso: str | None) -> list[dict]:
    """
    Fetch inbox messages newer than since_iso. No label-based filtering —
    every inbox message is returned, including promotional/automated mail.
    Returns dicts with an in-memory-only 'body' field that callers must
    never write to disk.
    """
    service = _build_service()

    if since_iso:
        try:
            dt = datetime.fromisoformat(since_iso)
            epoch = int(dt.timestamp())
        except Exception:
            epoch = int((datetime.now(timezone.utc) - timedelta(minutes=DEFAULT_LOOKBACK_MINUTES)).timestamp())
    else:
        epoch = int((datetime.now(timezone.utc) - timedelta(minutes=DEFAULT_LOOKBACK_MINUTES)).timestamp())

    query = f"in:inbox after:{epoch}"

    message_refs = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 50}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        message_refs.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    parsed = []
    for msg_ref in message_refs:
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()

            label_ids = msg.get("labelIds", [])
            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

            sender_raw = headers.get("from", "")
            display_name, sender_email = _parse_sender(sender_raw)
            subject = headers.get("subject", "(no subject)")

            internal_date_ms = int(msg.get("internalDate", 0))
            received_at = datetime.fromtimestamp(
                internal_date_ms / 1000, tz=timezone.utc
            ).isoformat()

            body = _strip_quoted_content(_extract_body(payload))[:MAX_BODY_CHARS]

            parsed.append({
                "gmail_message_id": msg["id"],
                "thread_id": msg.get("threadId", ""),
                "sender_name": display_name,
                "sender_email": sender_email,
                "subject": subject,
                "received_at": received_at,
                "labels": label_ids,
                "body": body,  # in-memory only — never persisted
            })

        except Exception as e:
            log.warning(f"Failed to parse message {msg_ref['id']}: {e}")
            continue

    return parsed
