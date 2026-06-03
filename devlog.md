# Charlie — Dev Log

A shared record of significant changes. Both Claude Code and Charlie read this.
Newest entries at the top.

2026-06-03: Updated principles.md — revised to 12 principles including Security, Testing, expanded Foresight/Scope/Approval rules; updated BUG-006 and BUG-004 accordingly
2026-06-03: Created principles.md — 10 core design principles for Charlie; to be loaded into Claude Code and agent system prompt
2026-06-03: Added daily 3am bug topic reconciliation — detects and recreates deleted bug topics automatically
2026-06-03: Added retry logic to agent.py for 529 overloaded and 429 rate-limit errors (3 retries: 5s, 15s, 30s)
2026-05-30: Added send_and_save() to bot.py — all substantive bot messages now saved to DB; only ephemeral status messages ("Still thinking...", "one moment...") excluded
2026-05-30: Fixed scheduler to persist sent messages to DB as assistant messages so agent can see them in history; added architectural rule to bugs.md
2026-05-29: Added capabilities boundary to system prompt — Claude Code runs on always-on Mac only
2026-05-29: Fixed /meta — Charlie's take now receives full conversation history; both meta and take persisted to DB
2026-05-29: Added com.charlie.gitpull.plist — daily 9am git pull for main Mac

- 2026-05-29: Updated BUG-006 to include file-scoping rule and incident documentation

---

## 2026-06-01 (2)
- Built bug topic system: core/tools/bugs.py, core/state.py
- Agent tools: log_bug (creates bugs.md entry + Telegram topic), resolve_bug (marks closed)
- Bot: /createbugtopics command, ForumTopicClosed handler (auto-reopens unresolved bugs)
- Bug topics named with ❗ emoji prefix for easy identification

## 2026-06-01
- Built /distil command: distils topic conversation into context-archive.md, then deletes raw history
- Created context-archive.md: loaded into Charlie's system prompt alongside charlie.md
- Added delete_topic_history() to history.py
- Three-option approval flow: approve (save + delete), discard (delete only), reject (keep history)

## 2026-05-29
- Added BUG-006 (pre-build checklist) and BUG-007 (active task list) to bugs.md
- Created bugs.md; logged BUG-001 (SQLite unbounded message history)
- Added /meta command: posts a ruthless three-section review of any topic's conversation via a fresh Claude call
- Fixed /meta: added Step 3 — after the review, calls the main Charlie agent (full context) for its reaction; response posted as "Charlie's take:" with label on first chunk; proposed charlie.md updates handled properly
- Added followups.md tracker; surfaces open chase items in morning briefing
- Added daily 8am check-in Telegram topic via APScheduler
- Changed timezone to America/New_York
- Added daily 9am git pull launchd agent on MacBook
- Wired automatic git commit and push to github.com/purnellious/charlie after every Claude Code change
- Added devlog.md as shared change log between Claude Code and Charlie
- Load devlog in Charlie's agent system prompt
- Initialised git repo, connected to github.com/purnellious/charlie
- Installed Charlie as launchctl service (com.charlie) on always-on Mac
- Fixed Claude Code tool to use claude-sonnet-4-6 and added retry logic for rate limits
- Built Charlie v1: Telegram topics bot, Sonnet agent, extended thinking, voice input, claude_code tool, morning briefing scheduler
