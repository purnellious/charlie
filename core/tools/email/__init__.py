"""
Email monitor tool — polls Jonathan's inbox, triages each new message with
Claude Haiku, and pushes one batched Telegram digest per poll. gmail.modify
scope — supports archive/mark-read/mark-unread (on-request only, no
autonomous use). No send, reply, forward, or delete capability exists
anywhere in this tool.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram.error import BadRequest

from core.scheduler import proactive_send
from . import db
from .fetch import fetch_new_emails
from .triage import triage_email

log = logging.getLogger(__name__)

TOPIC_NAME = "📧 Email"
FIRST_RUN_BACKFILL_HOURS = 24
MAX_CHUNK = 4000


def _format_digest(rows: list) -> str:
    count = len(rows)
    lines = [f"📧 {count} new email{'s' if count != 1 else ''}\n"]
    for r in rows:
        if r["actionability"] == "ACTIONABLE":
            action_line = f"Action needed: {r['summary']}"
        elif r["actionability"] == "RECOMMENDATION":
            action_line = f"FYI: {r['summary']}"
        else:
            action_line = f"No action needed: {r['summary']}"
        lines.append(
            f"From: {r['sender_name']} <{r['sender_email']}> — {r['subject']} "
            f"[#{r['id']}, thread={r['thread_id']}]\n"
            f"  {action_line}\n"
        )
    return "\n".join(lines).strip()


def _chunks(text: str) -> list[str]:
    return [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]


async def _get_or_create_topic(app, group_id: str) -> int:
    topic_id = db.get_email_topic_id()
    if topic_id:
        return topic_id
    forum_topic = await app.bot.create_forum_topic(chat_id=group_id, name=TOPIC_NAME)
    topic_id = forum_topic.message_thread_id
    db.set_email_topic_id(topic_id)
    log.info(f"Created Email topic (thread_id={topic_id})")
    return topic_id


def _poll_sync() -> int:
    """
    Synchronous fetch+triage+db-write pipeline, run inside asyncio.to_thread
    by poll_and_notify so a slow Gmail/Haiku round-trip never blocks the
    bot's event loop. Returns the count of newly-triaged emails (rows
    themselves are read back via db.get_unnotified() by the caller, so a
    prior send failure's leftovers are picked up too, not just this call's).
    """
    db.init_db()

    last_synced = db.get_last_synced()
    if last_synced is None:
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_BACKFILL_HOURS)).isoformat()
    else:
        since_iso = last_synced

    fetched = fetch_new_emails(since_iso)
    now_iso = datetime.now(timezone.utc).isoformat()

    unseen = db.filter_unseen(fetched)
    if not unseen:
        db.set_last_synced(now_iso)
        return 0

    new_count = 0
    for email in unseen:
        verdict = triage_email(email)
        row_id = db.insert_triaged_email(email, verdict)
        if row_id is None:
            continue  # defensive — filter_unseen should already exclude duplicates
        new_count += 1

    # Only advance the cursor after every fetched email has been inserted —
    # if triage/insert raises partway through, the next poll re-fetches this
    # same window instead of permanently skipping whatever wasn't saved yet.
    db.set_last_synced(now_iso)

    db.prune_old_emails()
    return new_count


async def poll_and_notify(app, group_id: str) -> None:
    await asyncio.to_thread(_poll_sync)

    unnotified = await asyncio.to_thread(db.get_unnotified)
    if not unnotified:
        return

    topic_id = await _get_or_create_topic(app, group_id)
    digest = _format_digest(unnotified)
    chunks = _chunks(digest)
    for i, chunk in enumerate(chunks):
        await proactive_send(app, group_id, topic_id, chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)  # matches bugs.py's convention for rapid Telegram sends

    for row in unnotified:
        await asyncio.to_thread(db.mark_notified, row["id"])


async def send_alert(app, group_id: str, text: str) -> None:
    """Used by the scheduler's consecutive-failure alert — ensures the topic exists first."""
    topic_id = await _get_or_create_topic(app, group_id)
    await proactive_send(app, group_id, topic_id, text)


async def reconcile_email_topic(app, group_id: str) -> None:
    """
    Daily check that the persisted Email topic still exists in Telegram.
    Duplicates the probe technique used elsewhere in this codebase
    (unpin_all_forum_topic_messages as an existence check, fail-safe on
    anything unexpected) as a small local helper rather than importing
    another tool's private internals.
    """
    db.init_db()
    topic_id = db.get_email_topic_id()
    if not topic_id:
        return
    try:
        await app.bot.unpin_all_forum_topic_messages(chat_id=group_id, message_thread_id=topic_id)
    except BadRequest as e:
        err = str(e).lower()
        if "thread" in err or "message_thread" in err:
            log.warning(f"Email topic {topic_id} is gone — clearing so it's recreated on next poll")
            db.set_email_topic_id(None)
        else:
            log.warning(f"Unexpected error checking Email topic {topic_id}: {e}")
    except Exception as e:
        log.warning(f"Could not verify Email topic {topic_id}: {e}")
