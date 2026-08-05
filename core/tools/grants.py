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
from zoneinfo import ZoneInfo

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

log = logging.getLogger(__name__)


def _today() -> date:
    """
    "Today" in the app's configured TIMEZONE (scheduler.py's own env var, default UTC),
    not whatever timezone the host OS happens to be set to — date.today() would silently
    anchor deadline checks to the wrong day on a server in a different timezone, exactly
    the "is this actually still open" mistake validate_L3's date-anchoring fix exists to
    prevent, just one level removed.
    """
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "UTC"))).date()

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
    # Populated by validate_L3 from the live source page — empty until then.
    eligibility_notes: str = ""  # a restriction/priority worth surfacing upfront (may be empty)
    mediums: str = ""            # e.g. "painting, sculpture" or "All media"
    theme_summary: str = ""      # one-line theme/purpose of the call
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
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                url               TEXT    NOT NULL UNIQUE,
                title             TEXT    NOT NULL,
                deadline          TEXT,
                source            TEXT,
                category          TEXT,
                description       TEXT,
                apply_link        TEXT,
                eligibility_notes TEXT,
                mediums           TEXT,
                theme_summary     TEXT,
                first_seen        TEXT    NOT NULL DEFAULT (date('now')),
                last_seen         TEXT    NOT NULL DEFAULT (date('now'))
            )
        """)
        # Migrate DBs created before eligibility_notes/mediums/theme_summary existed.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(opportunities)")}
        for col in ("eligibility_notes", "mediums", "theme_summary"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE opportunities ADD COLUMN {col} TEXT")
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
            INSERT INTO opportunities (url, title, deadline, source, category, description, apply_link,
                                        eligibility_notes, mediums, theme_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                last_seen         = date('now'),
                title             = excluded.title,
                deadline          = excluded.deadline,
                category          = excluded.category,
                description       = excluded.description,
                eligibility_notes = excluded.eligibility_notes,
                mediums           = excluded.mediums,
                theme_summary     = excluded.theme_summary
        """, (opp.url, opp.title, opp.deadline, opp.source, opp.category,
              _smart_truncate(opp.description) if opp.description else None,
              opp.apply_url, opp.eligibility_notes, opp.mediums, opp.theme_summary))


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
        r.encoding = r.apparent_encoding
        return r
    except Exception as e:
        log.warning(f"GET {url} failed: {e}")
        return None


