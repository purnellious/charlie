# Charlie — Personal Chief of Staff

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
- A system prompt built from `charlie.md` + Charlie's personality
- Extended thinking (budget_tokens configurable via env)
- Tool definitions from the `TOOLS` list
Thinking blocks are sent to Telegram as `| ... |` messages before the response.
Returns updated message history + any proposed charlie.md update.

**`core/history.py`** — SQLite-backed conversation history, keyed by topic_id.
Thinking blocks are stripped before storage (they don't persist across sessions).

**`core/scheduler.py`** — APScheduler. Currently one job: creates a morning briefing
Telegram topic at the configured time each day.

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
