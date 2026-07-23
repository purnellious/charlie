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
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

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
    """
    CaFÉ / callforentry.org open calls.

    Tries each endpoint in order and returns as soon as one yields results:
      1. Main listing page  — Next.js app; parses __NEXT_DATA__ JSON if present,
         then falls back to generic HTML element search.
      2. festivals_unique_info.php — historically a JSON API; now returns a login
         page, but we probe it anyway in case the endpoint is restored.
      3. RSS feed (callforentry.org/rss.php) — currently 404, but tried as fallback.

    If nothing yields usable data, logs a clear warning and returns [].
    Does NOT raise — the pipeline continues without CaFÉ entries.
    """
    from bs4 import BeautifulSoup

    attempts = [
        "https://artist.callforentry.org/festivals.php?reset=1&apply=yes",
        "https://artist.callforentry.org/festivals_unique_info.php",
        "https://www.callforentry.org/rss.php",
    ]

    for url in attempts:
        log.info(f"CaFÉ: fetching {url}")
        r = _safe_get(url)
        if r is None or r.status_code != 200:
            log.info(f"CaFÉ: {url} → status {getattr(r, 'status_code', 'N/A')}, trying next")
            continue

        # ── RSS / Atom ─────────────────────────────────────────────────────
        if any(sig in r.text[:300] for sig in ("<?xml", "<rss", "<feed")):
            try:
                import feedparser
                feed = feedparser.parse(r.text)
                results = []
                for entry in feed.entries[:30]:
                    title = entry.get("title", "").strip()
                    link_url = entry.get("link", "")
                    if not title or not link_url:
                        continue
                    summary = entry.get("summary", "") or entry.get("description", "")
                    results.append(Opportunity(
                        title=title,
                        description=_html_to_text(summary)[:300],
                        url=link_url,
                        apply_url=link_url,
                        deadline=_extract_date_from_text(summary),
                        source="CaFÉ",
                    ))
                if results:
                    log.info(f"CaFÉ: {len(results)} entries from RSS at {url}")
                    return results[:30]
            except Exception as e:
                log.debug(f"CaFÉ: RSS/feedparser parse failed: {e}")
            continue

        # ── JSON API response ───────────────────────────────────────────────
        ct = r.headers.get("content-type", "")
        first_char = r.text.strip()[:1]
        if "json" in ct or first_char in ("{", "["):
            try:
                data = r.json()
                raw_items = data if isinstance(data, list) else (
                    data.get("festivals") or data.get("calls") or
                    data.get("items") or data.get("results") or []
                )
                results = []
                for item in raw_items[:30]:
                    if not isinstance(item, dict):
                        continue
                    title = (item.get("title") or item.get("name") or "").strip()
                    if not title:
                        continue
                    item_url = item.get("url") or item.get("link") or item.get("apply_url") or url
                    if item_url and not item_url.startswith("http"):
                        item_url = f"https://artist.callforentry.org{item_url}"
                    results.append(Opportunity(
                        title=title,
                        description=str(item.get("description") or item.get("summary") or "")[:300],
                        url=item_url,
                        apply_url=item_url,
                        deadline=item.get("deadline") or item.get("close_date") or None,
                        source="CaFÉ",
                    ))
                if results:
                    log.info(f"CaFÉ: {len(results)} entries from JSON API at {url}")
                    return results[:30]
                log.debug(f"CaFÉ: JSON response from {url} contained no usable items")
            except Exception as e:
                log.debug(f"CaFÉ: JSON parse failed for {url}: {e}")
            continue

        # ── HTML — try Next.js __NEXT_DATA__ first ─────────────────────────
        try:
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            log.error(f"CaFÉ: BS4 parse error on {url}: {e}")
            continue

        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script and next_script.string:
            try:
                data = json.loads(next_script.string)
                props = data.get("props", {}).get("pageProps", {})
                raw_items = (
                    props.get("festivals") or props.get("calls") or
                    props.get("items") or props.get("opportunities") or []
                )
                results = []
                for item in raw_items[:30]:
                    if not isinstance(item, dict):
                        continue
                    title = (item.get("title") or item.get("name") or "").strip()
                    if not title:
                        continue
                    item_url = item.get("url") or item.get("link") or ""
                    if item_url and not item_url.startswith("http"):
                        item_url = f"https://artist.callforentry.org{item_url}"
                    results.append(Opportunity(
                        title=title,
                        description=str(item.get("description") or "")[:300],
                        url=item_url or url,
                        apply_url=item_url or url,
                        deadline=item.get("deadline") or item.get("close_date") or None,
                        source="CaFÉ",
                    ))
                if results:
                    log.info(f"CaFÉ: {len(results)} entries from __NEXT_DATA__ at {url}")
                    return results[:30]
                log.debug("CaFÉ: __NEXT_DATA__ present but pageProps contains no festival items")
            except Exception as e:
                log.debug(f"CaFÉ: __NEXT_DATA__ parse failed: {e}")

        # ── HTML — generic listing element search ──────────────────────────
        entries = (
            soup.find_all("article") or
            soup.find_all("div", class_=re.compile(r"festival|call|opportunity|listing|card", re.I))
        )
        seen_hrefs: set = set()
        results = []
        for entry in entries[:30]:
            a_tag = entry.find("a", href=True)
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True) or entry.get_text(strip=True)[:80]
            if not title or len(title) < 8:
                continue
            href = a_tag["href"]
            if href.startswith("http"):
                entry_url = href
            elif href.startswith("/"):
                entry_url = f"https://artist.callforentry.org{href}"
            else:
                continue
            if entry_url in seen_hrefs:
                continue
            seen_hrefs.add(entry_url)
            context = entry.get_text(separator=" ")
            results.append(Opportunity(
                title=title,
                description=context[:300],
                url=entry_url,
                apply_url=entry_url,
                deadline=_extract_date_from_text(context),
                source="CaFÉ",
            ))
        if results:
            log.info(f"CaFÉ: {len(results)} entries via HTML parsing of {url}")
            return results[:30]

    log.warning(
        "CaFÉ: all endpoints returned no usable data. "
        "artist.callforentry.org is a JS-rendered Next.js application — "
        "the listing page requires JavaScript execution to populate. "
        "The festivals_unique_info.php API requires authentication. "
        "The RSS feed at callforentry.org/rss.php returned 404. "
        "Returning empty for this run — no CaFÉ entries will appear in the email."
    )
    return []