def _html_to_text(html: str) -> str:
    # Strip <script>/<style> block CONTENTS, not just their tags — a bare tag-strip
    # leaves raw JS/CSS source sitting in the output as if it were page text, eating
    # into whatever character budget a caller then truncates to before reaching any
    # real content.
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _smart_truncate(text: str, max_chars: int = 800) -> str:
    """First 3 sentences; fall back to last full stop before max_chars."""
    if not text:
        return ""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(sentences) >= 1:
        result = " ".join(sentences[:3])
        if len(result) <= max_chars:
            return result
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_stop = truncated.rfind(".")
    if last_stop > 0:
        return truncated[:last_stop + 1]
    return truncated


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
                        description=_smart_truncate(_html_to_text(summary)),
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
                        description=_smart_truncate(str(item.get("description") or item.get("summary") or "")),
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
                        description=_smart_truncate(str(item.get("description") or "")),
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
                description=_smart_truncate(context),
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
                    description = _smart_truncate(body_text)
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
                    description=_smart_truncate(parent_text),
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
    SKIP_TITLES = (
        "recipient", "winner", "awarded", "archive", "folder", "newsletter",
        "poet laureate", "arts awards", "award", "laureate", "history", "about",
        "2019", "2020", "2021", "2022", "2023", "2024",
    )
    NAV_SKIP_PATHS = {"about", "contact", "home", "news", "events", "blog", "staff", "board"}
    DEADLINE_RE = re.compile(
        r"(?:deadline[:\s]*|due[:\s]+|apply\s+by[:\s]+|applications?\s+due[:\s]+)",
        re.IGNORECASE,
    )

    index_pages = [
        f"{base_url}/grants-funding",
        f"{base_url}/grants",
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
            # Skip top-level navigation pages
            path = link_url[len(base_url):].lstrip("/")
            first_segment = path.split("/")[0].lower()
            if first_segment in NAV_SKIP_PATHS:
                continue
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
        description = _smart_truncate(" ".join(paragraphs[:3])) if paragraphs else ""

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

    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(addr, password)
        mail.select("INBOX")

        _, nums_data = mail.search(None, "UNSEEN")
        nums = nums_data[0].split() if nums_data[0] else []

        if not nums:
            log.info("Gmail: no unread messages")
            return []

        log.info(f"Gmail: {len(nums)} unread messages")

        email_texts = []
        content_nums = []  # had a usable body — only mark seen if extraction succeeds
        empty_nums = []     # too short/empty — nothing to lose, always safe to mark seen
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
                    content_nums.append(num)
                else:
                    empty_nums.append(num)
            except Exception as e:
                log.warning(f"Gmail: error processing message {num}: {e}")

        # A message with no usable body never had anything to extract in the first
        # place — mark it seen unconditionally so it doesn't pile up in the UNSEEN
        # search on every future poll (crowding out genuinely new mail, since only the
        # oldest 20 unread messages are ever fetched per run).
        for num in empty_nums:
            try:
                mail.store(num, "+FLAGS", "\\Seen")
            except Exception as e:
                log.warning(f"Gmail: could not mark message {num} as seen: {e}")

        if not email_texts:
            return []

        opportunities, extraction_ok = _extract_grants_from_emails(email_texts)

        # Only mark content-bearing messages read once extraction actually succeeded —
        # previously this happened unconditionally per-message during the fetch loop
        # above, so a failed batch extraction (bad API response, truncated output, etc.)
        # still left every message marked \Seen, permanently losing their content: the
        # next poll only ever looks at UNSEEN mail, so a silently-failed batch could
        # never be retried.
        if extraction_ok:
            for num in content_nums:
                try:
                    mail.store(num, "+FLAGS", "\\Seen")
                except Exception as e:
                    log.warning(f"Gmail: could not mark message {num} as seen: {e}")
        else:
            log.warning(
                f"Gmail: extraction failed — leaving {len(content_nums)} message(s) "
                f"unread so they're retried on the next poll"
            )

        return opportunities

    except Exception as e:
        log.error(f"Gmail inbox poll failed: {e}")
        return []
    finally:
        # Logout in its own guarded block, separate from the try/except above — a
        # logout failure (e.g. Gmail dropped the connection during the extraction call)
        # must never discard opportunities/messages-marked-seen work already done; it
        # used to sit inside the same except that returns [] on any error.
        if mail is not None:
            try:
                mail.logout()
            except Exception as e:
                log.warning(f"Gmail: logout failed (non-fatal): {e}")


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


_EXTRACT_OPPORTUNITIES_TOOL = {
    "name": "record_opportunities",
    "description": "Record every grant, open call, residency, fellowship, or funding opportunity for visual artists found in the emails.",
    "input_schema": {
        "type": "object",
        "properties": {
            "opportunities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string", "description": "2-3 sentence description."},
                        "url": {"type": "string", "description": "Apply URL, or empty string if none given."},
                        "deadline": {"type": "string", "description": "ISO date e.g. 2026-08-15, or empty string if unknown."},
                    },
                    "required": ["title", "description", "url", "deadline"],
                },
            },
        },
        "required": ["opportunities"],
    },
}


def _forced_tool_call(client, tool: dict, tool_name: str, prompt: str, max_tokens: int, log_context: str) -> tuple:
    """
    Call Claude with a single tool forced, returning (input_dict, ok). ok is False on
    any failure — API error, output truncated by max_tokens mid-generation, or no
    tool_use block in the response — each logged with log_context so a caller can tell
    a genuine empty result from a failed one instead of treating both the same way.
    Shared by _extract_grants_from_emails and validate_L3 — both moved onto forced
    tool-use off a fragile regex-plus-json.loads pull from free text, for the same
    reliability reason.
    """
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": prompt}],
        )

        if response.stop_reason == "max_tokens":
            log.warning(f"{log_context}: hit the max_tokens cap — output may be incomplete")
            return None, False

        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use is None:
            log.warning(f"{log_context}: no tool call in response (stop_reason={response.stop_reason})")
            return None, False

        return tool_use.input, True
    except Exception as e:
        log.error(f"{log_context} failed: {e}")
        return None, False


