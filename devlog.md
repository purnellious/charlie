# Charlie — Dev Log

A shared record of significant changes. Both Claude Code and Charlie read this.

2026-07-30: Updated Larica wellness feeds — replaced New Scientist (406 paywalled) and Greater Good Magazine (404) with Healthline and Vox main RSS. Scientific American skipped (both candidate URLs failed: scientificamerican.com/feed/ 404, rss.sciam.com connection error). Popular Mechanics verified still working. Dry-run confirmed: 5/5 sections, 0 errors.

2026-07-30: Built Larica daily news email — core/tools/larica_news.py (RSS fetch + Sonnet summarisation + HTML builder), core/tools/larica_email.py (Gmail SMTP send), daily 08:00 America/New_York scheduler job. 5 sections: Top Stories, Art & Entertainment, NYC/Jersey City, Wellness & Growth, Heartwarming. Dry-run tested: 5/5 sections populated, all URLs valid, HTML structurally sound.

2026-07-30: Built the email monitor tool (Tier 3) — new `core/tools/email/` package polls `jonathan@ts.org`'s inbox every 2 minutes (read-only `gmail.readonly` OAuth, no send/reply/forward/delete capability anywhere), triages every new message via Claude Haiku into a suggested action (actionability/urgency/confidence/summary), and pushes one batched Telegram digest per poll to a persistent "📧 Email" topic — deliberately no pre-filtering in v1, no draft replies, no thread retriage, no auto-tasks. New `record_email_feedback` tool lets Jonathan correct a verdict via conversation, feeding recent corrections into future triage prompts. Only derived metadata is ever stored (`data/emails.db`) — raw email body is processed in memory only, never written to disk, per data-architecture.md. Re-authorised a stale Gmail OAuth token in the process (found and confirmed the existing one had been silently dead since ~2026-03-30). `/security-review` found no exploitable vulnerabilities; a manual code-review pass found and fixed 5 real correctness issues (cursor advancing before insert loop completed, silent "None/None" in feedback formatting after source-email pruning, one-shot failure alert never repeating for a sustained outage, pruning able to delete not-yet-notified rows, malformed-but-successful triage responses leaving summary silently blank) plus a Telegram rate-limit gap on multi-chunk digests. Logged BUG-016 (topic-existence check duplicated between bugs.py and this tool instead of shared) and BUG-017 (that same check can never actually detect a deleted topic — the bot lacks "manage pinned messages" permission — fails safe by assuming it still exists). Bartie's own 15-minute poller for this account will be turned off once this is confirmed live on the always-on Mac, to avoid duplicate notifications.

