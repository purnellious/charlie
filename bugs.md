# Charlie — Bug List

---

**Architectural rule: Any message sent directly via the Telegram bot (app.bot.send_message, bot.send_message, etc.) is NOT automatically saved to the messages DB. Any background process, scheduler, or tool that sends Telegram messages must explicitly write those messages to the DB as assistant messages (with the correct topic_id) immediately after sending, so the agent can see them in conversation history when the user replies.**

---

Open issues to be worked through over time. Newest bugs at the bottom.

**Fields per entry:**
- **Type:** Bug / Debt / Rule
- **Status:** Open / In Progress / Closed
- **Priority:** High / Medium / Low
- **Severity:** High (breaks things or violates core principles) / Medium (creates friction or inefficiency) / Low (nice to have)
- **Blocks anything current:** Yes / No — and what
- **Rough effort:** Small (< 1 hour) / Medium (half day) / Large (multi-session)

---

## BUG-001 — SQLite message history grows indefinitely
**Type:** Debt
**Status:** Closed
**Priority:** Medium
**Severity:** High — violates Jonathan's core data minimalism principle and accumulates sensitive conversation data indefinitely
**Blocks anything current:** No — but will become a cost and privacy problem over time
**Rough effort:** Medium
**Logged:** 2026-05-29
**Topic ID:** 1693

**Problem:**
All messages (user and assistant) are stored in `data/charlie.db` with no expiry, cleanup, or deletion logic. The database grows forever. This means: (1) sensitive conversation data accumulates indefinitely on disk, (2) the full history is passed to the Claude API on every message in a topic, increasing cost and latency over time, and (3) it directly contradicts the design principle of keeping Charlie lightweight and data-minimal. This was not surfaced to Jonathan until after the Meta tool was built — it should have been caught at design stage.

**What needs fixing:**
Decide on and implement a retention policy. Options: auto-delete after N days, topic-scoped retention only (wipe when topic goes inactive), or removing persistent history entirely and relying on Telegram's own record (fetched on demand). Also: the disclosure failure should be prevented going forward by the data architecture document (see BUG-003).

**Resolved:** 2026-07-29 — ruled out "rely on Telegram's own record" (option 3): the Bot API has no endpoint to fetch a topic's history, only live updates as they arrive; doing this for real would mean authenticating as Jonathan's actual Telegram account instead of the bot, a materially bigger and less secure change. Went with a hybrid of the other two options: a topic becomes a candidate once **Jonathan's own last message** in it (not Charlie's scheduled posts — falls back to the topic's first message if he never replied) is 60+ days old. At that point: ≤20 messages deletes silently (low value); >20 messages gets a warning message with a 7-day grace period, cancelled by simply replying (no separate command needed — a reply is new activity, which pulls the topic out of contention on the next run). New daily scheduled job (`_run_retention_sweep`, 04:00) in `core/scheduler.py`; new `core/tools/retention.py`; new query/warning-tracking functions in `core/history.py` (new `deletion_warnings` table). `data-architecture.md`'s retention row for `charlie.db` updated to match.

Also addresses the recurring-Telegram-topic-deletion concern raised in the same conversation: distilling before deleting a topic in Telegram (existing `/distil` workflow) already clears its DB row immediately, so nothing is ever actually orphaned for someone who distils first — the sweep here is the safety net for topics that just go quiet rather than being deliberately closed out.

Note: this resolves the *retention* (disk/privacy) half of the original problem. The *per-turn API cost* half (a long-lived active topic resending its full history every message, regardless of age) was explicitly scoped out as a separate concern — discussed and deliberately deferred, not part of this fix.

**Tested:** 7 scenarios against synthetic data on the always-on Mac's real venv (small-old-silent-delete, large-old-first-warning, warned-past-grace-delete, warned-within-grace-untouched, warned-then-replied-cancelled, recently-active-untouched, never-replied-falls-back-to-first-message) — all passed. Caught and fixed a real bug during testing: a naive "check for a reply since warned_at" approach was dead code (a reply already removes the topic from the stale-topics query before that check would run) and would have left orphaned warning rows that caused instant deletion with no fresh warning if a topic went stale again after being saved once. Fixed by clearing a topic's warning whenever it's no longer in the stale set. Confirmed against the real `charlie.db`: 0 topics currently qualify (oldest data is 58 days old), so the first scheduled run tonight won't do anything unexpected. Deployed and restarted cleanly on the always-on Mac — `_run_retention_sweep` confirmed registered in the scheduler log.

**Touches:**
`core/history.py`, `core/tools/retention.py` (new), `core/scheduler.py`, `data-architecture.md`.

---

## BUG-002 — No deployment verification step after builds
**Type:** Bug
**Status:** Closed
**Priority:** High
**Severity:** High — causes Charlie to declare things "live" that aren't actually running, destroying trust in deployment announcements
**Blocks anything current:** No — but affects every future build
**Rough effort:** Small
**Logged:** 2026-05-29
**Topic ID:** 1696

**Problem:**
After a Claude Code build, Charlie announces completion without verifying the service is actually running the new code. In the /meta build, the bot process was running a pre-deployment version and hadn't been restarted — Charlie said "Built and live" and invited Jonathan to test it, and it failed immediately. Announcing completion prematurely is worse than saying nothing.

**What needs fixing:**
After any Claude Code build, the workflow should automatically: (1) confirm the service has been restarted and is running the new code, (2) run a basic smoke test or health-check where possible, and (3) only declare something "live" once verified. If verification isn't possible automatically, Charlie should explicitly say "changes are deployed — restart required before testing" rather than "built and live."

**Resolved:** 2026-07-29 — reuses BUG-006's `scope` field: `claude_code.py`'s `_requires_restart()` flags any build whose scope includes non-`.md` files (Python doesn't hot-reload, so a code change on disk doesn't affect the already-running process). `run_claude_code`'s result now says RESTART REQUIRED in that case instead of implying it's live, and Charlie is instructed to tell Jonathan plainly and ask before restarting — never restart automatically, since that's a system-state change (Principle 3) and self-restarting mid-conversation can't reliably report its own result.

New `restart_charlie` tool (gated on an explicit `jonathan_confirmed` field, validated in Python) launches a **detached** script (`core/tools/restart_and_verify.sh`, spawned with `start_new_session=True` so it survives the very process it kills) that: stops/starts `com.charlie`, confirms a new PID is alive, tails `charlie.log` for a clean startup with no errors, optionally re-runs a build's own verify script if one was provided, and reports the result via a direct Telegram API call — deliberately not through the Python app, since that's what's being restarted. Layers are kept honest rather than conflated: a clean process-level check does NOT claim the feature itself was tested — Charlie is told to say so explicitly and ask Jonathan to try it, unless a build-specific verify script was actually re-run.

**Verified for real, 2026-07-29:** (1) isolated proof that a `start_new_session=True` child survives its parent being abruptly killed (simulated the exact `launchctl stop` scenario locally before trusting it); (2) full live run on the always-on Mac — created a temporary Telegram topic, ran `restart_and_verify.sh` against the real `com.charlie` service, confirmed a new PID, a clean log, and a real delivered Telegram message, then deleted the test topic.

**Follow-up, 2026-07-29 (found while discussing BUG-003):** the restart confirmation message is sent via a direct Telegram API call, bypassing `proactive_send` — which meant it wasn't being saved to `charlie.db`, the same failure mode `proactive_send` exists to prevent. Fixed: `send_telegram()` in `restart_and_verify.sh` now also calls `save_message()` directly (via a temp file, not a command-line argument, to survive arbitrary log content safely). Re-verified live on the always-on Mac with a second temporary topic — confirmed the message is now present in `charlie.db` after a real restart.

**Touches:**
`core/tools/claude_code.py` (`_requires_restart`), `core/agent.py` (`restart_charlie` tool + dispatch + `topic_id` param on `handle_turn`), `core/bot.py` (`topic_id` threaded through both call sites), new `core/tools/restart.py` and `core/tools/restart_and_verify.sh`.

---

## BUG-003 — No data architecture document
**Type:** Debt
**Status:** Closed
**Priority:** High
**Severity:** High — without this, every new tool build risks violating Jonathan's data minimalism principle without anyone noticing until after the fact
**Blocks anything current:** No — but should be consulted before any new tool is built
**Rough effort:** Small
**Logged:** 2026-05-29
**Topic ID:** 1698
**Updated:** 2026-06-03

**Problem:**
Jonathan has strong, clearly stated preferences about data minimalism and privacy — only store what's necessary, nothing persisted beyond its purpose, sensitive data never leaving local systems unnecessarily. Despite this, the SQLite persistent history was built and growing without Jonathan knowing. There is no document that records what is stored, where, for how long, and what the deletion policy is. This means data decisions are being made implicitly rather than deliberately.

**Status note:**
`data-architecture.md` was created on 2026-06-03. It documents authorised data stores, retention policies, what leaves the machine, prompt injection protection, and mandatory rules for Claude Code before any data-touching build. The document is active and should be reviewed and updated whenever a new tool is built. The retention policy item (linked to BUG-001) is still unresolved and noted in the document.

**Resolved:** 2026-07-29 — the retention policy item that kept this open is now resolved (BUG-001 landed, `charlie.db`'s row updated with the real 60-day policy). Re-read the full document end to end while closing this out: everything else in it (what's never stored, what leaves the machine per module, future email/calendar rules, prompt injection protection, mandatory pre-build rules) is still accurate — no other stale sections found. Added the missing `deletion_warnings` table (new from BUG-001) to the Authorised Data Stores table for completeness, and bumped the "Last updated" footer.

One thing this did **not** resolve, deliberately split off as its own bug rather than solved here: asking "when is the system actually forced to read this document" surfaced that it isn't — it's not in `CLAUDE.md`'s required-reading list and not loaded into Charlie's system prompt, unlike `principles.md`. Logged separately as **BUG-015**, since it's a distinct enforcement problem (same shape as BUG-006, but for this document) rather than a content gap in the document itself.

**Touches:**
`data-architecture.md` (exists; requires ongoing maintenance as new tools are added). Relevant to all future tool builds.

---

## BUG-004 — Meta review prompt is not documented; /meta scope too narrow
**Type:** Debt
**Status:** Open
**Priority:** Medium
**Severity:** Medium — the prompt is the core of the Meta tool and should be reviewable and improvable, but isn't visible anywhere; and /meta currently only considers charlie.md updates when it should consider all system documents
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-05-29
**Topic ID:** 1702
**Updated:** 2026-06-03

**Problem:**
The /meta tool makes a fresh Claude call with a specific system prompt instructing it to be ruthless, use three categories, and focus on actionable improvements. This prompt is hardcoded in `core/tools/meta.py` but is not documented or visible anywhere outside the code. The prompt itself should be subject to review and iteration — including via the /meta process itself — but can't be meaningfully reviewed if it's buried in source code.

Additionally, /meta's scope is currently limited to evaluating whether `charlie.md` needs updating. It should be broader: after reviewing a conversation, /meta should assess whether any system document needs updating — including `charlie.md`, `principles.md`, `context-archive.md`, and `followups.md` — and surface those recommendations as part of the review output.

**What needs fixing:**
1. Extract the meta review prompt into a standalone readable file — e.g. `core/tools/meta-prompt.md` — and have `meta.py` read from it at runtime. This makes the prompt easy to review, edit, and improve without touching code.
2. Update the prompt to explicitly instruct the reviewer to flag when Charlie states uncertain inferences as facts, and to evaluate whether build announcements were premature.
3. Expand /meta's scope: after reviewing the conversation, /meta should assess all system documents (`charlie.md`, `principles.md`, `context-archive.md`, `followups.md`) and flag any that appear to need updating based on what the conversation revealed. Surface these as a dedicated section in the review output.

**Touches:**
`core/tools/meta.py`, new file: `core/tools/meta-prompt.md`

---

## BUG-005 — No design principles document
**Type:** Debt
**Status:** Closed
**Priority:** High
**Severity:** High — without written principles, architectural decisions are made ad hoc and Jonathan has to re-state his preferences every time rather than them being baked into how Charlie operates
**Blocks anything current:** No — but affects every future build and design decision
**Rough effort:** Small
**Logged:** 2026-05-29
**Resolved:** 2026-06-03

**Problem:**
Several core principles have been clearly established through conversation: (1) hub-and-spokes architecture — Charlie is the agent, tools are discrete spokes, (2) data minimalism — store only what's necessary, nothing persisted beyond its purpose, (3) human approval before action — Charlie never acts autonomously on recommendations without Jonathan's sign-off, (4) lightweight and efficient — no sprawling databases or unnecessary complexity. None of these are written down. They exist only in conversation history. This means they can be violated accidentally on any build, and Charlie cannot reliably consult them.

**Resolution:**
`principles.md` was created on 2026-06-03 with 12 principles covering: hub-and-spokes architecture, data minimalism, human approval, honesty, cost-consciousness, design foresight, explicitness, security, testing, scope discipline, pre-build checklist, and living law. It is loaded into Charlie's system prompt as the first context section (before charlie.md), and is listed as required reading in CLAUDE.md before any Claude Code action.

**Touches:**
`principles.md` (exists; loaded into agent system prompt in `core/agent.py`).

---

## BUG-006 — Pre-build checklist exists but is not enforced
**Type:** Debt
**Status:** Closed
**Priority:** High
**Severity:** High — a checklist that is never auditably verified provides no protection; builds can still violate core principles without anyone noticing
**Blocks anything current:** No — but should be resolved before the next tool is built
**Rough effort:** Small
**Logged:** 2026-05-29
**Topic ID:** 1705
**Reopened:** 2026-06-03

