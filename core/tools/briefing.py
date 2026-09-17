"""
Morning Briefing v2 — dynamic daily planner.

A scheduler module, not an agent tool — same pattern as core/tools/email/
(fetches, synthesises, and is posted by the scheduler on its own; Charlie's
agent loop never calls into this directly). core/scheduler.py owns topic
creation/Telegram sending; this module owns gathering data and producing the
message text, so the whole thing stays testable/dry-runnable with no Telegram
dependency at all.

Prompt-injection note: the synthesis call below has no tool access and only
ever produces plain text — the same trust model news.py's generate_briefing()
already uses for the same reason (both ingest untrusted RSS/email content).
"""
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

log = logging.getLogger(__name__)

CHARLIE_ROOT = Path(__file__).parent.parent.parent
EMAILS_DB_PATH = CHARLIE_ROOT / "data" / "emails.db"
FOLLOWUPS_PATH = CHARLIE_ROOT / "followups.md"
MODEL = "claude-haiku-4-5-20251001"  # round-1 build review: Sonnet is overkill for this synthesis

EMAIL_LOOKBACK_HOURS = 24
MAX_EMAILS = 5
MAX_NEWS_ITEMS = 2


def _today() -> date:
    """Same TIMEZONE-anchored pattern as grants.py's _today() (BUG-035) and
    reminders.py's — date.today() would silently use the host OS's timezone."""
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "UTC"))).date()


