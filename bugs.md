# Charlie — Bug List

---

**Architectural rule: Any message sent directly via the Telegram bot (app.bot.send_message, bot.send_message, etc.) is NOT automatically saved to the messages DB. Any background process, scheduler, or tool that sends Telegram messages must explicitly write those messages to the DB as assistant messages (with the correct topic_id) immediately after sending, so the agent can see them in conversation history when the user replies.**

---

Open issues to be worked through over time. Newest bugs at the bottom.

**Fields per entry:**
- **Status:** Open / In Progress / Closed
- **Priority:** High / Medium / Low
- **Severity:** High (breaks things or violates core principles) / Medium (creates friction or inefficiency) / Low (nice to have)
- **Blocks anything current:** Yes / No — and what
- **Rough effort:** Small (< 1 hour) / Medium (half day) / Large (multi-session)

---

## BUG-001 — SQLite message history grows indefinitely
**Status:** Open
**Priority:** Medium
**Severity:** High — violates Jonathan's core data minimalism principle and accumulates sensitive conversation data indefinitely
**Blocks anything current:** No — but will become a cost and privacy problem over time
**Rough effort:** Medium
**Logged:** 2026-05-29

**Problem:**
All messages (user and assistant) are stored in `data/charlie.db` with no expiry, cleanup, or deletion logic. The database grows forever. This means: (1) sensitive conversation data accumulates indefinitely on disk, (2) the full history is passed to the Claude API on every message in a topic, increasing cost and latency over time, and (3) it directly contradicts the design principle of keeping Charlie lightweight and data-minimal. This was not surfaced to Jonathan until after the Meta tool was built — it should have been caught at design stage.

**What needs fixing:**
Decide on and implement a retention policy. Options: auto-delete after N days, topic-scoped retention only (wipe when topic goes inactive), or removing persistent history entirely and relying on Telegram's own record (fetched on demand). Also: the disclosure failure should be prevented going forward by the data architecture document (see BUG-003).

**Touches:**
`core/db.py`, `core/bot.py`

---

## BUG-002 — No deployment verification step after builds
**Status:** Open
**Priority:** High
**Severity:** High — causes Charlie to declare things "live" that aren't actually running, destroying trust in deployment announcements
**Blocks anything current:** No — but affects every future build
**Rough effort:** Small
**Logged:** 2026-05-29

**Problem:**
After a Claude Code build, Charlie announces completion without verifying the service is actually running the new code. In the /meta build, the bot process was running a pre-deployment version and hadn't been restarted — Charlie said "Built and live" and invited Jonathan to test it, and it failed immediately. Announcing completion prematurely is worse than saying nothing.

**What needs fixing:**
After any Claude Code build, the workflow should automatically: (1) confirm the service has been restarted and is running the new code, (2) run a basic smoke test or health-check where possible, and (3) only declare something "live" once verified. If verification isn't possible automatically, Charlie should explicitly say "changes are deployed — restart required before testing" rather than "built and live."

**Touches:**
`core/bot.py`, launchd service restart logic, Claude Code task template/workflow

---

## BUG-003 — No data architecture document
**Status:** Open
**Priority:** High
**Severity:** High — without this, every new tool build risks violating Jonathan's data minimalism principle without anyone noticing until after the fact
**Blocks anything current:** No — but should be consulted before any new tool is built
**Rough effort:** Small
**Logged:** 2026-05-29

**Problem:**
Jonathan has strong, clearly stated preferences about data minimalism and privacy — only store what's necessary, nothing persisted beyond its purpose, sensitive data never leaving local systems unnecessarily. Despite this, the SQLite persistent history was built and growing without Jonathan knowing. There is no document that records what is stored, where, for how long, and what the deletion policy is. This means data decisions are being made implicitly rather than deliberately.

**What needs fixing:**
Create `data-architecture.md` in the project root. It should record: every data store in the system (what it is, where it lives, what it contains), retention policy for each, deletion logic (or lack thereof), and whether anything leaves the local machine (and to which external APIs). This document should be reviewed and updated before any new tool is built that involves storing or transmitting data.

**Touches:**
New file: `data-architecture.md`. Relevant to all future tool builds.

---

## BUG-004 — Meta review prompt is not documented
**Status:** Open
**Priority:** Medium
**Severity:** Medium — the prompt is the core of the Meta tool and should be reviewable and improvable, but isn't visible anywhere
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-05-29

**Problem:**
The /meta tool makes a fresh Claude call with a specific system prompt instructing it to be ruthless, use three categories, and focus on actionable improvements. This prompt is hardcoded in `core/tools/meta.py` but is not documented or visible anywhere outside the code. The prompt itself should be subject to review and iteration — including via the /meta process itself — but can't be meaningfully reviewed if it's buried in source code.