**Problem:**
The pre-build checklist now exists as Principle 11 in `principles.md`. But documentation alone is not enforcement. CLAUDE.md instructs Claude Code to read `principles.md` before any action — but nothing prevents it from reading Principle 11 and silently ignoring it. The checklist is assumed to be consulted; it is never verified.

The original problem (builds violating core principles without detection) remains unsolved. Additionally, Claude Code tasks currently have broad file access by default with no scoping constraints. On 2026-05-29, a diagnostic task focused on debugging /meta silently rewrote bugs.md from scratch, wiping BUG-002 through BUG-005.

**What needs fixing:**
1. Claude Code must explicitly output its answers to the Principle 11 checklist at the start of every build task, before any code is written.
2. Charlie must verify that checklist answers are present and complete before making the `run_claude_code` call.
3. This makes the checklist visible and auditable — not just assumed.

**Why this was incorrectly closed:**
BUG-006 was closed on 2026-06-03 when Principle 11 was added to `principles.md`. That resolved the absence of a written checklist, but not the absence of an enforcement mechanism. The distinction matters: a checklist that Claude Code can read and ignore is not meaningfully different from no checklist. Closing the bug conflated documentation with enforcement.

**Updated:** 2026-07-29 — Principle 11 now also includes a Build Tier classification and a required file/scope declaration (feeds the new Principle 9 scope-diff check), and Principle 9 gained a matching Post-Build Checklist (resolves BUG-011). This makes the checklists more complete, but does not resolve this bug: nothing yet forces Claude Code to output its checklist answers or blocks a build from starting/committing if it hasn't. The enforcement mechanism described below is still unbuilt.

**Resolved:** 2026-07-29 — `run_claude_code`'s tool schema in `core/agent.py` now requires `tier`, `scope`, and `checklist` fields (plus `jonathan_confirmed_risk` for Tier 3); the API enforces these as required at the schema level, so Charlie cannot call the tool without populating them. A new `_validate_claude_code_call()` gate runs before dispatch and rejects incomplete or under-confirmed calls with no extra API cost. Separately, `core/tools/claude_code.py` now checks actual changed files (`git status --porcelain`) against the declared `scope` after each run, before `_auto_commit` — anything outside scope blocks the commit/push entirely and is surfaced instead, which is the direct fix for the failure mode that wiped bugs.md twice (this bug, and again on 2026-07-08 per BUG-011's restore note). Auto-push behaviour is otherwise unchanged for all tiers — a Tier-3 push-confirmation step was considered and deliberately dropped as redundant friction with no new decision-relevant information at that point.

Verified locally: `_validate_claude_code_call` against 6 pass/fail cases, and `_out_of_scope`/`_changed_files` against this session's own real uncommitted changes (correctly passed a matching scope, correctly flagged a narrowed scope, correctly flagged an unrelated scope).

**Deployed and verified on the always-on Mac, 2026-07-29:** pulled onto 10.0.0.119, `com.charlie` restarted cleanly (new PID, scheduler + all 5 jobs registered, no errors in `charlie.log`), and the same 6 validation cases plus scope-diff matching re-run successfully against that machine's own venv. Found and preserved an unrelated uncommitted local distillation (`context-archive.md`, job search/financial context, committed there as its own entry) that predated this pull — it was rebased on top rather than overwritten. That commit is sitting locally on the always-on Mac, one push behind origin, because pushing over a non-interactive SSH session hit a locked login keychain (expected — not something to bypass); it will go out automatically the next time Charlie completes a real build there (which pushes from within its own already-unlocked session), or can be pushed manually next time someone is logged into that Mac directly. **Not yet verified**: an actual live Telegram-triggered build going through this gate end-to-end — that still requires Jonathan to ask Charlie to build something.

**Touches:**
`principles.md` (Principle 11 — Pre-Build Checklist), `core/tools/claude_code.py` (pre-call verification), Charlie system prompt or tool logic (checklist gate)

---

## BUG-007 — No active task list within a session
**Type:** Debt
**Status:** Open
**Priority:** Medium
**Severity:** Medium — without this, open threads and action items get dropped mid-conversation, requiring Jonathan to re-raise them
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-05-29
**Topic ID:** 1708

**Problem:**
During longer conversations, multiple tasks, open questions, and action items accumulate. There is no mechanism for Charlie to track what's been raised but not yet resolved within a session. This leads to items being dropped — for example, the /meta option 2 fix was agreed upon but not built until Jonathan re-raised it later in the conversation.

**What needs fixing:**
Charlie should maintain an internal running list of open items during a session and proactively surface any unresolved items before closing out a conversation. This could be as simple as Charlie noting at natural breakpoints: "Still open from earlier: X, Y, Z — shall we tackle those?" The implementation could be in-context tracking or a lightweight session scratchpad.

**Touches:**
`core/bot.py` or system prompt behaviour. Possibly a session state mechanism.

---

## BUG-008 — Architectural rules should live in their own document
**Type:** Debt
**Status:** Open
**Priority:** Medium
**Severity:** Medium — architectural rules buried in bugs.md are easy to miss and hard to maintain; separation of concerns is a basic hygiene issue
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-05-30
**Topic ID:** 1711

**Problem:**
Architectural rules are currently sitting at the top of `bugs.md` (above the bug list itself). This is the wrong home for them — bugs.md is a tracking document, not an architecture reference. Rules mixed into a bug list are harder to find, easier to overlook, and create confusion about what the document is for.

**What needs fixing:**
Create `architecture.md` in the project root. This document should contain standing rules and architectural constraints that apply to all future development — starting with the Telegram/DB rule currently sitting at the top of `bugs.md`:

> Any message sent directly via the Telegram bot (app.bot.send_message, bot.send_message, etc.) is NOT automatically saved to the messages DB. Any background process, scheduler, or tool that sends Telegram messages must explicitly write those messages to the DB as assistant messages (with the correct topic_id) immediately after sending, so the agent can see them in conversation history when the user replies.

Once `architecture.md` is created and this rule is in it, remove the rule block from the top of `bugs.md` so bugs.md contains only the bug list header and field definitions.

Charlie's system prompt context-loading section (in `core/agent.py` or `core/bot.py`, wherever `charlie.md` and `devlog.md` are loaded) should also load `architecture.md`, so its rules are visible to Charlie in every session without Jonathan having to repeat them.

**Touches:**
New file: `architecture.md` (project root). `bugs.md` (remove the rule block once moved). `core/agent.py` or `core/bot.py` (add architecture.md to context loading).

---

## BUG-009 — Scheduler-created topics should write a pending_context handoff note
**Type:** Debt
**Status:** Open
**Priority:** Medium
**Severity:** Medium — without this, the agent has no structured way to know a topic was scheduler-created or what task it relates to; if DB write order is wrong the context is lost entirely
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-05-30
**Topic ID:** 1759

**Problem:**
When the scheduler creates a Telegram topic and sends an opening message, the main agent has no structured way to know the topic was scheduler-created or what task it relates to. If the DB write is missed or ordered incorrectly, the agent loses context entirely and must infer its purpose from message history order — which is fragile.

**What needs fixing:**
When the scheduler creates a topic and sends its opening message, also write a structured handoff record — either to a `pending_context` table in the DB or as a standardised metadata message — containing: topic_id, task name (e.g. "daily_checkin"), timestamp, and opening message text. The agent should check for this on load so it can recover context explicitly rather than inferring it from message history order.

**Touches:**
`core/scheduler.py`, `core/db.py` (new `pending_context` table or equivalent), `core/agent.py` or `core/bot.py` (check for handoff on topic load).

---

## BUG-010 — Diagnostics / health-check tool missing
**Type:** Debt
**Status:** Open
**Priority:** Low
**Severity:** Low — no active failure, but silent component failures currently go undetected until noticed manually
**Blocks anything current:** No
**Rough effort:** Medium
**Logged:** 2026-06-03
**Topic ID:** 748

**Problem:**
There is no way to quickly check whether all Charlie components are running correctly. If the scheduler silently dies, the bot stops responding, the DB becomes inaccessible, or the agent errors on startup, Jonathan has no visibility until he notices something is missing. Silent failures are the hardest to catch.

**What needs fixing:**
Build a /health or /status command in Telegram that checks all critical components and reports their status: (1) Telegram bot — responsive, (2) APScheduler — running and jobs registered, (3) SQLite DB — accessible and readable, (4) Agent — can instantiate and reach Anthropic API, (5) Key files present — charlie.md, principles.md, context-archive.md, followups.md. Output should be a clear pass/fail per component with any error detail surfaced. Consider also adding an automated silent check on a daily or hourly schedule that alerts Jonathan if anything is down.

**Partial progress (2026-07-31):** The "automated silent check that alerts Jonathan if anything is down" half of this is now partly covered by the new standalone `watchdog.py` + heartbeat mechanism (checks process liveness + a genuine functional heartbeat every 3 min, alerts and attempts one auto-restart on failure — see devlog). Left open because the on-demand `/health` command itself (components 2-5 above) still doesn't exist.

**Touches:**
TBD

---

## BUG-011 — No end-state verification checklist after builds
**Type:** Rule
**Status:** Closed
**Priority:** Medium
**Severity:** Medium — builds get declared complete without confirming they actually work end-to-end from Jonathan's perspective
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-06-10
**Topic ID:** 1086
**Restored:** 2026-07-29 — this entry (along with BUG-012 and BUG-013) was silently deleted by an unrelated commit on 2026-07-08 ("Check all the RSS feeds..."), an unscoped task that shouldn't have touched bugs.md at all. Recovered from git history. BUG-012 and BUG-013 (World Cup topic issues) were reviewed and intentionally not restored — the World Cup module is deprecated, making them moot. This entry was judged still relevant and restored as-is.
**Resolved:** 2026-07-29 — the Post-Build Checklist called for here was added to Principle 9 in `principles.md`, covering all five items plus a scope-diff check and a `/code-review`/`/security-review` gate for Tier 2/3 builds (new Build Tiers section added to Principle 11). This resolves the documentation gap. Note: it still relies on Claude Code actually running the checklist each time — the same enforcement gap BUG-006 describes for the pre-build side applies here too, and isn't fixed by this change alone.

**Problem:**
After a build completes, Charlie declares it "live" or "done" without running through a post-build checklist. Jonathan never gets confirmation that the tool works end-to-end, doesn't know how to monitor it, and doesn't know what failure looks like. The World Cup Tracker was declared live without Jonathan ever seeing a test Telegram notification arrive.

**What needs fixing:**
Add a mandatory post-build checklist to principles.md (Principle 9 or 11) that Charlie must run before closing any build: (1) has the happy path been verified end-to-end, (2) has Jonathan confirmed receipt of any user-facing output, (3) does Jonathan know how to check if it's running, (4) does Jonathan know what failure looks like, (5) has devlog.md been updated. Charlie should not say "we're live" until this checklist is complete.

**Touches:**
TBD

---