def _fetch_actionable_emails() -> list[dict]:
    """
    Read-only snapshot of emails.db (last 24h, most urgent first) — never
    touches notified_at or any other monitor state, so this runs safely
    alongside the live 2-minute poller (core/tools/email/__init__.py) without
    any shared-state coordination between them.
    """
    if not EMAILS_DB_PATH.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=EMAIL_LOOKBACK_HOURS)).isoformat()
    try:
        conn = sqlite3.connect(EMAILS_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT sender_name, sender_email, subject, summary, urgent, received_at
                FROM emails
                WHERE actionability = 'ACTIONABLE' AND received_at >= ?
                ORDER BY urgent DESC, received_at DESC
                LIMIT ?
                """,
                (cutoff, MAX_EMAILS),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"Briefing: email fetch failed: {e}")
        return []


_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y")
_SECTION_HEADER_RE = re.compile(r"^###\s")
_SECTION_DEADLINE_RE = re.compile(r"deadline:?\s+(.+?)\s*$", re.IGNORECASE)
_SUBHEADING_RE = re.compile(r"^\*\*(.+)\*\*$")
_INLINE_DEADLINE_RE = re.compile(r"\|\s*deadline:\s*([^|]+)", re.IGNORECASE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
MAX_FOLLOWUPS = 15


def _parse_date(text: str) -> date | None:
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _days_phrase(days_remaining: int) -> str:
    if days_remaining < 0:
        return f"{-days_remaining} days overdue"
    if days_remaining == 0:
        return "due today"
    return f"due in {days_remaining} days"


def _load_due_followups() -> list[dict]:
    """
    Parses followups.md's Open section. Every item's deadline is resolved in
    code — its own inline `| deadline: YYYY-MM-DD` field, or one inherited
    from an enclosing '### <Section> — deadline <date>' header — and
    annotated with a code-computed days_remaining, so the synthesis model is
    only ever handed an already-correct number, never asked to do date
    arithmetic itself. (Root cause of a real incident: a hand-written
    "X days from <date>" note written elsewhere got parroted back unchanged
    weeks later, because nothing recomputed it — see devlog 2026-09-17.)

    Items with no resolvable deadline (ongoing/ambient items like "study
    regularly") are intentionally excluded — they aren't date-driven and
    don't need daily resurfacing; Jonathan can still ask about them any time.

    Replaces this function's old 'chase from:' parsing — followups.md moved
    to 'deadline:'/'added:' fields on 2026-09-15 without the parser being
    updated to match, which silently zeroed out this entire section of the
    briefing for two days with no error anywhere.
    """
    today = _today()
    in_open = False
    section_deadline: date | None = None
    subheading: str | None = None
    due: list[dict] = []
    try:
        with open(FOLLOWUPS_PATH, "r") as f:
            for raw_line in f:
                line = raw_line.strip()
                if line.startswith("## "):
                    # Track membership with a flag rather than breaking out of
                    # the loop on the first non-"## Open" heading — a break
                    # would silently zero out the whole section if "## Open"
                    # isn't literally the file's first "## " heading (code
                    # review, round 1: this was the exact "silent zero output,
                    # no error" failure class this rewrite was built to kill).
                    in_open = (line == "## Open")
                    section_deadline = None
                    subheading = None
                    continue
                if not in_open:
                    continue
                if _SECTION_HEADER_RE.match(line):
                    deadline_match = _SECTION_DEADLINE_RE.search(line)
                    section_deadline = _parse_date(deadline_match.group(1)) if deadline_match else None
                    subheading = None
                    continue
                sub_match = _SUBHEADING_RE.match(line)
                if sub_match:
                    subheading = sub_match.group(1).strip()
                    continue
                if not line.startswith("- [ ]"):
                    continue
                body = line[len("- [ ]"):].strip()
                inline_match = _INLINE_DEADLINE_RE.search(body)
                deadline = _parse_date(inline_match.group(1)) if inline_match else section_deadline
                if deadline is None:
                    continue
                desc = _BOLD_RE.sub(r"\1", body.split("|")[0].strip())
                if subheading:
                    desc = f"{subheading} — {desc}"
                due.append({
                    "description": desc,
                    "deadline": deadline.isoformat(),
                    "days_remaining": (deadline - today).days,
                })
    except FileNotFoundError:
        pass
    except Exception as e:
        # Log and fall through to whatever was already parsed, rather than
        # discarding it — a parse error partway through the file shouldn't
        # cost every item found before it (code review, round 1).
        log.error(f"Briefing: followups read failed: {e}")
    due.sort(key=lambda d: d["days_remaining"])
    return due


def _relevant_news(articles_by_topic: dict) -> list[dict]:
    """Flatten + score every already-fetched article, keep the (rare) ones
    clearing RELEVANCE_THRESHOLD, highest first, capped at MAX_NEWS_ITEMS."""
    from core.tools.news import relevance_score, RELEVANCE_THRESHOLD

    scored = []
    for articles in articles_by_topic.values():
        for row in articles:
            article = dict(row)
            score, category = relevance_score(article)
            if score >= RELEVANCE_THRESHOLD:
                scored.append((score, category, article))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {"title": a["title"], "summary": a["summary"], "category": cat}
        for _, cat, a in scored[:MAX_NEWS_ITEMS]
    ]


def _fetch_news_section() -> list[dict]:
    """Reuses whatever the daily news job already fetched this run (see
    news.get_fetched_articles's 5-minute shared-fetch cache) rather than
    fetching RSS a second time."""
    try:
        from core.tools.news import get_fetched_articles
        articles_by_topic = get_fetched_articles()
        return _relevant_news(articles_by_topic)
    except Exception as e:
        log.error(f"Briefing: news fetch failed: {e}")
        return []


def _quiet_day_message(today: date) -> str:
    return f"Good morning. It's {today.strftime('%A, %d %B %Y')}. Nothing urgent on the radar — a clear one."


def _synthesize(today, charlie_context, archive_excerpt, emails, due_followups, reminders_due, news_items, current_focus) -> str:
    parts = [f"Today is {today.strftime('%A, %d %B %Y')}."]
    if charlie_context:
        parts.append(f"\nWhat Charlie knows about Jonathan:\n{charlie_context}")
    if archive_excerpt:
        parts.append(f"\nRecent distilled context:\n{archive_excerpt}")
    if emails:
        lines = "\n".join(
            f"- From {e['sender_name'] or e['sender_email']}: {e['subject']} — {e['summary']}"
            + (" [URGENT]" if e["urgent"] else "")
            for e in emails
        )
        parts.append(f"\nActionable emails (last 24h):\n{lines}")
    if due_followups:
        shown = due_followups[:MAX_FOLLOWUPS]
        lines = "\n".join(
            f"- {d['description']} — {d['deadline']} ({_days_phrase(d['days_remaining'])})"
            for d in shown
        )
        overflow = len(due_followups) - len(shown)
        if overflow > 0:
            nearest_omitted = due_followups[len(shown)]
            lines += (
                f"\n- (+{overflow} more open follow-ups, nearest one due "
                f"{nearest_omitted['deadline']} — see followups.md for the full list)"
            )
        parts.append(
            "\nOpen follow-ups (days remaining are already computed — do not recalculate "
            f"or restate them differently):\n{lines}"
        )
    if reminders_due:
        lines = "\n".join(
            f"- {r['description']}" + (f" ({r['context']})" if r.get("context") else "")
            for r in reminders_due
        )
        parts.append(f"\nReminders due today:\n{lines}")
    if news_items:
        lines = "\n".join(f"- [{n['category']}] {n['title']}: {n['summary']}" for n in news_items)
        parts.append(f"\nNews that might matter:\n{lines}")
    if current_focus:
        parts.append(
            f"\nJonathan's current priority sequencing (agreed {current_focus['set_on']}, "
            f"still active): {current_focus['note']}"
        )

    prompt = "\n".join(parts)

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=(
            "You are writing Jonathan's dynamic morning briefing as Charlie. Structure it as "
            "a short, actionable daily planner using only the sections below that have real "
            "content — omit any section with nothing to say, don't pad it out:\n\n"
            "- Today's focus — 1-3 top priorities, drawn from judgment across everything given. "
            "If a current priority sequencing note is given, treat it as the anchor for this "
            "section rather than re-deriving sequencing from scratch\n"
            "- Emails to action — the actionable emails, with any relevant context connecting "
            "them to past discussions if it's given\n"
            "- Open items / reminders — follow-ups and reminders due\n"
            "- News that matters — only if genuinely relevant, framed as 'because of X, you "
            "might want to Y'\n\n"
            "Every day-count you're given (e.g. 'due in 12 days', '3 days overdue') is already "
            "computed correctly — never recalculate one, restate it differently, or invent a "
            "day-count for a date that wasn't given one. Plain text only, no markdown headers "
            "or asterisks. Keep it tight — this is a planner Jonathan reads in 30 seconds, not "
            "a report."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text.strip() for b in response.content if hasattr(b, "text")), "")
    return text or _quiet_day_message(today)


def build_briefing_text(charlie_context: str, archive_excerpt: str, dry_run: bool = False) -> tuple[str, list[str], date]:
    """
    Gathers every data source and returns (message_text, fired_reminder_ids,
    today). Synchronous and Telegram-agnostic on purpose — core/scheduler.py
    owns topic creation/sending. Must be called off the event loop
    (asyncio.to_thread) — this does blocking I/O (sqlite, file reads, an RSS
    fetch) and one synchronous Anthropic call.

    charlie_context/archive_excerpt are passed in rather than read from disk
    here, on purpose (build review, round 3): keeps this module's own file
    I/O surface to just followups.md/emails.db — charlie.md and a bounded
    context-archive.md excerpt stay the caller's responsibility.

    Does NOT advance/fire the returned reminder ids itself — that must only
    happen once the caller has confirmed message_text was actually delivered
    (code review: advancing here, before the Telegram send, would mark a
    reminder as shown even on a send failure, permanently losing that
    occurrence with no retry, unlike _send_news_briefing's retry path). Call
    core.tools.reminders.advance_after_briefing(fired_reminder_ids, today)
    after a successful send.

    dry_run=True (manual same-day testing, not a Jonathan-facing feature):
    skips the news fetch entirely (a real RSS fetch marks articles shown,
    which would rob the next real news post of content) and always returns
    an empty fired_reminder_ids list — mirrors run_grants_pipeline(dry_run=...)'s
    "does not write" convention.
    """
    today = _today()

    emails = _fetch_actionable_emails()
    due_followups = _load_due_followups()

    try:
        from core.tools import reminders as reminders_mod
        reminders_due = reminders_mod.get_due_reminders(today)
    except Exception as e:
        log.error(f"Briefing: reminders fetch failed: {e}")
        reminders_due = []

    try:
        from core.tools.focus import get_active_focus
        current_focus = get_active_focus()
    except Exception as e:
        log.error(f"Briefing: focus fetch failed: {e}")
        current_focus = None

    news_items = [] if dry_run else _fetch_news_section()

    if not emails and not due_followups and not reminders_due and not news_items and not current_focus:
        text = _quiet_day_message(today)
    else:
        text = _synthesize(
            today, charlie_context, archive_excerpt, emails, due_followups,
            reminders_due, news_items, current_focus,
        )

    fired_reminder_ids = [] if dry_run else [r["id"] for r in reminders_due]
    return text, fired_reminder_ids, today
