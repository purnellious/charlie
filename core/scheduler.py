"""
Scheduler — manages timed jobs.
v1: morning briefing only (creates a new Telegram topic each day).
v2: added daily check-in reminder at configurable time (default 08:00).
v3: morning briefing includes open follow-up items from followups.md.
v4 (Morning Briefing v2, Aug 2026): replaced the thin v3 briefing and the
separate daily check-in with one dynamic planner (core/tools/briefing.py) —
weekdays only, sharing its RSS fetch with the news job. See devlog.md.
"""

import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

from core.history import save_message

log = logging.getLogger(__name__)


async def proactive_send(app: Application, group_id: str, thread_id: int, text: str):
    """
    Send a proactive message into a topic AND save it to conversation history.
    All scheduler jobs must use this instead of calling app.bot.send_message directly —
    otherwise Charlie has no memory of what it said when the user replies.
    """
    await app.bot.send_message(
        chat_id=group_id,
        message_thread_id=thread_id,
        text=text,
    )
    save_message(thread_id, "assistant", text)

CHARLIE_DOC_PATH = os.path.join(os.path.dirname(__file__), "..", "charlie.md")
CONTEXT_ARCHIVE_PATH = os.path.join(os.path.dirname(__file__), "..", "context-archive.md")


def _load_charlie_context() -> str:
    try:
        with open(CHARLIE_DOC_PATH, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _load_context_archive_excerpt(max_entries: int = 5) -> str:
    """Last `max_entries` distilled entries from context-archive.md, passed as a
    plain string — core/tools/briefing.py deliberately does no file I/O of its
    own for this (build review, round 3), so the read and the bounding both
    happen here. Entries are separated by a bare '---' line, matching /distil's
    existing append format (see core/bot.py's _append_to_context_archive)."""
    try:
        with open(CONTEXT_ARCHIVE_PATH, "r") as f:
            content = f.read()
    except FileNotFoundError:
        return ""
    entries = [e.strip() for e in content.split("\n---\n") if e.strip()]
    return "\n\n---\n\n".join(entries[-max_entries:])


async def _send_morning_briefing(app: Application, dry_run: bool = False):
    """Morning Briefing v2 — replaces both the old thin _create_morning_briefing
    and the separate daily _create_checkin (Jonathan's explicit call, 13 Aug
    2026 design: one message, not two). Runs weekdays only; see setup_scheduler
    for the Monday-vs-Tue-Fri timing split."""
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return

    try:
        import asyncio
        from core.tools.briefing import build_briefing_text

        charlie_context = _load_charlie_context()
        archive_excerpt = _load_context_archive_excerpt()
        message_text, fired_reminder_ids, today = await asyncio.to_thread(
            build_briefing_text, charlie_context, archive_excerpt, dry_run
        )

        if dry_run:
            log.info(f"Morning briefing (dry run):\n{message_text}")
            return

        topic_name = f"Morning — {datetime.now().strftime('%d %b')}"
        forum_topic = await app.bot.create_forum_topic(chat_id=group_id, name=topic_name)
        thread_id = forum_topic.message_thread_id
        await proactive_send(app, group_id, thread_id, message_text)
        log.info(f"Morning briefing topic created: '{topic_name}' (thread_id={thread_id})")

        # Only mark reminders as shown once they've actually reached Jonathan
        # (code review: advancing this inside build_briefing_text, before the
        # topic/send above, meant a Telegram failure here still silently
        # consumed a one-off or pushed a recurring reminder a full cycle
        # forward with no retry, unlike _send_news_briefing).
        if fired_reminder_ids:
            from core.tools.reminders import advance_after_briefing
            await asyncio.to_thread(advance_after_briefing, fired_reminder_ids, today)

    except Exception as e:
        log.error(f"Morning briefing failed: {e}")


async def _send_news_briefing(app: Application, is_retry: bool = False):
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return

    topic_name = f"News — {datetime.now().strftime('%d %b')}"

    try:
        import asyncio
        from core.tools.news import generate_briefing
        briefing = await asyncio.to_thread(generate_briefing)

        forum_topic = await app.bot.create_forum_topic(
            chat_id=group_id,
            name=topic_name,
        )
        thread_id = forum_topic.message_thread_id
        await proactive_send(app, group_id, thread_id, briefing)
        log.info(f"News briefing topic created: '{topic_name}' (thread_id={thread_id})")

    except Exception as e:
        attempt = "retry" if is_retry else "attempt"
        log.error(f"News briefing {attempt} failed: {e}")
        if not is_retry:
            scheduler = app.bot_data.get("scheduler")
            if scheduler:
                run_at = datetime.now() + timedelta(minutes=5)
                scheduler.add_job(
                    _send_news_briefing,
                    trigger="date",
                    run_date=run_at,
                    args=[app],
                    kwargs={"is_retry": True},
                )
                log.info(f"News briefing retry scheduled for {run_at.strftime('%H:%M:%S')}")
            else:
                log.error("News briefing: scheduler unavailable, cannot schedule retry.")


async def _run_grants_pipeline(app: Application):
    """
    Run the weekly artist grant finder pipeline (Art Grant Finder V2 — multi-artist):
    sync artist profiles from the Google Sheet, scrape/validate opportunities (unchanged
    persistence timing — a matching failure below can never affect this), run per-artist
    matching, attempt delivery of personalized digests, then send Jonathan an admin
    summary. Runs synchronous I/O (scraping, SMTP, Anthropic calls) in an executor to
    avoid blocking the event loop.
    """
    import asyncio
    try:
        from core.tools.grants import (
            run_grants_pipeline, sync_artist_profiles, get_new_signups,
            run_matching, run_delivery, _today, init_db,
        )
        from core.tools.grants_email import (
            format_admin_summary, format_no_artists_email, send_grants_email,
        )

        sheet_url = os.getenv("ARTIST_PROFILES_SHEET_URL", "").strip()
        loop = asyncio.get_event_loop()

        def _sync_run():
            # Computed once and threaded into sync/matching/delivery/new-signups below —
            # not each independently calling _today(), which could drift if the run ever
            # straddled midnight. Note this does NOT reach validate_L2/validate_L3 inside
            # run_grants_pipeline() — those still call _today() independently; unchanged,
            # pre-existing behavior from before this multi-artist rework, not reopened here.
            run_today = _today()

            # init_db() is otherwise only ever called from inside run_grants_pipeline()
            # below — but sync_artist_profiles runs BEFORE that, so on a database that's
            # never run the multi-artist migration yet, the sync would crash with "no
            # such table: artist_profiles" on its very first real run. Idempotent
            # (CREATE TABLE IF NOT EXISTS), so calling it again here is free.
            init_db()

            if sheet_url:
                sync_artist_profiles(sheet_url, run_today)
            else:
                log.warning("ARTIST_PROFILES_SHEET_URL not set — skipping artist profile sync")

            results = run_grants_pipeline(dry_run=False)
            match_stats = run_matching(run_today)
            delivery_stats = run_delivery(run_today)

            if not match_stats:
                # No active, past-onboarding-delay artists this run (Sheet unset/empty,
                # or everyone's still in their onboarding delay) — without this, the
                # admin summary below would be the ONLY email sent, and it's just counts
                # with no actual listing. Send the raw scrape as a real, useful digest
                # instead, matching the old single-recipient behavior.
                subject, html_body = format_no_artists_email(results)
                send_grants_email(subject, html_body)
                log.warning(
                    "Grants pipeline: no active artist profiles this run — sent the raw "
                    "scrape listing to the admin instead of personalized digests"
                )

            admin_stats = {
                "scraped": len(results),
                "evaluated": sum(s.get("evaluated", 0) for s in match_stats.values()),
                "per_artist": delivery_stats["per_artist"],
                "new_signups": get_new_signups(run_today),
                "send_failures": delivery_stats["send_failures"],
                "expired_undelivered": delivery_stats["expired_undelivered"],
            }
            subject, html_body = format_admin_summary(admin_stats)
            admin_sent = send_grants_email(subject, html_body)  # GRANT_RECIPIENT_EMAIL

            return len(results), admin_sent

        count, admin_sent = await loop.run_in_executor(None, _sync_run)
        if admin_sent:
            log.info(f"Grants pipeline complete: {count} opportunities scraped, admin summary sent")
        else:
            log.error(f"Grants pipeline: {count} opportunities scraped but admin summary send failed")
    except Exception as e:
        log.error(f"Grants pipeline failed: {e}")


async def _reconcile_bug_topics(app: Application):
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return
    try:
        from core.tools.bugs import reconcile_bug_topics
        recreated = await reconcile_bug_topics(app.bot, group_id)
        if recreated:
            log.info(f"Bug topic reconciliation recreated: {', '.join(recreated)}")
        else:
            log.info("Bug topic reconciliation: all topics intact.")
    except Exception as e:
        log.error(f"Bug topic reconciliation failed: {e}")


async def _run_retention_sweep(app: Application):
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return
    try:
        from core.tools.retention import run_retention_sweep
        await run_retention_sweep(app, group_id)
    except Exception as e:
        log.error(f"Retention sweep failed: {e}")


async def _run_larica_briefing(app: Application):
    """
    Fetch news, summarise with Sonnet, and email Larica's daily briefing.
    Silent — no Telegram notification.
    """
    import asyncio
    try:
        from core.tools.larica_news import run_larica_pipeline

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: run_larica_pipeline(dry_run=False))

        if result["email_sent"]:
            log.info(
                f"Larica briefing complete: "
                f"{sum(len(s['stories']) for s in result['sections'])} stories across "
                f"{len(result['sections'])} sections"
            )
        else:
            log.error("Larica briefing: pipeline ran but email send failed")
        if result["errors"]:
            log.warning(f"Larica briefing feed errors: {result['errors']}")
    except Exception as e:
        log.error(f"Larica briefing failed: {e}")