## BUG-014 — Claude Code runs with blanket --dangerously-skip-permissions; no mid-build pause for out-of-scope or risky actions
**Type:** Debt
**Status:** Open
**Priority:** Low
**Severity:** Low-Medium — no active failure, but a real residual gap that the BUG-006 scope-diff fix does not close
**Blocks anything current:** No — deliberately deferred, not blocking anything
**Rough effort:** Medium (requires evaluating Claude Code's permission/allowlist options, or a larger SDK-based rearchitecture if the interactive route is ever chosen)
**Logged:** 2026-07-29
**Topic ID:** N/A (raised in a Claude Code planning session, not a Telegram topic)

**Context (why this was logged, in full, so it can be picked up cold):**
This came up while designing the BUG-006 fix (schema-enforced tier/scope/checklist on `run_claude_code`, plus a post-run scope-diff gate in `core/tools/claude_code.py` that blocks auto-commit/push if Claude Code touches files outside its declared scope). Jonathan asked whether Claude Code's inability to pause mid-build and ask a question — e.g., when it discovers it needs to touch a file outside declared scope to do the job properly — was worth fixing with a bigger architectural change (an interactive session instead of a one-shot run).

**Problem:**
`core/tools/claude_code.py` invokes Claude Code as `claude --dangerously-skip-permissions --model ... -p <task>` — a single non-interactive, one-shot subprocess call with a 10-minute timeout. `--dangerously-skip-permissions` disables Claude Code's own built-in tool-approval prompts entirely for the duration of the run. The BUG-006 scope-diff fix bounds *file-level* risk well: any git-tracked file changed outside the declared scope blocks the commit/push and gets surfaced instead of silently landing on GitHub. But the scope-diff check only inspects `git status --porcelain` — it has no visibility into non-file actions taken during the run: network calls, hitting external APIs, deleting something outside the repo, or any other side effect that doesn't show up as a tracked file change. Those actions would normally be caught by Claude Code's own default permission prompts — but the blanket `--dangerously-skip-permissions` flag turns that off, precisely the mechanism that would otherwise ask before doing something risky.

**Decision reached (why this is Open/deferred, not being built now):**
Concluded this does NOT currently warrant a full interactive-pause architecture (e.g., moving off one-shot `-p` mode to a Claude Agent SDK session with a custom `canUseTool` callback that routes approval requests to Telegram and blocks until Jonathan replies). Reasoning: (1) that's a materially bigger project than BUG-006 asked for — a genuine rearchitecture, not a scoped fix; (2) it's disproportionate given Charlie/Claude Code builds are Jonathan-initiated, not adversarial, and low-stakes for a personal project; (3) per Principle 6 (Design with Foresight, Build with Discipline), don't build abstractions before they're needed. If this surfaces as an actual incident (not just a theoretical gap), that would be the trigger to revisit.

**What needs fixing (when this is picked up):**
The cheaper, more proportionate lever, if action is ever wanted: replace the blanket `--dangerously-skip-permissions` flag with a scoped tool-permission allowlist — e.g. auto-allow file edits within `~/charlie/` (so builds stay just as smooth) while still requiring approval for things like arbitrary bash/network calls. Check Claude Code's current CLI flags/settings for a permission-mode or `--allowedTools`-style option that could express this without needing full interactivity. The bigger interactive-pause option remains on the table for later but should be treated as its own separately-scoped build (Tier 2/3, its own Principle 11 checklist) — not bundled into a "quick fix."

**Touches:**
`core/tools/claude_code.py` (the `claude` CLI invocation flags in `run()`). Possibly `core/agent.py` / Telegram plumbing if the interactive route is ever chosen instead.

---

## BUG-015 — data-architecture.md is not actually loaded or required-read anywhere
**Type:** Debt
**Status:** Closed
**Priority:** Medium
**Severity:** Medium — the document calls itself "a mandatory reference" and "Rules for Claude Code (Mandatory)," but nothing outside the document itself points to it, so that mandate only works if the builder already knows to open the file
**Blocks anything current:** No
**Rough effort:** Small-Medium (needs a decision on *how* it should be surfaced, not just where)
**Logged:** 2026-07-29
**Topic ID:** N/A (raised in a Claude Code planning session, while closing out BUG-003)

**Context (why this was logged, in full, so it can be picked up cold):**
Came up while closing BUG-003 ("no data architecture document"). That document (`data-architecture.md`) exists and is comprehensive — but when asked "when is the system forced to read that document?", a direct check found the answer is: never, mechanically. `principles.md`, `charlie.md`, `devlog.md`, and `context-archive.md` are all loaded fresh into Charlie's system prompt every turn (`_build_system_prompt()` in `core/agent.py`). `CLAUDE.md`'s "Required reading before any action" list names `principles.md`, `bugs.md`, and `devlog.md` explicitly for Claude Code sessions. A grep across every `.py` file plus `CLAUDE.md`/`principles.md` for "data-architecture" returned zero hits. The only place that tells anyone to consult it is a paragraph *inside the document itself* ("Rules for Claude Code (Mandatory): before building any feature that involves... check this document") — self-referential, so it only works if the builder already knows the file exists and opens it unprompted.

**Problem:**
This is structurally the same failure mode BUG-006 fixed for the Pre-Build Checklist: a rule that exists on paper with nothing forcing anyone to actually encounter it before it matters. The difference is BUG-006's checklist at least lived inside `principles.md`, which *was* already required reading — `data-architecture.md` isn't required reading anywhere, and isn't loaded into Charlie's context either, so a data-touching build could proceed without anyone (Charlie or Claude Code) ever seeing it.

**What needs deciding (not just fixing) when this is picked up:**
Where should this actually get read? Options worth weighing, not yet decided: (1) add it to `CLAUDE.md`'s required-reading list alongside `principles.md`/`bugs.md`/`devlog.md` — simplest, but only covers Claude Code sessions, not Charlie's own live conversations where a data decision could also get made; (2) load it into Charlie's system prompt like `principles.md` — covers both, but grows the system prompt (cost-conscious tradeoff, Principle 5); (3) fold its content directly into `principles.md` as a new principle, retiring it as a standalone file — reduces the "which document governs this" ambiguity but is a bigger structural change than it sounds, since `principles.md` is meant to stay lean and this document is fairly long. Whichever route, the Build Tier system (Principle 11) already asks "what data will be stored, and why?" for every build — worth cross-referencing rather than solving twice.

**Resolved:** 2026-07-29 — went with option (1). There are actually two separate "required reading" tracks in this system, not one: Charlie's live system prompt (`principles.md`, `charlie.md`, `devlog.md`, `context-archive.md`, reloaded every turn per `_build_system_prompt()`) versus Claude Code's build-time required reading (`CLAUDE.md`'s list — `principles.md`, `bugs.md`, `devlog.md`). Confirmed `bugs.md` is *never* loaded into Charlie's system prompt (no path constant, no `.read_text()` call for it anywhere in `agent.py`) — it's build-time-only, and `data-architecture.md`'s content has the same profile: decision-relevant only when something data-touching is being built, not during ordinary conversation. Added it to `CLAUDE.md`'s required-reading list as a fourth file, alongside `bugs.md`. Also added a one-line pointer from `principles.md`'s Pre-Build Checklist (the "what data will be stored" question) to `data-architecture.md`, so it's surfaced twice, not just once.

**Accepted tradeoff:** this means Charlie's own live conversations don't have this document loaded — if Jonathan asks Charlie directly what's stored and for how long, Charlie won't have it memorized and would need to say so rather than answer from context. Deliberate: loading it into the system prompt like `principles.md` would mean paying its (growing) token cost on every single turn, for content that's only actually relevant a few times a week when something's being built. Option (2) or (3) remain available later if this tradeoff ever stops being the right one.

**Touches:**
`CLAUDE.md` and/or `core/agent.py` (`_build_system_prompt()`) and/or `principles.md`, depending on which option is chosen.

---

## BUG-016 — Telegram topic-existence check duplicated instead of shared
**Type:** Debt
**Status:** Closed
**Priority:** Low
**Severity:** Low — cosmetic duplication, not a functional bug
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-07-30
**Resolved:** 2026-07-31 — closed as moot rather than built. While fixing [[BUG-017]] for both tools, the two mechanisms ended up diverging for good structural reasons, not just accidental duplication: the email tool no longer probes existence at all — it self-heals reactively on an actual failed send, since it has a regular 2-minute poll cadence to hook into. Bug topics have no equivalent regular send channel, so a proactive nightly probe (now via `edit_forum_topic`, see BUG-017) is still the right shape there. A shared helper would now have to abstract over two genuinely different strategies, not just one duplicated technique — not worth building for that.

**Problem:**
`core/tools/bugs.py`'s `_topic_exists()` (probes via `unpin_all_forum_topic_messages`, treats a "thread" `BadRequest` as deletion, fails safe on anything unexpected) is the only existing implementation of "does this Telegram forum topic still exist." The new email monitor tool (`core/tools/email/`) needs the same check for its own persistent Email topic, but importing `bugs.py`'s private helper would be a tool-calling-tool dependency (Principle 1, Hub-and-Spokes). Building the email tool's own build scope didn't include touching `bugs.py`, so the ~15-line technique was duplicated locally in `core/tools/email/__init__.py::reconcile_email_topic` instead.

**What needs fixing:**
Extract a shared `core/telegram_utils.py` (or similar) with a single `topic_exists(bot, group_id, topic_id)` helper, and have both `bugs.py` and `core/tools/email/` call it. Not urgent — the duplication is small and low-risk, but will grow if a third topic-owning tool needs the same check later.

**Touches:**
`core/telegram_utils.py` (new), `core/tools/bugs.py`, `core/tools/email/__init__.py`

---

## BUG-017 — Topic-existence check can never actually detect a deleted topic (missing bot permission)
**Type:** Bug
**Status:** Closed (both email and bugs.py instances)
**Priority:** Low
**Severity:** Low — fails safe (assumes the topic still exists), so the worst case is a deleted topic doesn't get auto-recreated; no data loss or incorrect risky assumption
**Blocks anything current:** No
**Rough effort:** Small (Telegram admin settings change) to Medium (if a different probe technique is wanted instead)
**Logged:** 2026-07-30
**Resolved (email):** 2026-07-31 — the email topic no longer depends on this broken probe at all. `core/tools/email/__init__.py`'s `poll_and_notify`/`send_alert` now go through a new `_send_to_email_topic` helper that catches a `BadRequest` from an actually-deleted topic directly (on the real send call, not a separate permission-gated probe) and recreates it immediately, within one poll cycle (≤2 min) instead of waiting up to a day for a check that never worked. The daily `reconcile_email_topic` job and function have been removed entirely as redundant. This resolves the bug by making the broken check unnecessary rather than fixing it.
**Resolved (bugs.py):** 2026-07-31 — found while investigating Jonathan's report that almost none of his 21 logged bugs had a live Telegram topic. `core/tools/bugs.py`'s `_topic_exists()` used the identical `unpin_all_forum_topic_messages` probe and hit the same permission wall, always falling into the fail-safe "assume exists" branch. Bug topics don't have a regular send cadence to hook a reactive self-heal into (unlike email's 2-minute poll), so this needed an actual working existence check rather than email's reactive approach. Replaced with `edit_forum_topic(name=<the bug's own current topic name>)` — verified live against both a real existing topic and a fake one before relying on it: Telegram returns a specific `Topic_not_modified` error when the topic exists and the name already matches (a true no-op, zero visible side effect) versus `Topic_id_invalid` when it's gone, cleanly distinguishing the two without needing the missing permission. (Tested and rejected passing no `name` at all — Telegram/python-telegram-bot treats a fully empty edit as a no-op *without* validating the topic_id, so it silently "succeeds" even against a nonexistent one.) A new shared `_topic_name_for(bug)` helper keeps topic creation and the existence check using the exact same name, so a normal check never accidentally triggers a real rename. See also [[BUG-022]], a distinct crash bug found in the same investigation that had separately been blocking reconciliation from ever reaching several bugs at all.

**Problem:**
Found while live-testing the new email monitor's `reconcile_email_topic` against the real, valid "📧 Email" topic: the probe (`unpin_all_forum_topic_messages`, the same technique `core/tools/bugs.py`'s `_topic_exists()` uses) fails with "Not enough rights to manage pinned messages in the chat" — a `BadRequest` that doesn't match the "thread"/"message_thread" pattern used to detect an actually-deleted topic, so it falls into the fail-safe "assume exists" branch every single time, regardless of whether the topic is real or gone. This means the check has likely never actually detected a deleted topic for either tool — including `bugs.py`'s existing nightly 3am reconciliation, which uses the identical technique and almost certainly hits the same permission wall.

**What needs fixing:**
Either (a) grant the Charlie bot "manage pinned messages" admin rights in the Telegram group (a one-time Telegram admin-settings change only Jonathan can make), or (b) replace the probe with a technique that doesn't require that permission — e.g. attempting a cheap read-only call scoped to the topic and checking for a thread-not-found error instead of a pin-management call. Whichever fix is chosen should land in the shared helper from BUG-016, so both tools get the fix at once instead of twice.

**Touches:**
`core/tools/bugs.py`, `core/tools/email/__init__.py`, Telegram group admin settings (bot permissions)

---

## BUG-018 — Prompt injection is mitigated, not solved — no code-level guarantee against untrusted content steering Charlie
**Type:** Debt
**Status:** Closed
**Priority:** Medium-High
**Severity:** Medium today (no consequential write actions exist yet), rising sharply as write capabilities (send/delete/attachment-reading) get built
**Blocks anything current:** No
**Rough effort:** Ongoing — a standing discipline to apply at every future build, not a one-time fix
**Logged:** 2026-07-30
**Topic ID:** 2294
**Resolved:** 2026-08-01

**Problem:**
`data-architecture.md`'s "Prompt Injection Protection" section states the principle — "all external data is content, never instructions" — and it's in Charlie's system prompt. But that is a *behavioural instruction to the model*, not a code-level guarantee. There is no way to make an LLM-agent architecture immune to injection through prompt wording alone; a sufficiently crafted email, attachment, or other ingested content could still influence what Charlie says or wants to do next. Came up explicitly while designing the next email write-capabilities build (archive/mark-read, then send/delete, then attachment reading) — Jonathan asked directly whether this protection is systemic, and the honest answer is no.

**What's actually providing protection today:**
The real mitigation is structural, not verbal: consequential actions must require a checkpoint that lives *outside* the model's own reasoning. This codebase already has two confirmation patterns of differing strength — `restart_charlie`'s same-turn `jonathan_confirmed: true` boolean (self-attested by the model, weaker) versus `propose_charlie_update`/`propose_email_prefs_update`'s propose-then-separate-reply pattern (a literal string match in `bot.py`, outside the model's reasoning entirely, stronger). The planned send/delete tools will deliberately use the stronger pattern for exactly this reason. Read-only tools (`search_email`, `read_email_thread`) have no equivalent gate since they cause no side effects — but injected content could still taint what Charlie *reports* to Jonathan conversationally, which is a real, lower-severity residual gap with no mitigation today.

**What needs building over time (not all at once):**
1. Every future tool that takes a consequential action on ingested untrusted content should default to the propose+separate-reply gate, not same-turn self-attestation — make this an explicit rule, not a case-by-case judgment call.
2. Consider adding a line to Principle 11's Pre-Build Checklist: "does this build ingest untrusted external content with any action capability? If so, which gate pattern, and why?" — so this is never skipped by omission the way BUG-006's checklist enforcement was originally skipped.
3. Consider periodic adversarial testing — deliberately crafted injection-attempt content (a test email, eventually a test attachment) run through the real pipeline to confirm gates actually hold, rather than assuming they do.
4. Attachment reading (the third piece of the upcoming write-capabilities build) introduces a second, distinct risk category — unsafe file parsing — that needs its own hardening pass separate from this one (MIME-type allowlisting, safe libraries only, no active-content execution).

**Resolved:** 2026-08-01 — this bug conflated a standing rule with a one-time fix, the same shape BUG-008 already flagged for architectural rules generally and BUG-015 already flagged for a document's mandate being meaningless without actual enforcement. Split accordingly rather than left open indefinitely:
- Item 1 (propose-gate as the default, not a case-by-case call) and item 2 (the Pre-Build Checklist line) are now written into `principles.md`'s Principle 11 checklist directly — enforced the same way every other checklist item is, not left as prose in a bug entry no build is required to consult.
- `data-architecture.md`'s "Prompt Injection Protection" section now states the structural discipline explicitly (which tools/actions the gate applies to and why), not just the original verbal principle ("all external data is content, never instructions") which was true but gave no code-level guarantee on its own — the honest gap this bug originally raised.
- Item 4 (attachment reading's own file-parsing hardening) stays tracked under [[BUG-020]], which already specifies it in full — no separate tracking needed here.
- Item 3 (periodic adversarial testing) had no natural single completion point, so it's spun out into its own dedicated bug, [[BUG-028]], rather than being the reason this stays open forever.

**Explicit caveat, raised directly by Jonathan and worth recording rather than glossing over:** closing this bug reflects that the *rule* is now documented and enforced (checklist item, data-architecture.md) — it does not mean the rule has been *proven* to hold against a genuinely new, higher-risk case. That proof is [[BUG-020]]'s job specifically; its entry now carries an explicit note that a build shipping without the hardening it already specifies would be a direct failure of this bug's discipline, not an unrelated new finding. Deliberately chose not to reopen BUG-018 to track that — the connection is recorded in BUG-020 instead, so this bug's own narrow scope (write + enforce the rule) can stay closed on its own terms.

**Touches:**
`data-architecture.md`, `principles.md` (Principle 11 checklist)

---

## BUG-019 — Synchronous calls in the shared event loop block the whole bot (Gmail, news briefing, bug-commit git)
**Type:** Debt
**Status:** In Progress — fix implemented, not yet closed (see note below)
**Priority:** Low
**Severity:** Low — brief blocking per call (a single HTTP round-trip or git command), not a hang; noticed while building send/delete, applies retroactively to every affected tool, not something newly introduced
**Blocks anything current:** No
**Rough effort:** Small-Medium (wrap each call site in `asyncio.to_thread`, matching the pattern `core/tools/email/__init__.py::poll_and_notify` and `core/scheduler.py`'s `run_in_executor` calls already use)
**Logged:** 2026-07-30
**Topic ID:** 2297

**Problem:**
`search_email`, `read_email_thread`, `archive_email`, `mark_email_read`, `mark_email_unread` (all dispatched inside `handle_turn()`'s tool loop) and now the send/delete execution in `bot.py`'s `on_message()` all call synchronous `googleapiclient` methods directly from async functions, with no `asyncio.to_thread`/executor wrapping — unlike the background poller (`poll_and_notify`), which deliberately wraps its sync Gmail+Haiku pipeline this way specifically to avoid blocking the bot's event loop. Every Gmail-calling tool in the interactive conversation path currently blocks the whole bot (all topics, not just the one making the call) for the duration of that HTTP round-trip.

**Update (2026-08-01, BUG-023):** two more unwrapped call sites landed with the reply/reply-all/forward build — `forward_email()`/`get_forward_preview()`'s dispatch in `bot.py`'s `on_message()` (`"forward it"` handler), plus `resolve_send_recipients()`'s thread lookup called from `_send_email_proposal_text()`/`_forward_email_proposal_text()` at proposal-preview time. Forward's attachment downloads are a meaningfully larger payload than any existing call site here, so this debt item is more consequential than it was before, not just wider. Still not fixed in this build — deliberately out of scope, tracked here rather than accreting silently.

**Update (2026-08-05):** two more unwrapped call sites added incidentally by the BUG-029/BUG-030 fixes — `trash_thread()` and `get_thread_summary()` are now each called in a per-thread loop (batch delete), same unwrapped shape as before, just repeated per item.

Also, a broader architecture question from Jonathan ("is Charlie generally orchestrator-with-worker-streams?") turned up two more instances of the exact same root problem, outside email entirely, via a full-codebase survey:
- **News briefing:** `generate_briefing()` (`core/tools/news.py`) does synchronous `feedparser.parse()` across up to 9 RSS sources plus a synchronous `anthropic.Anthropic().messages.create()` call — called unwrapped from *both* `scheduler.py`'s `_send_news_briefing` job and the live tool-dispatch loop's `tool_get_news_briefing()` (`agent.py`, no `await`/`to_thread`). Unlike the Gmail case, this one also blocks a *scheduled* job, not just an interactive turn.
- **Bug-tracking git commits:** `_commit_bugs_md()` (`core/tools/bugs.py`) runs `subprocess.run(["git", ...], timeout=10/30)` synchronously on every `log_bug`/`resolve_bug` call, both invoked directly from `agent.py`'s dispatch loop. A slow `git push` blocks the whole bot for up to 30 seconds.

The survey also confirmed the *intended* pattern (orchestrator delegates, stays responsive) is correctly followed elsewhere: Claude Code builds (`asyncio.create_subprocess_exec` + `await`), self-restart (detached subprocess), the council/judge-panel tool (`asyncio.gather`), the grants and Larica pipelines (`run_in_executor`), World Cup data (async httpx), and distillation (`AsyncAnthropic`). These three — Gmail, news briefing, bug-commit git — are the exceptions, not the rule. See [[BUG-034]] for a related but distinct finding from the same survey (Telegram's `concurrent_updates` is off, serializing all conversations regardless of this bug's fix).

**Fix implemented, 2026-08-05 (not yet closed):** wrapped all 14 direct blocking call sites in `asyncio.to_thread(...)` — `agent.py`'s search_email/read_email_thread/archive_email/mark_email_read/mark_email_unread/log_bug/resolve_bug/get_news_briefing dispatch branches; `bot.py`'s send_email/trash_thread/forward_email execution plus all six proposal-preview-helper call sites; `scheduler.py`'s `_send_news_briefing` job. Empirically verified with a real Gmail call and a fake concurrent coroutine: unwrapped, the other coroutine's first tick was delayed ~1s (the whole bot frozen for that long); wrapped, it ticked on schedule throughout a 2.5s call with no gap.

This surfaced two further rounds of real bugs via `/code-review`, both worth recording in full since they're a good example of a fix at this scale needing more than one pass:

1. **Round 1 review** found that `asyncio.to_thread` removed an *accidental* protection: since everything used to run serially on the single event loop thread, `bugs.py`'s read-modify-write of `bugs.md` (`create_bug_entry`/`close_bug`/`set_bug_topic_id`/`clear_bug_topic_id`/`reopen_bug`, each read-then-write-then-git-commit) and `news.py`'s `generate_briefing()` (read-unshown-articles-then-mark-shown) could never actually overlap — now, running on real OS threads, they genuinely could, risking duplicate `BUG-NNN` ids / lost writes / a `git commit` `index.lock` collision, or duplicate news delivered twice across two Telegram topics. **Fixed** by adding a module-level `threading.Lock()` in each file (`_BUGS_LOCK`, `_BRIEFING_LOCK`) around the full read-modify-write-commit critical section. Verified empirically against a scratch git repo: 12 concurrent `create_bug_entry` calls produced 12 unique ids with the lock versus only 2 unique ids and 3 surviving entries (9 lost) without it; the news lock was shown to fully serialize 4 concurrent 0.3s sections with zero overlap ever detected.

2. **Round 2 review** found the fix for round 1 introduced a *new* instance of this exact bug, one level down: `create_bug_topic()` and `reconcile_bug_topics()` (both `async def`, awaited directly on the event loop from `agent.py`'s `log_bug` dispatch, `bot.py`'s `/createbugtopics` handler, and `scheduler.py`'s nightly reconciliation job) called the now lock-guarded `set_bug_topic_id`/`clear_bug_topic_id` *synchronously* — so the event loop itself could block waiting on `_BUGS_LOCK.acquire()` for as long as a worker thread held it through a slow `git push`, freezing every Telegram topic, the exact failure this whole bug is about, reintroduced through lock contention instead of a raw Gmail call. **Fixed** by wrapping those two call sites in `asyncio.to_thread` too. Verified empirically: a background thread held `_BUGS_LOCK` for 1s while a concurrent coroutine ticked every 50ms — unwrapped, the first tick was delayed the full ~0.9s; wrapped, ticks stayed on schedule throughout. Round 2 also flagged `news.py`'s lock scope as broader than necessary (covering the RSS fetch, not just the shown/unshown span) — moved outside the lock as a latency cleanup.

3. **Round 3 review** (checking the round 2 delta) found that latency "cleanup" was itself a correctness regression: with the fetch unlocked, a second concurrent `generate_briefing()` call's lock turn could consume the very articles the first call just fetched before the first call reached `_get_unshown_articles()` — the first call (which may be the one that actually fetched the news, e.g. the scheduled briefing) would then falsely report "No new articles," a fetch result silently stolen rather than duplicated. **Reverted** — `init_db()`/`_fetch_all_feeds()` moved back inside `_BRIEFING_LOCK`, accepting the minor added latency in the rare case two briefing calls overlap as the correct trade against actually losing news content. Re-verified the lock still fully serializes after the revert (4×0.3s sections, ~1.2s elapsed, zero overlap).

**Deliberately left open:** three rounds of review and fixes on mocked/scratch data is still not the same as proof it works live, per this log's own standard (see BUG-030). Needs deployment to the always-on Mac and real usage — ideally overlapping a live email/bug/news action with the scheduled noon news briefing or nightly bug reconciliation, since that's exactly the condition all three rounds of races depended on — before this is marked Closed.

**Touches:**
`core/agent.py` (tool dispatch branches: search_email, read_email_thread, archive_email, mark_email_read, mark_email_unread, log_bug, resolve_bug, get_news_briefing), `core/bot.py` (send/delete/forward execution blocks, proposal-preview helpers), `core/tools/news.py` (`generate_briefing`, new `_BRIEFING_LOCK`), `core/tools/bugs.py` (`create_bug_entry`, `close_bug`, `set_bug_topic_id`, `clear_bug_topic_id`, `reopen_bug`, `create_bug_topic`, `reconcile_bug_topics`, new `_BUGS_LOCK`), `core/scheduler.py` (`_send_news_briefing`)

---

## BUG-020 — Attachment reading not yet built
**Type:** Debt
**Status:** Closed (2026-08-01)
**Priority:** Medium
**Severity:** N/A — capability gap, not a defect. Third piece of the agreed email write-capability roadmap (notify+suggest → search/read+preferences → archive/mark-read → send/delete → attachment reading), the only piece not yet started.
**Blocks anything current:** No
**Rough effort:** Medium — needs its own security research pass, not just a straightforward tool addition
**Logged:** 2026-07-30
**Topic ID:** 2300

**Problem:**
Charlie can search, read, send, and delete email, but has no way to open or read an email attachment (PDF, DOCX, images, etc.) even when Jonathan explicitly asks. This is the one piece of the originally-agreed write-capability roadmap still outstanding.

**What needs fixing:**
Needs its own hardening pass distinct from the email-body work already done, since parsing arbitrary file formats is a different risk category than reading email text:
- Explicit MIME-type allowlist checked before download — refuse anything executable, macro-enabled (`.docm`/`.xlsm`), or archive-type (could nest further threats), before spending any bytes on it.
- Well-maintained, memory-safe parsing libraries only for allowed types (e.g. `pypdf`/`pdfplumber` for PDF, `python-docx` for Word) — no shelling out to external converters, no code path that could execute embedded/active content.
- Extracted text treated with the same "content, not instructions" discipline already applied to email bodies (see [[BUG-018]]) — no special exemption just because it came from a file instead of a message body.
- A length cap on extracted text, matching `read_email_thread`'s existing `max_chars` pattern, to bound token cost on a large document.
- Only on-request, same trust tier as `search_email`/`read_email_thread` — no autonomous path.

**This build is the actual proof of [[BUG-018]]'s rule, not just a build that should mention it.** BUG-018 was closed (2026-08-01) on the strength of the propose-gate discipline now being a documented, enforced Pre-Build Checklist item — but that rule has so far only ever been applied to cases it was written *from* (email send/forward/delete), not tested against a genuinely new risk category. Attachment reading is that test. If this build ships without the MIME allowlist, without memory-safe parsing libraries, or without treating extracted text as content-not-instructions, that is a direct failure of BUG-018's discipline, not just a shortcoming of this bug alone — flag it as such explicitly if it happens, rather than letting it surface as an apparently-unrelated new finding. The Pre-Build Checklist question this build must actually answer (from `principles.md`, added when BUG-018 closed): "Does this build ingest untrusted external content with any action capability? If so, which gate pattern is used, and why?"

**Touches:**
`core/tools/email/fetch.py` (new function(s)), `core/agent.py` (new tool), `requirements.txt` (new parsing dependencies), `data-architecture.md`

**Resolution (2026-08-01):** Built `read_email_attachment` (`core/tools/email/fetch.py`) — PDF/DOCX/plain-text only, no propose-gate needed (read-only, same trust tier as `search_email`/`read_email_thread`). Went through 7 rounds of independent Plan-agent design review before implementation (matching BUG-026/027's precedent), each round catching a real, concrete gap in the previous one's fix:
1. Two must-fix gaps: MIME+extension alone is spoofable (added magic-byte checking as a third required signal); `pypdf`/`python-docx` have unaddressed DoS classes (PDF hang, DOCX zip-bomb/entity-expansion).
2. The zip-bomb pre-scan's original design (trusting `ZipInfo.file_size`) was unsound — `zipfile` doesn't validate that field until after full decompression. Fixed via bounded chunked reads.
3. The entity/DOCTYPE literal scan riding on those chunked reads could miss a token split across a chunk boundary. Fixed via a 16-byte overlap tail.
4. The byte-tally was scoped to XML-named members only, but `python-docx`'s real loader (`PackageReader._walk_phys_parts`) unboundedly reads every relationship-reachable part regardless of name. Fixed by scoping to the whole archive.
5. The entity/DOCTYPE scan had the same name-scoping flaw. Investigating the fix surfaced that `python-docx==1.2.0` already constructs its lxml parsers with `resolve_entities=False` (verified directly against the installed library's source) — closing entity-expansion by default. The hand-rolled scan was removed entirely rather than further patched, since keeping it produced real false positives on legitimate SVG/altChunk content for a risk the library already closes.
6. Final full-plan re-review confirmed convergence with no further findings.

Then a Tier-3 `/code-review` + `/security-review` pass on the actual implementation. Security review (independently verified XXE/SSRF via a local HTTP listener test — zero requests received; zip-slip; filename/path-injection) found no HIGH/MEDIUM findings. Code review found 9 issues, all addressed same-session: a depth-cap regression silently narrowing the pre-existing `forward_email` path (fixed — now logs a warning on truncation); hardcoded UTF-8 decoding for plain-text attachments (fixed — BOM detection + cp1252 fallback); ambiguous tool docs causing wrong-message targeting in multi-message threads (fixed — explicit gmail_message_id guidance); a single bad PDF page discarding all other pages' text (fixed — per-page isolation); a misleading "scanned/image-based" message for non-PDF empty-text cases (fixed — type-aware wording); duplicated timeout-thread logic vs `bot.py`'s `_wait_for_network` (fixed — extracted to shared `core/utils.py`); and this doc/devlog gap (fixed here). One finding (memory not reclaimed on an abandoned hung-parse thread) was disclosed in code rather than fixed — would need process-based isolation, out of this build's scope.

Verified end-to-end against the real mailbox (a genuine Regus invoice PDF attachment), plus `get_forward_preview`/`forward_email` re-tested post-refactor to confirm no regression on the existing propose-gated path. Full design detail, residual-gap disclosures, and the three-signal/zip-bomb/timeout mechanisms are documented in `data-architecture.md`'s new "Read capability: attachment reading" entry.

This is the actual, proven test of [[BUG-018]]'s discipline referenced above — shipped with the MIME+extension+magic-byte allowlist, memory-safe parsing libraries, content-not-instructions framing, and a propose-gate-exemption reasoning that was itself reviewed, not assumed.

---

## BUG-021 — No control over outgoing email formatting (fonts/sizing) or a signature
**Type:** Debt
**Status:** Open
**Priority:** Low
**Severity:** N/A — feature gap, not a defect
**Blocks anything current:** No
**Rough effort:** Small-Medium for formatting alone; Medium if an embedded signature image is wanted (real MIME complexity)
**Logged:** 2026-07-30
**Topic ID:** 2303

**Problem:**
Jonathan asked whether he can set default fonts/sizing for outgoing email and add a signature (possibly with an image). `send_email()` currently sends plain text only (`MIMEText`, `Content-Type: text/plain`) — plain text has no font/size concept at all; the recipient's client renders it however it wants regardless of anything Charlie specifies. Getting any formatting control requires switching to HTML email.

**What needs deciding (not yet decided) before this can be built:**
1. Jonathan's actual font/size preferences — not yet gathered.
2. Signature image approach, if any — a real tradeoff, not yet chosen: **embedded inline** (attached as part of the email, referenced via `cid:` — always displays for the recipient, but needs a real multipart-MIME rework of `send_email()`) versus **hosted external image** (a link — simpler to build, but many email clients block external images by default until the recipient clicks "show images," so it may not display automatically) versus **text-only signature, no image**.
3. Given inline CSS is required for reliable rendering (most email clients strip `<style>` blocks), `send_email()` would need to build a proper HTML body (likely `multipart/alternative` with both a plain-text and HTML part, for compatibility with clients that don't render HTML) rather than the current single `MIMEText`.

**What needs fixing (once the above is decided):**
Rework `send_email()` to construct a multipart message with inline-styled HTML (font-family/font-size), append a signature block (text, or an embedded/hosted image per the decision above), while keeping the existing header-injection guard and reply-threading logic intact.

**Touches:**
`core/tools/email/fetch.py` (`send_email()`), possibly a new small config file/mechanism for storing font/size/signature preferences (separate from `email-preferences.md`, which is freeform triage-behavior prose, not a natural fit for structural settings like these)

---

## BUG-022 — reconcile_bug_topics crashed nightly on a non-numeric Topic ID, silently blocking every later bug from getting a topic
**Type:** Bug
**Status:** Closed
**Priority:** High
**Severity:** High — silently broke bug-topic creation entirely for 2+ days with no visible error to Jonathan; found while investigating his report that "almost none" of 21 bugs have an open Telegram topic
**Blocks anything current:** No (fixed same-session as found)
**Rough effort:** Small
**Logged:** 2026-07-31
**Resolved:** 2026-07-31

**Problem:**
When BUG-014 was logged (2026-07-29) its `Topic ID` field was set to the literal string `N/A (raised in a Claude Code planning session, not a Telegram topic)` — a deliberate marker meaning "this bug wasn't raised via Telegram, so it has no topic," not "topic ID unknown." `core/tools/bugs.py`'s `reconcile_bug_topics()` treats any non-empty `topic_id` field as a real ID to verify and calls `int(raw_topic_id)` on it directly, uncaught. Confirmed via `charlie.log`: every nightly run since — 2026-07-30 03:00:01 and 2026-07-31 03:00:00 — crashed at that line with `invalid literal for int() with base 10: 'N/A (...)'`, caught only by the outer wrapper in `core/scheduler.py`'s `_reconcile_bug_topics` (which just logs the failure), meaning the function aborted every night partway through the bug list in file order — before ever reaching BUG-016, 018, 019, 020, or 021 (all Open, all logged after BUG-014, all with no topic_id at all). That's the real reason those 5 bugs never got a Telegram topic, distinct from (though compounding) [[BUG-017]].

**Fix:** `reconcile_bug_topics()` now explicitly recognizes a `Topic ID` value starting with `N/A` as "deliberately topic-less" and skips it entirely (no create, no verify) — rather than either crashing on it or misinterpreting it as "missing, go create one." Also wrapped the remaining `int(raw_topic_id)` parse in a try/except so any future malformed value degrades to a per-bug warning-and-skip instead of aborting reconciliation for every bug still to come.

**Verified:** Ran the fixed function locally against a mock bot and the real `bugs.md` — completed without crashing, correctly skipped BUG-014/015, correctly reached and attempted topic creation for BUG-016/018/019/020/021 (blocked only by `core/state` not being initialized outside the real bot process — a test-harness limitation, not a code issue), and correctly left already-existing topics (BUG-007/008/009/010, simulated as still live) untouched.

**Found by code review, fixed same session:** `create_bug_topic()` used to create the real Telegram topic, send its opening message, and only *then* persist the topic_id to `bugs.md` — a transient failure sending that message would leave a live, orphaned Telegram topic with no record in `bugs.md`, and the next reconciliation run would see "no topic_id" and create a genuine duplicate. This was always possible in principle, but the old broken `_topic_exists` check almost never actually reached the "recreate" path (it fail-safed to "assume exists"), so it rarely mattered in practice — fixing BUG-017 for bugs.py makes that path fire for real for the first time, so this gap needed closing alongside it, not left for later. Fixed by moving `set_bug_topic_id()` to immediately after topic creation, before the opening-message send.

**Touches:**
`core/tools/bugs.py` (`reconcile_bug_topics`)

---

## BUG-023 — Charlie can't forward emails (no true forward capability)
**Type:** Debt
**Status:** Closed
**Priority:** Medium
**Severity:** Medium — workaround is composing a new email, but attachments and original headers are lost
**Blocks anything current:** No
**Rough effort:** Medium
**Logged:** 2026-07-31
**Topic ID:** 2351
**Resolved:** 2026-08-01

**Problem:**
Charlie can only compose new emails. There's no way to forward an existing email with its original headers, inline content, and attachments preserved — which is the standard way Jonathan routes receipts and invoices to finance@ts.org.

**What needs fixing:**
Add a forward_email tool that uses the Gmail API to forward an existing thread/message to a specified address, preserving original headers and attachments. Needs the same propose-then-confirm gate as send_email.

**Resolved:** 2026-08-01 — while scoping this, Jonathan also asked for the fuller set of missing mail-manipulation abilities: CC/BCC, reply (auto-addressed to the original sender), and reply-all. Built all of it together as one Tier-3 change, all riding the existing propose-then-distinct-reply-phrase gate (no new confirmation pattern needed):

- `send_email()` (`core/tools/email/fetch.py`) extended: `to` is now optional when `thread_id` is given (omitting it replies to the original sender); new `cc`/`bcc`/`reply_all` params. Reply-all merges the original message's To+Cc into `cc`, minus Jonathan's own address and the resolved recipient — self-exclusion applies only to that auto-derived portion, so an explicit "cc me" survives. Addresses are parsed via `email.utils.getaddresses` (handles multi-address/quoted-name lists correctly) and deduplicated by lowercased address after merging auto-derived and explicit recipients.
- New `resolve_send_recipients()` is the single implementation of this derivation — `send_email()` calls it at actual-send time, and `bot.py`'s proposal-text builder calls the identical function to render the preview Jonathan confirms. This was a real gap in the initial design (a draft plan had the preview and the actual send each computing recipients independently, which risked exactly the preview/action divergence this whole gate exists to prevent) — caught and fixed before implementation, not after.
- `get_thread_summary()`'s fetched headers extended to include `To`/`Cc` (needed for reply-all's derivation).
- Header-injection guard (rejects `\r`/`\n`) extended from `to`/`subject` to `cc`, `bcc`, and the auto-filled reply subject (a pre-existing latent gap — the reply subject comes from the original message, previously unchecked).
- New `forward_email()`/`get_forward_preview()`: forwards a thread's most recent message, including attachments (fetched via `messages().attachments().get()`, byte-identical to the original), as a brand-new message (no `threadId`/`References` — the new recipient isn't part of the original conversation). Rejects control characters in to/cc/bcc/subject and in each attachment filename (a new risk: filenames come from the *original* sender, reachable via arbitrary inbound mail). Total attachment size capped at 20MB (`MAX_FORWARD_ATTACHMENT_BYTES`), checked from metadata before downloading anything; if any single attachment fails to fetch, the whole forward fails rather than sending with one silently missing.
- `propose_send_email` schema extended (optional `to`, new `cc`/`bcc`/`reply_all`); its dispatch in `agent.py` now validates up front (neither `to` nor `thread_id` present → clean tool-level error, no pending proposal built) rather than only failing at send time with a broken preview.
- New `propose_forward_email` tool + a new `PENDING_FORWARD_EMAIL` gate in `bot.py` (`"forward it"`/`"cancel"`), mirroring send/delete exactly — wired into `_other_pending_note`, both proposal-dispatch sites (`_run_charlie_turn` and `on_meta_command`), and a new `_forward_email_proposal_text()` builder (sender/subject/message-count/attachments/cc/bcc/note).
- **Critical fix:** the existing "send it" dispatch in `bot.py` (`on_message()`) previously called `send_email()` without `cc`/`bcc`/`reply_all` — extending the schema without fixing this exact call would have meant Jonathan approving a preview with real CC/reply-all recipients while the actual send silently dropped them. Fixed to pass all three through from the pending proposal.
- Long reply-all previews (a large derived Cc list) are chunked via a new `send_and_save_chunked()` helper, matching the existing `_send_chunks`/`_chunks`/`on_meta_command` chunking pattern, so Telegram's message-length limit doesn't silently truncate a preview.
- System prompt in `agent.py` updated: the capabilities-boundary line ("never send, reply, forward, or delete anything — no such capability exists") was already stale before this build (send/delete existed) and is now corrected; `propose_forward_email` folded into the existing "never call proactively" reinforcement alongside `propose_send_email`/`propose_delete_email`. `core/tools/email/__init__.py`'s equally-stale module docstring fixed too.
- No new OAuth scope needed — `gmail.modify` already covers everything here.

**Code review found and fixed 1 more issue:** `get_forward_preview()`'s attachment walk only recognized attachments referenced via Gmail's `attachmentId` (a separate fetch call) — Gmail's `format=full` response can also inline a small attachment's bytes directly as `body.data` on the part itself, with no `attachmentId`. The original version would have silently dropped such an attachment from the forward with no error and no visible sign anything was missing. Fixed to recognize and forward both representations (verified locally: an inline-`body.data` attachment forwards byte-identical, without an unnecessary `attachments().get()` call).

**Independent second review (2026-08-01), the above was built by a remote session with no live credentials and no way to open a PR — it exported its work as a patch, applied and re-reviewed locally against the real repo before trusting or deploying it. Found and fixed 2 more real issues:**
- **`_parse_addr_list()` silently dropped valid recipients on any semicolon.** Verified directly against the real stdlib: `email.utils.getaddresses(["alice@ts.org, bob@ts.org;"])` returns a single empty pair for the *entire* string, not a partial parse — so a semicolon anywhere in an explicit `cc`/`bcc` (a common non-Gmail separator convention, exactly the case this function's own docstring claimed to handle) silently resolved to **zero** recipients, with the preview showing no Cc line at all and no error anywhere. This directly contradicted this build's own stated design intent. Fixed by normalizing semicolons to commas and stripping stray/repeated/trailing separators before parsing — reverified against the original failing case plus a bare trailing-comma edge case that turned out to have the identical failure mode.
- **`propose_forward_email`'s dispatch in `agent.py` was missing the proposal-time validation `propose_send_email` got** — it built the pending proposal unconditionally even if the model supplied an empty-string `to`/`thread_id` (schema `required` doesn't prevent an empty string from satisfying it). Fixed to validate up front and return a clean tool-level error, matching `propose_send_email`'s pattern exactly, before a broken proposal could ever reach Jonathan.

**Noted but not changed:** the same review flagged that `resolve_send_recipients()`/`get_forward_preview()` independently re-fetch the live Gmail thread once at proposal time and again at confirm time — if a new message lands in an active thread in the (typically brief) window between Jonathan seeing a preview and confirming, the resolved recipients/forwarded message could differ from what was shown. This is not new to this build: the original `send_email()`'s subject re-fetch already had the identical characteristic before any of this work started. Documented here rather than engineered around now — the window is narrow, and closing it properly would mean snapshotting resolved values at proposal time instead of re-deriving at confirm time, a larger architectural change than this build's scope, not a quick fix alongside it.

**Touches:**
`core/tools/email/fetch.py`, `core/agent.py`, `core/bot.py`, `core/tools/email/__init__.py`, `bugs.md`, `data-architecture.md`, `devlog.md`.

---

## BUG-024 — read_email_thread not surfacing CC addresses
**Type:** Bug
**Status:** Closed
**Priority:** High
**Severity:** High — Charlie is misreporting email recipients, leading to incorrect reply-all behaviour and potential missed recipients
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-07-31
**Topic ID:** 2366
**Resolved:** 2026-08-01

**Problem:**
When reading an email thread, Charlie is not correctly surfacing CC addresses from the email headers. Jonathan manually verified that laricalschnell@gmail.com was CC'd on thread 19fbb1018ab8cff5, but Charlie reported no CC addresses present.

**What needs fixing:**
Investigate how read_email_thread parses and surfaces CC/To/BCC headers from the Gmail API response. Ensure all recipient fields are correctly extracted and reported to the agent so reply-all and recipient visibility work accurately.

**Resolved:** 2026-08-01 — root cause confirmed directly: `get_thread_content()` (the function behind `read_email_thread`) only ever parsed `From`/`Subject` headers per message, never `To`/`Cc` — a pre-existing gap predating the BUG-023 reply/reply-all/forward build, not introduced by it (that build's own reply-all derivation uses a separate function, `get_thread_summary()`, which already fetched `To`/`Cc` correctly). Fixed by adding `To`/`Cc` lines per message (Cc line omitted when empty, to avoid clutter on the common no-CC case). Verified live against the exact thread Jonathan reported (19fbb1018ab8cff5) — `laricalschnell@gmail.com` now correctly appears in the Cc line — and against a real no-CC thread, confirming no stray empty Cc line appears there.

**Touches:**
`core/tools/email/fetch.py` (`get_thread_content`)

---

## BUG-025 — Email send confirmation displays "Sent to None" instead of recipient addresses
**Type:** Bug
**Status:** Closed
**Priority:** Medium
**Severity:** Medium — confusing and misleading, but email sends correctly; cosmetic issue in the confirmation message
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-07-31
**Topic ID:** 2384
**Resolved:** 2026-08-01

**Problem:**
After a successful email send, the confirmation message displayed "Sent to None" instead of the actual recipient addresses. The email itself sent correctly (verified in Sent folder). Observed on a reply-all send to purnelljonathan@gmail.com with laricalschnell@gmail.com CC'd.

**What needs fixing:**
Investigate the send confirmation message builder in bot.py — the recipient field is resolving to None instead of the actual To/CC addresses at confirmation time. Likely the resolved recipients aren't being passed through to the confirmation message correctly.

**Resolved:** 2026-08-01 — confirmed root cause directly: `bot.py`'s "send it" handler built the confirmation text from `pending['to']`, the raw (often `None`) value from the original proposal, not what actually got resolved and sent — `None` whenever `to` was omitted for a reply/reply-all, since the real recipient is only derived inside `resolve_send_recipients()` at send time. A gap in the original BUG-023 review: the "critical fix" there confirmed cc/bcc/reply_all were passed *into* `send_email()` correctly, but never checked what the *confirmation message afterward* used. Fixed by having `send_email()` return its resolved `{"to", "subject", "cc", "bcc", "message_id"}` dict, and building the confirmation from that return value (also now shows Cc when present, not just To). Verified live: a real send returns the correct resolved dict, and simulating the exact reply-all case that used to render "Sent to None." now correctly renders the real resolved recipient and Cc list.

**Touches:**
`core/tools/email/fetch.py` (`send_email` now returns resolved dict), `core/bot.py` (confirmation message uses the return value)

---

## BUG-026 — Forward tool can only forward the most recent message in a thread
**Type:** Bug
**Status:** Closed
**Priority:** Medium
**Severity:** Medium — causes silent data loss when multiple emails are grouped in the same thread and only the most recent is forwarded
**Blocks anything current:** No
**Rough effort:** Medium
**Logged:** 2026-08-01
**Topic ID:** 2448
**Resolved:** 2026-08-01

**Problem:**
propose_forward_email forwards only the most recent message in a thread. When multiple distinct emails share the same subject and get grouped into one thread (e.g. two separate Extra Space Storage receipts), there is no way to selectively forward an earlier message in the thread — it always picks the last one.

**What needs fixing:**
Add the ability to specify a particular message within a thread to forward, rather than always defaulting to the most recent. Could be done by exposing a message_id parameter, or by surfacing individual messages in the thread for selection.

**Resolved:** 2026-08-01 — grounded against the exact real thread Jonathan hit this on (`19fb992df84ac5be`: two separate Extra Space Storage receipts plus Jonathan's own earlier manual "Fwd:" reply, 3 messages total). Confirmed directly that the old code picked Jonathan's own prior forward, not either real receipt — some of what looked like BUG-027's formatting bug was actually this wrong-message-selected symptom.

Added an optional `gmail_message_id` parameter threaded through `get_forward_preview()`/`forward_email()`/`propose_forward_email` — thread_id-based "most recent" stays the default, but a specific message can now be targeted, validated against the thread's own message list (clean error if it doesn't belong). Named `gmail_message_id`, not the shorter `message_id` — this codebase already uses bare `message_id` for a different thing (the RFC822 `Message-ID:` header used for reply-threading in `get_thread_summary`/`resolve_send_recipients`/`send_email`), and conflating the two given this exact codebase's history of similar-field-confusion bugs (BUG-023, BUG-025) would have been a real risk, not just a style nit. `get_thread_content()` (already touched for BUG-024) gains a `Gmail Message ID:` line per message so Charlie can find a specific one after reading a thread, and `propose_forward_email`'s tool description now explicitly tells Charlie to check there and pass a specific id when a thread has more than one distinct message. The confirmation preview always states which message is being forwarded when a thread has more than one (e.g. "message 2 of 3, dated ..."), regardless of whether it was explicit or defaulted, so ambiguity is never silent.

This plan went through four rounds of independent review (an explicit ask from Jonathan to keep reviewing until both sides converged) — real, substantive issues were caught and fixed each round: an inconsistent/colliding parameter name (fixed to `gmail_message_id` throughout, including a couple of stale references the first fix pass itself missed), a fragile `<body>`-detection regex that broke on a `>` inside a quoted HTML attribute (fixed to an attribute-aware pattern, independently tested by the reviewing agent, not just trusted), a missing wiring point (the tool description never telling Charlie the feature existed), and one unused return field with an inaccurate justification (dropped rather than kept "for future use").

**Touches:**
`core/tools/email/fetch.py` (`get_forward_preview`, `forward_email`, `get_thread_content`), `core/agent.py` (`propose_forward_email` schema + dispatch), `core/bot.py` (`_forward_email_proposal_text`, "forward it" dispatch)

---

## BUG-027 — forward_email sends a fresh email instead of a proper forward
**Type:** Bug
**Status:** Closed
**Priority:** High
**Severity:** High — forwarded emails arrive as fresh plain-text messages, losing original formatting, attachments, and thread context entirely
**Blocks anything current:** No
**Rough effort:** Medium
**Logged:** 2026-08-01
**Topic ID:** 2462
**Resolved:** 2026-08-01

**Problem:**
propose_forward_email/forward_email does not produce a proper email forward. Instead of forwarding the original message with its headers, formatting, and attachments intact (as a real "Fwd:"), it constructs a fresh email with only the note text. The result doesn't group with other forwarded receipts in the recipient's inbox and the original email content/formatting is completely lost.

**What needs fixing:**
Rework forward_email to construct a proper MIME forward — the original message should be attached or inlined as a forwarded message (RFC 2822 compliant), with original headers (From, Date, Subject, To) included in the forward body, and original attachments re-attached. The result should look identical to hitting "Forward" in a real email client.

**Resolved:** 2026-08-01 — verified directly against the real receipt this was reported on before designing anything: it has no real attachments at all (a `multipart/alternative` with only `text/plain`/`text/html` parts — so "losing attachments" specifically didn't apply to this email; the real loss was formatting), and its HTML references all images via external `https://` URLs, not `cid:`-embedded ones — meaning preserving the original HTML as-is is enough to preserve the look, no inline-image-rewriting complexity needed for this case.

