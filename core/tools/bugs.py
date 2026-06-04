"""
Bug management tool — reads and writes bugs.md, creates Telegram topics for bugs.
All bug state lives in bugs.md as the single source of truth.
"""

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from core.history import save_message

log = logging.getLogger(__name__)

BUGS_PATH = Path(__file__).parent.parent.parent / "bugs.md"

# Telegram topic name limit
MAX_TOPIC_NAME = 128


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_bugs() -> list[dict]:
    """Parse bugs.md into a list of bug dicts."""
    if not BUGS_PATH.exists():
        return []

    content = BUGS_PATH.read_text()
    sections = re.split(r'\n(?=## BUG-\d+)', content)
    bugs = []

    for section in sections:
        header = re.match(r'## (BUG-\d+) — (.+)', section.strip())
        if not header:
            continue

        bug_id = header.group(1)
        title = header.group(2).strip()

        def field(name: str) -> str | None:
            m = re.search(rf'\*\*{re.escape(name)}:\*\*\s*(.+)', section)
            return m.group(1).strip() if m else None

        problem_m = re.search(r'\*\*Problem:\*\*\n(.*?)(?=\n\*\*|\Z)', section, re.DOTALL)
        problem = problem_m.group(1).strip() if problem_m else ""

        bugs.append({
            'bug_id': bug_id,
            'title': title,
            'type': field('Type'),
            'status': field('Status'),
            'priority': field('Priority'),
            'severity': field('Severity'),
            'effort': field('Rough effort'),
            'logged': field('Logged'),
            'topic_id': field('Topic ID'),
            'problem': problem,
        })

    return bugs


def get_next_bug_id() -> str:
    bugs = parse_bugs()
    if not bugs:
        return "BUG-001"
    max_num = max(int(b['bug_id'].split('-')[1]) for b in bugs)
    return f"BUG-{max_num + 1:03d}"


def get_bug_by_topic_id(topic_id: int) -> dict | None:
    for bug in parse_bugs():
        if bug.get('topic_id') and str(bug['topic_id']) == str(topic_id):
            return bug
    return None


def get_bug_by_id(bug_id: str) -> dict | None:
    for bug in parse_bugs():
        if bug['bug_id'] == bug_id:
            return bug
    return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def clear_bug_topic_id(bug_id: str):
    """Remove the Topic ID field from a bug entry."""
    lines = BUGS_PATH.read_text().splitlines()
    result = []
    in_bug = False
    for line in lines:
        if line.startswith(f'## {bug_id} — '):
            in_bug = True
        elif re.match(r'^## BUG-\d+', line):
            in_bug = False
        if in_bug and line.startswith('**Topic ID:**'):
            continue  # strip the stale topic_id
        result.append(line)
    BUGS_PATH.write_text('\n'.join(result))
    log.info(f"Cleared topic_id for {bug_id}")


def set_bug_topic_id(bug_id: str, topic_id: int):
    """Add or update the Topic ID field in a bug entry."""
    lines = BUGS_PATH.read_text().splitlines()
    result = []
    in_bug = False
    topic_set = False

    for line in lines:
        if line.startswith(f'## {bug_id} — '):
            in_bug = True
            topic_set = False
        elif re.match(r'^## BUG-\d+', line):
            in_bug = False

        # Replace existing Topic ID line
        if in_bug and line.startswith('**Topic ID:**'):
            result.append(f'**Topic ID:** {topic_id}')
            topic_set = True
            continue

        result.append(line)

        # Insert after **Logged:** if not yet added
        if in_bug and line.startswith('**Logged:**') and not topic_set:
            result.append(f'**Topic ID:** {topic_id}')
            topic_set = True

    BUGS_PATH.write_text('\n'.join(result))
    log.info(f"Set topic_id={topic_id} for {bug_id}")


def close_bug(bug_id: str, resolution: str):
    """Mark a bug as Closed and append resolution notes."""
    lines = BUGS_PATH.read_text().splitlines()
    result = []
    in_bug = False

    for line in lines:
        if line.startswith(f'## {bug_id} — '):
            in_bug = True
        elif re.match(r'^## BUG-\d+', line):
            in_bug = False

        if in_bug and line.startswith('**Status:** Open'):
            result.append('**Status:** Closed')
            result.append(f'**Resolution:** {resolution}')
        else:
            result.append(line)

    BUGS_PATH.write_text('\n'.join(result))
    log.info(f"Closed {bug_id}")


def reopen_bug(bug_id: str):
    """Mark a bug as Open (removing Closed/Resolution lines)."""
    lines = BUGS_PATH.read_text().splitlines()
    result = []
    in_bug = False

    for line in lines:
        if line.startswith(f'## {bug_id} — '):
            in_bug = True
        elif re.match(r'^## BUG-\d+', line):
            in_bug = False

        if in_bug and line.startswith('**Status:** Closed'):
            result.append('**Status:** Open')
            continue
        if in_bug and line.startswith('**Resolution:**'):
            continue  # Remove resolution line on reopen

        result.append(line)

    BUGS_PATH.write_text('\n'.join(result))
    log.info(f"Reopened {bug_id}")


