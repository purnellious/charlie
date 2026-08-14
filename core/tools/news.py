"""
News tool — RSS feed management and briefing generation.
Charlie calls get_news_briefing() to fetch and summarise today's news.
Source management tools let Charlie add/remove/list RSS feeds on request.
"""
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic
import feedparser

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent.parent / "data" / "news.db"
MODEL = "claude-haiku-4-5-20251001"  # Haiku is fine for summarisation — save Sonnet for Charlie

# generate_briefing()'s read-unshown-then-mark-shown pattern (below) has no locking of its
# own; before it was invoked via asyncio.to_thread (BUG-019), the single event loop thread
# made the noon scheduler job and a live get_news_briefing request mutually exclusive by
# accident. Running them on real OS threads makes it possible for both to read the same
# "unshown" articles before either marks them shown, delivering duplicate news content
# twice. This lock restores that mutual exclusion explicitly.
_BRIEFING_LOCK = threading.Lock()

# How long a fetch stays "fresh" for get_fetched_articles() callers to share —
# see that function's docstring. Morning Briefing v2 (Aug 2026): the weekday
# briefing and the daily news post fire at the same scheduled instant and both
# need this same fetch, without either re-fetching or racing the other's mark-
# shown step.
#
# Code review: Monday is NOT at the same instant — scheduler.py deliberately
# runs Monday's briefing 10 minutes before the news job to dodge the Monday
# grants pipeline. With a 300s (5 min) window that gap fell outside the
# freshness cache, so the Monday news job always re-fetched, found everything
# already marked shown by the earlier briefing run, and posted "No new
# articles" most Mondays. 900s covers the Monday offset with margin; keep this
# above whatever offset setup_scheduler ever uses for Monday.
_SHARED_FETCH_FRESHNESS_SECONDS = 900
_shared_fetch_cache = {"timestamp": 0.0, "articles_by_topic": None}

# Morning Briefing v2's "News that matters" section: a cheap, keyword-only
# heuristic (deliberately NOT an API call — this runs over every fetched
# article, and BUG-019 already established that cost/latency at that volume
# is worth avoiding) against Jonathan's professional world. Matches are meant
# to be rare — see relevance_score()'s docstring.
PROFESSIONAL_PROFILE = {
    "AI regulation": ["ai act", "ai regulation", "artificial intelligence act", "ai policy", "ai executive order"],
    "legal/regulatory": ["lawsuit", "legislation", "compliance", "court ruling", "sec charges", "doj", "sec v."],
    "crypto regulation": ["crypto regulation", "stablecoin", "sec crypto", "monero", "blockchain law", "digital asset act"],
    "South Africa": ["south africa", "johannesburg", "cape town", "south african rand", "sars "],
}
RELEVANCE_THRESHOLD = 0.5

_DEFAULT_SOURCES = [
    ("BBC World",          "http://feeds.bbci.co.uk/news/world/rss.xml",              "World News"),
    ("Reuters World",      "https://feeds.reuters.com/reuters/worldNews",              "World News"),
    ("The Guardian World", "https://www.theguardian.com/world/rss",                   "World News"),
    ("Daily Maverick",     "https://dailymaverick.co.za/feed/",                       "South Africa"),
    ("News24",             "https://feeds.news24.com/articles/news24/TopStories/rss", "South Africa"),
    ("CoinDesk",           "https://www.coindesk.com/arc/outboundfeeds/rss/",         "Crypto Regulation"),
    ("CoinTelegraph",      "https://cointelegraph.com/rss",                            "Crypto Regulation"),
    ("MIT Tech Review",    "https://www.technologyreview.com/feed/",                  "AI Regulation"),
    ("The Verge",          "https://www.theverge.com/rss/index.xml",                  "AI Regulation"),
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                name   TEXT    NOT NULL,
                url    TEXT    NOT NULL UNIQUE,
                topic  TEXT    NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                url          TEXT    NOT NULL UNIQUE,
                title        TEXT    NOT NULL,
                summary      TEXT,
                topic        TEXT    NOT NULL,
                source_name  TEXT,
                published_at TEXT,
                shown        INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
    _seed_sources()


def _seed_sources():
    with _conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        if count > 0:
            return
        conn.executemany(
            "INSERT OR IGNORE INTO sources (name, url, topic) VALUES (?, ?, ?)",
            _DEFAULT_SOURCES,
        )


def db_add_source(name: str, url: str, topic: str) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO sources (name, url, topic) VALUES (?, ?, ?)",
            (name, url, topic),
        )
        return cur.lastrowid