`forward_email()` now sends `multipart/alternative` (plain-text fallback + the real original HTML, wrapped in a "Forwarded message" header block) nested inside the existing `multipart/mixed` (attachments), instead of rebuilding everything as a single plain-text `MIMEText`. New `_extract_html_body()` in `fetch.py` returns the raw, unstripped HTML part (falls back to plain-text-only, unchanged from before, when the original has none). A real gap caught in design review: the actual receipt's HTML is a full standalone document (`<!DOCTYPE html><html><head>...`), the normal shape for marketing/receipt email — naively prepending the header block in front of it would produce invalid HTML5 with the header landing before `<!DOCTYPE>`/`<head>`. Fixed by detecting the opening `<body>` tag and injecting the header immediately after it, preserving the original document's `<head>`/`<style>` wrapper. Header fields embedded in this HTML block (sender name, subject, etc. — all attacker-influenced, since they come from the original sender) are `html.escape()`'d, a rendering-integrity guard distinct from `_reject_control_chars`'s existing CRLF/header-injection guard elsewhere in the same function. `cid:`-referenced inline images (not present in the email this was grounded against) aren't handled — logs a warning rather than silently producing broken image icons if a future forward target has them.

**Security review found and fixed a real content-spoofing bypass (2026-08-01):** the `<body>`-tag detection was originally an attribute-aware regex (independently tested against several cases including a quoted-attribute-`>` edge case) — but a regex has no concept of HTML structure, so it matches the *first text occurrence* of something `<body...>`-shaped anywhere in the string, including one an attacker plants inside an HTML comment or a `<script>` string literal (e.g. `<!--<body>-->`) earlier than the real tag. That would splice the entire "Forwarded message" disclosure block — sender identity, subject, date, and Jonathan's own note — into the comment, invisible to any standards-compliant renderer, while the attacker's real, later `<body>` content displays with *zero indication it was forwarded from someone else*. Concretely: anyone who emails Jonathan could craft an HTML email that, once forwarded through Charlie to a third party, would appear to originate directly and solely from Jonathan, with no forwarding disclosure at all — a phishing/spoofing vector that defeats the exact safeguard this feature exists to provide. Fixed by replacing the regex with a real HTML tokenizer (Python's stdlib `html.parser.HTMLParser`, new `_BodyTagFinder`/`_find_body_insertion_point`) — comments and `<script>`/`<style>` content are opaque to a real parser, so only the genuine structural `<body>` tag ever fires a start-tag event.

**A second confirmation pass on that exact fix caught a second, more subtle instance of the identical bug class:** the first version reconstructed an absolute string offset from `HTMLParser.getpos()`'s (line, column) plus a separately-built line-offset table (via `str.splitlines()`). `splitlines()` treats Python's full universal-newline set as line boundaries (`\r`, `\v`, `\f`, U+2028/U+2029, etc.), but `HTMLParser`'s own internal line counter only increments on a literal `\n` — any original HTML mixing a real `\n` with one of those other separators before the real `<body>` tag desynced the two counting schemes, landing the disclosure header at the wrong offset (reproduced directly: `"<html>\rfoo\n<body>hello</body></html>"` computed offset 13, splicing the header mid-tag, instead of the correct 17) — reopening the exact same disclosure-bypass via different math, not the same mechanism. Fixed by not reconstructing offsets from line/column at all: overriding `parse_starttag()` and capturing its own return value (the real absolute buffer index HTMLParser itself computes) the moment `handle_starttag` fires for "body," which sidesteps the line-counting mismatch entirely. Also added a defensive guard (raises if fed more than once) against a related, currently-unreachable failure mode the same reviewer identified: this technique's offset is only valid for a single, un-chunked `feed()` call, which happens to be how the only caller uses it today, but wasn't enforced — now it is, so a future refactor toward streamed/chunked feeding can't silently reintroduce this.

Also fixed in the same pass: `import html` (for `html.escape`) was added at module level, shadowing `_strip_html`'s pre-existing same-named parameter — not a live bug (that function never needed `html.escape`), but a latent trap for a future edit; the parameter is now `raw_html`.

**Touches:**
`core/tools/email/fetch.py` (`_extract_html_body`, `_BodyTagFinder`/`_find_body_insertion_point` new, `_strip_html` parameter renamed, `get_forward_preview`/`forward_email` extended)

---

## BUG-028 — No recurring adversarial/pen-testing practice against Charlie's live system
**Type:** Debt
**Status:** Open
**Priority:** Medium
**Severity:** N/A — capability/practice gap, not a defect
**Blocks anything current:** No
**Rough effort:** Medium — needs a real test harness, not just a one-off exercise
**Logged:** 2026-08-01
**Topic ID:** 2552

**Problem:**
[[BUG-018]]'s resolution absorbed its concrete, closeable items (the propose-gate default rule, the Pre-Build Checklist line) into `principles.md`/`data-architecture.md`. Its one remaining item — "periodic adversarial testing... to confirm gates actually hold, rather than assuming they do" — had no natural completion point, so it's tracked here as its own real, buildable thing instead of a bug that never closes.

Every `/code-review`/`/security-review` pass this session has checked that a gate *structurally exists* in the code. None of them have tested whether crafted, adversarial content sent to Charlie's real inbox can actually get past that gate in practice, or — a distinct and arguably more realistic risk — whether it can get Charlie to construct a *misleading proposal preview* that leads Jonathan to genuinely (if mistakenly) type the confirm phrase himself. The gate itself can't catch that second case, since it only verifies the literal confirm phrase was typed, not that what was approved was accurately represented.

**What needs fixing:**
1. Build a small, repeatable set of adversarial test emails/scenarios — e.g. embedded instructions attempting to trigger an unintended send/forward/delete, attempts to manipulate a reply-all recipient list, attempts to get a misleading recipient/attachment list shown in a proposal preview, attempts to make Charlie narrate a proposal as more benign than it is.
2. Run these against the real, live system (not a mock) — since the whole point is testing actual model behavior in production conditions, not code structure. Test payloads should target low-stakes blast radius (e.g. self-addressed sends, not real third parties) so a genuine gate failure during testing can't leak or send anything real.
3. Define a cadence: at minimum, run the relevant subset after any build touching a write-capable email tool (natural extension of the existing Tier-3 process); additionally on a standing recurring cadence independent of any build, since behavioral drift can happen without any code changing.
4. Any real finding gets logged as its own bug, same as every other review pass this session — not filed away in a separate, less-visible log.

**Touches:**
TBD — likely a new small test-harness script/doc, plus a cadence note added to `principles.md` or `data-architecture.md`.

---

## BUG-029 — read_email_thread fails to surface most recent messages in a thread
**Type:** Bug
**Status:** Closed
**Priority:** High
**Severity:** High — causes Charlie to act on stale email content and misreport thread state to Jonathan
**Blocks anything current:** No
**Rough effort:** Medium
**Logged:** 2026-08-01
**Topic ID:** 2531

**Problem:**
read_email_thread consistently fails to return the most recent message(s) in a thread. Observed twice in the same conversation with thread 19ed5cca3178e967: (1) after a new email arrived today about mice/permit, read_email_thread still reported the June 17 message as the latest; (2) after Erika replied to Jonathan's thank you mentioning humidity and buckling floors, read_email_thread again failed to surface that message. Charlie drafted and reasoned based on stale content as a result.

**Root cause, confirmed 2026-08-05:** not pagination or sort order (both already correct — BUG-024's sort by `internalDate` works fine). `get_thread_content()` sorted messages oldest-first, joined them into one string, and truncated at a fixed `max_chars` (8000) by slicing the joined string from character 0 — `combined[:max_chars]`. Since each reply quotes the entire conversation before it, a long thread's combined text can cross 8000 characters by its second or third message, and the tail-slice then silently discarded every message after that point — including whatever's actually new. Confirmed directly against thread 19ed5cca3178e967 (36 messages, June 17 – Aug 4): the old logic hit the cap partway through message 2, discarding all of July and August, including the exact humidity/floors message Jonathan reported missing.

**Fix implemented, 2026-08-05 (not yet closed):** `get_thread_content()` now selects whole messages newest-first, stopping at the first one that doesn't fit (not skipping it to try an older, smaller one, which a code-review workflow caught as a bug in the first draft — it could otherwise create a non-contiguous gap the omission note wouldn't disclose). Whatever's omitted is always the contiguous oldest block. Omitted messages aren't dropped without a trace — each gets a one-line stub (date/sender/subject plus its Gmail Message ID and any attachment names), since [[BUG-020]]'s `read_email_attachment` and `propose_forward_email` both locate a specific earlier message by the ID in this function's output; a second code-review pass caught that the first draft's blanket omission note would silently break those tools' earlier-message lookups on exactly the long threads this omits from. The newest message's own content is never sliced to make room for the note or stub section — if the budget is still tight after dropping everything else, the oldest *kept* message is demoted to a stub too (repeatedly, if needed) rather than truncating into the newest message's tail; a code-review-caught edge case (the note itself pushing an otherwise-fitting result over budget) is handled the same way, with a floor ensuring even a last-resort truncation never cuts into the newest message's own header/ID. Verified against the real thread (19ed5cca3178e967): all 36 messages' Gmail Message IDs are discoverable, the 3 most recent messages are present including the humidity/floors one originally reported missing, and output stays within the 8000-char budget. Went through two `/code-review` passes (high effort) — the first caught 3 real regressions in the initial draft (newest-content loss via the note-length interaction, lost metadata for omitted messages, non-contiguous gaps), all fixed and re-verified against the exact adversarial scenarios the review constructed.

**Closed:** 2026-08-05 — deployed to the always-on Mac (clean restart, no errors) and live-tested with two separate long threads through the live Email topic (topic 2271): Jonathan asked Charlie to read the Erika Odle thread (`19ed5cca3178e967`) and, separately, the Hanekom/RCC disbursements thread (`19df2bb461a93383`). Charlie reported the latest message on each as, respectively, Jonathan's own Aug 4 reply and Zaheer Seedat's Aug 5 reply. Verified independently via the Gmail API directly (not the chat transcript) against both threads: the Erika thread's actual newest message is `Tue, 4 Aug 2026 06:09:13 -0400` from Jonathan Purnell (matches, once converted from the tool's UTC display), and the Hanekom thread's actual newest message is `Wed, 5 Aug 2026 09:36:24 +0000` from Zaheer Seedat (exact match) — both correct on threads Charlie previously would have reported stale content for.

