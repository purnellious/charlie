"""
Scheduler — manages timed jobs.
v1: morning briefing only (creates a new Telegram topic each day).
"""

import logging
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

log = logging.getLogger(__name__)


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

        await app.bot.send_message(
            chat_id=group_id,
            message_thread_id=thread_id,
            text=f"Good morning. It's {today}.\n\nWhat does today look like for you?",
        )
        log.info(f"Morning briefing topic created: '{topic_name}' (thread_id={thread_id})")

    except Exception as e:
        log.error(f"Morning briefing failed: {e}")


async def setup_scheduler(app: Application):
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        log.warning("TELEGRAM_GROUP_ID not set — scheduler not started.")
        return

    briefing_time = os.getenv("MORNING_BRIEFING_TIME", "07:30")
    hour, minute = map(int, briefing_time.split(":"))
    timezone = os.getenv("TIMEZONE", "UTC")

    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        _create_morning_briefing,
        trigger="cron",
        hour=hour,
        minute=minute,
        args=[app],
    )
    scheduler.start()

    log.info(f"Morning briefing scheduled for {briefing_time} ({timezone}) daily.")
    app.bot_data["scheduler"] = scheduler


async def teardown_scheduler(app: Application):
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
