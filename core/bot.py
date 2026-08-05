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

# Pending email-preferences.md update proposals — keyed by topic_id
PENDING_EMAIL_PREFS: dict[int, dict] = {}

# Pending send-email proposals — keyed by topic_id. Distinct confirm phrase ("send it"),
# not "approve" — this is the first capability that can contact a third party.
PENDING_SEND_EMAIL: dict[int, dict] = {}

# Pending delete-email (trash) proposals — keyed by topic_id. Distinct confirm phrase
# ("delete it"), not "approve".
PENDING_DELETE_EMAIL: dict[int, dict] = {}

# Pending forward-email proposals — keyed by topic_id. Distinct confirm phrase
# ("forward it"), same reasoning as send/delete — a third-party-contact action gets its
# own unambiguous trigger.
PENDING_FORWARD_EMAIL: dict[int, dict] = {}

# Pending distillation proposals — keyed by topic_id
# Value: {"distillate": str} or {"distillate": "NOTHING_TO_KEEP"}
PENDING_DISTIL: dict[int, dict] = {}

# Tracks topics with an active agent turn in progress — prevents overlapping calls
ACTIVE_TOPICS: set[int] = set()


async def send_and_save(bot, topic_id: int, text: str):
    """Send a message to Telegram and save it to conversation history as an assistant message."""
    await bot.send_message(
        chat_id=GROUP_ID,
        text=text,
        message_thread_id=topic_id,
    )
    save_message(topic_id, "assistant", text)


async def send_and_save_chunked(bot, topic_id: int, text: str, chunk_size: int = 4000):
    """Same as send_and_save, but split across multiple messages if text exceeds
    chunk_size — same shape as on_meta_command's review-chunking loop. A reply-all
    preview's resolved Cc list can run long enough to need this, unlike the other,
    length-capped proposal previews."""
    for i in range(0, len(text), chunk_size):
        await send_and_save(bot, topic_id, text[i:i + chunk_size])