_email_fail_count = 0


async def _poll_inbox_email(app: Application):
    """
    Runs every 2 minutes. A failed poll self-heals at the next tick — unlike
    the cron jobs above, there's no missed-slot retry logic needed here.
    After 3 consecutive failures (~6 min), sends one bounded Telegram alert
    so a silently-broken/expired credential doesn't just look like an
    unexplained absence of email notifications. Re-alerts every 30 failures
    thereafter (~hourly) rather than just once, so a sustained outage (e.g.
    a revoked token that never gets fixed) doesn't quietly degrade back into
    the exact silent-failure mode this alert exists to catch.
    """
    global _email_fail_count
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return
    try:
        from core.tools.email import poll_and_notify
        await poll_and_notify(app, group_id)
        _email_fail_count = 0
    except Exception as e:
        _email_fail_count += 1
        log.error(f"Email inbox poll failed ({_email_fail_count} in a row): {e}")
        if _email_fail_count == 3 or _email_fail_count % 30 == 0:
            try:
                from core.tools.email import send_alert
                await send_alert(
                    app, group_id,
                    f"Email monitor has failed {_email_fail_count} times in a row. Last error: {e}",
                )
            except Exception as alert_e:
                log.error(f"Email monitor failure alert itself failed: {alert_e}")


HEARTBEAT_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "heartbeat.txt")


