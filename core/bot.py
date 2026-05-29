"""
Charlie — Telegram bot entry point.
Receives messages from a Telegram forum group with topics.
Every topic routes to the Charlie agent.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# Ensure ~/charlie is on sys.path for core.* imports
_CHARLIE_ROOT = str(Path(__file__).parent.parent)
if _CHARLIE_ROOT not in sys.path:
    sys.path.insert(0, _CHARLIE_ROOT)

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from core.history import init_db, load_history, save_message
from core.scheduler import setup_scheduler, teardown_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "").strip()
THINKING_ENABLED = os.getenv("THINKING_ENABLED", "true").lower() == "true"
THINKING_BUDGET = int(os.getenv("THINKING_BUDGET", "2000"))

# Pending charlie.md update proposals — keyed by topic_id
PENDING_UPDATES: dict[int, dict] = {}

# Tracks topics with an active agent turn in progress — prevents overlapping calls
ACTIVE_TOPICS: set[int] = set()


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = str(update.effective_chat.id)
    if chat_id != GROUP_ID:
        return

    topic_id = update.message.message_thread_id
    if topic_id is None:
        return  # Ignore general chat outside topics

    # Transcribe voice messages
    if update.message.voice:
        try:
            file = await context.bot.get_file(update.message.voice.file_id)
            audio_bytes = bytes(await file.download_as_bytearray())
            from core.transcribe import transcribe_voice
            user_text = await transcribe_voice(audio_bytes)
            if not user_text:
                await update.message.reply_text("Couldn't make out that voice message.")
                return
        except Exception as e:
            log.error(f"Transcription failed: {e}")
            await update.message.reply_text("Voice transcription failed — try again or type it.")
            return
    elif update.message.text:
        user_text = update.message.text
    else:
        return

    # Handle approve/reject for pending charlie.md update proposals
    if topic_id in PENDING_UPDATES:
        response_lower = user_text.strip().lower()
        if response_lower in ("approve", "yes", "ok", "save"):
            update_data = PENDING_UPDATES.pop(topic_id)
            _write_charlie_doc(update_data["proposed_content"])
            await update.message.reply_text("charlie.md updated.")
            return
        elif response_lower in ("reject", "no", "skip", "discard"):
            PENDING_UPDATES.pop(topic_id)
            await update.message.reply_text("Update discarded.")
            return

    # Prevent overlapping turns in the same topic
    if topic_id in ACTIVE_TOPICS:
        await update.message.reply_text("Still thinking — please wait.")
        return

    ACTIVE_TOPICS.add(topic_id)
    try:
        await _run_charlie_turn(update, context, topic_id, user_text)
    finally:
        ACTIVE_TOPICS.discard(topic_id)


async def _run_charlie_turn(update, context, topic_id: int, user_text: str):
    from core.agent import handle_turn

    async def send_fn(text: str):
        try:
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=text,
                message_thread_id=topic_id,
            )
        except Exception as e:
            log.warning(f"send_fn failed: {e}")

    messages = load_history(topic_id)
    old_count = len(messages)

    try:
        updated_messages, proposed_update = await handle_turn(
            user_text=user_text,
            messages=messages,
            send_fn=send_fn,
            thinking_enabled=THINKING_ENABLED,
            thinking_budget=THINKING_BUDGET,
        )
    except Exception as e:
        log.error(f"Agent error in topic {topic_id}: {e}")
        await send_fn(f"Something went wrong — {e}")
        return

    # Save the new messages (user message + assistant turn(s))
    new_messages = updated_messages[old_count:]
    for msg in new_messages:
        save_message(topic_id, msg["role"], msg["content"])

    # Handle a proposed charlie.md update
    if proposed_update:
        PENDING_UPDATES[topic_id] = proposed_update
        preview = proposed_update["proposed_content"]
        if len(preview) > 800:
            preview = preview[:800] + "\n...[truncated]"
        await send_fn(
            f"Proposed update to charlie.md\n\n"
            f"Reason: {proposed_update['reason']}\n\n"
            f"---\n{preview}\n---\n\n"
            f"Reply 'approve' to save or 'reject' to discard."
        )


def _write_charlie_doc(content: str):
    charlie_doc = Path(__file__).parent.parent / "charlie.md"
    charlie_doc.write_text(content)
    log.info("charlie.md updated")


async def on_topic_created(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greet when a new topic is opened in the group."""
    if str(update.effective_chat.id) != GROUP_ID:
        return
    topic = update.message.forum_topic_created
    if topic:
        log.info(f"New topic created: '{topic.name}'")
        await update.message.reply_text(
            f"New topic: {topic.name}. What are we working on?"
        )


async def post_init(app: Application):
    await setup_scheduler(app)


async def post_shutdown(app: Application):
    await teardown_scheduler(app)


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing from .env")
    if not GROUP_ID:
        raise SystemExit("TELEGRAM_GROUP_ID is missing from .env")

    init_db()
    log.info("Charlie is starting...")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.VOICE) & ~filters.COMMAND,
        on_message,
    ))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.FORUM_TOPIC_CREATED,
        on_topic_created,
    ))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
