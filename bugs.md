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
**Status:** Open
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

**Touches:**
`core/db.py`, `core/bot.py`

---

## BUG-002 — No deployment verification step after builds
**Type:** Bug
**Status:** Open
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

**Touches:**
`core/bot.py`, launchd service restart logic, Claude Code task template/workflow

---

## BUG-003 — No data architecture document
**Type:** Debt
**Status:** Open
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

Verified locally: `_validate_claude_code_call` against 6 pass/fail cases, and `_out_of_scope`/`_changed_files` against this session's own real uncommitted changes (correctly passed a matching scope, correctly flagged a narrowed scope, correctly flagged an unrelated scope). **Not yet verified**: a live, Telegram-triggered build going through this gate end-to-end on the always-on Mac — that requires syncing and restarting `com.charlie` there first.

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