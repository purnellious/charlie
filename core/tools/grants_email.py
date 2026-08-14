"""
Artist grant finder — email formatting and sending.

Clean seam: format_grants_email() and send_grants_email() are intentionally separate
functions. A human approval gate can be inserted between them later with minimal disruption.
"""
import logging
import os
import smtplib
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

log = logging.getLogger(__name__)

# (internal_key, display_label, subtitle) — deliberately scope-based, not place-specific:
# this now goes out to artists who aren't necessarily NJ/Jersey-City-based (Art Grant
# Finder V2), so "Jersey City, Hudson County" wording would show up verbatim, and wrongly,
# in a distant artist's own personalized email.
_CATEGORIES = [
    ("Local / Municipal", "📍 Local / Regional", "Municipal and nearby local programmes"),
    ("State-level",       "🏛️ State-level",       "State-wide grant programmes"),
    ("Open Calls",        "🎭 Open Calls",         "Exhibitions, residencies, and juried shows"),
    ("National Grants",   "🌎 National Grants",    "Open to US artists nationally"),
]

# Used only to decide whether to add a one-line heads-up that this week's "Local/
# Regional" picks lean New Jersey — the scrape sources are inherently NJ/NYC-local
# (only CaFÉ + the Gmail newsletter are geography-agnostic), so an artist outside this
# area won't get much from that category even when nothing formally excludes them.
_NJ_NYC_KEYWORDS = (
    "nj", "new jersey", "jersey city", "hoboken", "hudson county",
    "ny", "new york", "nyc", "brooklyn", "manhattan", "queens", "bronx",
)


def _is_nj_nyc_adjacent(location: str) -> bool:
    loc = (location or "").lower()
    return any(kw in loc for kw in _NJ_NYC_KEYWORDS)


def _monday_of_week(today: date) -> date:
    return today - timedelta(days=today.weekday())


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Formatting — the seam
# ---------------------------------------------------------------------------

def _render_opportunity_card(opp: dict, score: "int | None" = None) -> str:
    """
    Shared card renderer — one opportunity's title/deadline/eligibility/theme/mediums/
    description/apply-link. score=None renders without a match-percentage badge; used
    for both the personalized per-artist email and (via score=None) any future context
    that wants the same layout without a score attached.
    """
    deadline_raw = opp.get("deadline") or ""
    dl = f"Deadline: {deadline_raw}" if deadline_raw else "Deadline: Not specified"
    apply_url = opp.get("apply_link") or opp.get("url") or "#"
    title = opp.get("title", "")
    description = opp.get("description", "")
    eligibility_notes = opp.get("eligibility_notes") or ""
    mediums = opp.get("mediums") or ""
    theme_summary = opp.get("theme_summary") or ""

    badge = ""
    if score is not None:
        badge = (
            f' <span style="background:#1a73e8; color:#fff; border-radius:12px; '
            f'padding:2px 10px; font-size:12px; vertical-align:middle;">{score}% match</span>'
        )

    body = [
        '<div style="margin-bottom: 28px; padding: 16px 18px; '
        'background: #f7f7f7; border-radius: 4px;">'
        f'<p style="margin: 0 0 4px;"><strong>{_esc(title)}</strong>{badge}'
        f' &mdash; <span style="color: #555; font-size: 13px;">{_esc(dl)}</span></p>'
    ]
    # Eligibility notes go right after the title, before anything else — a
    # restriction/priority worth knowing up front shouldn't be buried below the
    # general description.
    if eligibility_notes:
        body.append(
            f'<p style="margin: 6px 0; color: #8a5a00; font-size: 13px;">'
            f'&#9888; {_esc(eligibility_notes)}</p>'
        )
    if theme_summary:
        body.append(f'<p style="margin: 8px 0;">{_esc(theme_summary)}</p>')
    if mediums:
        body.append(
            f'<p style="margin: 4px 0; color: #666; font-size: 12px;">'
            f'Mediums: {_esc(mediums)}</p>'
        )
    body.append(f'<p style="margin: 8px 0;">{_esc(description)}</p>')
    body.append(
        f'<p style="margin: 8px 0 0;"><a href="{_esc(apply_url)}" '
        'style="color: #1a73e8; text-decoration: none;">Apply here →</a></p>'
        '</div>'
    )
    return "".join(body)


