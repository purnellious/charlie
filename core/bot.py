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
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from core.history import init_db, load_history, save_message, delete_topic_history
from core.scheduler import setup_scheduler, teardown_scheduler
from core import state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "").strip()
THINKING_ENABLED = os.getenv("THINKING_ENABLED", "true").lower() == "true"
THINKING_BUDGET = int(os.getenv("THINKING_BUDGET", "2000"))

# Pending charlie.md update proposals — keyed by topic_id
PENDING_UPDATES: dict[int, dict] = {}

# Pending distillation proposals — keyed by topic_id
# Value: {"distillate": str} or {"distillate": "NOTHING_TO_KEEP"}
PENDING_DISTIL: dict[int, dict] = {}

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

    # Handle approve/reject/discard for pending distillation proposals
    if topic_id in PENDING_DISTIL:
        response_lower = user_text.strip().lower()
        pending = PENDING_DISTIL.get(topic_id, {})
        distillate = pending.get("distillate", "NOTHING_TO_KEEP")

        if response_lower == "approve":
            PENDING_DISTIL.pop(topic_id)
            if distillate != "NOTHING_TO_KEEP":
                _append_to_context_archive(distillate)
                await update.message.reply_text("Saved to context-archive.md. Conversation history deleted.")
            else:
                await update.message.reply_text("Nothing to archive. Conversation history deleted.")
            delete_topic_history(topic_id)
            return
        elif response_lower == "discard":
            PENDING_DISTIL.pop(topic_id)
            delete_topic_history(topic_id)
            await update.message.reply_text("Conversation history deleted. Nothing archived.")
            return
        elif response_lower == "reject":
            PENDING_DISTIL.pop(topic_id)
            await update.message.reply_text("Distillation rejected. History kept — run /distil again if you want to retry.")
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


async def on_meta_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /meta — ruthless review, then Charlie's reaction."""
    if not update.message:
        return

    chat_id = str(update.effective_chat.id)
    if chat_id != GROUP_ID:
        return

    topic_id = update.message.message_thread_id
    if topic_id is None:
        await update.message.reply_text("/meta only works inside a topic.")
        return

    async def send(text: str):
        try:
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=text,
                message_thread_id=topic_id,
            )
        except Exception as e:
            log.warning(f"Meta send failed: {e}")

    await send("Running meta-review — one moment...")

    # Load conversation history before anything is saved to DB
    history = load_history(topic_id)

    # Step 1+2: Build transcript → fresh Claude review → post it
    try:
        from core.tools.meta import run_meta_review
        review = await run_meta_review(topic_id)
    except Exception as e:
        log.error(f"Meta review failed for topic {topic_id}: {e}")
        await send(f"Meta review failed: {e}")
        return

    chunk_size = 4000
    for i in range(0, len(review), chunk_size):
        await send(review[i:i + chunk_size])

    # Step 3: Charlie reacts with full conversation history + meta review as final user message
    charlie_instruction = (
        "Here is the /meta critic's analysis of the above conversation:\n\n"
        f"{review}"
    )

    label_sent = False
    charlie_take_parts: list[str] = []

    async def charlie_send_fn(text: str):
        nonlocal label_sent
        if not text.startswith("| "):
            charlie_take_parts.append(text)
        if not label_sent and not text.startswith("| "):
            text = "**Charlie's take:**\n\n" + text
            label_sent = True
        await send(text)

    try:
        from core.agent import handle_turn
        _, proposed_update = await handle_turn(
            user_text=charlie_instruction,
            messages=history,
            send_fn=charlie_send_fn,
            thinking_enabled=THINKING_ENABLED,
            thinking_budget=THINKING_BUDGET,
        )

        # Persist both outputs to history
        save_message(topic_id, "assistant", "[/meta review]\n\n" + review)
        if charlie_take_parts:
            save_message(topic_id, "assistant", "[Charlie's take]\n\n" + "\n".join(charlie_take_parts))

        if proposed_update:
            PENDING_UPDATES[topic_id] = proposed_update
            preview = proposed_update["proposed_content"]
            if len(preview) > 800:
                preview = preview[:800] + "\n...[truncated]"
            await send(
                f"Proposed update to charlie.md\n\n"
                f"Reason: {proposed_update['reason']}\n\n"
                f"---\n{preview}\n---\n\n"
                f"Reply 'approve' to save or 'reject' to discard."
            )
    except Exception as e:
        log.error(f"Charlie's take failed for topic {topic_id}: {e}")
        await send(f"Charlie's take failed: {e}")