2026-07-29: Resolved BUG-015 — data-architecture.md added to CLAUDE.md's required-reading list (4th file, alongside bugs.md), confirmed as build-time-only content rather than loaded into Charlie's system prompt like principles.md/charlie.md/devlog.md/context-archive.md (bugs.md already set this precedent — never loaded into agent.py's system prompt). Added a pointer from principles.md's Pre-Build Checklist too. Accepted tradeoff: Charlie's live conversations don't have this memorized, by design, to avoid paying its token cost every turn for content that's rarely conversation-relevant.

2026-07-29: Resolved BUG-003 (data architecture doc) — its only remaining gap (charlie.db retention row) was closed by BUG-001; re-read the whole document and confirmed nothing else was stale; added the missing deletion_warnings table entry and bumped the footer. Fixed a real gap found in the process: BUG-002's restart confirmation message bypassed proactive_send and wasn't being saved to charlie.db — fixed in restart_and_verify.sh, re-verified live. Logged BUG-015 separately: data-architecture.md calls itself "mandatory" but isn't in CLAUDE.md's required-reading list or loaded into Charlie's system prompt like principles.md is — same enforcement gap BUG-006 fixed, but for this document. Not yet resolved, needs a decision on where it should actually be read from.

2026-07-29: Resolved BUG-001 — 60-day inactivity-based retention for charlie.db (measured by Jonathan's own last message, not Charlie's scheduled posts). <=20 messages deletes silently; >20 warns with a 7-day grace period, cancelled by replying. New daily _run_retention_sweep job (04:00), new core/tools/retention.py, new deletion_warnings table in core/history.py. Ruled out dropping persistent history entirely (Bot API can't fetch topic history). Left the per-turn API context-size issue as a separate, deferred concern. Tested 7 scenarios on the always-on Mac's real venv, caught and fixed a real dead-code bug in the process (see bugs.md for detail); confirmed 0 topics currently qualify on the real DB, so tonight's first run is a no-op.

2026-07-29: Resolved BUG-002 — claude_code.py now flags RESTART REQUIRED when a build's scope touches non-.md files (Python doesn't hot-reload); Charlie asks Jonathan before restarting rather than auto-restarting or claiming "live." New restart_charlie tool launches a detached script (survives the process it kills) that stops/starts com.charlie, verifies new PID + clean startup log, optionally re-runs a build's own verify script, and reports back via direct Telegram API call. Explicitly does not claim the feature itself was tested unless a verify script confirms it — process-alive and feature-works are kept as separate, honestly-labelled checks. Verified for real: isolated parent-kill-survival test, plus a full live run on the always-on Mac (temp Telegram topic, real restart, confirmed new PID/clean log/delivered message, topic cleaned up after).

2026-07-29: Resolved BUG-006 — run_claude_code now requires tier/scope/checklist fields (schema-enforced) plus jonathan_confirmed_risk for Tier 3; core/agent.py validates completeness before dispatch (no extra API call), and core/tools/claude_code.py checks actual changed files against declared scope before auto-commit, blocking commit/push on any mismatch instead of silently pushing everything. This is the mechanical fix for the failure mode that wiped bugs.md twice. Verified locally, then deployed to the always-on Mac and re-verified there: com.charlie restarted cleanly on the new code (new PID, scheduler + jobs OK, no errors), same validation checks re-run successfully in its venv. Also recovered an unrelated uncommitted distillation (context-archive.md, job search/financial context) found on the always-on Mac during the pull — committed and rebased in rather than overwritten. Still not verified via an actual live Telegram-triggered build.
2026-07-29: Logged BUG-014 — blanket --dangerously-skip-permissions in claude_code.py disables Claude Code's own default caution for non-file side effects (network calls etc.) that the new scope-diff gate can't see. Deferred, not built — documented for later with full context from the BUG-006 design discussion.
2026-07-29: Added Post-Build Checklist to Principle 9 and Build Tiers (1/Low, 2/Standard, 3/High) to Principle 11 in principles.md, resolving BUG-011. Tiers scale post-build requirements by risk — Tier 2+ mandates a scope-diff check and /code-review; Tier 3 adds explicit pre-build approval and /security-review. BUG-006 (checklist enforcement) remains open and unaffected — this only strengthens the checklists' content, not whether Claude Code actually runs them.
2026-07-29: Restored BUG-011 (no end-state verification checklist) after discovering it — along with BUG-012 and BUG-013 — had been silently deleted from bugs.md by an unrelated commit on 2026-07-08 ("Check all the RSS feeds..."), an unscoped task that shouldn't have touched bugs.md. Recovered all three from git history; BUG-012/013 (World Cup topic issues) reviewed and intentionally left out since the World Cup module is deprecated. This is the same failure mode BUG-006 already documents (unscoped task silently rewriting a protected file) recurring two months later — worth reinforcing file-scoping in task prompts going forward.
2026-07-28: Fixed daily auto-pull on main Mac — com.charlie.gitpull launchd job was running plain `git pull`, which aborts on any dirty working tree; it had been failing silently every day since 2026-06-03, leaving this Mac 40 commits behind. New `gitpull.sh` stashes (--include-untracked) before pulling; does not auto-pop, so any local WIP is preserved but requires manual review rather than being silently reapplied. Plist updated to call the script; LaunchAgents copy reloaded and tested (clean-tree and dirty-tree cases both verified).
2026-07-27: Fixed grants pipeline (5 fixes) — L3 rewritten as strict quality filter (JSON match/reject with rejection criteria); within-run URL dedup added before L1; descriptions use _smart_truncate (3 sentences/800 chars); encoding fixed via apparent_encoding in _safe_get; JCAC tightened to 4 specific entry pages, expanded skip titles, nav path filter added
2026-07-23: Fixed load_dotenv in grants pipeline — added load_dotenv() to grants.py and grants_email.py so GRANT_GMAIL_ADDRESS/GRANT_GMAIL_PASSWORD/GRANT_RECIPIENT_EMAIL load correctly when run standalone (not via bot.py)
2026-07-23: Fixed grants scrapers — NJSCA: corrected col-xl-9 column detection (NJ.gov has 4 such divs; was picking nav column, now picks content column); JCAC: rewrote to follow internal links only, skip pages with disqualifying titles, extract paragraphs + deadline patterns from each page
Newest entries at the top.

2026-07-23: Built grants email + scheduler layer (Part 2) — grants_email.py: format_grants_email(list[dict])->(subject,html), send_grants_email(subject,html)->bool; fixed scheduler _run_grants_pipeline (wrong import/return); Monday 08:00 job registered; SMTP send tested and confirmed
2026-07-23: Built grants pipeline (Part 1) — core/tools/grants.py; scrapes CaFÉ, NJSCA, JCAC, Gmail; L1/L2/L3 verification; dedup against grants.db; Haiku categorisation; run_grants_pipeline(dry_run) returns list[dict]; grants_email.py (Part 2) already present
2026-07-08: Added retry logic to news briefing — on transient failure, schedules a one-off date job 5 minutes later (is_retry=True); second failure logs and gives up; cron schedule untouched
2026-07-08: Noon news briefing failed at 12:00 EDT — transient SQLite "unable to open database file" error on Daily Maverick article storage, coinciding with simultaneous DNS failures on 4 other sources (IOL, Al Jazeera, BBC, Guardian); 4 sources (MIT Tech Review, The Verge, CoinDesk, CoinTelegraph) fetched successfully before the failure; briefing manually sent to Telegram (thread_id=1622); news.db is healthy
2026-07-08: Updated news sources — fixed Daily Maverick URL (rss/ path), replaced dead News24 feed with IOL, replaced dead Reuters feed with Al Jazeera World
2026-07-08: Added Council tool — core/tools/council.py; 8-member pool (conservative, opportunist, long_term_thinker, pragmatist, financial_skeptic, user_advocate, contrarian, minimalist); two-round hybrid (parallel independent takes → parallel debate) + synthesis; all on Sonnet with extended thinking; Charlie handles composition conversationally before calling convene_council
2026-07-08: Added news module — core/tools/news.py (RSS fetch + Haiku summarisation + source management), 4 new agent tools (get_news_briefing, news_add_source, news_remove_source, news_list_sources), noon scheduled briefing topic (NEWS_BRIEFING_TIME env var), feedparser dependency added

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