def _other_pending_note(topic_id: int, just_resolved: str) -> str:
    """
    Cheap collision mitigation: PENDING_DISTIL/PENDING_UPDATES/PENDING_EMAIL_PREFS can
    already (pre-existing, not introduced here) all independently be pending for the same
    topic at once, with the same 'approve'/'reject' words resolving whichever is checked
    first. Rather than leave the others silently waiting on an unprompted reply, re-
    surface every one still pending immediately after resolving one — a genuine three-way
    collision means two others could still be waiting, not just one.
    """
    notes = []
    if just_resolved != "distil" and topic_id in PENDING_DISTIL:
        notes.append("a pending distillation — reply 'approve', 'discard', or 'reject'")
    if just_resolved != "charlie_doc" and topic_id in PENDING_UPDATES:
        notes.append("a pending charlie.md update — reply 'approve' or 'reject'")
    if just_resolved != "email_prefs" and topic_id in PENDING_EMAIL_PREFS:
        notes.append("a pending email-preferences.md update — reply 'approve' or 'reject'")
    if just_resolved != "send_email" and topic_id in PENDING_SEND_EMAIL:
        notes.append("a pending email to send — reply 'send it' or 'cancel'")
    if just_resolved != "delete_email" and topic_id in PENDING_DELETE_EMAIL:
        notes.append("a pending email delete — reply 'delete it' or 'cancel'")
    if just_resolved != "forward_email" and topic_id in PENDING_FORWARD_EMAIL:
        notes.append("a pending email forward — reply 'forward it' or 'cancel'")
    if not notes:
        return ""
    return "\n\n" + "\n".join(f"You also still have {n}." for n in notes)


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
                await send_and_save(context.bot, topic_id, "Couldn't make out that voice message.")
                return
        except Exception as e:
            log.error(f"Transcription failed: {e}")
            await send_and_save(context.bot, topic_id, "Voice transcription failed — try again or type it.")
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
                await send_and_save(context.bot, topic_id, "Saved to context-archive.md. Conversation history deleted." + _other_pending_note(topic_id, "distil"))
            else:
                await send_and_save(context.bot, topic_id, "Nothing to archive. Conversation history deleted." + _other_pending_note(topic_id, "distil"))
            delete_topic_history(topic_id)
            return
        elif response_lower == "discard":
            PENDING_DISTIL.pop(topic_id)
            delete_topic_history(topic_id)
            await send_and_save(context.bot, topic_id, "Conversation history deleted. Nothing archived." + _other_pending_note(topic_id, "distil"))
            return
        elif response_lower == "reject":
            PENDING_DISTIL.pop(topic_id)
            await send_and_save(context.bot, topic_id, "Distillation rejected. History kept — run /distil again if you want to retry." + _other_pending_note(topic_id, "distil"))
            return

    # Handle approve/reject for pending charlie.md update proposals
    if topic_id in PENDING_UPDATES:
        response_lower = user_text.strip().lower()
        if response_lower in ("approve", "yes", "ok", "save"):
            update_data = PENDING_UPDATES.pop(topic_id)
            _write_charlie_doc(update_data["proposed_content"])
            await send_and_save(context.bot, topic_id, "charlie.md updated." + _other_pending_note(topic_id, "charlie_doc"))
            return
        elif response_lower in ("reject", "no", "skip", "discard"):
            PENDING_UPDATES.pop(topic_id)
            await send_and_save(context.bot, topic_id, "Update discarded." + _other_pending_note(topic_id, "charlie_doc"))
            return

    # Handle approve/reject for pending email-preferences.md update proposals
    if topic_id in PENDING_EMAIL_PREFS:
        response_lower = user_text.strip().lower()
        if response_lower in ("approve", "yes", "ok", "save"):
            update_data = PENDING_EMAIL_PREFS.pop(topic_id)
            _write_email_prefs_doc(update_data["proposed_content"])
            await send_and_save(context.bot, topic_id, "email-preferences.md updated." + _other_pending_note(topic_id, "email_prefs"))
            return
        elif response_lower in ("reject", "no", "skip", "discard"):
            PENDING_EMAIL_PREFS.pop(topic_id)
            await send_and_save(context.bot, topic_id, "Update discarded." + _other_pending_note(topic_id, "email_prefs"))
            return

    # Handle send-it/cancel for pending send-email proposals. Distinct confirm phrase
    # ("send it") deliberately not shared with "approve" — the first capability able to
    # contact a third party gets its own unambiguous trigger.
    if topic_id in PENDING_SEND_EMAIL:
        response_lower = user_text.strip().lower()
        if response_lower == "send it":
            pending = PENDING_SEND_EMAIL.pop(topic_id)
            try:
                from core.tools.email.fetch import send_email
                # Use the RESOLVED recipients send_email() actually sent to for the
                # confirmation, not pending['to'] — that's the raw proposal value and
                # is None for any reply/reply-all where 'to' was auto-derived rather
                # than explicitly supplied (see BUG-025: this previously rendered as
                # the literal string "Sent to None.").
                resolved = send_email(
                    to=pending["to"], subject=pending["subject"],
                    body=pending["body"], thread_id=pending["thread_id"],
                    cc=pending.get("cc"), bcc=pending.get("bcc"),
                    reply_all=pending.get("reply_all", False),
                )
                confirmation = f"Sent to {resolved['to']}."
                if resolved.get("cc"):
                    confirmation += f" Cc: {resolved['cc']}."
                await send_and_save(context.bot, topic_id, confirmation + _other_pending_note(topic_id, "send_email"))
            except Exception as e:
                log.error(f"Send failed for topic {topic_id}: {e}")
                await send_and_save(context.bot, topic_id, f"Send failed: {e}" + _other_pending_note(topic_id, "send_email"))
            return
        elif response_lower in ("cancel", "no", "stop", "discard"):
            PENDING_SEND_EMAIL.pop(topic_id)
            await send_and_save(context.bot, topic_id, "Send cancelled." + _other_pending_note(topic_id, "send_email"))
            return

    # Handle delete-it/cancel for pending delete-email proposals. Distinct confirm phrase
    # ("delete it"), same reasoning as send.
    if topic_id in PENDING_DELETE_EMAIL:
        response_lower = user_text.strip().lower()
        if response_lower == "delete it":
            pending = PENDING_DELETE_EMAIL.pop(topic_id)
            from core.tools.email.fetch import trash_thread
            failures = []
            for thread_id in pending["thread_ids"]:
                try:
                    trash_thread(thread_id)
                except Exception as e:
                    log.error(f"Delete failed for topic {topic_id}, thread {thread_id}: {e}")
                    failures.append(f"{thread_id}: {e}")
            total = len(pending["thread_ids"])
            deleted = total - len(failures)
            if not failures:
                summary = "Deleted (moved to Trash — recoverable for 30 days)." if total == 1 \
                    else f"Deleted all {total} (moved to Trash — recoverable for 30 days)."
            else:
                summary = (
                    f"Deleted {deleted}/{total} (moved to Trash — recoverable for 30 days). "
                    f"Failed:\n" + "\n".join(f"- {f}" for f in failures)
                )
            await send_and_save(context.bot, topic_id, summary + _other_pending_note(topic_id, "delete_email"))
            return
        elif response_lower in ("cancel", "no", "stop", "discard"):
            PENDING_DELETE_EMAIL.pop(topic_id)
            await send_and_save(context.bot, topic_id, "Delete cancelled." + _other_pending_note(topic_id, "delete_email"))
            return

    # Handle forward-it/cancel for pending forward-email proposals. Distinct confirm
    # phrase ("forward it"), same reasoning as send/delete.
    if topic_id in PENDING_FORWARD_EMAIL:
        response_lower = user_text.strip().lower()
        if response_lower == "forward it":
            pending = PENDING_FORWARD_EMAIL.pop(topic_id)
            try:
                from core.tools.email.fetch import forward_email
                forward_email(
                    thread_id=pending["thread_id"], to=pending["to"],
                    gmail_message_id=pending.get("gmail_message_id"),
                    cc=pending.get("cc"), bcc=pending.get("bcc"),
                    note=pending.get("note", ""),
                )
                await send_and_save(context.bot, topic_id, f"Forwarded to {pending['to']}." + _other_pending_note(topic_id, "forward_email"))
            except Exception as e:
                log.error(f"Forward failed for topic {topic_id}: {e}")
                await send_and_save(context.bot, topic_id, f"Forward failed: {e}" + _other_pending_note(topic_id, "forward_email"))
            return
        elif response_lower in ("cancel", "no", "stop", "discard"):
            PENDING_FORWARD_EMAIL.pop(topic_id)
            await send_and_save(context.bot, topic_id, "Forward cancelled." + _other_pending_note(topic_id, "forward_email"))
            return

    # Prevent overlapping turns in the same topic
    if topic_id in ACTIVE_TOPICS:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text="Still thinking — please wait.",
            message_thread_id=topic_id,
        )
        return

    ACTIVE_TOPICS.add(topic_id)
    try:
        await _run_charlie_turn(update, context, topic_id, user_text)
    finally:
        ACTIVE_TOPICS.discard(topic_id)


