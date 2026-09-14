"""
Per-topic conversation history backed by SQLite.
Each topic_id has its own independent conversation thread.
Messages are stored in Anthropic API format and reloaded as plain dicts.
"""

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "charlie.db"


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id  TEXT    NOT NULL,
                role      TEXT    NOT NULL,
                content   TEXT    NOT NULL,
                timestamp TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deletion_warnings (
                topic_id  TEXT PRIMARY KEY,
                warned_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def load_history(topic_id: int) -> list:
    """Load full message history for a topic in Anthropic API format."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE topic_id = ? ORDER BY id",
            (str(topic_id),)
        ).fetchall()

    messages = []
    for role, content_str in rows:
        try:
            content = json.loads(content_str)
        except (json.JSONDecodeError, TypeError):
            content = content_str
        messages.append({"role": role, "content": content})
    return messages


def _serialize_content(content) -> str:
    """
    Turn a message's content (a plain string or a list of API response blocks) into
    the JSON string stored in the messages table. Thinking blocks are stripped —
    they don't need to persist.
    """
    if isinstance(content, str):
        return json.dumps(content)
    elif isinstance(content, list):
        serialized = []
        for block in content:
            # Skip thinking blocks
            block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if block_type == "thinking":
                continue
            if hasattr(block, "model_dump"):
                serialized.append(block.model_dump())
            elif isinstance(block, dict):
                serialized.append(block)
            else:
                serialized.append(str(block))
        return json.dumps(serialized)
    else:
        return json.dumps(str(content))


def save_message(topic_id: int, role: str, content):
    """
    Save a single message to history.
    content can be a plain string or a list of API response blocks.
    """
    save_messages(topic_id, [(role, content)])


def save_messages(topic_id: int, entries: list):
    """
    Save all of one turn's new messages (each entry a (role, content) pair) in a
    single transaction — either every message from this turn is persisted, or none
    are. Use this instead of looping over save_message() whenever the messages
    being saved include a model-generated tool_use/tool_result pair.

    BUG-040: a turn's messages were previously saved one at a time with no
    transaction. A transient failure mid-loop (observed cause: a brief
    'unable to open database file' hiccup) could persist a tool_use block from
    one message while never reaching the save call for its paired tool_result —
    permanently corrupting that topic's history, since every future turn resends
    the same broken pair to the Anthropic API and gets rejected. Saving the whole
    batch in one transaction means a mid-batch failure rolls back everything
    already staged in it, so the pairing can never be split across a partial save.
    """
    if not entries:
        return
    rows = [(str(topic_id), role, _serialize_content(content)) for role, content in entries]
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:  # commits all rows on success, rolls back all of them on exception
            conn.executemany(
                "INSERT INTO messages (topic_id, role, content) VALUES (?, ?, ?)",
                rows
            )
    finally:
        conn.close()


def delete_topic_history(topic_id: int):
    """Delete all messages for a topic. Called after distillation is approved or discarded,
    or by the retention sweep (BUG-001)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE topic_id = ?", (str(topic_id),))
        conn.execute("DELETE FROM deletion_warnings WHERE topic_id = ?", (str(topic_id),))
        conn.commit()
    log.info(f"Deleted conversation history for topic {topic_id}")


def get_stale_topics(inactive_days: int) -> list:
    """Topics where Jonathan's own activity (last message with role='user', falling back
    to the topic's very first message if he never replied) is older than inactive_days.
    Charlie's own scheduled posts (e.g. an unanswered morning briefing) don't count as
    activity — see BUG-001."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT topic_id,
                   COUNT(*) AS msg_count,
                   COALESCE(MAX(CASE WHEN role = 'user' THEN timestamp END), MIN(timestamp)) AS last_activity
            FROM messages
            GROUP BY topic_id
            HAVING last_activity < datetime('now', ?)
            """,
            (f"-{inactive_days} days",)
        ).fetchall()
    return [{"topic_id": r[0], "msg_count": r[1], "last_activity": r[2]} for r in rows]


def get_warning(topic_id) -> str:
    """Return the warned_at timestamp for a topic, or None if never warned."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT warned_at FROM deletion_warnings WHERE topic_id = ?", (str(topic_id),)
        ).fetchone()
    return row[0] if row else None


def set_warning(topic_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO deletion_warnings (topic_id, warned_at) VALUES (?, datetime('now'))",
            (str(topic_id),)
        )
        conn.commit()


def clear_warning(topic_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM deletion_warnings WHERE topic_id = ?", (str(topic_id),))
        conn.commit()


def get_all_warned_topic_ids() -> list:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT topic_id FROM deletion_warnings").fetchall()
    return [r[0] for r in rows]