async def _heartbeat(app: Application):
    """
    Writes a timestamp only after a genuinely successful Telegram API call —
    unlike 'job executed successfully' in the log (which fires even when a
    poll inside the job failed and was caught), this is a real functional-
    health signal the standalone watchdog.py script can trust.
    """
    try:
        await app.bot.get_me()
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(datetime.now().timestamp()))
    except Exception as e:
        log.warning(f"Heartbeat check failed: {e}")


async def setup_scheduler(app: Application):
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        log.warning("TELEGRAM_GROUP_ID not set — scheduler not started.")
        return

    # Shared daily slot: the news job runs here every day, and the weekday
    # morning briefing (Tue-Fri) shares it — both fire at the same instant so
    # they share one RSS fetch (see news.get_fetched_articles). Monday's
    # briefing runs 10 minutes earlier to avoid colliding with the Monday
    # 08:00 grants pipeline (Jonathan's explicit call, 13 Aug 2026 design) —
    # derived as an offset, not a separate env var, so it can't drift out of
    # sync with the base time if that's ever reconfigured.
    briefing_time = os.getenv("MORNING_BRIEFING_TIME", "08:00")
    briefing_hour, briefing_minute = map(int, briefing_time.split(":"))
    _monday_dt = datetime(2000, 1, 1, briefing_hour, briefing_minute) - timedelta(minutes=10)
    monday_hour, monday_minute = _monday_dt.hour, _monday_dt.minute

    timezone = os.getenv("TIMEZONE", "UTC")

    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        _send_morning_briefing,
        trigger="cron",
        day_of_week="mon",
        hour=monday_hour,
        minute=monday_minute,
        id="morning_briefing_monday",
        replace_existing=True,
        args=[app],
    )
    scheduler.add_job(
        _send_morning_briefing,
        trigger="cron",
        day_of_week="tue-fri",
        hour=briefing_hour,
        minute=briefing_minute,
        id="morning_briefing_tuefri",
        replace_existing=True,
        args=[app],
    )
    scheduler.add_job(
        _send_news_briefing,
        trigger="cron",
        hour=briefing_hour,
        minute=briefing_minute,
        args=[app],
    )
    scheduler.add_job(
        _reconcile_bug_topics,
        trigger="cron",
        hour=3,
        minute=0,
        args=[app],
    )
    scheduler.add_job(
        _run_grants_pipeline,
        trigger="cron",
        day_of_week="mon",
        hour=8,
        minute=0,
        args=[app],
    )
    scheduler.add_job(
        _run_retention_sweep,
        trigger="cron",
        hour=4,
        minute=0,
        args=[app],
    )
    scheduler.add_job(
        _run_larica_briefing,
        trigger="cron",
        hour=8,
        minute=0,
        timezone="America/New_York",
        id="larica_morning_briefing",
        replace_existing=True,
        args=[app],
    )
    scheduler.add_job(
        _poll_inbox_email,
        trigger="interval",
        minutes=2,
        args=[app],
    )
    scheduler.add_job(
        _heartbeat,
        trigger="interval",
        minutes=3,
        args=[app],
    )
    scheduler.start()

    log.info(f"Morning briefing scheduled for {monday_hour:02d}:{monday_minute:02d} Monday, "
             f"{briefing_time} Tue-Fri ({timezone}).")
    log.info(f"News briefing scheduled for {briefing_time} ({timezone}) daily.")
    log.info(f"Bug topic reconciliation scheduled for 03:00 ({timezone}) daily.")
    log.info(f"Grant finder pipeline scheduled for Monday 08:00 ({timezone}) weekly.")
    log.info(f"Retention sweep scheduled for 04:00 ({timezone}) daily.")
    log.info("Larica morning briefing scheduled for 08:00 America/New_York daily.")
    log.info("Email inbox poll scheduled every 2 minutes.")
    log.info("Heartbeat scheduled every 3 minutes.")
    app.bot_data["scheduler"] = scheduler


async def teardown_scheduler(app: Application):
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
