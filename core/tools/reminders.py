"""
Lightweight recurring/one-off reminder store for the Morning Briefing.

Added conversationally via Charlie's add_reminder/dismiss_reminder agent tools —
no approval ceremony, do-then-inform (Jonathan's explicit sign-off, 13 Aug 2026,
Morning Briefing v2 design). Self-contained per CLAUDE.md's tool-file convention:
no imports from other Charlie modules besides the standard library.
"""
import json
import logging
import os
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

REMINDERS_PATH = Path(__file__).parent.parent.parent / "data" / "reminders.json"

# add_reminder/dismiss_reminder (interactive, any time Jonathan is chatting) and
# advance_after_briefing (the scheduler, once a day) all do an unlocked read-
# modify-write of reminders.json — exactly the race BUG-019 already found and
# fixed for bugs.md (_BUGS_LOCK) and news.py (_BRIEFING_LOCK): two overlapping
# writers can each read the pre-write state and the second save silently loses
# the first one's change. Same fix here.
_REMINDERS_LOCK = threading.Lock()

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_VALID_RECURRENCES_SIMPLE = {"once", "daily"}


def _today() -> date:
    """"Today" in the app's configured TIMEZONE, not the host OS's — same reasoning
    as grants.py's _today() (BUG-035): a server in a different timezone must not
    silently anchor due-date checks to the wrong day."""
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "UTC"))).date()


def load_reminders() -> list[dict]:
    if not REMINDERS_PATH.exists():
        return []
    try:
        return json.loads(REMINDERS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Failed to load reminders.json: {e}")
        return []


def save_reminders(reminders: list[dict]) -> None:
    REMINDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = REMINDERS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(reminders, indent=2))
    tmp_path.replace(REMINDERS_PATH)  # atomic on the same filesystem


def _validate_recurrence(recurrence: str) -> None:
    if recurrence in _VALID_RECURRENCES_SIMPLE:
        return
    if recurrence.startswith("weekly:"):
        weekday = recurrence.split(":", 1)[1].strip().lower()
        if weekday in _WEEKDAYS:
            return
    if recurrence.startswith("monthly:"):
        day_str = recurrence.split(":", 1)[1].strip()
        if day_str.isdigit() and 1 <= int(day_str) <= 31:
            return
    raise ValueError(
        f"Unknown recurrence {recurrence!r} — expected 'once', 'daily', "
        f"'weekly:<weekday>', or 'monthly:<1-31>'"
    )


def _next_monthly(from_date: date, day: int) -> date:
    """Next occurrence (from_date included) of a given day-of-month, clamped to
    the last valid day of a short month (e.g. day=31 in April lands on the 30th)
    rather than raising."""
    def _clamped(year: int, month: int) -> date:
        # Find the last valid day of (year, month), then clamp `day` to it.
        if month == 12:
            last_day = (date(year + 1, 1, 1) - timedelta(days=1)).day
        else:
            last_day = (date(year, month + 1, 1) - timedelta(days=1)).day
        return date(year, month, min(day, last_day))

    candidate = _clamped(from_date.year, from_date.month)
    if candidate >= from_date:
        return candidate
    year = from_date.year + (1 if from_date.month == 12 else 0)
    month = 1 if from_date.month == 12 else from_date.month + 1
    return _clamped(year, month)


def _compute_next_due(recurrence: str, from_date: date) -> str:
    """Returns the next due date (from_date included) as an ISO string."""
    _validate_recurrence(recurrence)
    if recurrence in ("once", "daily"):
        return from_date.isoformat()
    if recurrence.startswith("weekly:"):
        weekday = recurrence.split(":", 1)[1].strip().lower()
        target = _WEEKDAYS.index(weekday)
        days_ahead = (target - from_date.weekday()) % 7
        return (from_date + timedelta(days=days_ahead)).isoformat()
    if recurrence.startswith("monthly:"):
        day = int(recurrence.split(":", 1)[1].strip())
        return _next_monthly(from_date, day).isoformat()
    raise ValueError(f"Unhandled recurrence: {recurrence}")  # unreachable — _validate_recurrence gates this


def add_reminder(description: str, recurrence: str, context: str | None = None) -> dict:
    """Write immediately, no approval gate — do-then-inform per Jonathan's
    explicit sign-off. Raises ValueError on an invalid recurrence string."""
    today = _today()
    reminder = {
        "id": str(uuid.uuid4()),
        "description": description,
        "recurrence": recurrence,
        "context": context,
        "next_due": _compute_next_due(recurrence, today),
        "fired": False,
        "created_at": today.isoformat(),
    }
    with _REMINDERS_LOCK:
        reminders = load_reminders()
        reminders.append(reminder)
        save_reminders(reminders)
    log.info(f"Reminder added: {description!r} ({recurrence})")
    return reminder


def dismiss_reminder(reminder_id: str) -> dict | None:
    """Removes a reminder outright (not a soft-delete — dismissal is a deliberate,
    conversational action, unlike a one-off's automatic 'fired' after it appears).
    Returns the removed reminder dict, or None if no match."""
    with _REMINDERS_LOCK:
        reminders = load_reminders()
        match = next((r for r in reminders if r["id"] == reminder_id), None)
        if match is None:
            return None
        reminders = [r for r in reminders if r["id"] != reminder_id]
        save_reminders(reminders)
    log.info(f"Reminder dismissed: {match['description']!r}")
    return match


def find_reminders_by_description(text: str) -> list[dict]:
    """Best-effort case-insensitive substring match, for dismiss_reminder's tool
    handler — Charlie identifies a reminder by what Jonathan calls it in
    conversation, not by its opaque id. Returns every match (not just the
    first) so the caller can detect ambiguity — the add_reminder/dismiss_reminder
    tool description tells Charlie to ask which one before dismissing when more
    than one plausibly matches; returning only a single guess here would make
    that impossible to enforce and risk silently dismissing the wrong one."""
    text_lower = text.strip().lower()
    if not text_lower:
        return []
    return [r for r in load_reminders() if text_lower in r["description"].lower()]


def get_due_reminders(check_date: date) -> list[dict]:
    """Reminders due on or before check_date. A fired one-off never reappears;
    a recurring reminder is purely date-gated (its 'fired' field is unused)."""
    check_str = check_date.isoformat()
    return [
        r for r in load_reminders()
        if r["next_due"] <= check_str and not (r["recurrence"] == "once" and r["fired"])
    ]


def advance_after_briefing(fired_ids: list[str], check_date: date) -> None:
    """Called once a briefing has actually included these reminders. One-offs are
    marked fired (kept, not deleted — Jonathan's explicit call: data deletion
    needs a real dismiss, not an automatic side effect of appearing once).
    Recurring reminders get next_due advanced to their next occurrence after
    check_date, so they don't immediately re-fire in the same run.

    Must be called only after the briefing containing these reminders has
    actually been delivered (not merely composed) — calling it any earlier
    marks a reminder as shown even if the send that was supposed to show it
    then fails."""
    if not fired_ids:
        return
    fired_ids = set(fired_ids)
    with _REMINDERS_LOCK:
        reminders = load_reminders()
        for r in reminders:
            if r["id"] not in fired_ids:
                continue
            if r["recurrence"] == "once":
                r["fired"] = True
            else:
                r["next_due"] = _compute_next_due(r["recurrence"], check_date + timedelta(days=1))
        save_reminders(reminders)