def _extract_grants_from_emails(email_texts: list) -> tuple:
    """
    Use Claude Haiku to identify grant/open call mentions in email content.
    Returns (opportunities, ok) — ok is False on any failure (API error, no tool call
    in the response, hitting the output token cap mid-generation) so poll_gmail_inbox
    knows not to mark the source emails read; a failed batch should be retried on the
    next poll, not silently lost.

    Previously this asked for a bare JSON array in free text and pulled it out with a
    `\\[.*\\]` regex — fragile against anything that wasn't a clean, complete array (e.g.
    output truncated by the token cap mid-array, which real newsletter-heavy weeks hit
    more often, not less, since more source content means more opportunities to describe
    in the same fixed token budget). A regex miss returned [] with no logging at all,
    making a real week's newsletters vanish with zero trace. Forced tool-use guarantees
    a structurally valid result whenever generation completes, and every other failure
    path now logs specifically instead of falling through silently.
    """
    combined = "\n\n---EMAIL---\n\n".join(email_texts)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = (
        "You are reviewing email newsletters for artist grant and open call opportunities.\n\n"
        f"Emails:\n{combined}\n\n"
        "Call record_opportunities with every grant, open call, residency, fellowship, or "
        "funding opportunity for visual artists found above. If nothing is found, call it "
        "with an empty list."
    )

    result, ok = _forced_tool_call(
        client, _EXTRACT_OPPORTUNITIES_TOOL, "record_opportunities", prompt,
        max_tokens=4096, log_context="Gmail AI extraction",
    )
    if not ok:
        return [], False

    items = result.get("opportunities", [])
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
    log.info(f"Gmail: extracted {len(results)} opportunities via AI ({len(email_texts)} emails processed)")
    return results, True


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
    today = _today()
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

# Larica's actual eligibility profile — used by validate_L3 to reject opportunities she
# genuinely can't apply to (a different region's residents-only call, an educator-only
# award) while keeping ones that merely mention a preference/priority for another group
# without disqualifying her. Not a medium restriction — she isn't limited to one medium,
# so medium is only ever extracted for display, never used to reject.
_ARTIST_PROFILE = (
    "A visual artist (not an art educator, curator, or student) based in Jersey City, NJ. "
    "Not restricted to one medium — medium-specific opportunities (e.g. sculpture-only "
    "grants) are fine to include; medium is informational only and must never be used as "
    "a reason to reject an opportunity."
)

_L3_TOOL = {
    "name": "review_opportunity",
    "description": "Record the review verdict for a scraped grant/open-call opportunity.",
    "input_schema": {
        "type": "object",
        "properties": {
            "match": {
                "type": "boolean",
                "description": "True only if this is a currently-open opportunity the artist described below is genuinely eligible for.",
            },
            "issues": {
                "type": "string",
                "description": "If match is false, the specific reason for rejection. If match is true, 'OK'.",
            },
            "eligibility_notes": {
                "type": "string",
                "description": "Any eligibility restriction or priority worth surfacing up front (e.g. a community/demographic priority, a specific-region focus) — even when it doesn't disqualify the artist. Empty string if there's nothing notable.",
            },
            "mediums": {
                "type": "string",
                "description": "The media/disciplines this opportunity is seeking, e.g. 'painting, sculpture' — or 'All media' if unspecified/open to all.",
            },
            "theme_summary": {
                "type": "string",
                "description": "One sentence on the theme or purpose of this opportunity, e.g. what kind of work or statement it's looking for.",
            },
        },
        "required": ["match", "issues", "eligibility_notes", "mediums", "theme_summary"],
    },
}


