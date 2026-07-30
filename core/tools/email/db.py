"""
SQLite layer for the email monitor tool (data/emails.db).
No raw email body/snippet is ever stored here — only derived metadata
(see data-architecture.md). thread_id and labels are kept even though
unused today, since they're free from the API response and would
otherwise leave a permanent gap for a future filtering/retriage feature.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent.parent / "data" / "emails.db"

EMAIL_RETENTION_DAYS = 30      # dedup relies on the UNIQUE constraint, not retention —
                               # this just bounds local growth + supports "what did I miss" queries
FEEDBACK_RETENTION_CAP = 200   # small, high-value, kept by count rather than time


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT    NOT NULL UNIQUE,
                thread_id        TEXT    NOT NULL,
                sender_name      TEXT,
                sender_email     TEXT    NOT NULL,
                subject          TEXT    NOT NULL,
                received_at      TEXT    NOT NULL,
                labels           TEXT,
                actionability    TEXT,
                urgent           INTEGER NOT NULL DEFAULT 0,
                confidence       TEXT,
                summary          TEXT,
                notified_at      TEXT,
                created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_received_at ON emails(received_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_thread_id ON emails(thread_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_feedback (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id         INTEGER REFERENCES emails(id),
                original_verdict TEXT    NOT NULL,
                user_correction  TEXT    NOT NULL,
                context_snippet  TEXT,
                created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                id             INTEGER PRIMARY KEY CHECK (id = 1),
                last_synced    TEXT,
                email_topic_id INTEGER
            )
        """)
        conn.execute("INSERT OR IGNORE INTO sync_state (id, last_synced, email_topic_id) VALUES (1, NULL, NULL)")


def filter_unseen(fetched: list[dict]) -> list[dict]:
    """
    Return only the messages not already stored (by gmail_message_id).
    Bodies are never persisted, so triage must happen on this in-memory
    result *before* insertion — there is no later "unprocessed" row to
    come back to, by design (see data-architecture.md).
    """
    if not fetched:
        return []
    ids = [e["gmail_message_id"] for e in fetched]
    with _conn() as conn:
        placeholders = ",".join("?" * len(ids))
        existing = {
            row["gmail_message_id"] for row in conn.execute(
                f"SELECT gmail_message_id FROM emails WHERE gmail_message_id IN ({placeholders})",
                ids,
            ).fetchall()
        }
    return [e for e in fetched if e["gmail_message_id"] not in existing]


def insert_triaged_email(email: dict, verdict: dict) -> int | None:
    """
    Insert one fully-triaged email (verdict already computed). Returns the
    new row id, or None if it turned out to already exist (defensive —
    filter_unseen should have already excluded it).
    """
    with _conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO emails
               (gmail_message_id, thread_id, sender_name, sender_email, subject, received_at,
                labels, actionability, urgent, confidence, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (email["gmail_message_id"], email["thread_id"], email.get("sender_name"), email["sender_email"],
             email["subject"], email["received_at"], json.dumps(email.get("labels", [])),
             verdict["actionability"], int(verdict["urgent"]), verdict["confidence"], verdict["summary"]),
        )
        return cur.lastrowid if cur.rowcount == 1 else None


def get_unnotified() -> list[sqlite3.Row]:
    """
    Rows already fully triaged but not yet successfully sent in a digest —
    includes anything left over from a poll whose Telegram send failed,
    not just what the current call just triaged. All fields needed for a
    digest line are already in this row; no body/re-fetch needed.
    """
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM emails WHERE notified_at IS NULL ORDER BY received_at"
        ).fetchall()


def mark_notified(email_id: int):
    with _conn() as conn:
        conn.execute(
            "UPDATE emails SET notified_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), email_id),
        )


def get_email_by_id(email_id: int) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()


def add_feedback(email_id: int, original_verdict: str, user_correction: str, context_snippet: str = ""):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO email_feedback (email_id, original_verdict, user_correction, context_snippet) "
            "VALUES (?, ?, ?, ?)",
            (email_id, original_verdict, user_correction, context_snippet),
        )
    _prune_feedback()


def get_recent_feedback(limit: int = 20) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            """SELECT f.*, e.sender_email, e.subject FROM email_feedback f
               LEFT JOIN emails e ON e.id = f.email_id
               ORDER BY f.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def get_last_synced() -> str | None:
    with _conn() as conn:
        row = conn.execute("SELECT last_synced FROM sync_state WHERE id=1").fetchone()
        return row["last_synced"] if row else None


def set_last_synced(iso_timestamp: str):
    with _conn() as conn:
        conn.execute("UPDATE sync_state SET last_synced=? WHERE id=1", (iso_timestamp,))


def get_email_topic_id() -> int | None:
    with _conn() as conn:
        row = conn.execute("SELECT email_topic_id FROM sync_state WHERE id=1").fetchone()
        return row["email_topic_id"] if row and row["email_topic_id"] is not None else None


def set_email_topic_id(topic_id: int):
    with _conn() as conn:
        conn.execute("UPDATE sync_state SET email_topic_id=? WHERE id=1", (topic_id,))


def prune_old_emails(days: int = EMAIL_RETENTION_DAYS):
    """Never prunes a row that hasn't been successfully notified yet — e.g. during
    a persistent Telegram outage — so a pending notification can't silently vanish."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.execute(
            "DELETE FROM emails WHERE received_at < ? AND notified_at IS NOT NULL",
            (cutoff,),
        )


def _prune_feedback(cap: int = FEEDBACK_RETENTION_CAP):
    with _conn() as conn:
        conn.execute(
            """DELETE FROM email_feedback WHERE id NOT IN (
                   SELECT id FROM email_feedback ORDER BY created_at DESC LIMIT ?
               )""",
            (cap,),
        )