def _send_email_proposal_text(p: dict) -> str:
    # Grounds the preview in the real resolved recipients/subject rather than the model's
    # guess — resolve_send_recipients() is the SAME function send_email() calls at actual
    # send time, so this preview and the real send can never diverge.
    #
    # Only notable/non-default fields get their own line — Jonathan doesn't need "Bcc: none"
    # or a restated Reason when the body already says why; he does need a heads-up when a
    # reply-all is about to loop in several other people.
    from email.utils import getaddresses
    from core.tools.email.fetch import resolve_send_recipients
    try:
        resolved = resolve_send_recipients(
            p.get("thread_id"), p.get("to"), p.get("subject", ""),
            p.get("cc"), p.get("bcc"), p.get("reply_all", False),
        )
        to_display = resolved["to"]
        subject_display = resolved["subject"]
        cc_display = resolved["cc"]
        bcc_display = resolved["bcc"]
        error_note = ""
    except Exception as e:
        to_display = p.get("to") or "(unresolved)"
        subject_display = p.get("subject", "")
        cc_display = p.get("cc") or ""
        bcc_display = p.get("bcc") or ""
        error_note = f"\n\n(Could not resolve recipients: {e})"

    thread_id = p.get("thread_id")
    kind = "Replying to" if thread_id else "Emailing"
    quote_note = " (includes the quoted original below it)" if thread_id else ""
    lines = [f"{kind} {to_display} — {subject_display}{quote_note}"]
    if cc_display:
        cc_count = len([a for _, a in getaddresses([cc_display]) if a])
        count_note = f" ({cc_count} people)" if p.get("reply_all") and cc_count > 1 else ""
        lines.append(f"Cc: {cc_display}{count_note}")
    if bcc_display:
        lines.append(f"Bcc: {bcc_display}")
    lines += ["", p.get("body", ""), "", "Reply 'send it' to send, or 'cancel' to discard."]
    return "\n".join(lines) + error_note