**Touches:**
`core/tools/email/fetch.py` (`get_thread_content`)

**Cross-reference, [[BUG-020]]:** `read_email_attachment` (built the same day this bug was logged) defaults to a thread's most recent message via the identical pattern implicated here — `threads().get()`'s message list, sorted by `internalDate`, take the last one (`get_forward_preview`/`get_thread_summary` share this same pattern too, and predate this bug). Shipped anyway rather than held back: the exposure is narrower there (only matters when `gmail_message_id` is omitted, the target attachment is genuinely on the newest message, and that message isn't surfaced — and the tool's own description was just hardened this session to push toward always passing `gmail_message_id` whenever a thread has multiple messages), and whichever fix lands here will apply to `read_email_attachment` automatically, since it's the same underlying fetch. Whoever resolves this bug should verify `read_email_attachment`'s default-message-targeting behavior as part of confirming the fix, not just `read_email_thread`'s digest.

---

## BUG-030 — Email deletions falsely reported as successful when they didn't execute
**Type:** Bug
**Status:** Closed
**Priority:** High
**Severity:** High — Charlie tells Jonathan emails were deleted when they weren't, giving false confidence
**Blocks anything current:** No
**Rough effort:** Medium
**Logged:** 2026-08-04
**Topic ID:** 2720

