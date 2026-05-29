# Charlie — Bug List

Open issues to be worked through over time. Add new bugs at the bottom with incrementing IDs.

---

## BUG-001 — SQLite message history grows indefinitely
**Status:** Open  
**Priority:** Medium  
**Logged:** 2026-05-29  

**Problem:**  
All messages (user and assistant) are stored in `data/charlie.db` with no expiry, cleanup, or deletion logic. The database grows forever. This means: (1) sensitive conversation data accumulates indefinitely on disk, (2) the full history is passed to the Claude API on every message in a topic, increasing cost and latency over time, and (3) it contradicts the design principle of keeping Charlie lightweight and data-minimal.

**What needs fixing:**  
Decide on and implement a retention policy — options include auto-delete after N days, topic-scoped retention only, or removing persistent history entirely and relying on Telegram's own record.

**Touches:**  
`core/db.py` (or wherever save_message / load_history live), `core/bot.py`
