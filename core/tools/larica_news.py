"""
Larica daily news pipeline.
Fetches RSS feeds by section, uses Sonnet to select and summarise stories,
builds an HTML email, and optionally sends it.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic
import feedparser
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

log = logging.getLogger(__name__)

MODEL = os.getenv("CHARLIE_MODEL", "claude-sonnet-4-6")
_REQUEST_TIMEOUT = 10
_USER_AGENT = "Mozilla/5.0 (compatible; Charlie/1.0; +larica-news)"

# ---------------------------------------------------------------------------
# Section configuration
# ---------------------------------------------------------------------------

_SECTIONS = [
    {
        "key": "top_stories",
        "label": "Top Stories",
        "count": 3,
        "depth": "short",
        "feeds": [
            ("NYT NY Region", "https://rss.nytimes.com/services/xml/rss/nyt/NYRegion.xml"),
            ("The New Yorker", "https://www.newyorker.com/feed/everything"),
            ("Gothamist", "https://gothamist.com/feed"),
        ],
    },
    {
        "key": "art_entertainment",
        "label": "Art & Entertainment",
        "count": 3,
        "depth": "short",
        "feeds": [
            ("Artnet News", "https://news.artnet.com/feed"),
            ("Hyperallergic", "https://hyperallergic.com/feed/"),
            ("The Art Newspaper", "https://www.theartnewspaper.com/rss.xml"),
            ("Variety", "https://variety.com/feed/"),
            ("Deadline", "https://deadline.com/feed/"),
        ],
    },
    {
        "key": "movies",
        "label": "🎬 Movies",
        "count": 3,
        "depth": "short",
        "feeds": [
            ("Variety Film", "https://variety.com/v/film/feed/"),
            ("The Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
            ("RogerEbert.com", "https://www.rogerebert.com/feed"),
            ("IndieWire", "https://www.indiewire.com/feed/"),
            ("Collider", "https://collider.com/feed/"),
        ],
    },
    {
        "key": "food_travel",
        "label": "🍽️ Food & Travel",
        "count": 3,
        "depth": "short",
        "feeds": [
            ("Condé Nast Traveler", "https://www.cntraveler.com/feed/rss"),
            ("Bon Appétit", "https://www.bonappetit.com/feed/rss"),
            ("Eater NY", "https://ny.eater.com/rss/index.xml"),
            ("NYT Food", "https://rss.nytimes.com/services/xml/rss/nyt/DiningandWine.xml"),
        ],
    },
    {
        "key": "nyc_local",
        "label": "NYC / Jersey City",
        "count": 3,
        "depth": "short",
        "feeds": [
            ("Gothamist", "https://gothamist.com/feed"),
            ("Jersey Digs", "https://www.jerseydigs.com/feed/"),
            ("NJ Spotlight News", "https://www.njspotlightnews.org/feed/"),
        ],
    },
    {
        "key": "wellness",
        "label": "Wellness & Growth",
        "count": 1,
        "depth": "medium",
        "feeds": [
            ("Healthline", "https://www.healthline.com/rss/health-news"),
            ("Vox", "https://www.vox.com/rss/index.xml"),
            ("Popular Mechanics", "https://www.popularmechanics.com/rss/all.xml/"),
        ],
    },
]

_SYSTEM_PROMPT = (
    "You are curating a daily news email for a woman living in Jersey City, NJ. "
    "She is interested in art, entertainment, film, food, travel, local NYC/NJ news, "
    "and personal growth. Write in a warm, clear, intelligent tone. "
    "Never fabricate details — only summarise what is in the provided articles."
)

# ---------------------------------------------------------------------------
# RSS fetching
# ---------------------------------------------------------------------------

def _fetch_feed(source_name: str, url: str, errors: list) -> list[dict]:
    """Fetch a single RSS feed. Returns list of article dicts; appends to errors on failure."""
    try:
        resp = requests.get(
            url,
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            allow_redirects=True,
        )
        if resp.status_code in (402, 403):
            log.info(f"Feed blocked ({resp.status_code}): {source_name}")
            errors.append(f"{source_name}: HTTP {resp.status_code} (skipped)")
            return []
        if resp.status_code != 200:
            log.warning(f"Feed error ({resp.status_code}): {source_name}")
            errors.append(f"{source_name}: HTTP {resp.status_code}")
            return []
        feed = feedparser.parse(resp.content)
    except Exception as e:
        log.warning(f"Feed fetch failed: {source_name} — {e}")
        errors.append(f"{source_name}: {e}")
        return []

    articles = []
    for entry in feed.entries:
        url_val = getattr(entry, "link", "").strip()
        if not url_val:
            continue
        articles.append({
            "title": getattr(entry, "title", "").strip(),
            "url": url_val,
            "description": _get_description(entry),
            "published": _parse_published(entry),
            "source_name": source_name,
        })
    return articles


def _parse_published(entry) -> Optional[datetime]:
    """Return UTC datetime from feedparser entry, or None."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _get_description(entry) -> str:
    """Extract best available description text from a feedparser entry."""
    for attr in ("summary", "description", "content"):
        val = getattr(entry, attr, None)
        if val:
            if isinstance(val, list):
                val = val[0].get("value", "") if val else ""
            text = re.sub(r"<[^>]+>", " ", str(val))
            text = re.sub(r"\s+", " ", text).strip()
            return text[:500]
    return ""