**Problem:**
When Jonathan confirmed batch email deletions with "delete it", the chat showed "Deleted (moved to Trash — recoverable for 30 days)" but a subsequent inbox search confirmed all 7 emails were still present. The deletion confirmations were false positives. Same may apply to earlier individual deletions in this session.

**Root cause, confirmed 2026-08-05 (found via a fresh repro — Jonathan asked to batch-delete 4 "delete candidates" from a digest, confirmed once, and only the Amtrak email was actually deleted):** the whole delete pipeline was single-item at every layer, despite Charlie describing the action as a "batch delete":
1. `propose_delete_email`'s tool schema (`core/agent.py`) only accepted one `thread_id` — no way to pass a list.
2. `handle_turn()` stored the proposal in a single variable, not a list — if Charlie called the tool once per email (the only option available), each call silently overwrote the previous one.
3. `PENDING_DELETE_EMAIL` in `bot.py` was `dict[int, dict]` — one pending delete per topic, not a queue.
4. The "delete it" handler popped exactly one pending dict and trashed exactly one thread.

So a single "delete it" only ever deleted whichever email was proposed *last* (silently discarding the rest) — `trash_thread()` itself has no swallowed-error path (a bare `.execute()` that raises normally, and the handler's existing try/except already reported real failures), so the false-positive read was really "N-1 of N were never even proposed," not a lie about the one that did execute. This matches this bug's original 7-email report closely enough (only the final proposed thread would've actually gone to Trash) to treat as the same root cause rather than a separate one — no other error-swallowing path was found in `trash_thread`/the confirm handler during this investigation.

**Fix implemented, 2026-08-05 (not yet closed):** made the pipeline genuinely list-based instead of single-item: `propose_delete_email`'s schema now takes `thread_ids: string[]` (tool description explicitly tells Charlie to pass every id in one call for a batch, not one call per email); `PENDING_DELETE_EMAIL` holds `{"thread_ids": [...], "reason": ...}`; `_delete_email_proposal_text()` fetches and lists a fresh Gmail summary per thread (one bad id doesn't blank the rest of the preview); the "delete it" handler loops `trash_thread()` over every id, collecting per-thread failures independently and reporting "Deleted M/N... Failed: ..." rather than a blanket success — so a partial failure can never again be reported as if the whole batch succeeded. Single-item deletes still work unchanged (a one-element list). Dry-run verified locally against mocked `get_thread_summary`/`trash_thread` (all-succeed, single-item, and partial-failure cases) since this repo has no test suite.

**Closed:** 2026-08-05 — deployed to the always-on Mac (rsync + `launchctl stop/start com.charlie`, clean restart, no errors in `charlie.log`) and live-tested: Jonathan asked Charlie to "batch delete the delete candidates" again in the live Email topic (topic 2271). Charlie proposed all 3 remaining threads in a single `propose_delete_email` call (`thread_ids: ["19fcfafa8a98ca92", "19fd1cebd8d05cde", "19fd1858f1e84011"]` — Buffer, Elliptic, 1% Better), Jonathan replied "delete it" once, and Charlie reported "Deleted all 3." Verified independently via the Gmail API directly (not just the chat transcript, per this bug's original "falsely reported" failure mode) — fetched all 3 thread_ids and confirmed each carries the `TRASH` label. Root cause fully resolved: the single-item pipeline (BUG-030's actual defect) is now list-based end to end, and reporting is grounded in what actually happened per thread rather than a blanket success message.

**Touches:**
`core/agent.py` (`propose_delete_email` schema + dispatch + system prompt), `core/bot.py` (`PENDING_DELETE_EMAIL`, `_delete_email_proposal_text`, the "delete it" handler)

---

## BUG-031 — forward_email breaks the thread chain (sends as an unrelated new message)
**Type:** Bug
**Status:** Closed
**Priority:** Medium
**Severity:** Medium — the forwarded copy doesn't group with the original conversation in Jonathan's own Sent/All Mail, unlike a real Gmail forward; content/formatting/attachments themselves are unaffected (that was BUG-027, already resolved)
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-08-05
**Topic ID:** TBD — no Telegram topic yet, logged directly via Claude Code; run /createbugtopics to backfill

**Problem:**
Jonathan forwarded the same email two ways — once through Charlie, once manually in Gmail — and compared them directly. The manual forward grouped with the original thread in his mailbox; Charlie's did not. `forward_email()` (`core/tools/email/fetch.py`) was built to explicitly send the forward as a brand-new message with no `threadId` and no `References`/`In-Reply-To` to the original — a deliberate choice documented directly in its own docstring at the time (distinct from BUG-027, which fixed the forward losing formatting/attachments, not this). Living with it, that choice reads as the forward "breaking the chain" rather than a real forward.

**What needs fixing:**
Set `threadId` on the `messages().send()` call to the original thread, and set `In-Reply-To`/`References` to the original message's real RFC822 `Message-ID` header — matching how `send_email()` already threads replies, and matching what Gmail's own "Forward" button does under the hood.

**Resolved:** 2026-08-05 — `get_forward_preview()` now also returns the forwarded message's `Message-ID` header; `forward_email()` sets `threadId` + `In-Reply-To`/`References` from it before sending. Live-verified, not just deployed: Jonathan forwarded a real email (an Anthropic receipt, thread `19fd2a25a3a5744b`) to `purnelljonathan@gmail.com` through Charlie; fetched the thread directly via the Gmail API afterward (read-only, no reliance on eyeballing Gmail's UI) and confirmed the sent forward shares `threadId` with the original message, with `In-Reply-To`/`References` exactly matching the original's real `Message-ID`, and `Subject` correctly prefixed `Fwd:`. Same mechanism `send_email()` already used for replies. Only affects grouping in Jonathan's own mailbox (Sent/All Mail) — the recipient's client never saw the original Message-ID, so nothing changes on their end.

