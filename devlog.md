# Charlie — Dev Log

A shared record of significant changes. Both Claude Code and Charlie read this.
Newest entries at the top.

2026-06-10: World Cup Phase 1 complete — core/tools/world_cup.py API tool verified against live API; get_match_context, get_standings_context, get_upcoming_fixtures, is_south_africa_match all working; added defensive error handling to all functions
2026-06-10: Built World Cup Tracker — pre-game notifications (30min all games, 60min SA games), post-game SA result messages, football-data.org API integration
2026-06-05: bugs.py now commits and pushes bugs.md to git after every write — topic_ids no longer wiped by rsync
2026-06-03: Added daily 3am bug topic reconciliation — detects and recreates deleted bug topics automatically; also covers open bugs with no topic_id
2026-06-03: Added retry logic to agent.py for 529 overloaded and 429 rate-limit errors (3 retries: 5s, 15s, 30s)
2026-06-03: Reopened BUG-006 — checklist exists in principles.md but enforcement mechanism not yet built; checklist answers must be explicitly output before each build
2026-06-03: System doc alignment pass — fixed CLAUDE.md (Principle 8→11, agent.py description updated); closed BUG-005 (principles.md live and loaded); updated BUG-003 (data-architecture.md now exists); added BUG-009/010 separator in bugs.md
2026-06-03: Updated principles.md — revised to 12 principles including Security, Testing, expanded Foresight/Scope/Approval rules; updated BUG-006 and BUG-004 accordingly
2026-06-03: Created principles.md — 10 core design principles for Charlie; to be loaded into Claude Code and agent system prompt
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