def format_artist_match_email(artist: dict, matches: list) -> tuple:
    """
    artist: dict with at least name/location.
    matches: list of opportunity dicts (same shape run_grants_pipeline's output already
    used) each with an added "score" key. Returns (subject, html_body).
    """
    today = date.today()
    monday = _monday_of_week(today)
    week_label = monday.strftime("%-d %B %Y")
    name = artist.get("name") or "there"
    subject = f"\U0001f3a8 Your Artist Grant Matches — Week of {week_label}"

    by_cat: dict = {cat_key: [] for cat_key, _, _ in _CATEGORIES}
    for m in matches:
        cat = m.get("category") or "Open Calls"
        if cat not in by_cat:
            cat = "Open Calls"
        by_cat[cat].append(m)
    for opps in by_cat.values():
        opps.sort(key=lambda o: o.get("score") or 0, reverse=True)

    h: list = [
        "<!DOCTYPE html><html lang='en'><body>",
        '<div style="font-family: Georgia, serif; max-width: 620px; margin: 0 auto; color: #222; line-height: 1.6;">',
        '<h1 style="font-size: 22px; border-bottom: 2px solid #222; padding-bottom: 8px; margin-top: 32px;">'
        f"\U0001f3a8 Hi {_esc(name)}, here are your matches</h1>",
        f'<p style="color: #666; font-size: 13px; margin-top: -4px;">Week of {week_label}</p>',
    ]

    if not _is_nj_nyc_adjacent(artist.get("location") or ""):
        h.append(
            '<p style="color: #8a5a00; font-size: 13px; margin-top: 8px;">Heads up — our local '
            'sources are NJ/NYC-focused, so this week\'s "Local / Regional" picks lean New '
            'Jersey and may not be very relevant to you specifically.</p>'
        )

    for cat_key, cat_display, subtitle in _CATEGORIES:
        opps = by_cat.get(cat_key, [])
        if not opps:
            continue
        h.append(
            f'<h2 style="font-size: 17px; margin-top: 36px; '
            f'border-left: 4px solid #222; padding-left: 12px;">{_esc(cat_display)}</h2>'
        )
        if subtitle:
            h.append(f'<p style="color: #666; font-size: 12px; margin-top: -6px;">{_esc(subtitle)}</p>')
        for opp in opps:
            h.append(_render_opportunity_card(opp, score=opp.get("score")))

    h.append(
        '<div style="margin-top: 48px; padding-top: 14px; border-top: 1px solid #ddd; '
        'font-size: 11px; color: #aaa;">Sourced by Charlie | '
        'To update your profile or unsubscribe, contact purnelljonathan@gmail.com</div>'
    )
    h.append("</div></body></html>")

    return subject, "\n".join(h)


def format_no_artists_email(opportunities: list) -> tuple:
    """
    Fallback digest sent to the admin when there are no active, past-onboarding-delay
    artist profiles this run (e.g. ARTIST_PROFILES_SHEET_URL isn't set yet, or the Sheet
    is empty) — without this, the admin summary alone would report a bare count with no
    actual grant listing, a real regression from the pre-multi-artist behavior where
    every scraped/verified opportunity always reached a human via a real email.
    """
    today = date.today()
    monday = _monday_of_week(today)
    week_label = monday.strftime("%-d %B %Y")
    subject = f"\U0001f3a8 Artist Grants & Open Calls — Week of {week_label} (no artist profiles yet)"

    h: list = [
        "<!DOCTYPE html><html lang='en'><body>",
        '<div style="font-family: Georgia, serif; max-width: 620px; margin: 0 auto; color: #222; line-height: 1.6;">',
        '<h1 style="font-size: 22px; border-bottom: 2px solid #222; padding-bottom: 8px; margin-top: 32px;">'
        "\U0001f3a8 Artist Grants &amp; Open Calls</h1>",
        f'<p style="color: #666; font-size: 13px; margin-top: -4px;">Week of {week_label}</p>',
        '<p style="color: #8a5a00;">No active artist profiles yet, so nothing has been '
        'personally matched or sent to anyone — here is everything found this week, '
        'unfiltered by any profile.</p>',
    ]
    if not opportunities:
        h.append("<p>No new opportunities were found this week either.</p>")
    else:
        for opp in opportunities:
            h.append(_render_opportunity_card(opp, score=None))
    h.append(
        '<div style="margin-top: 48px; padding-top: 14px; border-top: 1px solid #ddd; '
        'font-size: 11px; color: #aaa;">Sourced by Charlie</div>'
    )
    h.append("</div></body></html>")
    return subject, "\n".join(h)