def create_bug_entry(
    title: str,
    type: str,
    priority: str,
    severity: str,
    effort: str,
    problem: str,
    what_to_fix: str,
) -> str:
    """Append a new bug entry to bugs.md. Returns the new bug ID."""
    bug_id = get_next_bug_id()
    today = datetime.now().strftime('%Y-%m-%d')

    entry = f"""
## {bug_id} — {title}
**Type:** {type}
**Status:** Open
**Priority:** {priority}
**Severity:** {severity}
**Blocks anything current:** No
**Rough effort:** {effort}
**Logged:** {today}

**Problem:**
{problem}

**What needs fixing:**
{what_to_fix}

**Touches:**
TBD

---
"""
    content = BUGS_PATH.read_text() if BUGS_PATH.exists() else ""
    BUGS_PATH.write_text(content.rstrip('\n') + '\n' + entry)
    log.info(f"Created {bug_id}: {title}")
    return bug_id


# ---------------------------------------------------------------------------
# Telegram topic creation
# ---------------------------------------------------------------------------

async def create_bug_topic(bug: dict) -> int:
    """
    Create a Telegram forum topic for a bug.
    Saves the topic_id back to bugs.md and posts an opening message.
    Returns the thread_id.
    """
    from core import state

    app = state.get_app()
    group_id = state.group_id

    topic_name = f"❗ {bug['bug_id']} — {bug['title']}"
    topic_name = topic_name[:MAX_TOPIC_NAME]

    forum_topic = await app.bot.create_forum_topic(
        chat_id=group_id,
        name=topic_name,
    )
    thread_id = forum_topic.message_thread_id

    # Opening message with bug summary
    problem_preview = bug.get('problem', '')
    if len(problem_preview) > 300:
        problem_preview = problem_preview[:300] + '...'

    opening = (
        f"{bug['bug_id']} — {bug['title']}\n\n"
        f"Type: {bug.get('type', '?')} | Priority: {bug.get('priority', '?')} | "
        f"Severity: {bug.get('severity', '?')} | Effort: {bug.get('effort', '?')}\n\n"
        f"{problem_preview}"
    )

    await app.bot.send_message(
        chat_id=group_id,
        message_thread_id=thread_id,
        text=opening,
    )
    save_message(thread_id, "assistant", opening)

    set_bug_topic_id(bug['bug_id'], thread_id)
    log.info(f"Created topic {thread_id} for {bug['bug_id']}")
    return thread_id


async def create_topics_for_all_open_bugs() -> list[str]:
    """Batch-create topics for all open bugs without a topic_id. Returns list of bug IDs processed."""
    bugs = parse_bugs()
    to_create = [b for b in bugs if b.get('status') == 'Open' and not b.get('topic_id')]
    created = []

    for bug in to_create:
        try:
            await create_bug_topic(bug)
            created.append(bug['bug_id'])
            await asyncio.sleep(0.5)  # avoid Telegram rate limiting
        except Exception as e:
            log.error(f"Failed to create topic for {bug['bug_id']}: {e}")

    return created


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

async def _topic_exists(bot, group_id: str, topic_id: int) -> bool:
    """Return True if the Telegram forum topic still exists.

    Uses unpin_all_forum_topic_messages — idempotent (no visible effect if no pins),
    but fails with a thread-related error if the topic has been deleted.
    Fails safe: any unexpected error is treated as the topic existing, to avoid
    false positives that would create duplicate topics.
    """
    from telegram.error import BadRequest
    try:
        await bot.unpin_all_forum_topic_messages(chat_id=group_id, message_thread_id=topic_id)
        return True
    except BadRequest as e:
        err = str(e).lower()
        if "thread" in err or "message_thread" in err:
            return False  # topic is gone
        # Unexpected BadRequest (e.g. permissions) — assume exists to avoid duplicates
        log.warning(f"Unexpected error checking topic {topic_id}: {e}")
        return True
    except Exception as e:
        log.warning(f"Could not verify topic {topic_id}: {e}")
        return True  # fail safe


async def reconcile_bug_topics(bot, group_id: str) -> list[str]:
    """
    Ensure every open bug has a live Telegram topic.
    - Bugs with no topic_id: create one.
    - Bugs with a stale topic_id (topic deleted): clear and recreate.
    Returns list of bug IDs that had topics created or recreated.
    """
    bugs = parse_bugs()
    open_bugs = [b for b in bugs if b.get('status') == 'Open']
    created = []

    for bug in open_bugs:
        raw_topic_id = bug.get('topic_id')

        if not raw_topic_id:
            # No topic at all — create one
            log.info(f"{bug['bug_id']} has no topic — creating")
            try:
                await create_bug_topic(bug)
                created.append(bug['bug_id'])
            except Exception as e:
                log.error(f"Failed to create topic for {bug['bug_id']}: {e}")
            await asyncio.sleep(0.5)
        else:
            # Has a topic_id — verify it still exists
            topic_id = int(raw_topic_id)
            if not await _topic_exists(bot, group_id, topic_id):
                log.warning(f"Topic {topic_id} for {bug['bug_id']} is gone — recreating")
                clear_bug_topic_id(bug['bug_id'])
                fresh_bug = get_bug_by_id(bug['bug_id'])
                try:
                    await create_bug_topic(fresh_bug)
                    created.append(bug['bug_id'])
                except Exception as e:
                    log.error(f"Failed to recreate topic for {bug['bug_id']}: {e}")
                await asyncio.sleep(0.5)

    return created