async def on_distil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /distil — distil topic into context-archive.md, then delete raw history."""
    if not update.message:
        return

    chat_id = str(update.effective_chat.id)
    if chat_id != GROUP_ID:
        return

    topic_id = update.message.message_thread_id
    if topic_id is None:
        await update.message.reply_text("/distil only works inside a topic.")
        return

    await update.message.reply_text("Distilling conversation — one moment...")

    try:
        from core.tools.distil import run_distil
        distillate = await run_distil(topic_id)
    except Exception as e:
        log.error(f"Distillation failed for topic {topic_id}: {e}")
        await update.message.reply_text(f"Distillation failed: {e}")
        return

    PENDING_DISTIL[topic_id] = {"distillate": distillate}

    if distillate == "NOTHING_TO_KEEP":
        await update.message.reply_text(
            "Nothing worth archiving in this conversation.\n\n"
            "Reply 'approve' to delete the history, or 'reject' to keep it."
        )
    else:
        preview = distillate if len(distillate) <= 1200 else distillate[:1200] + "\n...[truncated]"
        await update.message.reply_text(
            f"Proposed archive entry:\n\n{preview}\n\n"
            f"'approve' — save to context-archive.md and delete history\n"
            f"'discard' — delete history without saving\n"
            f"'reject' — keep history and try again"
        )


def _append_to_context_archive(distillate: str):
    archive = Path(__file__).parent.parent / "context-archive.md"
    if archive.exists():
        content = archive.read_text()
        content = content.replace("\n\n*No entries yet.*", "")
        content = content.rstrip() + f"\n\n{distillate}\n\n---"
    else:
        content = f"# Charlie — Context Archive\n\n---\n\n{distillate}\n\n---"
    archive.write_text(content)
    log.info("context-archive.md updated")


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


async def on_create_bug_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/createbugtopics — batch-create Telegram topics for all open bugs without one."""
    if str(update.effective_chat.id) != GROUP_ID:
        return
    await update.message.reply_text("Creating topics for all open bugs without one...")
    from core.tools.bugs import create_topics_for_all_open_bugs
    created = await create_topics_for_all_open_bugs()
    if created:
        await update.message.reply_text(f"Done. Created topics for: {', '.join(created)}")
    else:
        await update.message.reply_text("No open bugs needed a topic.")


async def on_topic_closed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reopen a bug topic if the bug is not yet resolved."""
    if str(update.effective_chat.id) != GROUP_ID:
        return
    topic_id = update.message.message_thread_id
    if topic_id is None:
        return
    from core.tools.bugs import get_bug_by_topic_id
    bug = get_bug_by_topic_id(topic_id)
    if bug and bug.get('status') == 'Open':
        try:
            await context.bot.reopen_forum_topic(
                chat_id=GROUP_ID,
                message_thread_id=topic_id,
            )
            await context.bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=topic_id,
                text=f"{bug['bug_id']} is still open — topic reopened. Resolve the bug before closing.",
            )
            log.info(f"Reopened topic {topic_id} for unresolved {bug['bug_id']}")
        except Exception as e:
            log.error(f"Failed to reopen topic {topic_id}: {e}")


async def post_init(app: Application):
    state.set_app(app, GROUP_ID)
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
    app.add_handler(CommandHandler("meta", on_meta_command))
    app.add_handler(CommandHandler("distil", on_distil_command))
    app.add_handler(CommandHandler("createbugtopics", on_create_bug_topics))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.FORUM_TOPIC_CLOSED,
        on_topic_closed,
    ))
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
