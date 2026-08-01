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
from email.utils import parseaddr, getaddresses, formataddr

from googleapiclient.discovery import build

from .auth import get_credentials, ACCOUNT_EMAIL

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 500
DEFAULT_LOOKBACK_MINUTES = 3  # defensive fallback only — callers should pass an explicit since_iso
MAX_FORWARD_ATTACHMENT_BYTES = 20 * 1024 * 1024  # safety margin under Gmail's 25MB send limit


def _build_service():
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_part(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace").strip()


def _decode_bytes(data: str) -> bytes:
    """Same padding fix as _decode_part, but for binary content (attachments) — no
    text decode."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _parse_addr_list(raw: str | None) -> list[tuple[str, str]]:
    """Parse a comma-separated address string via email.utils.getaddresses, which
    correctly handles multiple addresses and quoted display names (unlike a naive
    split). Returns (display_name, address) pairs; entries with no address are
    dropped (e.g. an empty or malformed string)."""
    if not raw or not raw.strip():
        return []
    return [(name, addr) for name, addr in getaddresses([raw]) if addr]


def _format_addr_list(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(formataddr((name, addr)) if name else addr for name, addr in pairs)


def _dedup_addr_list(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Dedup by lowercased address (so 'Alice <alice@ts.org>' and 'alice@ts.org' collapse
    to one entry), keeping the more descriptive (named) form and first-seen order."""
    by_addr: dict[str, tuple[str, str]] = {}
    order: list[str] = []
    for name, addr in pairs:
        key = addr.lower()
        if key not in by_addr:
            by_addr[key] = (name, addr)
            order.append(key)
        elif name and not by_addr[key][0]:
            by_addr[key] = (name, addr)
    return [by_addr[k] for k in order]


def _reject_control_chars(label: str, value: str) -> None:
    if any(c in value for c in "\r\n"):
        raise ValueError(f"Refusing to send: '{label}' contains a control character.")


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
    Metadata-only fetch (From/Subject/Date/Message-ID/To/Cc headers) of a thread's most
    recent message — cheap, no body. Grounds delete-confirmation previews in real fetched
    data rather than the model's paraphrase, supplies the Message-ID needed for
    send_email()'s reply threading, and supplies To/Cc needed for reply-all's recipient
    derivation (resolve_send_recipients()).
    """
    service = _build_service()
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date", "Message-ID", "To", "Cc"],
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
        "to_header": headers.get("To", ""),
        "cc_header": headers.get("Cc", ""),
    }


def resolve_send_recipients(
    thread_id: str | None, to: str | None, subject: str,
    cc: str | None, bcc: str | None, reply_all: bool,
) -> dict:
    """
    Resolve the final To/Subject/Cc/Bcc (and, for a reply, the original Message-ID) for a
    send — whether a fresh compose or a reply/reply-all within thread_id. This is the
    single source of truth for that derivation: send_email() calls it at actual-send
    time, and bot.py's proposal-text builder calls it at preview time, so the preview
    Jonathan approves and what actually gets sent can never diverge.

    - Omitting `to` with `thread_id` set means "reply to the original sender."
    - `reply_all=True` (only meaningful with `thread_id`) merges the original To+Cc
      headers into `cc`, minus Jonathan's own address and the resolved `to` — self-
      exclusion applies only to this auto-derived portion, so an explicitly-passed `cc`
      (e.g. "cc me") survives untouched.
    - `cc`/`bcc` are comma-separated address strings; auto-derived and explicit
      recipients are merged and deduplicated by address after both are parsed through
      getaddresses (so a mixed 'Name <addr>' vs bare 'addr' collapses to one entry).

    Raises ValueError if: neither `to` nor `thread_id` is given; a thread_id lookup
    fails; or any resolved to/subject/cc/bcc value contains a control character.
    """
    if (not to or not to.strip()) and not thread_id:
        raise ValueError("Refusing to send: need either 'to' or 'thread_id' (to reply within a thread).")

    resolved_to = to.strip() if to and to.strip() else None
    resolved_subject = subject
    message_id = None
    explicit_cc_pairs = _parse_addr_list(cc)

    if thread_id:
        try:
            summary = get_thread_summary(thread_id)
        except Exception as e:
            raise ValueError(f"Could not look up thread {thread_id} to reply within it: {e}")

        orig_subject = summary["subject"]
        resolved_subject = orig_subject if orig_subject.strip().lower().startswith("re:") else f"Re: {orig_subject}"
        message_id = summary.get("message_id")

        if not resolved_to:
            resolved_to = summary["sender_email"]

        if reply_all:
            self_addr = ACCOUNT_EMAIL.lower()
            resolved_to_lower = resolved_to.lower()
            auto_pairs = _parse_addr_list(summary.get("to_header")) + _parse_addr_list(summary.get("cc_header"))
            auto_pairs = [
                (name, addr) for name, addr in auto_pairs
                if addr.lower() not in (self_addr, resolved_to_lower)
            ]
            cc_pairs = _dedup_addr_list(auto_pairs + explicit_cc_pairs)
        else:
            cc_pairs = _dedup_addr_list(explicit_cc_pairs)
    else:
        cc_pairs = _dedup_addr_list(explicit_cc_pairs)

    if not resolved_to:
        raise ValueError("Refusing to send: could not resolve a 'to' address.")

    resolved_cc = _format_addr_list(cc_pairs)
    resolved_bcc = _format_addr_list(_dedup_addr_list(_parse_addr_list(bcc)))

    _reject_control_chars("to", resolved_to)
    _reject_control_chars("subject", resolved_subject)
    if resolved_cc:
        _reject_control_chars("cc", resolved_cc)
    if resolved_bcc:
        _reject_control_chars("bcc", resolved_bcc)

    return {
        "to": resolved_to, "subject": resolved_subject,
        "cc": resolved_cc, "bcc": resolved_bcc, "message_id": message_id,
    }


def send_email(
    to: str | None = None, subject: str = "", body: str = "", thread_id: str | None = None,
    cc: str | None = None, bcc: str | None = None, reply_all: bool = False,
) -> None:
    """
    Send an email via messages().send(). Recipients/subject/threading are resolved
    through resolve_send_recipients() — the same function bot.py's proposal-text builder
    calls to render the preview Jonathan confirms, so what actually sends can never
    diverge from what he saw and approved.

    `to` is optional only when `thread_id` is given — omitting it means "reply to the
    original sender." `reply_all=True` (only meaningful with `thread_id`) additionally
    CCs everyone from the original message's To+Cc, minus Jonathan's own address and the
    resolved recipient, merged with any explicit `cc`. `cc`/`bcc` are comma-separated
    address strings. If thread_id is given, threads the reply via In-Reply-To/References
    (matching Gmail's documented requirements) and prefixes the subject with "Re: "
    (unless already present).
    """
    from email.mime.text import MIMEText

    resolved = resolve_send_recipients(thread_id, to, subject, cc, bcc, reply_all)

    msg = MIMEText(body)
    msg["To"] = resolved["to"]
    msg["Subject"] = resolved["subject"]
    if resolved["cc"]:
        msg["Cc"] = resolved["cc"]
    if resolved["bcc"]:
        msg["Bcc"] = resolved["bcc"]

    body_dict = {}
    if thread_id:
        if resolved["message_id"]:
            msg["In-Reply-To"] = resolved["message_id"]
            msg["References"] = resolved["message_id"]
        body_dict["threadId"] = thread_id

    body_dict["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    service = _build_service()
    service.users().messages().send(userId="me", body=body_dict).execute()


def get_forward_preview(thread_id: str) -> dict:
    """
    Sender/subject/date/attachments (filename, mimeType, size) and body of a thread's
    most recent message, plus the thread's total message count — surfaced so a
    >1-message thread doesn't silently forward only its most recent entry without
    Jonathan noticing. Needs a full-format fetch (unlike get_thread_summary's
    metadata-only fetch) since attachment parts and body content only appear in the
    full payload.
    """
    service = _build_service()
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))
    if not messages:
        raise ValueError(f"Thread {thread_id} has no messages.")
    last = messages[-1]
    payload = last.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    display_name, sender_email = _parse_sender(headers.get("From", ""))

    attachments = []

    def _walk(part):
        filename = part.get("filename")
        body = part.get("body", {})
        # Gmail's full-format response references most attachments via attachmentId
        # (fetched separately, see forward_email), but can inline small ones directly
        # as body.data instead — both cases are handled so a small attachment isn't
        # silently dropped from the forward.
        if filename and body.get("attachmentId"):
            attachments.append({
                "filename": filename,
                "mime_type": part.get("mimeType", "application/octet-stream"),
                "attachment_id": body["attachmentId"],
                "inline_data": None,
                "size": body.get("size", 0),
            })
        elif filename and body.get("data"):
            attachments.append({
                "filename": filename,
                "mime_type": part.get("mimeType", "application/octet-stream"),
                "attachment_id": None,
                "inline_data": body["data"],
                "size": body.get("size") or len(_decode_bytes(body["data"])),
            })
        for sub in part.get("parts", []):
            _walk(sub)

    _walk(payload)

    return {
        "gmail_message_id": last["id"],
        "sender_name": display_name,
        "sender_email": sender_email,
        "subject": headers.get("Subject", "(no subject)"),
        "date": headers.get("Date", ""),
        "to_header": headers.get("To", ""),
        "message_count": len(messages),
        "attachments": attachments,
        "body": _extract_body(payload).strip(),
    }


def forward_email(thread_id: str, to: str, cc: str | None = None, bcc: str | None = None, note: str = "") -> None:
    """
    Forwards a thread's most recent message (including attachments) to to/cc/bcc, as a
    brand-new message — no threadId, no References to the original Message-ID (explicit
    choice: the new recipient isn't part of the original conversation; the tradeoff is
    the forwarded copy won't visually group with the original thread in Sent/All Mail).

    Rejects control characters in to/cc/bcc, the original subject, and each attachment
    filename — the filename case is a genuinely new risk: it comes from the ORIGINAL
    sender, reachable via arbitrary inbound mail, not just Jonathan's own input. Refuses
    if total attachment size exceeds MAX_FORWARD_ATTACHMENT_BYTES, checked from metadata
    before downloading anything. If any single attachment fails to fetch, the whole
    forward fails rather than silently sending one with an attachment missing — nothing
    is sent until the final messages().send() call.
    """
    from email import encoders
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    preview = get_forward_preview(thread_id)

    to_pairs = _parse_addr_list(to)
    if not to_pairs:
        raise ValueError("Refusing to forward: 'to' is empty or invalid.")
    to_fmt = _format_addr_list(to_pairs)
    cc_fmt = _format_addr_list(_dedup_addr_list(_parse_addr_list(cc)))
    bcc_fmt = _format_addr_list(_dedup_addr_list(_parse_addr_list(bcc)))

    _reject_control_chars("to", to_fmt)
    if cc_fmt:
        _reject_control_chars("cc", cc_fmt)
    if bcc_fmt:
        _reject_control_chars("bcc", bcc_fmt)

    orig_subject = preview["subject"]
    _reject_control_chars("subject", orig_subject)
    subject = orig_subject if orig_subject.strip().lower().startswith("fwd:") else f"Fwd: {orig_subject}"

    total_size = sum(a["size"] for a in preview["attachments"])
    if total_size > MAX_FORWARD_ATTACHMENT_BYTES:
        raise ValueError(
            f"Refusing to forward: total attachment size ({total_size} bytes) exceeds "
            f"the {MAX_FORWARD_ATTACHMENT_BYTES}-byte limit."
        )

    for a in preview["attachments"]:
        _reject_control_chars("attachment filename", a["filename"])

    service = _build_service()

    fetched_attachments = []
    for a in preview["attachments"]:
        if a.get("inline_data"):
            raw_bytes = _decode_bytes(a["inline_data"])
        else:
            try:
                att = service.users().messages().attachments().get(
                    userId="me", messageId=preview["gmail_message_id"], id=a["attachment_id"]
                ).execute()
            except Exception as e:
                raise ValueError(f"Could not fetch attachment '{a['filename']}': {e}")
            raw_bytes = _decode_bytes(att["data"])
        fetched_attachments.append((a["filename"], a["mime_type"], raw_bytes))

    quoted = (
        f"---------- Forwarded message ----------\n"
        f"From: {preview['sender_name']} <{preview['sender_email']}>\n"
        f"Date: {preview['date']}\n"
        f"Subject: {preview['subject']}\n"
        f"To: {preview['to_header']}\n\n"
        f"{preview['body']}"
    )
    text_body = f"{note}\n\n{quoted}" if note.strip() else quoted

    msg = MIMEMultipart()
    msg["To"] = to_fmt
    if cc_fmt:
        msg["Cc"] = cc_fmt
    if bcc_fmt:
        msg["Bcc"] = bcc_fmt
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body))

    for filename, mime_type, raw_bytes in fetched_attachments:
        maintype, _, subtype = mime_type.partition("/")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(raw_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    body_dict = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    service.users().messages().send(userId="me", body=body_dict).execute()


def trash_thread(thread_id: str) -> None:
    """Move the whole thread to Trash — reversible (Gmail's own 30-day trash retention,
    recoverable via threads().untrash()), never a permanent delete."""
    service = _build_service()
    service.users().threads().trash(userId="me", id=thread_id).execute()