**Touches:**
`core/tools/email/fetch.py` (`get_forward_preview`, `forward_email`)

---

## BUG-032 — send_email replies don't quote the original message

**Type:** Bug
**Status:** Closed
**Priority:** Medium
**Severity:** Medium — a Charlie reply threads correctly (BUG-031's fix, plus reply already had `threadId`/`References`) but the recipient sees only the new text, with no quoted original below it the way a normal client reply shows
**Blocks anything current:** No
**Rough effort:** Medium
**Logged:** 2026-08-05
**Topic ID:** TBD — no Telegram topic yet, logged directly via Claude Code; run /createbugtopics to backfill

**Problem:**
Jonathan replied to the same thread twice — once directly from Gmail, once through Charlie — and compared them. Both threaded correctly (BUG-031 was specifically about forward, not this), but Charlie's reply body was only the new text; a normal client reply also appends "On {date}, {sender} wrote:" plus the quoted original below. `send_email()` built the message as a bare `MIMEText(body)` — literally just the raw reply text, no quote of any kind, for either plain or HTML mail. Jonathan asked for reply to have the same quoting fidelity forward already has (BUG-027): plain-text fallback always, real HTML preservation when the original has one.

**What needs fixing:**
Append a quote of the message being replied to below `body`, matching forward's approach — original HTML preserved and nested in a `<blockquote>` when present (needs the original's outer `<html>`/`<head>`/`<body>` wrapper stripped first, since nesting a second full document inside a blockquote is invalid), plain `"> "`-prefixed quote otherwise.

