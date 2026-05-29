# Charlie — Dev Log

A shared record of significant changes. Both Claude Code and Charlie read this.
Newest entries at the top.

---

## 2026-05-29
- Rewrote bugs.md with full structure; added BUG-002 through BUG-005 from /meta review
- Created bugs.md; logged BUG-001 (SQLite unbounded message history)
- Added /meta command: posts a ruthless three-section review of any topic's conversation via a fresh Claude call
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
