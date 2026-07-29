"""
Scheduler — manages timed jobs.
v1: morning briefing only (creates a new Telegram topic each day).
v2: added daily check-in reminder at configurable time (default 08:00).
v3: morning briefing includes open follow-up items from followups.md.
"""

import logging
import os
from datetime import date, datetime, timedelta

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

FOLLOWUPS_PATH = os.path.join(os.path.dirname(__file__), "..", "followups.md")


def _load_due_followups() -> list[str]:
    """Return descriptions of open follow-ups whose chase-from date is today or earlier."""
    today = date.today()
    due = []
    try:
        with open(FOLLOWUPS_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("- [ ]"):
                    continue
                if "chase from:" not in line:
                    continue
                # Extract chase-from date
                after_chase = line.split("chase from:")[1]
                chase_str = after_chase.split("|")[0].strip()
                try:
                    chase_date = date.fromisoformat(chase_str)
                except ValueError:
                    continue
                if chase_date <= today:
                    # Extract description (between "- [ ] " and first "|")
                    desc = line[len("- [ ] "):].split("|")[0].strip()
                    due.append(desc)
    except FileNotFoundError:
        pass
    return due


async def _create_morning_briefing(app: Application):
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return

    today = datetime.now().strftime("%A, %d %B %Y")
    topic_name = f"Morning — {datetime.now().strftime('%d %b')}"

    try:
        forum_topic = await app.bot.create_forum_topic(
            chat_id=group_id,
            name=topic_name,
        )
        thread_id = forum_topic.message_thread_id

        due_followups = _load_due_followups()
        followup_section = ""
        if due_followups:
            items = "\n".join(f"• {d}" for d in due_followups)
            followup_section = f"\n\n**Follow-ups**\n{items}"

        message_text = f"Good morning. It's {today}.{followup_section}\n\nWhat does today look like for you?"
        await proactive_send(app, group_id, thread_id, message_text)
        log.info(f"Morning briefing topic created: '{topic_name}' (thread_id={thread_id})")

    except Exception as e:
        log.error(f"Morning briefing failed: {e}")


async def _create_checkin(app: Application):
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return

    topic_name = f"Check-in — {datetime.now().strftime('%d %b %Y')}"

    try:
        forum_topic = await app.bot.create_forum_topic(
            chat_id=group_id,
            name=topic_name,
        )
        thread_id = forum_topic.message_thread_id

        message_text = "Good morning Jonathan — daily context check-in. I have 5 questions for you today. Ready when you are."
        await proactive_send(app, group_id, thread_id, message_text)
        log.info(f"Check-in topic created: '{topic_name}' (thread_id={thread_id})")

    except Exception as e:
        log.error(f"Check-in creation failed: {e}")


async def _send_news_briefing(app: Application, is_retry: bool = False):
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return

    topic_name = f"News — {datetime.now().strftime('%d %b')}"

    try:
        from core.tools.news import generate_briefing
        briefing = generate_briefing()

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
    Run the weekly artist grant finder pipeline and send the email.
    Runs synchronous I/O (scraping, SMTP) in an executor to avoid blocking the event loop.
    """
    import asyncio
    try:
        from core.tools.grants import run_grants_pipeline
        from core.tools.grants_email import format_grants_email, send_grants_email

        loop = asyncio.get_event_loop()

        def _sync_run():
            results = run_grants_pipeline(dry_run=False)
            subject, html_body = format_grants_email(results)
            success = send_grants_email(subject, html_body)
            return len(results), success

        count, success = await loop.run_in_executor(None, _sync_run)
        if success:
            log.info(f"Grants pipeline complete: {count} opportunities emailed")
        else:
            log.error(f"Grants pipeline: {count} opportunities found but email send failed")
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


async def setup_scheduler(app: Application):
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        log.warning("TELEGRAM_GROUP_ID not set — scheduler not started.")
        return

    briefing_time = os.getenv("MORNING_BRIEFING_TIME", "07:30")
    briefing_hour, briefing_minute = map(int, briefing_time.split(":"))

    checkin_time = os.getenv("CHECKIN_TIME", "08:00")
    checkin_hour, checkin_minute = map(int, checkin_time.split(":"))

    news_time = os.getenv("NEWS_BRIEFING_TIME", "12:00")
    news_hour, news_minute = map(int, news_time.split(":"))

    timezone = os.getenv("TIMEZONE", "UTC")

    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        _create_morning_briefing,
        trigger="cron",
        hour=briefing_hour,
        minute=briefing_minute,
        args=[app],
    )
    scheduler.add_job(
        _create_checkin,
        trigger="cron",
        hour=checkin_hour,
        minute=checkin_minute,
        args=[app],
    )
    scheduler.add_job(
        _send_news_briefing,
        trigger="cron",
        hour=news_hour,
        minute=news_minute,
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
    scheduler.start()

    log.info(f"Morning briefing scheduled for {briefing_time} ({timezone}) daily.")
    log.info(f"Check-in scheduled for {checkin_time} ({timezone}) daily.")
    log.info(f"News briefing scheduled for {news_time} ({timezone}) daily.")
    log.info(f"Bug topic reconciliation scheduled for 03:00 ({timezone}) daily.")
    log.info(f"Grant finder pipeline scheduled for Monday 08:00 ({timezone}) weekly.")
    log.info(f"Retention sweep scheduled for 04:00 ({timezone}) daily.")
    app.bot_data["scheduler"] = scheduler


async def teardown_scheduler(app: Application):
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