**What needs fixing:**
Extract the meta review prompt into a standalone readable file — e.g. `core/tools/meta-prompt.md` — and have `meta.py` read from it at runtime. This makes the prompt easy to review, edit, and improve without touching code. The prompt itself should also be reviewed: it should explicitly instruct the reviewer to flag when Charlie states uncertain inferences as facts, and to evaluate whether build announcements were premature.

**Touches:**
`core/tools/meta.py`, new file: `core/tools/meta-prompt.md`

---

## BUG-005 — No design principles document
**Status:** Open
**Priority:** High
**Severity:** High — without written principles, architectural decisions are made ad hoc and Jonathan has to re-state his preferences every time rather than them being baked into how Charlie operates
**Blocks anything current:** No — but affects every future build and design decision
**Rough effort:** Small
**Logged:** 2026-05-29

**Problem:**
Several core principles have been clearly established through conversation: (1) hub-and-spokes architecture — Charlie is the agent, tools are discrete spokes, (2) data minimalism — store only what's necessary, nothing persisted beyond its purpose, (3) human approval before action — Charlie never acts autonomously on recommendations without Jonathan's sign-off, (4) lightweight and efficient — no sprawling databases or unnecessary complexity. None of these are written down. They exist only in conversation history. This means they can be violated accidentally on any build, and Charlie cannot reliably consult them.

**What needs fixing:**
Create `principles.md` in the project root. Document the established architectural and operational principles explicitly. This file should be loaded into Charlie's system prompt context (alongside charlie.md and devlog.md) so that principles are consulted automatically on every build decision, not just when Jonathan happens to re-state them.

**Touches:**
New file: `principles.md`. Should be added to system prompt context loading in `core/bot.py` or equivalent.

---

## BUG-006 — No pre-build checklist
**Status:** Open
**Priority:** High
**Severity:** High — without this, builds can violate core principles (data minimalism, hub-and-spokes architecture) without anyone noticing until after the fact; Claude Code tasks can also silently overwrite or destroy files outside their intended scope
**Blocks anything current:** No — but should be in place before the next tool is built
**Rough effort:** Small (once BUG-003 and BUG-005 are resolved)
**Logged:** 2026-05-29

**Problem:**
There is no formal pre-build checklist that Charlie references before starting any Claude Code task. Established principles (hub-and-spokes architecture, data minimalism, human approval before action) and data architecture decisions should be consulted before every build, not just when Jonathan happens to re-state them. The SQLite disclosure failure (BUG-001) is a direct example of what happens without this.

Additionally, Claude Code tasks currently have broad file access by default with no scoping constraints. If a task prompt doesn't explicitly list which files are off-limits or read-only, Claude Code will modify whatever it deems relevant — even files entirely outside the task's intent. On 2026-05-29, a diagnostic task focused on debugging /meta silently rewrote bugs.md from scratch, wiping BUG-002 through BUG-005. The data was only recovered because the loss was noticed and git history was available.

**What needs fixing:**
Once `principles.md` (BUG-005) and `data-architecture.md` (BUG-003) exist, create a pre-build checklist that Charlie explicitly runs through before any Claude Code task — not just builds, but any task. Checklist should include at minimum:
- Does this touch data storage? If so, does it comply with data-architecture.md?
- Does the architecture comply with principles.md?
- Has Jonathan approved the design before the task starts?
- Which files is this task explicitly allowed to modify? All others are read-only.
- Are any protected files (bugs.md, charlie.md, devlog.md, followups.md) being touched? If so, is that explicitly intended?

The file-scoping rule should be embedded in every Claude Code task prompt going forward: explicitly state what the task may and may not modify. Protected files like bugs.md, charlie.md, devlog.md, and followups.md should require explicit permission to modify.

**Dependencies:** BUG-003 (data-architecture.md), BUG-005 (principles.md)

**Touches:**
New file: `pre-build-checklist.md` or addition to `principles.md`. System prompt context loading. Claude Code task prompt template.

---

## BUG-007 — No active task list within a session
**Status:** Open
**Priority:** Medium
**Severity:** Medium — without this, open threads and action items get dropped mid-conversation, requiring Jonathan to re-raise them
**Blocks anything current:** No
**Rough effort:** Small
**Logged:** 2026-05-29

**Problem:**
During longer conversations, multiple tasks, open questions, and action items accumulate. There is no mechanism for Charlie to track what's been raised but not yet resolved within a session. This leads to items being dropped — for example, the /meta option 2 fix was agreed upon but not built until Jonathan re-raised it later in the conversation.

**What needs fixing:**
Charlie should maintain an internal running list of open items during a session and proactively surface any unresolved items before closing out a conversation. This could be as simple as Charlie noting at natural breakpoints: "Still open from earlier: X, Y, Z — shall we tackle those?" The implementation could be in-context tracking or a lightweight session scratchpad.

**Touches:**
`core/bot.py` or system prompt behaviour. Possibly a session state mechanism.