def _forward_email_proposal_text(p: dict) -> str:
    # Grounds the preview in real, freshly-fetched sender/subject/attachment metadata
    # rather than the model's paraphrase — same reasoning as _delete_email_proposal_text.
    #
    # Only notable/non-default details get called out: which message in a multi-message
    # thread, whether there's an attachment. No attachments / single-message thread / no
    # note are all the common case and stay silent rather than spelling out "none" each time.
    try:
        from core.tools.email.fetch import get_forward_preview
        preview = get_forward_preview(p["thread_id"], gmail_message_id=p.get("gmail_message_id"))
        detail_bits = []
        if preview["message_count"] > 1:
            detail_bits.append(
                f"message {preview['selected_index'] + 1} of {preview['message_count']}, "
                f"dated {preview['date']}"
            )
        if preview["attachments"]:
            names = ", ".join(a["filename"] for a in preview["attachments"])
            plural = "s" if len(preview["attachments"]) > 1 else ""
            detail_bits.append(f"attachment{plural}: {names}")
        detail = f" ({'; '.join(detail_bits)})" if detail_bits else ""
        header = (
            f"Forwarding \"{preview['subject']}\" from {preview['sender_name']} "
            f"<{preview['sender_email']}>{detail} to {p.get('to', '')}"
        )
    except Exception as e:
        header = f"(could not fetch thread details: {e})"

    lines = [header]
    if p.get("cc"):
        lines.append(f"Cc: {p['cc']}")
    if p.get("bcc"):
        lines.append(f"Bcc: {p['bcc']}")
    if p.get("note"):
        lines += ["", f"Note: {p['note']}"]
    elif p.get("reason"):
        lines += ["", f"({p['reason']})"]
    lines += ["", "Reply 'forward it' to forward, or 'cancel' to discard."]
    return "\n".join(lines)