**Resolved:** 2026-08-05 — `send_email()` now fetches the replied-to message via the existing `get_forward_preview()` (same one `forward_email` uses) and appends `"On {date}, {sender} wrote:"` plus the quote below `body`. New `_BodyEndTagFinder`/`_extract_body_inner_html` (mirrors `_BodyTagFinder`/`_find_body_insertion_point`, same real-tokenizer approach rather than a regex — here to avoid truncating genuine quoted content that happens to contain a `</body>`-shaped string in a comment/script, not a spoofing bypass like BUG-027's, since the reply's own trusted text is always escaped and placed before the untrusted quote, with nothing legitimate positioned after it to blend into) extracts just the original's inner content for nesting inside a `<blockquote>`. Sends `multipart/alternative` (plain quote always; HTML quote only when the original had `html_body`) instead of a bare `MIMEText`. A failure fetching the quote doesn't block the reply — it still sends, just without one. `bot.py`'s send preview now notes "(includes the quoted original below it)" for a reply, so the preview doesn't diverge from what's actually sent.

Live-verified, not just deployed: Jonathan replied to a thread through Charlie; fetched the actual sent message via the Gmail API afterward and decoded both parts directly. A stronger test than the pre-deploy mocks covered — this reply landed 3 levels deep in an existing back-and-forth (Jonathan's own prior Gmail reply had already quoted an earlier Charlie reply, which quoted the original), and both parts handled the nesting correctly: the plain part quoted the full visible prior text as `>`-prefixed lines; the HTML part wrapped the extracted inner content — including Gmail's own nested `<blockquote class="gmail_quote">` from the message before it — in a properly nested, correctly-escaped `<blockquote>`, structurally identical to a native Gmail reply. Confirmed by comparing against the earlier, pre-fix reply still sitting in the same thread (a bare `text/plain` with no quote at all — msg `19fd2bfb9a71883f`), which made the before/after difference concrete rather than inferred.

**Touches:**
`core/tools/email/fetch.py` (`_BodyEndTagFinder`, `_extract_body_inner_html`, `send_email`), `core/bot.py` (`_send_email_proposal_text`)

---

## BUG-033 — forward_email's HTML note loses line breaks

**Type:** Bug
**Status:** Closed
**Priority:** Low
**Severity:** Low — cosmetic only, plain-text part was always correct; the HTML part (what most recipients actually see) collapsed a multi-line note into one line
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-08-05
**Topic ID:** TBD — no Telegram topic yet, logged directly via Claude Code; run /createbugtopics to backfill

**Problem:**
Jonathan noticed a forwarded email's note — shown with line breaks in Charlie's Telegram preview ("Hi Jonathan / You might find this interesting. / Kind regards, / Jonathan") — arrived as one run-on line in the actual received email. `forward_email()`'s HTML path built `f"<div>{html.escape(note)}</div>"` — `html.escape` doesn't touch literal `\n` characters, and HTML collapses raw newlines to whitespace by default, so a multi-line note only ever displayed correctly in the plain-text part (invisible to any HTML-rendering client, which is most of them, including Gmail). Exact same bug class `send_email()`'s reply body already avoided correctly in the same session (BUG-032) via `.replace("\n", "<br>")` — forward's note just never got that treatment.

**What needs fixing:**
Convert the note's line breaks to `<br>` before embedding it in the HTML part, matching the pattern already used for the reply body.

**Resolved:** 2026-08-05 — one-line fix: `html.escape(note).replace(chr(10), '<br>')` instead of `html.escape(note)` alone. Live-verified: Jonathan forwarded the original HBS newsletter message again through Charlie with a fresh multi-line note; fetched the real sent message via the Gmail API and found `<div>Hi Jonathan<br><br>I know I sent this already but you should have a look.<br><br>Kind regards,<br>Jonathan</div>` — every line break correctly rendered as `<br>`. The plain-text part (always correct) was unaffected.

**Touches:**
`core/tools/email/fetch.py` (`forward_email`)

---

## BUG-034 — Telegram updates are processed serially; Charlie can't reason about two topics at once
**Type:** Debt
**Status:** Open
**Priority:** Low
**Severity:** N/A — architectural constraint, not a defect; no reported incident
**Blocks anything current:** No
**Rough effort:** Medium-Large (not a mechanical fix — requires auditing shared in-memory state for cross-topic races before it's safe to flip on)
**Logged:** 2026-08-05
**Topic ID:** TBD — no Telegram topic yet, logged directly via Claude Code; run /createbugtopics to backfill

**Problem:**
Found during a full-codebase concurrency survey prompted by Jonathan asking whether Charlie is generally built as "the agent in charge, with subordinate functions as worker streams." `core/bot.py`'s `Application.builder()...build()` never calls `.concurrent_updates(True)`, so python-telegram-bot processes Telegram updates **serially** — one `on_message`/`handle_turn` call at a time, for the entire bot, regardless of which topic it's for. Even after [[BUG-019]]'s fix (delegating blocking Gmail/news/git calls to worker threads so they don't freeze the loop), the actual Claude API call inside `handle_turn()` is still awaited synchronously within that one serial update-processing task. So if Jonathan messages Charlie in two different topics close together, the second one queues behind the first's entire turn (including its own Claude round-trip and any tool calls) rather than getting a concurrent response.

This is a distinct question from BUG-019: BUG-019 is "don't block on I/O you could delegate"; this is "should two conversations be able to make progress at the same time at all." Whether that's actually a problem depends on what Jonathan expects from Charlie day-to-day — a personal Chief of Staff bot with one owner may reasonably only ever have one active conversation anyway, in which case this is a non-issue in practice, or he may want to message multiple topics in a burst and get parallel responses.

**What needs deciding (not yet decided):**
1. Does Jonathan actually want concurrent cross-topic responses, or is one-at-a-time acceptable/expected for a single-owner assistant?
2. If concurrency is wanted: enabling `concurrent_updates(True)` requires auditing every piece of shared in-memory state for cross-topic races first — `PENDING_SEND_EMAIL`/`PENDING_DELETE_EMAIL`/`PENDING_FORWARD_EMAIL` (keyed by `topic_id`, likely fine per-topic but unverified under real concurrency), `ACTIVE_TOPICS` (exists specifically to block overlapping turns *within* the same topic — needs to keep working correctly once cross-topic overlap is allowed), and the cost implication of multiple concurrent Claude API calls (more simultaneous spend, may want a concurrency cap rather than fully unbounded).

**What needs fixing:**
TBD — pending the decision above. Not a "wrap in asyncio.to_thread" fix like BUG-019; this is a real architectural choice with its own tradeoffs.

**Touches:**
`core/bot.py` (`Application` builder), plus whatever shared state the audit above turns up

**Cross-reference, [[BUG-019]]:** the two bugs came from the same investigation and are complementary — BUG-019 makes individual blocking calls non-blocking; this bug is about whether the bot can process more than one conversation's turn at a time in the first place, which BUG-019's fix alone does not enable.

---

## BUG-035 — Grant finder silently dropped newsletter opportunities; no eligibility/geography filtering at all
**Type:** Bug
**Status:** In Progress — fix implemented, not yet closed (see note below)
**Priority:** Medium
**Severity:** High — Jonathan's own description: "frustratingly bad." Real, currently-open grants for Larica were dropped from the weekly digest with zero trace, and separately, opportunities she's flatly ineligible for (wrong region, educator-only) were being sent as if valid
**Blocks anything current:** No
**Rough effort:** Large (already done — multi-stage fix, several rounds of live testing against real emails/pages, one `/code-review` pass)
**Logged:** 2026-08-05
**Topic ID:** TBD — no Telegram topic yet, logged directly via Claude Code; run /createbugtopics to backfill

**Problem:**
Jonathan reported the grant finder found zero open grants on Monday (2026-08-03), but newsletters he could see in the inbox looked like they should have surfaced some. Investigation found two independent, compounding root causes, plus (once actually reviewing real recovered opportunities with Jonathan) a third, larger design gap:

1. **Gmail extraction silently failed.** `_extract_grants_from_emails()` asked Claude for a bare JSON array in free text and pulled it out with a `\[.*\]` regex. If the array didn't fully complete — most likely on a newsletter-heavy week, since more source content means more opportunities to describe within the same fixed 2000-token output cap — the regex found no match and the function returned `[]` with **zero logging of any kind**. Confirmed directly: re-running the exact 4 unread newsletters from Monday's run through the same extraction logic found 13 real opportunities with deadlines through October — Claude *can* find them, it just silently failed to that Monday. Compounding this: `poll_gmail_inbox()` marked every fetched message `\Seen` unconditionally, regardless of whether extraction succeeded, so a failed batch's source emails became permanently unrecoverable — the next poll only ever looks at `UNSEEN` mail.

2. **`validate_L3` had no date anchor.** The AI cross-check that decides "is this still open" never told the model what today's date was. Confirmed directly: it rejected a real opportunity with a 2026-10-19 deadline, reasoning "we are currently past this date" — while the actual date was 2026-08-05, over two months earlier. Also confirmed the page-content given to `validate_L3` was truncated to 4000 characters without stripping `<script>`/`<style>` block *contents* (only their tags) — real eligibility text on one page didn't start until character ~17,700 of a ~43,000-character page, so it was never read at all.

3. **No eligibility/geography filtering existed anywhere in the pipeline.** Reviewing the recovered opportunities with Jonathan surfaced that "Open Calls" was explicitly categorised as "any geography" with nothing anywhere actually checking whether Larica (Jersey City, NJ) was eligible — a Portland, OR-restricted open call and an educator-only award both sailed through as if valid. Separately, Jonathan asked for eligibility restrictions/priorities to be surfaced prominently in the write-up rather than buried or omitted (a real example: an Erie Canal residency's priority for artists with Haudenosaunee Confederacy/Mohican Nation ties), and for the write-up to include sought mediums and a one-line theme/purpose — none of which existed before.

**Fix implemented, 2026-08-05 (not yet closed):**
- `_extract_grants_from_emails` and `validate_L3` both moved to forced tool-use (a JSON-schema-validated tool call) instead of free-text-plus-regex, guaranteeing structurally valid output whenever generation completes, with every failure path (API error, `stop_reason == "max_tokens"`, missing tool call) now logged specifically via a new shared `_forced_tool_call()` helper.
- `poll_gmail_inbox` only marks a message `\Seen` once its content was either successfully extracted as part of a successful batch, or had no usable body in the first place (nothing lost either way) — a failed batch now leaves its source emails unread so they're retried on the next poll instead of vanishing permanently.
- `validate_L3` now states today's actual date explicitly, anchored to the app's configured `TIMEZONE` (via a new `_today()` helper) rather than `date.today()`'s implicit OS-local behaviour — also applied to `validate_L2`, which had the same latent issue.
- `validate_L3` now checks eligibility against a hardcoded `_ARTIST_PROFILE` (visual artist, not an educator/curator/student, based in Jersey City NJ, not restricted to one medium) — rejects genuine geographic/role exclusions, but explicitly does NOT reject for a stated priority/preference toward another group (surfaces it instead) or for a medium-specific opportunity/medium uncertainty (also surfaced, never disqualifying).
- New `Opportunity` fields — `eligibility_notes`, `mediums`, `theme_summary` — populated by `validate_L3`, persisted to `grants.db` (with an `ALTER TABLE` migration for the pre-existing DB), and shown in the email (`eligibility_notes` as a prominent callout right after the title, before the general description).
- `_html_to_text` now strips `<script>`/`<style>` block contents, and `validate_L3`'s page-content budget was raised 4000 → 12000 chars.
- "Open Calls"'s "any geography" language removed from both `categorize()`'s prompt and the email's category subtitle, since geography is now actually filtered upstream.

A `/code-review` pass (high effort) on this found and fixed 4 further real issues before deployment: `mail.logout()` sat inside the same broad `except` that returns `[]` on failure, so a logout error after a *successful* extraction would have discarded already-extracted opportunities whose source emails were already marked `\Seen` — moved into its own `finally`-guarded block that can't clobber the return value; messages with too-short bodies to extract from were never being marked `\Seen` at all (only content-bearing ones were considered), risking them piling up and crowding out genuinely new mail in the 20-message-per-poll window — now split into `content_nums` (seen only on success) vs `empty_nums` (seen unconditionally, nothing to lose); a stale prompt string still claimed "4000 chars" after the truncation was raised to 12000; and the forced-tool-use validation logic was duplicated near-verbatim between the two call sites — extracted into the shared `_forced_tool_call()` helper mentioned above.

Every fix was verified against real, live data, not mocks: the exact 4 originally-missed emails, the exact opportunities Jonathan gave feedback on by name (Queer Spirit correctly rejected for its Portland, OR restriction; Liu Shiming correctly rejected for being educator-only; Erie Canal correctly verified with its Haudenosaunee Confederacy/Mohican Nation priority now surfaced verbatim; Exclaim!'s theme write-up landing almost word-for-word on Jonathan's own example phrasing; the sculpture-specific grants no longer incorrectly rejected for medium uncertainty), a direct API probe confirming `stop_reason == "max_tokens"` is detected correctly, and a live end-to-end run that caught 3 more genuinely new newsletters, extracted 9 opportunities, and correctly resolved down to 1 real, currently-open one after full validation.

**Deliberately left open:** per this log's standard, live-tested-by-me is not the same as Jonathan confirming the digest itself reads well and the judgment calls (what gets surfaced vs. rejected) match what he actually wants once he's seen a real weekly run land in his inbox naturally, not one triggered by Claude Code mid-investigation.

**Touches:**
`core/tools/grants.py` (`_extract_grants_from_emails`, `poll_gmail_inbox`, `validate_L2`, `validate_L3`, `_html_to_text`, `Opportunity`, `init_db`, `_mark_seen`, new `_today`/`_forced_tool_call`/`_ARTIST_PROFILE`/`_L3_TOOL`), `core/tools/grants_email.py` (email template, category subtitle)

---