# Charlie — Context Archive

Distilled context from completed topics. Each entry is the minimum useful signal extracted
via /distil when closing a topic. Loaded into Charlie's system prompt alongside charlie.md.

---

**News briefing system maintenance — 08 July 2026**

- Three broken RSS feeds were identified and replaced: Reuters → Al Jazeera, News24 → IOL, Daily Maverick URL fixed
- Updated source list: World News (BBC, Guardian, Al Jazeera), South Africa (Daily Maverick, IOL), Crypto Regulation (CoinDesk, CoinTelegraph), AI Regulation (MIT Tech Review, The Verge)
- A retry mechanism was added to the scheduled briefing: if the 12:00 run fails, it retries once at 12:05 before giving up
- Changes live in the DB; devlog updated and pushed to git

---

**NY Bar assessment & Chainlink interview — 09 Jul 2026**

- NY Bar has acknowledged receipt of UCT transcripts and is now assessing them; turnaround can be up to 6 months, which makes the February 2027 exam target tight — the submission date is the clock to watch
- Jonathan had his final Chainlink interview today (with the CEO/Founder) — outcome not yet known
- The pool permit Jonathan mentioned relates to his employer's property, held in a personal trust that Jonathan administers as part of his role — not Jonathan's own home

---

**Job search & career situation — 09 Jul 2026**

- Still in final interviews, no offers received yet. Chainlink final interview today (09 Jul); Human Agency final interview still being scheduled.
- TS Group: still technically employed, July salary uncertain — treating any continued payment as better than nothing while waiting for offers.
- Moar Labs: bank account opening blocked by employer's personal KYC issues; $50k not yet moved. No clear resolution path, just continuing to push.
- NY Bar: UCT transcripts submitted and under assessment; 6-month window flagged as tight against a February 2027 exam target.

---

**Follow-ups / to-do list status — 22 Jul 2026**

- Jonathan calls the follow-ups tracker his "to-do list" — terminology noted in charlie.md
- NY Bar: UCT docs already submitted and with the Bar; just waiting — no action needed
- Chainlink: post-interview, awaiting outcome
- Handover plan: added to follow-ups, not yet drafted — still to be worked on

---

**Job search & financial situation — 28 Jul 2026**

- Chainlink role fell through — they changed the role and did not proceed with Jonathan
- Human Agency is now the sole active job prospect; Jonathan proposed a trial period contract to the CEO, awaiting response
- TS Group July salary not yet paid but payment considered possible
- Moar Labs now has two bank accounts open (previously a blocker)
- Handover plan remains on the list but no realistic near-term trigger

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

**Ongoing situation context — 30 Jul 2026**
- Jonathan is anxious about the future; aggressively job hunting while keeping Human Agency as one thread (no CEO response yet, follow-up sent)
- TS Group July salary still unpaid
- Moar Labs $50k investment unblocked (both accounts now open)

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

**Twelve Sigma LLC debt — 13 August 2026**
- Outstanding balance confirmed at R82,874.41 (inclusive of interest and legal costs)
- Riccardo proposed offering R100k as settlement; Richard Matthews (Hanekom Attorneys) has communicated offer to Mr Coleman
- Court date: 15 September 2026; notice of set down being filed

**Extra Space Storage — 13 August 2026**
- Unit 1072, Fishkill NY — payment of $541.00 due 18 August 2026
- Payment reminder forwarded to finance@ts.org on 13 August (previous reminder also forwarded 11 August)

**Inbox management behaviour note — 13 August 2026**
- Bug logged (BUG-036): Charlie must not state negatives as fact when search coverage was insufficient; should default to "I'm not certain, let me check" before confirming absence of something

---