def validate_L3(opportunities: list) -> tuple:
    """
    For each opportunity, fetch its source URL and ask Claude Haiku to verify the scraped
    title, description, and deadline against the live page content — and to check
    eligibility against _ARTIST_PROFILE, and pull out eligibility notes, mediums, and a
    theme one-liner for the email write-up. Returns (verified, flagged).

    Anchors "is this still open" to today's actual date (the previous version never
    stated it, leaving the model to guess — it once concluded an October deadline had
    "passed" while reasoning from the wrong idea of what day it was). Uses forced
    tool-use rather than a free-text-plus-regex JSON pull, for the same reliability reason
    _extract_grants_from_emails was moved off that pattern.
    """
    if not opportunities:
        return [], []

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    today_str = _today().strftime("%Y-%m-%d")
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

            # 4000 was too tight — real eligibility detail can sit well past a page's nav/
            # header chrome (confirmed directly: one residency's actual eligibility text
            # started around character 17,700 of a ~43,000-char page). Bumped substantially
            # now that _html_to_text also strips script/style content instead of just tags.
            page_text = _html_to_text(r.text)[:12000]

            prompt = (
                "You are a quality filter for an artist grant finder tool. "
                f"Today's date is {today_str}. Review this page content and the scraped "
                "opportunity data.\n\n"
                f"Scraped data:\n"
                f"Title: {opp.title}\n"
                f"Description: {opp.description}\n"
                f"Deadline: {opp.deadline or 'not found'}\n"
                f"Apply URL: {opp.apply_url}\n\n"
                f"Source page content (truncated to 12000 chars):\n{page_text}\n\n"
                f"The artist applying is: {_ARTIST_PROFILE}\n\n"
                "Reject this entry (match: false) if ANY of the following are true:\n"
                f"- The opportunity is closed, or its deadline has already passed as of {today_str} "
                "(compare the actual dates carefully — do not assume a future-dated deadline has passed)\n"
                "- This is an informational or about page with no active application process\n"
                "- This is an archive, past winners, past recipients, or award history page\n"
                "- This is a general programme overview with no currently open call\n"
                "- There is no clear way for an artist to apply right now\n"
                "- The page is navigation, a folder index, or a category listing\n"
                "- The content is primarily about a past event or past cycle\n"
                "- Eligibility is genuinely restricted to a specific region, role, or group that "
                "excludes the artist described above (e.g. residents of a different specific city/"
                "state only, or restricted to educators/curators/students when the artist is none "
                "of those) — but do NOT reject for a stated preference or priority given to a "
                "particular group when others remain eligible to apply; note that in "
                "eligibility_notes instead. Never reject for a medium restriction (e.g. "
                "sculpture-only, painting-only) or for uncertainty about whether the artist works "
                "in that medium — record the medium in the mediums field instead, and approve "
                "on every other merit as normal\n\n"
                "Only approve (match: true) if ALL of the following are true:\n"
                "- There is an active, open opportunity that artists can currently apply for OR "
                "an upcoming opportunity with a future deadline clearly stated\n"
                "- There is a clear application process or link to apply\n"
                "- The opportunity is relevant to visual artists or artists generally\n"
                "- The artist described above is actually eligible to apply\n\n"
                "Call review_opportunity with your verdict."
            )

            result, ok = _forced_tool_call(
                client, _L3_TOOL, "review_opportunity", prompt,
                max_tokens=500, log_context=f"L3 check for {opp.title!r}",
            )
            if not ok:
                opp.flagged = True
                opp.flag_reason = "L3: Review output was truncated or missing"
                flagged.append(opp)
                continue

            match = result.get("match", False)
            issues = result.get("issues", "AI could not verify")

            if match:
                opp.eligibility_notes = result.get("eligibility_notes", "")
                opp.mediums = result.get("mediums", "")
                opp.theme_summary = result.get("theme_summary", "")
                verified.append(opp)
            else:
                opp.flagged = True
                opp.flag_reason = f"L3: {issues}"
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
        "3. Open Calls — exhibitions, residencies, juried shows the artist is eligible for "
        "(geography/eligibility is already filtered upstream — categorise on content, not location)\n"
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
    raw_count = len(all_opps)
    log.info(f"Stage 1 (Scrape): {raw_count} total")

    # Within-run deduplication by URL (keep first occurrence across all scrapers)
    seen_urls_run: set = set()
    deduped_run = []
    for opp in all_opps:
        if not opp.url or opp.url not in seen_urls_run:
            deduped_run.append(opp)
            if opp.url:
                seen_urls_run.add(opp.url)
        else:
            log.debug(f"Within-run dedup: skipping duplicate {opp.url!r}")
    all_opps = deduped_run
    log.info(f"After within-run dedup: {len(all_opps)} unique (was {raw_count})")

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
            "title":             opp.title,
            "url":               opp.url,
            "deadline":          opp.deadline,
            "source":            opp.source,
            "category":          opp.category,
            "description":       opp.description,
            "apply_link":        opp.apply_url,
            "eligibility_notes": opp.eligibility_notes,
            "mediums":           opp.mediums,
            "theme_summary":     opp.theme_summary,
        }
        for opp in final_opps
    ]