def _delete_email_proposal_text(p: dict) -> str:
    # Grounds the preview in a real, freshly-fetched sender/subject rather than trusting
    # only the model's free-text `reason` for what's being deleted. Reason stays on its own
    # line here (unlike send/forward) — there's no body or note to carry the "why" instead.
    # Each thread is looked up independently so one bad thread_id doesn't blank the whole
    # preview — Jonathan still sees what's fine to delete alongside what couldn't be found.
    from core.tools.email.fetch import get_thread_summary
    targets = []
    for thread_id in p["thread_ids"]:
        try:
            summary = get_thread_summary(thread_id)
            targets.append(f"{summary['sender_name']} <{summary['sender_email']}> — {summary['subject']}")
        except Exception as e:
            targets.append(f"(could not fetch thread {thread_id}: {e})")
    deleting = "\n".join(f"- {t}" for t in targets) if len(targets) > 1 else targets[0]
    label = "Deleting" if len(targets) == 1 else f"Deleting {len(targets)} threads"
    return (
        f"{label} (moved to Trash, recoverable for 30 days):\n{deleting}\n\n"
        f"{p['reason']}\n\n"
        f"Reply 'delete it' to delete, or 'cancel' to discard."
    )


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
        updated_messages, proposals = await handle_turn(
            user_text=user_text,
            messages=messages,
            send_fn=send_fn,
            topic_id=topic_id,
            thinking_enabled=THINKING_ENABLED,
            thinking_budget=THINKING_BUDGET,
        )
    except Exception as e:
        log.error(f"Agent error in topic {topic_id}: {e}")
        await send_and_save(context.bot, topic_id, f"Something went wrong — {e}")
        return

    # Save the new messages (user message + assistant turn(s))
    new_messages = updated_messages[old_count:]
    for msg in new_messages:
        save_message(topic_id, msg["role"], msg["content"])

    # Handle a proposed charlie.md update
    proposed_update = proposals.get("charlie_doc")
    if proposed_update:
        PENDING_UPDATES[topic_id] = proposed_update
        preview = proposed_update["proposed_content"]
        if len(preview) > 800:
            preview = preview[:800] + "\n...[truncated]"
        proposal_text = (
            f"Proposed update to charlie.md\n\n"
            f"Reason: {proposed_update['reason']}\n\n"
            f"---\n{preview}\n---\n\n"
            f"Reply 'approve' to save or 'reject' to discard."
        )
        await send_and_save(context.bot, topic_id, proposal_text)

    # Handle a proposed email-preferences.md update
    proposed_email_prefs = proposals.get("email_prefs")
    if proposed_email_prefs:
        PENDING_EMAIL_PREFS[topic_id] = proposed_email_prefs
        preview = proposed_email_prefs["proposed_content"]
        if len(preview) > 800:
            preview = preview[:800] + "\n...[truncated]"
        proposal_text = (
            f"Proposed update to email-preferences.md\n\n"
            f"Reason: {proposed_email_prefs['reason']}\n\n"
            f"---\n{preview}\n---\n\n"
            f"Reply 'approve' to save or 'reject' to discard."
        )
        await send_and_save(context.bot, topic_id, proposal_text)

    # Handle a proposed send
    proposed_send = proposals.get("send_email")
    if proposed_send:
        PENDING_SEND_EMAIL[topic_id] = proposed_send
        await send_and_save_chunked(context.bot, topic_id, _send_email_proposal_text(proposed_send))

    # Handle a proposed delete
    proposed_delete = proposals.get("delete_email")
    if proposed_delete:
        PENDING_DELETE_EMAIL[topic_id] = proposed_delete
        await send_and_save(context.bot, topic_id, _delete_email_proposal_text(proposed_delete))

    # Handle a proposed forward
    proposed_forward = proposals.get("forward_email")
    if proposed_forward:
        PENDING_FORWARD_EMAIL[topic_id] = proposed_forward
        await send_and_save_chunked(context.bot, topic_id, _forward_email_proposal_text(proposed_forward))


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

    # Ephemeral status — not saved
    await context.bot.send_message(
        chat_id=GROUP_ID,
        text="Running meta-review — one moment...",
        message_thread_id=topic_id,
    )

    # Load conversation history before anything is saved to DB
    history = load_history(topic_id)

    # Step 1+2: Build transcript → fresh Claude review → post it
    try:
        from core.tools.meta import run_meta_review
        review = await run_meta_review(topic_id)
    except Exception as e:
        log.error(f"Meta review failed for topic {topic_id}: {e}")
        await send_and_save(context.bot, topic_id, f"Meta review failed: {e}")
        return

    chunk_size = 4000
    for i in range(0, len(review), chunk_size):
        await send_and_save(context.bot, topic_id, review[i:i + chunk_size])

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
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=text,
            message_thread_id=topic_id,
        )

    try:
        from core.agent import handle_turn
        _, proposals = await handle_turn(
            user_text=charlie_instruction,
            messages=history,
            send_fn=charlie_send_fn,
            topic_id=topic_id,
            thinking_enabled=THINKING_ENABLED,
            thinking_budget=THINKING_BUDGET,
        )

        # Persist both outputs to history
        save_message(topic_id, "assistant", "[/meta review]\n\n" + review)
        if charlie_take_parts:
            save_message(topic_id, "assistant", "[Charlie's take]\n\n" + "\n".join(charlie_take_parts))

        proposed_update = proposals.get("charlie_doc")
        if proposed_update:
            PENDING_UPDATES[topic_id] = proposed_update
            preview = proposed_update["proposed_content"]
            if len(preview) > 800:
                preview = preview[:800] + "\n...[truncated]"
            proposal_text = (
                f"Proposed update to charlie.md\n\n"
                f"Reason: {proposed_update['reason']}\n\n"
                f"---\n{preview}\n---\n\n"
                f"Reply 'approve' to save or 'reject' to discard."
            )
            await send_and_save(context.bot, topic_id, proposal_text)

        proposed_email_prefs = proposals.get("email_prefs")
        if proposed_email_prefs:
            PENDING_EMAIL_PREFS[topic_id] = proposed_email_prefs
            preview = proposed_email_prefs["proposed_content"]
            if len(preview) > 800:
                preview = preview[:800] + "\n...[truncated]"
            proposal_text = (
                f"Proposed update to email-preferences.md\n\n"
                f"Reason: {proposed_email_prefs['reason']}\n\n"
                f"---\n{preview}\n---\n\n"
                f"Reply 'approve' to save or 'reject' to discard."
            )
            await send_and_save(context.bot, topic_id, proposal_text)

        proposed_send = proposals.get("send_email")
        if proposed_send:
            PENDING_SEND_EMAIL[topic_id] = proposed_send
            await send_and_save_chunked(context.bot, topic_id, _send_email_proposal_text(proposed_send))

        proposed_delete = proposals.get("delete_email")
        if proposed_delete:
            PENDING_DELETE_EMAIL[topic_id] = proposed_delete
            await send_and_save(context.bot, topic_id, _delete_email_proposal_text(proposed_delete))

        proposed_forward = proposals.get("forward_email")
        if proposed_forward:
            PENDING_FORWARD_EMAIL[topic_id] = proposed_forward
            await send_and_save_chunked(context.bot, topic_id, _forward_email_proposal_text(proposed_forward))
    except Exception as e:
        log.error(f"Charlie's take failed for topic {topic_id}: {e}")
        await send_and_save(context.bot, topic_id, f"Charlie's take failed: {e}")


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

    # Ephemeral status — not saved
    await context.bot.send_message(
        chat_id=GROUP_ID,
        text="Distilling conversation — one moment...",
        message_thread_id=topic_id,
    )

    try:
        from core.tools.distil import run_distil
        distillate = await run_distil(topic_id)
    except Exception as e:
        log.error(f"Distillation failed for topic {topic_id}: {e}")
        await send_and_save(context.bot, topic_id, f"Distillation failed: {e}")
        return

    PENDING_DISTIL[topic_id] = {"distillate": distillate}

    if distillate == "NOTHING_TO_KEEP":
        proposal_text = (
            "Nothing worth archiving in this conversation.\n\n"
            "Reply 'approve' to delete the history, or 'reject' to keep it."
        )
    else:
        preview = distillate if len(distillate) <= 1200 else distillate[:1200] + "\n...[truncated]"
        proposal_text = (
            f"Proposed archive entry:\n\n{preview}\n\n"
            f"'approve' — save to context-archive.md and delete history\n"
            f"'discard' — delete history without saving\n"
            f"'reject' — keep history and try again"
        )
    await send_and_save(context.bot, topic_id, proposal_text)


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


