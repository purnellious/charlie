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


def search_emails(query: str, max_results: int = 10) -> list[dict]:
    """
    Live Gmail search across the whole mailbox (Gmail's search API already excludes
    Spam/Trash by default, matching normal Gmail UI behaviour — no extra filtering
    needed). `query` is a raw Gmail search string (from:, subject:, after:, has:attachment,
    etc.) — the caller (Charlie) constructs it directly.

    messages().list() only returns id/threadId, so metadata (sender/subject/date/snippet)
    needs one metadata-only messages().get() call per result — no body/payload fetch,
    bounded to max_results calls total.
    """
    service = _build_service()

    result = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    refs = result.get("messages", [])

    results = []
    for ref in refs:
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()

            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender_raw = headers.get("from", "")
            display_name, sender_email = _parse_sender(sender_raw)

            internal_date_ms = int(msg.get("internalDate", 0))
            received_at = datetime.fromtimestamp(
                internal_date_ms / 1000, tz=timezone.utc
            ).isoformat()

            results.append({
                "gmail_message_id": msg["id"],
                "thread_id": msg.get("threadId", ""),
                "sender_name": display_name,
                "sender_email": sender_email,
                "subject": headers.get("subject", "(no subject)"),
                "received_at": received_at,
                "snippet": msg.get("snippet", ""),
            })
        except Exception as e:
            log.warning(f"Failed to fetch metadata for search result {ref['id']}: {e}")
            continue

    return results


def get_thread_content(thread_id: str, max_chars: int = 8000) -> str:
    """
    Fetch every message in a thread, chronological, full body per message — deliberately
    does NOT strip quoted content (unlike fetch_new_emails' triage-tuned extraction),
    since reading an old conversation's quoted history is usually the point. Truncated
    at max_chars to bound token cost on a runaway-long thread.
    """
    service = _build_service()
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()

    # Sort explicitly rather than assume the API response is already ordered.
    messages = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))

    parts = []
    for msg in messages:
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        sender_raw = headers.get("from", "")
        display_name, sender_email = _parse_sender(sender_raw)
        subject = headers.get("subject", "(no subject)")

        internal_date_ms = int(msg.get("internalDate", 0))
        received_at = datetime.fromtimestamp(
            internal_date_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")

        body = _extract_body(payload).strip()
        parts.append(f"[{received_at}] From: {display_name} <{sender_email}>\nSubject: {subject}\n\n{body}")

    combined = "\n\n---\n\n".join(parts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n[truncated]"
    return combined


def archive_thread(thread_id: str) -> None:
    """Remove the INBOX label from every message in the thread — matches Gmail's own
    Archive button behaviour. Reversible (the thread still exists, just not in Inbox)."""
    service = _build_service()
    service.users().threads().modify(
        userId="me", id=thread_id, body={"removeLabelIds": ["INBOX"]}
    ).execute()


def mark_thread_read(thread_id: str) -> None:
    """Remove UNREAD from every message in the thread."""
    service = _build_service()
    service.users().threads().modify(
        userId="me", id=thread_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def mark_thread_unread(thread_id: str) -> None:
    """Add UNREAD to every message in the thread — not just the most recent one, which is
    a deliberate simplification (matching archive_thread/mark_thread_read's thread-wide
    scope) rather than Gmail's own UI convention of flagging only the latest message."""
    service = _build_service()
    service.users().threads().modify(
        userId="me", id=thread_id, body={"addLabelIds": ["UNREAD"]}
    ).execute()


def get_thread_summary(thread_id: str) -> dict:
    """
    Metadata-only fetch (From/Subject/Date/Message-ID headers) of a thread's most recent
    message — cheap, no body. Grounds delete-confirmation previews in real fetched data
    rather than the model's paraphrase, and supplies the Message-ID needed for
    send_email()'s reply threading.
    """
    service = _build_service()
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date", "Message-ID"],
    ).execute()
    messages = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))
    if not messages:
        raise ValueError(f"Thread {thread_id} has no messages.")
    last = messages[-1]
    headers = {h["name"]: h["value"] for h in last.get("payload", {}).get("headers", [])}
    display_name, sender_email = _parse_sender(headers.get("From", ""))
    return {
        "sender_name": display_name,
        "sender_email": sender_email,
        "subject": headers.get("Subject", "(no subject)"),
        "message_id": headers.get("Message-ID"),
    }


def send_email(to: str, subject: str, body: str, thread_id: str | None = None) -> None:
    """
    Send an email via messages().send(). Rejects (raises ValueError) if `to` or `subject`
    contains a control character (\\r or \\n) — no legitimate use needs one, and rejecting
    is a simpler, safer guard against header injection than trying to sanitize it.

    If thread_id is given, replies within that thread: fetches the original Message-ID via
    get_thread_summary(), sets In-Reply-To/References headers and threadId on the send
    body, and prefixes the subject with "Re: " (unless already present) — matching Gmail's
    documented requirements for threading a reply.
    """
    from email.mime.text import MIMEText

    if not to.strip():
        raise ValueError("Refusing to send: 'to' is empty.")
    if any(c in to for c in "\r\n") or any(c in subject for c in "\r\n"):
        raise ValueError("Refusing to send: 'to' or 'subject' contains a control character.")

    msg = MIMEText(body)
    msg["To"] = to

    body_dict = {}
    if thread_id:
        try:
            summary = get_thread_summary(thread_id)
        except Exception as e:
            raise ValueError(f"Could not look up thread {thread_id} to reply within it: {e}")
        orig_subject = summary["subject"]
        msg["Subject"] = orig_subject if orig_subject.strip().lower().startswith("re:") else f"Re: {orig_subject}"
        if summary.get("message_id"):
            msg["In-Reply-To"] = summary["message_id"]
            msg["References"] = summary["message_id"]
        body_dict["threadId"] = thread_id
    else:
        msg["Subject"] = subject

    body_dict["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    service = _build_service()
    service.users().messages().send(userId="me", body=body_dict).execute()


def trash_thread(thread_id: str) -> None:
    """Move the whole thread to Trash — reversible (Gmail's own 30-day trash retention,
    recoverable via threads().untrash()), never a permanent delete."""
    service = _build_service()
    service.users().threads().trash(userId="me", id=thread_id).execute()