# ---------------------------------------------------------------------------
# Scraper: NJ State Council on the Arts
# ---------------------------------------------------------------------------

def scrape_njsca() -> list:
    """
    Scrape NJ State Council on the Arts.

    grant-programs.shtml — primary source. NJ.gov uses Bootstrap cards:
      .card.my-3 > .card-header > h3  →  grant programme name
      .card.my-3 > .card-body > p     →  description and inline deadline dates

    announcements-opportunities.shtml — secondary source, but this page is
    primarily a newsletter-signup wrapper; it holds very few concrete links.
    We fetch it anyway and extract any grant-relevant hrefs we find.

    Canonical URL for each card-based entry uses a #slug anchor so dedup
    treats each programme as a distinct entry while L1 still resolves (the
    HTTP GET hits the base page, ignoring the fragment).
    """
    from bs4 import BeautifulSoup

    GRANT_KEYWORDS = ["grant", "fund", "award", "fellowship", "residency", "commission",
                      "open call", "opportunity", "application", "artist"]
    SKIP_HREF_PARTS = ("mailto:", "sign-up", "signup", "youtu", "twitter",
                       "facebook", "constantcontact", "r20.", "visitor.")

    opportunities = []
    seen_urls: set = set()

    # ── Page 1: grant-programs.shtml ───────────────────────────────────────
    grants_url = "https://www.nj.gov/state/njsca/grant-programs.shtml"
    log.info(f"Scraping NJSCA grants page: {grants_url}")
    r = _safe_get(grants_url)
    if r is None or r.status_code != 200:
        log.warning(f"NJSCA grants page: status {getattr(r, 'status_code', 'N/A')}")
    else:
        try:
            soup = BeautifulSoup(r.text, "html.parser")

            # NJ.gov has multiple col-xl-9 columns (header image, breadcrumb, title,
            # main content). The main content column is always the last one with cards.
            content = None
            for div in reversed(soup.find_all("div", class_="col-xl-9")):
                if div.find("div", class_="card"):
                    content = div
                    break
            if not content:
                content = soup.find("main") or soup

            for card in content.find_all("div", class_="card"):
                header_div = card.find("div", class_="card-header")
                if not header_div:
                    continue
                heading = header_div.find(["h2", "h3", "h4"]) or header_div.find(["strong", "b"])
                if not heading:
                    continue
                title = heading.get_text(strip=True)
                if not title or len(title) < 5:
                    continue

                body_div = card.find("div", class_="card-body")
                if body_div:
                    body_text = body_div.get_text(separator=" ", strip=True)
                    description = body_text[:400]
                    deadline = _extract_date_from_text(body_text)
                    # Prefer the first non-social, non-newsletter link as apply_url
                    apply_url = grants_url
                    for a_tag in body_div.find_all("a", href=True):
                        href = a_tag["href"]
                        if any(skip in href for skip in SKIP_HREF_PARTS):
                            continue
                        if href.startswith("http"):
                            apply_url = href
                            break
                        elif href.startswith("/"):
                            apply_url = f"https://www.nj.gov{href}"
                            break
                else:
                    body_text = ""
                    description = ""
                    deadline = None
                    apply_url = grants_url

                # Anchor-based URL keeps each programme distinct for dedup
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                entry_url = f"{grants_url}#{slug}"
                if entry_url in seen_urls:
                    continue
                seen_urls.add(entry_url)

                opportunities.append(Opportunity(
                    title=title,
                    description=description,
                    url=entry_url,
                    apply_url=apply_url,
                    deadline=deadline,
                    source="NJSCA",
                ))

        except Exception as e:
            log.error(f"NJSCA grants page parse error: {e}")

    # ── Page 2: announcements-opportunities.shtml ──────────────────────────
    opp_url = "https://www.nj.gov/state/njsca/announcements-opportunities.shtml"
    log.info(f"Scraping NJSCA opportunities page: {opp_url}")
    r = _safe_get(opp_url)
    if r is None or r.status_code != 200:
        log.warning(f"NJSCA opportunities page: status {getattr(r, 'status_code', 'N/A')}")
    else:
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            # Same multi-column layout as grants page — take the last col-xl-9
            col9_divs = soup.find_all("div", class_="col-xl-9")
            content = col9_divs[-1] if col9_divs else (soup.find("main") or soup)
            for a_tag in content.find_all("a", href=True):
                text = a_tag.get_text(strip=True)
                href = a_tag["href"]
                if not text or len(text) < 10:
                    continue
                if any(skip in href for skip in SKIP_HREF_PARTS):
                    continue
                parent_text = a_tag.parent.get_text(separator=" ") if a_tag.parent else text
                if not any(kw in (text + parent_text).lower() for kw in GRANT_KEYWORDS):
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
                opportunities.append(Opportunity(
                    title=text,
                    description=parent_text[:300].strip(),
                    url=entry_url,
                    apply_url=entry_url,
                    deadline=_extract_date_from_text(parent_text),
                    source="NJSCA",
                ))
        except Exception as e:
            log.error(f"NJSCA opportunities page parse error: {e}")

    log.info(f"NJSCA: found {len(opportunities)} raw opportunities")
    return opportunities[:20]