def _filter_by_age(articles: list[dict], max_hours: int) -> list[dict]:
    """Return articles published within the last max_hours hours."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=max_hours)
    return [a for a in articles if a["published"] and a["published"] >= cutoff]


# ---------------------------------------------------------------------------
# Sonnet summarisation
# ---------------------------------------------------------------------------

def _call_sonnet(
    section_label: str,
    articles: list[dict],
    count: int,
    depth: str,
) -> list[dict]:
    """
    Call Sonnet to select and summarise the best N articles for a section.
    Returns list of {title, summary, url, source_name}.
    Falls back to raw data if the API call fails.
    """
    if not articles:
        return []

    candidates = []
    for i, a in enumerate(articles):
        candidates.append(
            f"{i + 1}. Title: {a['title']}\n"
            f"   Source: {a['source_name']}\n"
            f"   URL: {a['url']}\n"
            f"   Description: {a['description'][:300]}"
        )

    summary_instruction = (
        "2-4 sentences with slightly more depth and warmth"
        if depth == "medium"
        else "1-2 clear, warm sentences"
    )

    user_prompt = (
        f"Section: {section_label}\n\n"
        f"Available articles:\n\n" + "\n\n".join(candidates) + "\n\n"
        f"Select the best {count} article(s) for this section. "
        f"For each, write a summary ({summary_instruction}). "
        f"Return ONLY a JSON array. Each object must have exactly these keys: "
        f"title, summary, url, source_name. "
        f"Use the exact URLs from the input without alteration. "
        f"No text outside the JSON array."
    )

    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed[:count]
    except Exception as e:
        log.error(f"Sonnet call failed for '{section_label}': {e}")

    # Fallback: raw titles + truncated descriptions
    fallback = []
    for a in articles[:count]:
        fallback.append({
            "title": a["title"],
            "summary": a["description"][:200] if a["description"] else "(No description available.)",
            "url": a["url"],
            "source_name": a["source_name"],
        })
    return fallback


# ---------------------------------------------------------------------------
# HTML building
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_html(sections_data: list[dict], date_str: str) -> str:
    """Build the full HTML email from assembled section data."""
    h = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        "</head>",
        "<body style='margin:0;padding:0;background:#ffffff;'>",
        '<div style="font-family:Georgia,\'Times New Roman\',serif;max-width:600px;'
        "margin:0 auto;padding:24px 24px 40px;color:#222222;line-height:1.65;"
        "background:#ffffff;\">",
        # Header
        '<div style="border-bottom:2px solid #e8e0d4;padding-bottom:20px;margin-bottom:32px;">',
        '<h1 style="margin:0 0 8px;font-size:26px;font-weight:normal;color:#1a1a1a;">'
        "Good morning, Larica ☀️</h1>",
        f'<p style="margin:0;font-size:14px;color:#999999;">{_esc(date_str)}</p>',
        "</div>",
    ]

    for section in sections_data:
        label = section["label"]
        stories = section.get("stories", [])
        is_wellness = section["key"] == "wellness"

        h.append(
            '<div style="margin-bottom:40px;">'
            f'<h2 style="font-size:11px;font-weight:bold;letter-spacing:0.14em;'
            f"text-transform:uppercase;color:#999999;margin:0 0 18px;"
            f'border-bottom:1px solid #f0ede8;padding-bottom:9px;">'
            f"{_esc(label)}</h2>"
        )

        if not stories:
            h.append(
                '<p style="color:#bbbbbb;font-style:italic;font-size:14px;">'
                "No stories available today.</p>"
            )
        else:
            for story in stories:
                title = story.get("title", "")
                url = story.get("url") or "#"
                source = story.get("source_name", "")
                summary = story.get("summary", "")

                if is_wellness:
                    h.append(
                        '<div style="background:#f9f7f4;border-left:3px solid #c8b89a;'
                        "padding:16px 20px;margin-bottom:20px;border-radius:0 4px 4px 0;\">"
                        '<p style="margin:0 0 5px;">'
                        f'<a href="{_esc(url)}" style="font-size:17px;font-weight:bold;'
                        "color:#1a1a1a;text-decoration:none;\">"
                        f"{_esc(title)}</a></p>"
                        f'<p style="margin:0 0 12px;font-size:11px;color:#bbbbbb;'
                        f'letter-spacing:0.05em;">{_esc(source)}</p>'
                        f'<p style="margin:0;font-size:15px;color:#444444;line-height:1.75;">'
                        f"{_esc(summary)}</p>"
                        "</div>"
                    )
                else:
                    h.append(
                        '<div style="margin-bottom:22px;">'
                        '<p style="margin:0 0 4px;">'
                        f'<a href="{_esc(url)}" style="font-size:16px;font-weight:bold;'
                        "color:#1a1a1a;text-decoration:none;\">"
                        f"{_esc(title)}</a></p>"
                        f'<p style="margin:0 0 8px;font-size:11px;color:#bbbbbb;'
                        f'letter-spacing:0.05em;">{_esc(source)}</p>'
                        f'<p style="margin:0;font-size:14px;color:#555555;line-height:1.65;">'
                        f"{_esc(summary)}</p>"
                        "</div>"
                    )

        h.append("</div>")

    # Footer
    h.append(
        '<div style="margin-top:40px;padding-top:16px;border-top:1px solid #e8e0d4;'
        "font-size:11px;color:#cccccc;text-align:center;\">"
        f"Curated for you by Charlie · {_esc(date_str)}"
        "</div>"
    )
    h.append("</div></body></html>")
    return "\n".join(h)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _make_subject() -> str:
    now = datetime.now()
    day = now.strftime("%A")
    date_part = now.strftime("%-d %B")
    return f"Your Morning Briefing ☀️ — {day}, {date_part}"


def run_larica_pipeline(dry_run: bool = False) -> dict:
    """
    Fetch RSS feeds, summarise with Sonnet, build HTML email, optionally send.
    Returns: {sections, html, email_sent, errors}
    """
    errors: list[str] = []
    seen_urls: set[str] = set()

    # Step 1: Fetch all feeds grouped by section
    section_raw: dict[str, list[dict]] = {}
    for section_cfg in _SECTIONS:
        raw: list[dict] = []
        for source_name, feed_url in section_cfg["feeds"]:
            raw.extend(_fetch_feed(source_name, feed_url, errors))
        section_raw[section_cfg["key"]] = raw

    # Step 2: Process each section
    sections_data = []
    for section_cfg in _SECTIONS:
        key = section_cfg["key"]
        label = section_cfg["label"]
        count = section_cfg["count"]
        depth = section_cfg["depth"]
        raw = section_raw.get(key, [])

        # Age filter: 24h, fallback to 48h, then all available
        candidates = _filter_by_age(raw, 24)
        if not candidates:
            candidates = _filter_by_age(raw, 48)
        if not candidates:
            candidates = raw

        # Deduplicate against cross-section seen URLs and within-section
        section_seen: set[str] = set()
        deduped: list[dict] = []
        for a in candidates:
            u = a["url"]
            if u and u not in seen_urls and u not in section_seen:
                deduped.append(a)
                section_seen.add(u)

        stories = _call_sonnet(label, deduped, count, depth)

        # Mark selected URLs as globally seen to prevent cross-section duplication
        for story in stories:
            if story.get("url"):
                seen_urls.add(story["url"])

        sections_data.append({"key": key, "label": label, "stories": stories})

    # Step 3: Build HTML
    date_str = datetime.now().strftime("%A, %-d %B %Y")
    html = _build_html(sections_data, date_str)

    # Step 4: Send (unless dry_run)
    email_sent = False
    if not dry_run:
        from core.tools.larica_email import send_larica_email
        email_sent = send_larica_email(html, _make_subject())

    return {
        "sections": sections_data,
        "html": html,
        "email_sent": email_sent,
        "errors": errors,
    }
