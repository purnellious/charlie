# Charlie — Context Archive

Distilled context from completed topics. Each entry is the minimum useful signal extracted
via /distil when closing a topic. Loaded into Charlie's system prompt alongside charlie.md.

Fully resolved or superseded entries get moved out to `context-archive-cold.md` (not loaded
into the live system prompt) rather than left here accumulating forever — see CLAUDE.md.

---

**Email drafting preferences established — 30 July 2026**

- No comma after greeting line (e.g. "Hi Jared" not "Hi Jared,")
- No em dashes or other AI drafting hallmarks; write plainly and humanly
- Always sign off: "Kind regards," new line "Jonathan"
- These rules apply to all future email drafts

**Jared Silver / watch sale — 30 July 2026**

- Ongoing sale of two watches via Stephen Silver Fine Jewelry (Jared Silver, President)
- Jared needs box and papers for both watches before finalising sales; waiting on MB&F service quote
- Jonathan replied 30 July confirming Riccardo is arranging boxes/papers and asking to be kept in loop on the MB&F quote

**Z3 Consultants inspection — 30 July 2026**

- Z3 Consultants performs recurring backflow certification inspections at 250 Maloney Road 21794
- Most recent inspection report: 3 June 2026 (PDF attachment: "250 Maloney Road 21794 F"), sent via QuickBooks

---

**Charlie email integration build-out — 30 Jul 2026**
- Email monitor (polling `jonathan@ts.org` every 2 minutes, Haiku triage, batched digests to 📧 Email topic) was already live; Jonathan extended it significantly on this day (search, read, archive, mark read/unread, send, delete)
- A couple of remaining bugs outstanding — Jonathan plans to address them soon, no urgency

---

**Email workflow & preferences — 31 July 2026**

- Jonathan forwards invoices/receipts to finance@ts.org as a matter of course; now that forward capability is live (BUG-023 resolved), Charlie should handle these
- Email drafting style confirmed: no comma after greeting, plain language, sign off "Kind regards," / new line "Jonathan", use "Hi everyone" / "Hi you two" / "Hi [Name] and [Name]" — not "Hi both"
- Two open items: (1) Verify tomorrow that the Extra Space Storage receipt forward to finance@ts.org actually sent (flagged as uncertain); (2) Ameena Chopdat (#19) still needs a response with income data for MyMonero, Monero Distribution Co, and DUST Technologies (CIPC annual return, due August 2026)
- BUG-024 (CC field not read correctly) and BUG-025 ("Sent to None" display bug) logged and open

---

**Extra Space Storage receipts & email forwarding bugs — 1 August 2026**

- Two Extra Space Storage receipts (Unit 1012 — $1,142.00 and Unit 1072 — $644.00) from 31 July 2026 still need to be manually forwarded to finance@ts.org; Charlie's forwarding tool is broken (BUG-026: can't selectively forward within a grouped thread; BUG-027: forward constructs a fresh email rather than a true forward, corrupting formatting and thread context)
- Ameena Chopdat email re: 2026 CIPC AR & BO submissions (due August 2026) for MyMonero, Monero Distribution Co, and DUST Technologies is pending — financial data sits with Zaheer (CFO), not Jonathan
- Reminder set for Tuesday morning: if Zaheer hasn't responded to Ameena by close of business Monday, prompt Jonathan to follow up

---

**Email management session — 11 August 2026**

