"""
Artist grant finder pipeline.
Five stages: Scrape → Validate (L1+L2) → Deduplicate → Validate (L3) → Categorise.
Caller passes final opportunities to grants_email.py for formatting and sending.

Sources: CaFÉ/callforentry.org, NJ State Council on the Arts,
         Jersey City Arts Council, Gmail inbox (newsletters).
"""
import imaplib
import email as email_lib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import anthropic
import requests

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent.parent / "data" / "grants.db"
MODEL = "claude-haiku-4-5-20251001"
_REQUEST_TIMEOUT = 15
_USER_AGENT = "Mozilla/5.0 (compatible; Charlie/1.0; +artist-grants-research)"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    title: str
    description: str
    url: str            # canonical source URL — dedup key
    apply_url: str      # direct apply/learn-more link (may equal url)
    deadline: Optional[str]  # ISO date string or human-readable, or None
    source: str         # e.g. "CaFÉ", "NJSCA", "JCAC", "Gmail"
    category: Optional[str] = None
    flagged: bool = False
    flag_reason: str = ""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        # Drop legacy table from initial schema if present
        conn.execute("DROP TABLE IF EXISTS seen_grants")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT    NOT NULL UNIQUE,
                title       TEXT    NOT NULL,
                deadline    TEXT,
                source      TEXT,
                category    TEXT,
                description TEXT,
                apply_link  TEXT,
                first_seen  TEXT    NOT NULL DEFAULT (date('now')),
                last_seen   TEXT    NOT NULL DEFAULT (date('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flagged (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                url        TEXT,
                title      TEXT,
                reason     TEXT,
                flagged_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
    log.info("grants.db initialised")


def _is_seen(url: str) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT id FROM opportunities WHERE url=?", (url,)).fetchone()
        return row is not None


def _mark_seen(opp: "Opportunity"):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO opportunities (url, title, deadline, source, category, description, apply_link)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                last_seen   = date('now'),
                title       = excluded.title,
                deadline    = excluded.deadline,
                category    = excluded.category,
                description = excluded.description
        """, (opp.url, opp.title, opp.deadline, opp.source, opp.category,
              opp.description[:500] if opp.description else None,
              opp.apply_url))


def _save_flagged(opp: "Opportunity"):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO flagged (url, title, reason) VALUES (?, ?, ?)",
            (opp.url, opp.title, opp.flag_reason),
        )


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _safe_get(url: str) -> Optional[requests.Response]:
    """GET a URL; return Response or None on any error."""
    try:
        r = requests.get(
            url,
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            allow_redirects=True,
        )
        return r
    except Exception as e:
        log.warning(f"GET {url} failed: {e}")
        return None


def _html_to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Date extraction helper
# ---------------------------------------------------------------------------

_DATE_PATTERNS = [
    (r"(January|February|March|April|May|June|July|August|September|October|November|December)"
     r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}", "%B %d %Y"),
    (r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}", "%b %d %Y"),
    (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    (r"\d{1,2}/\d{1,2}/\d{4}", "%m/%d/%Y"),
]


def _extract_date_from_text(text: str) -> Optional[str]:
    """Try to extract a deadline from text. Returns ISO date string or None."""
    for pattern, fmt in _DATE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        raw = re.sub(r"(st|nd|rd|th)", "", match.group(), flags=re.IGNORECASE)
        raw = re.sub(r",", "", raw).strip()
        for try_fmt in [fmt, fmt.replace("%B", "%b"), fmt.replace("%b", "%B")]:
            try:
                parsed = datetime.strptime(raw, try_fmt)
                return parsed.date().isoformat()
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Scraper: CaFÉ / callforentry.org
# ---------------------------------------------------------------------------

def scrape_cafe() -> list:
    """Scrape CaFÉ open calls listing for visual art opportunities."""
    url = "https://www.callforentry.org/festivals_unique_info.php"
    log.info(f"Scraping CaFÉ: {url}")

    r = _safe_get(url)
    if r is None or r.status_code != 200:
        log.warning(f"CaFÉ scrape failed: status {getattr(r, 'status_code', 'N/A')}")
        return []

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.error(f"CaFÉ parse error: {e}")
        return []

    opportunities = []
    seen_urls: set = set()

    # CaFÉ listing entries link to individual call detail pages
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        title = link.get_text(strip=True)

        if not title or len(title) < 8:
            continue

        # Resolve URL
        if href.startswith("http"):
            entry_url = href
        elif href.startswith("/"):
            entry_url = f"https://www.callforentry.org{href}"
        else:
            continue

        # Only follow links that look like individual call entries
        if not re.search(r"(entry|festival|call|detail|id=|view)", href, re.IGNORECASE):
            continue

        if entry_url in seen_urls or entry_url == url:
            continue
        seen_urls.add(entry_url)

        parent_text = link.parent.get_text(separator=" ") if link.parent else ""
        deadline = _extract_date_from_text(parent_text)

        opportunities.append(Opportunity(
            title=title,
            description=parent_text[:300].strip(),
            url=entry_url,
            apply_url=entry_url,
            deadline=deadline,
            source="CaFÉ",
        ))

    # Fallback: pick up heading-anchored listings if nothing found yet
    if not opportunities:
        for tag in soup.find_all(["h2", "h3", "h4"]):
            text = tag.get_text(strip=True)
            if not text or len(text) < 10 or len(text) > 200:
                continue
            nearby = tag.find("a") or (tag.parent and tag.parent.find("a"))
            if not nearby or not nearby.get("href"):
                continue
            href = nearby.get("href", "")
            if href.startswith("http"):
                entry_url = href
            elif href.startswith("/"):
                entry_url = f"https://www.callforentry.org{href}"
            else:
                continue
            if entry_url in seen_urls:
                continue
            seen_urls.add(entry_url)
            context = (tag.parent.get_text(separator=" ")[:300] if tag.parent else "")
            opportunities.append(Opportunity(
                title=text,
                description=context,
                url=entry_url,
                apply_url=entry_url,
                deadline=_extract_date_from_text(context),
                source="CaFÉ",
            ))

    log.info(f"CaFÉ: found {len(opportunities)} raw opportunities")
    return opportunities[:30]


# ---------------------------------------------------------------------------
# Scraper: NJ State Council on the Arts
# ---------------------------------------------------------------------------

def scrape_njsca() -> list:
    """Scrape NJ State Council on the Arts grants page."""
    url = "https://www.nj.gov/state/njsca/dos_njsca_grants.html"
    log.info(f"Scraping NJSCA: {url}")

    r = _safe_get(url)
    if r is None or r.status_code != 200:
        log.warning(f"NJSCA scrape failed: status {getattr(r, 'status_code', 'N/A')}")
        return []

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.error(f"NJSCA parse error: {e}")
        return []

    opportunities = []
    seen_urls: set = set()

    # NJ.gov: main content area, look for headings + nearby links
    content = (
        soup.find("div", id=re.compile(r"content|main", re.I))
        or soup.find("div", class_=re.compile(r"content|main", re.I))
        or soup.find("main")
        or soup
    )

    for heading in content.find_all(["h2", "h3", "h4", "strong"]):
        title = heading.get_text(strip=True)
        if not title or len(title) < 10 or len(title) > 250:
            continue
        if any(s in title.lower() for s in ["navigation", "menu", "search", "footer", "header"]):
            continue

        parent = heading.parent
        context = parent.get_text(separator=" ")[:500] if parent else ""

        link = heading.find("a") or (parent and parent.find("a", href=True))
        if link and link.get("href"):
            href = link["href"]
            if href.startswith("http"):
                entry_url = href
            elif href.startswith("/"):
                entry_url = f"https://www.nj.gov{href}"
            else:
                entry_url = url
        else:
            entry_url = url

        if entry_url in seen_urls:
            continue
        seen_urls.add(entry_url)

        opportunities.append(Opportunity(
            title=title,
            description=context[:300].strip(),
            url=entry_url,
            apply_url=entry_url,
            deadline=_extract_date_from_text(context),
            source="NJSCA",
        ))

    # Also collect explicitly grant-labelled links
    grant_keywords = ["grant", "fund", "award", "fellowship", "residency", "commission",
                      "open call", "opportunity", "deadline", "application"]
    for link in content.find_all("a", href=True):
        text = link.get_text(strip=True)
        href = link.get("href", "")
        if not text or len(text) < 8:
            continue
        parent_text = link.parent.get_text(separator=" ") if link.parent else text
        if not any(kw in (text + parent_text).lower() for kw in grant_keywords):
            continue

        if href.startswith("http"):
            entry_url = href
        elif href.startswith("/"):
            entry_url = f"https://www.nj.gov{href}"
        else:
            continue

        if entry_url in seen_urls:
            continue
        seen_urls.add(entry_url)

        context = parent_text[:300].strip()
        opportunities.append(Opportunity(
            title=text,
            description=context,
            url=entry_url,
            apply_url=entry_url,
            deadline=_extract_date_from_text(context),
            source="NJSCA",
        ))

    log.info(f"NJSCA: found {len(opportunities)} raw opportunities")
    return opportunities[:20]


# ---------------------------------------------------------------------------
# Scraper: Jersey City Arts Council
# ---------------------------------------------------------------------------

def scrape_jcac() -> list:
    """Scrape Jersey City Arts Council for open calls and grants."""
    base_url = "https://jerseycityartscouncil.org"
    log.info(f"Scraping JCAC: {base_url}")

    pages_to_try = [
        base_url,
        f"{base_url}/opportunities",
        f"{base_url}/grants",
        f"{base_url}/open-calls",
        f"{base_url}/resources",
        f"{base_url}/apply",
    ]

    opportunities = []
    seen_urls: set = set()
    grant_keywords = ["grant", "fund", "award", "fellowship", "residency", "commission",
                      "open call", "apply", "opportunity", "deadline", "call for"]
    skip_words = ["home", "about", "contact", "facebook", "instagram", "twitter",
                  "youtube", "linkedin", "donate", "newsletter", "subscribe"]

    for page_url in pages_to_try:
        r = _safe_get(page_url)
        if r is None or r.status_code != 200:
            continue

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            log.error(f"JCAC parse error on {page_url}: {e}")
            continue

        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True)
            href = link.get("href", "")

            if not text or len(text) < 8:
                continue
            if any(s in text.lower() for s in skip_words):
                continue

            parent_text = link.parent.get_text(separator=" ") if link.parent else text
            if not any(kw in (text + parent_text).lower() for kw in grant_keywords):
                continue

            if href.startswith("http"):
                entry_url = href
            elif href.startswith("/"):
                entry_url = f"{base_url}{href}"
            else:
                continue

            if entry_url in seen_urls:
                continue
            seen_urls.add(entry_url)

            context = parent_text[:300].strip()
            opportunities.append(Opportunity(
                title=text,
                description=context,
                url=entry_url,
                apply_url=entry_url,
                deadline=_extract_date_from_text(context),
                source="JCAC",
            ))

    log.info(f"JCAC: found {len(opportunities)} raw opportunities")
    return opportunities[:20]


# ---------------------------------------------------------------------------
# Gmail inbox polling
# ---------------------------------------------------------------------------

def poll_gmail_inbox() -> list:
    """
    Poll Gmail inbox for unread emails that may contain grant opportunities.
    Email content is processed in memory only — never written to disk.
    """
    addr = os.environ.get("GRANT_GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GRANT_GMAIL_PASSWORD", "").strip()

    if not addr or not password:
        log.warning("Gmail credentials not set — skipping inbox poll")
        return []

    log.info(f"Polling Gmail inbox: {addr}")

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(addr, password)
        mail.select("INBOX")

        _, nums_data = mail.search(None, "UNSEEN")
        nums = nums_data[0].split() if nums_data[0] else []

        if not nums:
            log.info("Gmail: no unread messages")
            mail.logout()
            return []

        log.info(f"Gmail: {len(nums)} unread messages")

        email_texts = []
        for num in nums[:20]:
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                msg = email_lib.message_from_bytes(msg_data[0][1])
                body = _extract_email_body(msg)
                if body and len(body.strip()) > 50:
                    subject = _decode_header_str(msg.get("Subject", ""))
                    email_texts.append(f"Subject: {subject}\n\n{body[:3000]}")
                mail.store(num, "+FLAGS", "\\Seen")
            except Exception as e:
                log.warning(f"Gmail: error processing message {num}: {e}")

        mail.logout()

        if not email_texts:
            return []

        return _extract_grants_from_emails(email_texts)

    except Exception as e:
        log.error(f"Gmail inbox poll failed: {e}")
        return []


def _decode_header_str(value: str) -> str:
    try:
        parts = email_lib.header.decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(str(part))
        return " ".join(decoded)
    except Exception:
        return str(value)


def _extract_email_body(msg) -> str:
    """Extract plain text from an email.Message object."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(part.get_payload(decode=True).decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass
    return "\n".join(parts)


def _extract_grants_from_emails(email_texts: list) -> list:
    """Use Claude Haiku to identify grant/open call mentions in email content."""
    combined = "\n\n---EMAIL---\n\n".join(email_texts)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = (
        "You are reviewing email newsletters for artist grant and open call opportunities.\n\n"
        f"Emails:\n{combined}\n\n"
        "Extract any grants, open calls, residencies, fellowships, or funding opportunities "
        "for visual artists. For each one found, output a JSON array entry:\n"
        '{"title": str, "description": str (2-3 sentences), "url": str (apply URL or ""), '
        '"deadline": str (ISO date e.g. "2026-08-15", or "")}\n\n'
        "Output ONLY a valid JSON array. If nothing found, output []."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = next((b.text.strip() for b in response.content if hasattr(b, "text")), "[]")
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return []
        items = json.loads(match.group())
        results = []
        for item in items:
            if not item.get("title"):
                continue
            results.append(Opportunity(
                title=item.get("title", ""),
                description=item.get("description", ""),
                url=item.get("url", ""),
                apply_url=item.get("url", ""),
                deadline=item.get("deadline") or None,
                source="Gmail",
            ))
        log.info(f"Gmail: extracted {len(results)} opportunities via AI")
        return results
    except Exception as e:
        log.error(f"Gmail AI extraction failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Validation — L1: Link check
# ---------------------------------------------------------------------------

def validate_L1(opportunities: list) -> tuple:
    """
    HTTP GET each URL. Flag entries with no URL, connection errors, or 4xx/5xx responses.
    Returns (valid, flagged).
    """
    valid, flagged = [], []

    for opp in opportunities:
        if not opp.url:
            opp.flagged = True
            opp.flag_reason = "L1: No URL"
            flagged.append(opp)
            log.info(f"L1 flagged (no URL): {opp.title!r}")
            continue

        r = _safe_get(opp.url)
        if r is None:
            opp.flagged = True
            opp.flag_reason = "L1: Connection error"
            flagged.append(opp)
            log.info(f"L1 flagged (connection error): {opp.title!r}")
        elif r.status_code >= 400:
            opp.flagged = True
            opp.flag_reason = f"L1: HTTP {r.status_code}"
            flagged.append(opp)
            log.info(f"L1 flagged (HTTP {r.status_code}): {opp.title!r}")
        else:
            valid.append(opp)

    log.info(f"L1: {len(valid)} valid, {len(flagged)} flagged")
    return valid, flagged


# ---------------------------------------------------------------------------
# Validation — L2: Deadline sanity
# ---------------------------------------------------------------------------

def validate_L2(opportunities: list) -> tuple:
    """
    Flag entries whose deadline is in the past or more than 2 years in the future.
    Entries with no parseable deadline pass through.
    Returns (valid, flagged).
    """
    today = date.today()
    two_years_out = today + timedelta(days=730)
    valid, flagged = [], []

    for opp in opportunities:
        if not opp.deadline:
            valid.append(opp)
            continue

        parsed = None
        for fmt in ["%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%m/%d/%Y"]:
            try:
                parsed = datetime.strptime(opp.deadline, fmt).date()
                break
            except ValueError:
                continue

        if parsed is None:
            valid.append(opp)
            continue

        if parsed < today:
            opp.flagged = True
            opp.flag_reason = f"L2: Deadline in the past ({opp.deadline})"
            flagged.append(opp)
            log.info(f"L2 flagged (past deadline {opp.deadline}): {opp.title!r}")
        elif parsed > two_years_out:
            opp.flagged = True
            opp.flag_reason = f"L2: Deadline too far in future ({opp.deadline})"
            flagged.append(opp)
            log.info(f"L2 flagged (far future {opp.deadline}): {opp.title!r}")
        else:
            valid.append(opp)

    log.info(f"L2: {len(valid)} valid, {len(flagged)} flagged")
    return valid, flagged


# ---------------------------------------------------------------------------
# Validation — L3: AI cross-check
# ---------------------------------------------------------------------------

def validate_L3(opportunities: list) -> tuple:
    """
    For each opportunity, fetch its source URL and ask Claude Haiku to verify
    the scraped title, description, and deadline against the live page content.
    Returns (verified, flagged).
    """
    if not opportunities:
        return [], []

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    verified, flagged = [], []

    for opp in opportunities:
        try:
            r = _safe_get(opp.url)
            if r is None or r.status_code >= 400:
                opp.flagged = True
                opp.flag_reason = f"L3: Could not fetch source page (HTTP {getattr(r, 'status_code', 'N/A')})"
                flagged.append(opp)
                log.info(f"L3 flagged (fetch failed): {opp.title!r}")
                continue

            page_text = _html_to_text(r.text)[:4000]

            prompt = (
                "You are verifying an artist grant/open call listing against its live source page.\n\n"
                f"Scraped data:\n"
                f"Title: {opp.title}\n"
                f"Description: {opp.description[:400]}\n"
                f"Deadline: {opp.deadline or 'not found'}\n"
                f"Apply URL: {opp.apply_url}\n\n"
                f"Source page content (truncated to 4000 chars):\n{page_text}\n\n"
                "Does this source page confirm that:\n"
                "1. The title matches or closely relates to content on the page?\n"
                "2. The description is broadly accurate?\n"
                "3. This is a real open call, grant, residency, or funding opportunity for visual artists?\n\n"
                "Reply with exactly one word on the first line: VERIFIED or MISMATCH\n"
                "Then one sentence explaining why."
            )

            response = client.messages.create(
                model=MODEL,
                max_tokens=120,
                messages=[{"role": "user", "content": prompt}],
            )

            answer = next((b.text.strip() for b in response.content if hasattr(b, "text")), "")
            lines = answer.split("\n", 1)
            verdict = lines[0].strip().upper()
            reason = lines[1].strip() if len(lines) > 1 else ""

            if "VERIFIED" in verdict:
                verified.append(opp)
            else:
                opp.flagged = True
                opp.flag_reason = f"L3: {reason or 'AI could not verify'}"
                flagged.append(opp)
                log.info(f"L3 flagged: {opp.title!r} — {opp.flag_reason}")

        except Exception as e:
            log.error(f"L3 check error for {opp.title!r}: {e}")
            # On our own error, pass through rather than silently drop
            verified.append(opp)

    log.info(f"L3: {len(verified)} verified, {len(flagged)} flagged")
    return verified, flagged


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(opportunities: list) -> tuple:
    """
    Compare against seen_grants table. Returns (new, already_seen).
    Updates last_seen for already-seen entries.
    """
    new, seen = [], []

    for opp in opportunities:
        if _is_seen(opp.url):
            with _conn() as conn:
                conn.execute(
                    "UPDATE opportunities SET last_seen=date('now') WHERE url=?",
                    (opp.url,),
                )
            seen.append(opp)
        else:
            new.append(opp)

    log.info(f"Dedup: {len(new)} new, {len(seen)} already seen")
    return new, seen


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------

CATEGORIES = [
    "Local / Municipal",
    "State-level",
    "Open Calls",
    "National Grants",
]


def categorize(opportunities: list) -> list:
    """
    Use Claude Haiku to assign each opportunity to one of 4 categories.
    All opportunities are batched into a single API call to minimise cost.
    """
    if not opportunities:
        return []

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    items_text = "\n".join(
        f"{i+1}. Title: {opp.title}\n   Description: {opp.description[:250]}\n   Source: {opp.source}"
        for i, opp in enumerate(opportunities)
    )

    prompt = (
        "Categorise each artist grant/open call into exactly one category:\n"
        "1. Local / Municipal — Jersey City, Hudson County, or other local/municipal programmes\n"
        "2. State-level — New Jersey state-wide opportunities\n"
        "3. Open Calls — exhibitions, residencies, juried shows at any geography\n"
        "4. National Grants — open to US artists nationally regardless of location\n\n"
        f"Opportunities:\n{items_text}\n\n"
        "Output one line per opportunity: [NUMBER]: [CATEGORY NAME]\n"
        "Use exact category names as listed above. Nothing else."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = next((b.text.strip() for b in response.content if hasattr(b, "text")), "")

        category_map: dict = {}
        for line in raw.split("\n"):
            m = re.match(r"^(\d+)[.:\s]+(.+)", line.strip())
            if not m:
                continue
            idx = int(m.group(1)) - 1
            cat_text = m.group(2).strip()
            for known in CATEGORIES:
                if known.lower() in cat_text.lower() or cat_text.lower() in known.lower():
                    category_map[idx] = known
                    break
            else:
                category_map[idx] = "Open Calls"

        for i, opp in enumerate(opportunities):
            opp.category = category_map.get(i, "Open Calls")

        log.info(f"Categorised {len(opportunities)} opportunities")

    except Exception as e:
        log.error(f"Categorisation failed: {e}")
        for opp in opportunities:
            opp.category = "Open Calls"

    return opportunities


# ---------------------------------------------------------------------------
# Scrape all sources
# ---------------------------------------------------------------------------

def scrape_all() -> list:
    """Run all scrapers and return combined raw opportunities."""
    all_opps: list = []

    for name, fn in [("CaFÉ", scrape_cafe), ("NJSCA", scrape_njsca), ("JCAC", scrape_jcac)]:
        try:
            results = fn()
            all_opps.extend(results)
        except Exception as e:
            log.error(f"Scraper {name} failed: {e}")

    try:
        gmail_results = poll_gmail_inbox()
        all_opps.extend(gmail_results)
    except Exception as e:
        log.error(f"Gmail poll failed: {e}")

    log.info(f"Total scraped: {len(all_opps)}")
    return all_opps


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_grants_pipeline(dry_run: bool = False) -> list:
    """
    Run the full artist grant finder pipeline.
    Returns list of new verified opportunities (dicts with all fields).
    If dry_run=True, does not write to DB.

    Stages:
        1. Scrape — fetch from all sources
        2. Validate L1 — link check
        3. Validate L2 — deadline sanity
        4. Deduplicate — skip previously seen URLs
        5. Validate L3 — AI cross-check against live page
        6. Categorise — assign to one of 4 buckets
    """
    init_db()
    log.info("=== Grant Finder Pipeline START ===")

    # Stage 1: Scrape
    all_opps = scrape_all()
    log.info(f"Stage 1 (Scrape): {len(all_opps)} total")

    # Stage 2: L1 + L2 validation
    valid_L1, flagged_L1 = validate_L1(all_opps)
    valid_L2, flagged_L2 = validate_L2(valid_L1)
    log.info(f"Stage 2 (Validate): {len(valid_L2)} pass, {len(flagged_L1)+len(flagged_L2)} flagged")

    # Stage 3: Deduplicate (against DB)
    new_opps, seen_opps = deduplicate(valid_L2)
    log.info(f"Stage 3 (Dedup): {len(new_opps)} new, {len(seen_opps)} already seen")

    # Stage 4: L3 AI cross-check (new only — saves Haiku calls on already-seen entries)
    verified, flagged_L3 = validate_L3(new_opps)
    log.info(f"Stage 4 (L3 AI): {len(verified)} verified, {len(flagged_L3)} flagged")

    # Stage 5: Categorise
    final_opps = categorize(verified)

    # Persist results (skip in dry run)
    if not dry_run:
        for opp in final_opps:
            _mark_seen(opp)
        log.info(f"Saved {len(final_opps)} opportunities to grants.db")
        for opp in flagged_L1 + flagged_L2 + flagged_L3:
            _save_flagged(opp)
        log.info(f"Saved {len(flagged_L1)+len(flagged_L2)+len(flagged_L3)} flagged entries to grants.db")
    else:
        log.info("[DRY RUN] Skipped writing to grants.db")

    log.info(f"=== Grant Finder Pipeline DONE === scraped={len(all_opps)} "
             f"passed_L1L2={len(valid_L2)} new={len(new_opps)} "
             f"verified={len(verified)} final={len(final_opps)}")

    return [
        {
            "title":       opp.title,
            "url":         opp.url,
            "deadline":    opp.deadline,
            "source":      opp.source,
            "category":    opp.category,
            "description": opp.description,
            "apply_link":  opp.apply_url,
        }
        for opp in final_opps
    ]