# ---------------------------------------------------------------------------
# Scraper: Jersey City Arts Council
# ---------------------------------------------------------------------------

def scrape_jcac() -> list:
    """
    Scrape Jersey City Arts Council for open calls and grants.

    Strategy: scan four index pages for internal JCAC links, then follow each
    link, check its title for skip words, extract description from paragraphs,
    and look for deadline text.
    """
    from bs4 import BeautifulSoup

    base_url = "https://jerseycityartscouncil.org"
    SKIP_TITLES = ("recipient", "winner", "awarded", "archive", "folder", "newsletter")
    DEADLINE_RE = re.compile(
        r"(?:deadline[:\s]*|due[:\s]+|apply\s+by[:\s]+|applications?\s+due[:\s]+)",
        re.IGNORECASE,
    )

    index_pages = [
        base_url,
        f"{base_url}/grants-funding",
        f"{base_url}/opportunities",
        f"{base_url}/open-calls",
    ]

    # Stage 1 — collect internal candidate links from all index pages
    candidate_urls: set = set()
    for page_url in index_pages:
        r = _safe_get(page_url)
        if r is None:
            log.info(f"JCAC: {page_url} → connection failed, skipping")
            continue
        if r.status_code == 404:
            log.info(f"JCAC: {page_url} → 404, skipping")
            continue
        if r.status_code != 200:
            log.warning(f"JCAC: {page_url} → HTTP {r.status_code}")
            continue

        try:
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            log.error(f"JCAC parse error on {page_url}: {e}")
            continue

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue
            if href.startswith("http"):
                if not href.startswith(base_url):
                    continue  # external domain — reject
                link_url = href
            elif href.startswith("/"):
                link_url = f"{base_url}{href}"
            else:
                continue
            link_url = link_url.split("#")[0].rstrip("/")
            if link_url and link_url != base_url.rstrip("/"):
                candidate_urls.add(link_url)

    log.info(f"JCAC: {len(candidate_urls)} candidate internal links to follow")

    # Stage 2 — follow each link, check title, extract content
    opportunities = []
    seen_urls: set = set()

    for link_url in sorted(candidate_urls):
        if link_url in seen_urls:
            continue
        seen_urls.add(link_url)

        r = _safe_get(link_url)
        if r is None or r.status_code != 200:
            continue

        try:
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            log.error(f"JCAC parse error on {link_url}: {e}")
            continue

        # Skip pages whose title/h1 contains disqualifying words
        title_tag = soup.find("title")
        h1_tag = soup.find("h1")
        page_title_lower = (title_tag.get_text(strip=True) if title_tag else "").lower()
        h1_lower = (h1_tag.get_text(strip=True) if h1_tag else "").lower()
        if any(skip in page_title_lower + " " + h1_lower for skip in SKIP_TITLES):
            log.debug(f"JCAC: skipping {link_url} (skip word in title)")
            continue

        title = h1_tag.get_text(strip=True) if h1_tag else ""
        if not title and title_tag:
            title = title_tag.get_text(strip=True).split("|")[0].split("–")[0].strip()
        if not title or len(title) < 5:
            continue

        # Extract description from first substantive paragraphs
        main = (
            soup.find("main")
            or soup.find("div", class_=re.compile(r"\b(content|entry|post|page)\b", re.I))
            or soup
        )
        paragraphs = [
            p.get_text(separator=" ", strip=True)
            for p in main.find_all("p")
            if len(p.get_text(strip=True)) > 40
        ]
        description = " ".join(paragraphs[:3])[:500] if paragraphs else ""

        # Look for deadline patterns; labelled text first, then any date
        page_text = soup.get_text(separator=" ")
        deadline = None
        m = DEADLINE_RE.search(page_text)
        if m:
            deadline = _extract_date_from_text(page_text[m.start():m.start() + 80])
        if not deadline:
            deadline = _extract_date_from_text(page_text)

        opportunities.append(Opportunity(
            title=title,
            description=description,
            url=link_url,
            apply_url=link_url,
            deadline=deadline,
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
