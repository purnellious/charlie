# Charlie — Personal Chief of Staff

## Required reading before any action

Before making any change to this project, read these three files in full:

1. **`principles.md`** — the non-negotiable design principles governing how Charlie is built. Every build decision must comply. Use the Pre-Build Checklist (Principle 11) before starting any new feature.
2. **`bugs.md`** — current open bugs and their status. Do not build something that duplicates or conflicts with an open bug.
3. **`devlog.md`** — recent changes. Understand what has changed before making further changes.

---

Charlie is Jonathan's personal Chief of Staff AI. Charlie is not a toolset — Charlie is an
intelligent assistant with a persistent personality, and tools give Charlie capabilities.

## Owner

Non-technical user. Claude Code writes and maintains all code. Never ask Jonathan to write
code or edit files directly.

## Architecture

This is an **agent-with-tools** system. The intelligence is centralised in Charlie. Tools
are capabilities Charlie can call — they do not have their own routing or personalities.

```
charlie/
├── CLAUDE.md              ← you are here
├── .env                   ← all secrets (never commit)
├── charlie.md             ← Charlie's persistent context about Jonathan (gitignored)
├── requirements.txt
├── start.sh               ← manual startup
├── com.charlie.plist      ← launchctl service definition (always-on Mac)
├── core/
│   ├── bot.py             ← Telegram entry point; routes all topic messages to Charlie
│   ├── agent.py           ← Charlie agent (Sonnet + extended thinking + tool loop)
│   ├── history.py         ← SQLite per-topic conversation history
│   ├── scheduler.py       ← APScheduler (morning briefing topic creation)
│   ├── transcribe.py      ← Groq Whisper voice transcription
│   └── tools/
│       ├── claude_code.py ← Claude Code tool (builds new capabilities)
│       └── ...            ← new tools added here over time
└── data/
    └── charlie.db         ← SQLite database (gitignored)
```

## Core files

**`core/bot.py`** — Telegram bot. Listens to a forum group with topics. Routes all messages
to `core/agent.py`. Handles voice transcription, charlie.md update approvals, and the
morning briefing scheduler. Each topic is an independent conversation.

**`core/agent.py`** — The Charlie agent. Calls Claude Sonnet with:
- A system prompt built from `principles.md` (loaded first, as the governing rules), then `charlie.md`, `devlog.md`, and `context-archive.md`
- Extended thinking (budget_tokens configurable via env)
- Tool definitions from the `TOOLS` list
Thinking blocks are sent to Telegram as `| ... |` messages before the response.
Returns updated message history + any proposed charlie.md update.
Missing context files fail silently — load proceeds without them.

**`core/history.py`** — SQLite-backed conversation history, keyed by topic_id.
Thinking blocks are stripped before storage (they don't persist across sessions).

**`core/scheduler.py`** — APScheduler. Currently one job: creates a morning briefing
Telegram topic at the configured time each day.

## Bug management

Bugs are tracked in `bugs.md` and each open bug has a dedicated Telegram topic (named
`❗ BUG-NNN — title`). The mapping between bug ID and topic_id is stored in bugs.md.

- **Logging a bug:** tell Charlie in natural language → Charlie calls the `log_bug` tool →
  `core/tools/bugs.py` creates the bugs.md entry and Telegram topic
- **Resolving a bug:** say "this is resolved" in the bug topic → Charlie assesses the
  conversation → calls `resolve_bug` tool if confirmed → marks bugs.md as Closed
- **Accidental close:** if you close a bug topic whose bug is still Open, Charlie automatically
  reopens it (via the `ForumTopicClosed` handler in bot.py)
- **Batch topic creation:** `/createbugtopics` command creates topics for all open bugs without one
- **After resolving:** run `/distil` to archive the resolution and delete the raw history

`core/state.py` holds the shared Telegram app reference so tools can make Telegram API calls.
Set by `post_init()` in bot.py.

## Adding a new scheduled job

All scheduler jobs that send proactive messages MUST use `proactive_send()` from
`core/scheduler.py` instead of calling `app.bot.send_message()` directly. This function
sends the message AND saves it to the conversation history DB in one call. If you use
`app.bot.send_message()` directly, Charlie will have no memory of what it said when the
user replies, breaking the conversation.

```python
await proactive_send(app, group_id, thread_id, message_text)
```

## Adding a new tool

1. Create `core/tools/your_tool.py` with an async entry function.
2. Add the tool definition to the `TOOLS` list in `core/agent.py`.
3. Add a dispatch handler inside the tool-calling loop in `handle_turn()` in `core/agent.py`.

New tools should be self-contained — their own rules, data fetching, and error handling live
inside the tool file. The base system prompt in `core/agent.py` stays lean.

## charlie.md

`charlie.md` is Charlie's persistent context document — everything Charlie knows about
Jonathan. It is gitignored and grows organically. Charlie proposes updates using the
`propose_charlie_update` tool; Jonathan approves them before anything is written.

## context-archive.md

`context-archive.md` stores distilled context from completed topics. It is committed to git
and loaded into Charlie's system prompt alongside `charlie.md`. Each entry is the minimum
useful signal from a conversation — added via `/distil` when closing a topic.

Do not write to this file directly. It is appended to by `_append_to_context_archive()` in
`core/bot.py` when a distillation is approved.

## devlog.md

`devlog.md` is the shared change log. Both Claude Code and Charlie read and write it.
When making a significant change, add a one-line entry at the top (date + what changed).
This keeps both Claude Code sessions and Charlie in sync on the current state of the system.

## Deployment

- Runs as `com.charlie` via launchctl on the always-on Mac (jonathanpurnell@10.0.0.119)
- Entry point: `core/bot.py` (WorkingDirectory: `/Users/jonathanpurnell/charlie`)
- venv at `~/charlie/venv/`
- Logs: `~/charlie/charlie.log`
- **To sync from primary Mac:**
  `rsync -avz --exclude='venv/' --exclude='*.db' --exclude='.git/' --exclude='.env' --exclude='charlie.md' -e "ssh -i ~/.ssh/bartie_key -o IdentitiesOnly=yes" ~/charlie/ jonathanpurnell@10.0.0.119:/Users/jonathanpurnell/charlie/`
- **To restart:**
  `launchctl stop com.charlie && launchctl start com.charlie` (on always-on Mac)

## Secrets (.env)

- `TELEGRAM_BOT_TOKEN` — from BotFather (separate bot from Bartie)
- `TELEGRAM_GROUP_ID` — forum group chat ID (negative number)
- `ANTHROPIC_API_KEY`
- `GROQ_API_KEY` — for Whisper transcription
- `MORNING_BRIEFING_TIME` — e.g. "07:30"
- `TIMEZONE` — IANA string, e.g. "Europe/London"
- `THINKING_ENABLED` — "true" / "false"
- `THINKING_BUDGET` — token budget for thinking (default 2000, min 1024)
- `CHARLIE_MODEL` — defaults to "claude-sonnet-4-6"

## Guiding principles

- **Charlie is the intelligence.** Tools give Charlie capabilities; they don't replace Charlie's judgment.
- **Keep the base prompt lean.** Domain-specific rules belong inside tool files, not in the system prompt.
- **charlie.md is the relationship.** Everything Charlie learns about Jonathan lives there.
- **Topics stay focused.** Each topic is a short, purposeful thread — not a sprawling backlog.
- **Cost-conscious by default.** Flag anything that will meaningfully increase API spend.