- **Standing rule confirmed:** All invoices and receipts should be forwarded to finance@ts.org with a brief covering note ("Hi guys / Here's the latest invoice/receipt from [sender] / Kind regards, Jonathan"), including all attachments and original email content. Jonathan had to remind Charlie of this mid-session — it is now in email-preferences.md.
- **Bug logged — BUG-029:** Charlie consistently fails to read the most recent message in a thread, falling back to older messages as if they are the latest. Affected the 250 Maloney thread at least twice.
- **Bug logged — BUG-030:** Batch deletes were falsely reporting "Deleted" when emails remained in the inbox. A fix was built and deployed mid-session; subsequent batch deletes appeared to work. Root cause may be stale search index rather than failed deletion.
- **250 Maloney Road (property sale):** Erika Odle managing the sale. Current strategy: $20k+ concession, buyer's agent commission raised to 3%, asking price reduced to $780k. Jonathan agreed to proceed on these terms but noted a more aggressive approach would be needed if no offer by end of August. Moisture/humidity damage reported (buckling floors). Ball is in Erika's court.
- **Watch sale (Jared Silver / MB&F):** MB&F service ticket submitted, awaiting quote. Boxes and papers still needed from Riccardo — Jonathan chased Jared on this.
- **XTM token transfer (Preston Byrne / Coinbase):** 26,315,789 XTM to be sent to wallet 0x0D81B1f99d65Ac1d34FFc3BFe0D206a9c7B25778. Emma Northcott (Coinbase) awaiting confirmation of timing. Preston has relayed to Riccardo. Ball with Riccardo.
- **Kala Holdings — Form D:** Filed and accepted by SEC (Aug 5, 2026, accession 0002147376-26-000001).
- **Kala Holdings — cap table:** Sarah Campbell (Fidelity) says no need to start fresh; has instructions. Unresolved.
- **Kala Holdings — Ellerra meeting:** Accepted 1pm EST Tuesday 11 August with Cary Barnhard.
- **Payroll (Kuhn Partner / Saskia):** August 2026 payroll received. €2,500 net per employee; €3,513.62 to social insurance by Aug 27; €954.32 to tax office by Sep 10. Marked as read — unclear if actioned.
- **Google Ads dispute:** $5,000 unauthorised charge from May 21, 2026 on card ending 0655 (used for GSuite, not Google Ads). Google confirmed unauthorised; advised to contact bank for chargeback. Unresolved.

---

**250 Maloney property (Erika Odle) — 13 August 2026**
- Property at 250 Maloney cannot be subdivided per the zoning administrator
- Erika reports multiple showings and an offer expected soon
- Jonathan checked in on 13 August; ball is in Erika's court

**Kala Holdings — ongoing threads — 13 August 2026**
- Edge & Node Ventures NDA fully signed (all parties, ~6 Aug)
- Thomas Pavey requesting due diligence docs: certificate of incorporation, bylaws, cap table, directors list, litigation confirmation, employment agreements, insurance policies
- Funding round involves Peter Maxakov (Dubai), Deep Ventures, and Justin Stanford (4Di) in addition to Thomas Pavey
- Sarah Campbell (Fidelity) can fix the cap table without starting fresh — needs a response

**Extra Space Storage — 13 August 2026**
- Unit 1072, Fishkill NY — payment of $541.00 due 18 August 2026
- Payment reminder forwarded to finance@ts.org on 13 August (previous reminder also forwarded 11 August)

**Inbox management behaviour note — 13 August 2026**
- Bug logged (BUG-036): Charlie must not state negatives as fact when search coverage was insufficient; should default to "I'm not certain, let me check" before confirming absence of something

---

**Jonathan's handover & open items — 15 Sep 2026**

- Jonathan is departing TS Group; full handover must be complete by **17 October 2026**. All corporate, property, account, and personal cleanup items are logged and tracked.
- **Twelve Sigma LLC debt resolved**; Human Agency no longer in consideration.
- **NY Bar:** Register on **1 Oct 2026** (reminder set); MPRE on **12 Nov 2026** (enrolled); UBE prep ongoing; Pro Bono hours and Skills Competency Requirement still to figure out. UCT transcripts resolved — no longer a risk to the Feb 2027 exam target.
- Jonathan wants Charlie in a tracking role on the handover — not sequencing or second-guessing, just logging completions and flagging deadline risk.
- IRS implications for Virgo Investments exit are under investigation; Jonathan will action when he has clarity.

---