def _write_email_prefs_doc(content: str):
    prefs_doc = Path(__file__).parent.parent / "email-preferences.md"
    prefs_doc.write_text(content)
    log.info("email-preferences.md updated")


async def on_topic_created(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greet when a new topic is opened in the group."""
    if str(update.effective_chat.id) != GROUP_ID:
        return
    topic = update.message.forum_topic_created
    if topic:
        topic_id = update.message.message_thread_id
        log.info(f"New topic created: '{topic.name}'")
        await send_and_save(
            context.bot, topic_id,
            f"New topic: {topic.name}. What are we working on?"
        )


async def on_create_bug_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/createbugtopics — batch-create Telegram topics for all open bugs without one."""
    if str(update.effective_chat.id) != GROUP_ID:
        return
    topic_id = update.message.message_thread_id
    # Ephemeral status — not saved
    await context.bot.send_message(
        chat_id=GROUP_ID,
        text="Creating topics for all open bugs without one...",
        message_thread_id=topic_id,
    )
    from core.tools.bugs import create_topics_for_all_open_bugs
    created = await create_topics_for_all_open_bugs()
    if created:
        await send_and_save(context.bot, topic_id, f"Done. Created topics for: {', '.join(created)}")
    else:
        await send_and_save(context.bot, topic_id, "No open bugs needed a topic.")


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
            reopen_text = f"{bug['bug_id']} is still open — topic reopened. Resolve the bug before closing."
            await send_and_save(context.bot, topic_id, reopen_text)
            log.info(f"Reopened topic {topic_id} for unresolved {bug['bug_id']}")
        except Exception as e:
            log.error(f"Failed to reopen topic {topic_id}: {e}")


async def post_init(app: Application):
    state.set_app(app, GROUP_ID)
    await setup_scheduler(app)


async def post_shutdown(app: Application):
    await teardown_scheduler(app)


def _wait_for_network(max_wait_seconds=120, attempt_timeout=5):
    """
    Block until api.telegram.org actually resolves, before anything Telegram/
    HTTP-related initializes. A process that starts while the network is
    still settling post-reboot can end up with a persistently bad cached DNS
    resolver state for its entire life, even after the network fully
    recovers moments later — this caused a ~12.5 hour outage on 2026-07-30.
    Each resolution attempt runs in a worker thread with its own bounded
    timeout — socket.getaddrinfo() itself has no built-in timeout and can
    hang indefinitely against an unresponsive (not just erroring) resolver,
    which would otherwise defeat the max_wait_seconds budget entirely. Gives
    up after max_wait_seconds and starts anyway, logging a warning —
    python-telegram-bot's own retry loop takes over from there for anything
    transient.
    """
    import socket
    import time

    from core.utils import run_with_timeout

    def _resolve():
        socket.getaddrinfo("api.telegram.org", 443)

    start = time.time()
    while time.time() - start < max_wait_seconds:
        # A fresh daemon thread per attempt (inside run_with_timeout) — never
        # reuse/join a worker across attempts. If getaddrinfo hangs (unresponsive
        # resolver, not just an erroring one), that thread is simply abandoned:
        # daemon=True means it won't block process exit, and starting a new thread
        # next attempt (rather than queuing behind a pooled worker) means one hung
        # attempt can't stall every attempt after it.
        try:
            run_with_timeout(_resolve, attempt_timeout)
            return
        except TimeoutError:
            continue  # hung attempt abandoned — retry immediately, no sleep
        except socket.gaierror:
            time.sleep(2)
    log.warning(f"Network still not resolving after {max_wait_seconds}s — starting anyway")


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing from .env")
    if not GROUP_ID:
        raise SystemExit("TELEGRAM_GROUP_ID is missing from .env")

    _wait_for_network()

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
