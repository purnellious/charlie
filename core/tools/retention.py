"""
Message history retention sweep (BUG-001).

Raw conversation history in charlie.db has no expiry by default, which both accumulates
sensitive content indefinitely and contradicts Data Minimalism. This sweep deletes stale
topic history after a period of inactivity — "inactivity" measured from Jonathan's own
last message, not Charlie's own scheduled posts, so a topic he's still iterating on
(even weekly) is never touched. Small topics are cleaned up silently; larger ones get a
warning and a grace period first, cancelled by simply replying.
"""

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

INACTIVE_DAYS = 60
SMALL_TOPIC_THRESHOLD = 20  # messages; at or below this, delete silently once stale
GRACE_DAYS = 7


async def run_retention_sweep(app, group_id: str):
    from core.history import (
        get_stale_topics, get_warning, set_warning, clear_warning,
        get_all_warned_topic_ids, delete_topic_history,
    )
    from core.scheduler import proactive_send

    deleted = []
    warned = []

    stale_topics = get_stale_topics(INACTIVE_DAYS)
    stale_ids = {t["topic_id"] for t in stale_topics}

    # A warned topic whose activity has since resumed (Jonathan replied) no longer shows
    # up as stale — clear its warning so a *future* stale period starts a fresh grace
    # window instead of looking like it already elapsed.
    for warned_topic_id in get_all_warned_topic_ids():
        if warned_topic_id not in stale_ids:
            clear_warning(warned_topic_id)

    for topic in stale_topics:
        topic_id = topic["topic_id"]
        msg_count = topic["msg_count"]

        if msg_count <= SMALL_TOPIC_THRESHOLD:
            delete_topic_history(int(topic_id))
            deleted.append((topic_id, msg_count, "small, silent"))
            continue

        warned_at = get_warning(topic_id)

        if warned_at is None:
            text = (
                f"This topic has been inactive for {INACTIVE_DAYS}+ days and has "
                f"{msg_count} messages. To keep conversation history from accumulating "
                f"indefinitely, it's scheduled for deletion in {GRACE_DAYS} days. Reply "
                "here if you want to keep it — otherwise I'll take care of it."
            )
            try:
                await proactive_send(app, group_id, int(topic_id), text)
                set_warning(topic_id)
                warned.append(topic_id)
            except Exception as e:
                log.warning(f"Failed to send retention warning for topic {topic_id}: {e}")
            continue

        warned_dt = datetime.strptime(warned_at, "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() - warned_dt >= timedelta(days=GRACE_DAYS):
            delete_topic_history(int(topic_id))
            deleted.append((topic_id, msg_count, "grace period elapsed"))

    if deleted:
        log.info(f"Retention sweep: deleted {len(deleted)} topic(s) — {deleted}")
    if warned:
        log.info(f"Retention sweep: warned {len(warned)} topic(s) — {warned}")
    if not deleted and not warned:
        log.info("Retention sweep: nothing to do.")