def db_remove_source(source_id: int):
    with _conn() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))


def db_get_all_sources() -> list:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM sources ORDER BY topic, name"
        ).fetchall()


def db_get_active_sources() -> list:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM sources WHERE active=1 ORDER BY topic, name"
        ).fetchall()


def _store_article(url, title, summary, topic, source_name, published_at):
    with _conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO articles (url, title, summary, topic, source_name, published_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (url, title, summary, topic, source_name, published_at))


def _get_unshown_articles(hours: int = 24) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM articles
            WHERE shown=0 AND created_at >= ?
            ORDER BY topic, published_at DESC
        """, (cutoff,)).fetchall()
    result: dict = {}
    for row in rows:
        result.setdefault(row["topic"], []).append(row)
    return result


def _mark_shown(article_ids: list):
    if not article_ids:
        return
    placeholders = ",".join("?" * len(article_ids))
    with _conn() as conn:
        conn.execute(f"UPDATE articles SET shown=1 WHERE id IN ({placeholders})", article_ids)


def _prune_old_articles(days: int = 7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.execute("DELETE FROM articles WHERE created_at < ?", (cutoff,))


# ---------------------------------------------------------------------------
# RSS fetching
# ---------------------------------------------------------------------------

def _parse_published(entry) -> str:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def _extract_summary(entry) -> str:
    raw = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
    clean = re.sub(r"<[^>]+>", "", raw)
    return clean[:500].strip()


def _fetch_all_feeds() -> int:
    _prune_old_articles()
    sources = db_get_active_sources()
    total = 0
    for source in sources:
        try:
            feed = feedparser.parse(source["url"])
            if feed.bozo and not feed.entries:
                log.warning(f"Feed parse issue for {source['name']}: {feed.bozo_exception}")
                continue
            for entry in feed.entries[:20]:
                url = getattr(entry, "link", "")
                title = getattr(entry, "title", "").strip()
                if not url or not title:
                    continue
                _store_article(
                    url=url,
                    title=title,
                    summary=_extract_summary(entry),
                    topic=source["topic"],
                    source_name=source["name"],
                    published_at=_parse_published(entry),
                )
                total += 1
            log.info(f"Fetched from {source['name']} ({source['topic']})")
        except Exception as e:
            log.error(f"Failed to fetch {source['name']}: {e}")
    return total


# ---------------------------------------------------------------------------
# Briefing generation
# ---------------------------------------------------------------------------

def get_fetched_articles(force_fresh: bool = False) -> dict:
    """
    Returns articles_by_topic — fetching + marking shown, or reusing another
    caller's fetch from within the last 5 minutes. Fetch-then-mark-shown must
    stay atomic under one lock acquisition (see _BRIEFING_LOCK's docstring for
    why splitting them let a second caller steal the first caller's freshly
    fetched articles) — callers must NOT separately call _mark_shown themselves.
    Must be called off the event loop (asyncio.to_thread/run_in_executor), same
    as generate_briefing() — this blocks on network I/O and a lock acquisition.
    """
    global _shared_fetch_cache
    with _BRIEFING_LOCK:
        age = time.time() - _shared_fetch_cache["timestamp"]
        cached = _shared_fetch_cache["articles_by_topic"]
        if not force_fresh and cached is not None and age < _SHARED_FETCH_FRESHNESS_SECONDS:
            return cached

        init_db()
        _fetch_all_feeds()
        articles_by_topic = _get_unshown_articles(hours=24)
        all_ids = [a["id"] for articles in articles_by_topic.values() for a in articles]
        _mark_shown(all_ids)
        _shared_fetch_cache = {"timestamp": time.time(), "articles_by_topic": articles_by_topic}
        return articles_by_topic


def relevance_score(article: dict, keywords: dict | None = None) -> tuple[float, str | None]:
    """
    Pure keyword heuristic, no API call — see PROFESSIONAL_PROFILE's comment for
    why. `article` is a plain dict with 'title' and 'summary' (callers holding a
    sqlite3.Row should pass dict(row)). Returns (score in [0, 1], matched
    category or None); callers should treat anything below RELEVANCE_THRESHOLD
    as not worth surfacing — this is meant to flag a rare "because of X, you
    might want to Y" moment, not a general news filter.
    """
    keywords = keywords or PROFESSIONAL_PROFILE
    text = f"{article.get('title') or ''} {article.get('summary') or ''}".lower()

    best_score, best_category = 0.0, None
    for category, terms in keywords.items():
        hits = sum(1 for term in terms if term in text)
        if hits == 0:
            continue
        score = min(1.0, hits / 2)
        if score > best_score:
            best_score, best_category = score, category
    return best_score, best_category


def _format_articles_for_prompt(articles_by_topic: dict) -> str:
    lines = []
    for topic, articles in articles_by_topic.items():
        if not articles:
            continue
        lines.append(f"{topic.upper()}:")
        for a in articles[:15]:
            summary = (a["summary"] or "")[:200]
            lines.append(f"  - {a['title']}: {summary}")
    return "\n".join(lines)


def generate_briefing() -> str:
    """
    Fetch fresh RSS articles and return a formatted news briefing.
    Called by the get_news_briefing tool handler and the daily 8am scheduler job.
    Uses Haiku internally for summarisation; returns clean text for Charlie to relay.

    Fetch + mark-shown happens atomically inside get_fetched_articles() (under
    _BRIEFING_LOCK) — by the time it returns here, a second caller within the
    freshness window (e.g. the same-instant weekday morning briefing) already
    sees the same, already-marked result, so no lock is needed around the
    summarisation step below.
    """
    articles_by_topic = get_fetched_articles()
    if not any(articles_by_topic.values()):
        return "No new articles since the last briefing."

    articles_context = _format_articles_for_prompt(articles_by_topic)
    today_str = date.today().strftime("%A, %-d %B %Y")

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=(
            f"Today is {today_str}. Generate a concise news briefing from the articles provided. "
            "For each topic, select the 3–5 most important articles. "
            "For 'Crypto Regulation' and 'AI Regulation', only include articles specifically about "
            "regulation, policy, or law — skip general crypto/AI news. "
            "Format:\n\nTOPIC NAME\n1. Headline — one-line summary (max 20 words)\n2. Headline — summary\n\n"
            "Rules: omit topics with no relevant articles. Plain text only. No markdown. "
            "Keep summaries tight and factual. Do not include source names."
        ),
        messages=[{"role": "user", "content": f"Articles:\n{articles_context}"}],
    )

    briefing_text = next(
        (block.text.strip() for block in response.content if hasattr(block, "text")),
        ""
    )
    if not briefing_text:
        return "Could not generate news briefing."

    return f"News Briefing — {today_str}\n\n{briefing_text}"


# ---------------------------------------------------------------------------
# Tool handlers (called from agent.py)
# ---------------------------------------------------------------------------

def tool_get_news_briefing() -> str:
    try:
        return generate_briefing()
    except Exception as e:
        log.error(f"News briefing failed: {e}")
        return f"News briefing failed: {e}"


def tool_add_source(name: str, url: str, topic: str) -> str:
    try:
        init_db()
        sid = db_add_source(name, url, topic)
        return f"Added source {sid}: {name} → {topic}"
    except Exception as e:
        return f"Failed to add source: {e}"


def tool_remove_source(source_id: int) -> str:
    try:
        init_db()
        db_remove_source(source_id)
        return f"Source {source_id} removed."
    except Exception as e:
        return f"Failed to remove source: {e}"


def tool_list_sources() -> str:
    try:
        init_db()
        sources = db_get_all_sources()
        if not sources:
            return "No sources configured."
        lines = [f"[{s['id']}] {s['name']} ({s['topic']}) — {s['url']}" for s in sources]
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to list sources: {e}"
