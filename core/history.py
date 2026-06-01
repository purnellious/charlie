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


def save_message(topic_id: int, role: str, content):
    """
    Save a message to history.
    content can be a plain string or a list of API response blocks.
    Thinking blocks are stripped before storage — they don't need to persist.
    """
    if isinstance(content, str):
        content_to_store = json.dumps(content)
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
        content_to_store = json.dumps(serialized)
    else:
        content_to_store = json.dumps(str(content))

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (topic_id, role, content) VALUES (?, ?, ?)",
            (str(topic_id), role, content_to_store)
        )
        conn.commit()


def delete_topic_history(topic_id: int):
    """Delete all messages for a topic. Called after distillation is approved or discarded."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE topic_id = ?", (str(topic_id),))
        conn.commit()
    log.info(f"Deleted conversation history for topic {topic_id}")
