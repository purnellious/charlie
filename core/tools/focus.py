"""
Lightweight "current priority sequencing" note for the Morning Briefing.

Set conversationally via Charlie's set_current_focus tool — no approval
ceremony, do-then-inform, same reversibility profile as reminders.py (see
CLAUDE.md's tool-file convention: self-contained, stdlib-only). Exists so a
real prioritization discussion with Jonathan (e.g. "close SA Companies and
Twelve Sigma first this week") persists into tomorrow's briefing instead of
Haiku re-deriving sequencing from the raw followups.md list every single day.

Deliberately self-expiring (expires_on is required and must be in the
future): a note with no expiry is exactly the "frozen forever" failure mode
that caused a stale relative day-count to get parroted back weeks after it
was written (see devlog 2026-09-17) — this file is built so that mistake
can't repeat here.

No lock around the read-modify-write, unlike reminders.json: the only writer
is this one conversational tool (the scheduler only ever reads), and
Telegram's concurrent_updates is off (BUG-034), so overlapping writes to the
same topic can't race here the way add_reminder/dismiss_reminder/
advance_after_briefing can race each other on reminders.json.
"""
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

FOCUS_PATH = Path(__file__).parent.parent.parent / "data" / "focus.json"


def _today() -> date:
    """"Today" in the app's configured TIMEZONE, not the host OS's — same
    reasoning as reminders.py's/grants.py's _today() (BUG-035)."""
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "UTC"))).date()


def set_focus(note: str, expires_on: str) -> dict:
    """Writes immediately, no approval gate. Raises ValueError if note is
    empty or expires_on isn't a valid future ISO date."""
    note = note.strip()
    if not note:
        raise ValueError("note cannot be empty")
    try:
        expiry = date.fromisoformat(expires_on)
    except ValueError:
        raise ValueError(f"expires_on must be an ISO date (YYYY-MM-DD), got {expires_on!r}")
    today = _today()
    if expiry < today:
        raise ValueError(f"expires_on ({expires_on}) is in the past — today is {today.isoformat()}")
    record = {"note": note, "expires_on": expiry.isoformat(), "set_on": today.isoformat()}
    FOCUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = FOCUS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(record, indent=2))
    tmp_path.replace(FOCUS_PATH)  # atomic on the same filesystem
    log.info(f"Focus set: {note!r} (expires {expiry.isoformat()})")
    return record


def get_active_focus() -> dict | None:
    """Returns the current focus record, or None if unset or past its own
    expiry — an expired note is treated as absent, never surfaced stale."""
    if not FOCUS_PATH.exists():
        return None
    try:
        record = json.loads(FOCUS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Failed to load focus.json: {e}")
        return None
    if date.fromisoformat(record["expires_on"]) < _today():
        return None
    return record
