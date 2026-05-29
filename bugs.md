# Charlie — Bug List

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