def format_admin_summary(run_stats: dict) -> tuple:
    """
    run_stats:
    {
        "scraped": int, "evaluated": int,
        "per_artist": {email: {"name": str, "sent": int, "avg_score": float|None}},
        "new_signups": [{"email": str, "name": str}],
        "send_failures": [{"email": str, "error": str}],
        "expired_undelivered": int,
    }
    Returns (subject, html_body).
    """
    today = date.today()
    monday = _monday_of_week(today)
    week_label = monday.strftime("%-d %B %Y")
    subject = f"Grant Finder — Weekly Run Summary ({week_label})"

    h: list = [
        f"<h2>Grant Finder — Weekly Run Summary</h2>",
        f"<p>Week of {week_label}</p>",
        f"<p>{run_stats.get('scraped', 0)} opportunities scraped, "
        f"{run_stats.get('evaluated', 0)} evaluated across all artists.</p>",
        "<ul>",
    ]
    for email, s in run_stats.get("per_artist", {}).items():
        name = s.get("name") or email
        sent = s.get("sent", 0)
        avg = s.get("avg_score")
        if sent:
            avg_str = f" (avg score {avg:.0f}%)" if avg is not None else ""
            h.append(f"<li>✓ {_esc(name)}: {sent} match(es) sent{avg_str}</li>")
        else:
            h.append(f"<li>– {_esc(name)}: 0 matches above threshold this week</li>")
    h.append("</ul>")

    if run_stats.get("new_signups"):
        h.append("<p>\U0001f195 New signups this run (matching starts next week):</p><ul>")
        for s in run_stats["new_signups"]:
            h.append(f"<li>{_esc(s.get('name') or s.get('email'))} ({_esc(s.get('email'))})</li>")
        h.append("</ul>")

    if run_stats.get("send_failures"):
        h.append("<p>⚠️ Send failures:</p><ul>")
        for f in run_stats["send_failures"]:
            h.append(f"<li>{_esc(f.get('email'))}: {_esc(f.get('error'))}</li>")
        h.append("</ul>")

    if run_stats.get("expired_undelivered"):
        h.append(
            f"<p>{run_stats['expired_undelivered']} previously-pending match(es) expired "
            f"before they could be delivered (their deadline passed).</p>"
        )

    return subject, "\n".join(h)


# ---------------------------------------------------------------------------
# Sending — the other side of the seam
# ---------------------------------------------------------------------------

def send_grants_email(subject: str, html_body: str, to_addr: str = None) -> bool:
    """
    Sends the formatted email via Gmail SMTP.
    Returns True on success, False on failure.
    Reads from env: GRANT_GMAIL_ADDRESS, GRANT_GMAIL_PASSWORD, and — only when to_addr
    isn't explicitly passed — GRANT_RECIPIENT_EMAIL (the admin-summary recipient).
    Per-artist personalized sends pass their own address explicitly.
    """
    from_addr = os.environ.get("GRANT_GMAIL_ADDRESS", "").strip()
    password  = os.environ.get("GRANT_GMAIL_PASSWORD", "").strip()
    to_addr   = (to_addr or os.environ.get("GRANT_RECIPIENT_EMAIL", "")).strip()

    if not all([from_addr, password, to_addr]):
        log.error(
            "send_grants_email: GRANT_GMAIL_ADDRESS, GRANT_GMAIL_PASSWORD, and a "
            "recipient (explicit to_addr or GRANT_RECIPIENT_EMAIL) must all be set"
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(from_addr, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        log.info(f"Grant email sent: {subject!r} → {to_addr}")
        return True
    except Exception as e:
        log.error(f"send_grants_email failed: {e}")
        return False
