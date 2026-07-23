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

log = logging.getLogger(__name__)

# (internal_key, display_label, subtitle)
_CATEGORIES = [
    ("Local / Municipal", "📍 Local / Municipal",        "Jersey City, Hudson County, and nearby municipal programmes"),
    ("State-level",       "🏛️ State-level (New Jersey)", "New Jersey state-wide grant programmes"),
    ("Open Calls",        "🎭 Open Calls",                "Exhibitions, residencies, and juried shows — any geography"),
    ("National Grants",   "🌎 National Grants",           "Open to US artists nationally"),
]


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

def format_grants_email(opportunities: list) -> tuple:
    """
    Takes a list of opportunity dicts (output of run_grants_pipeline).
    Returns (subject, html_body) tuple.
    This is the SEAM — format is completely separate from send.
    """
    today = date.today()
    monday = _monday_of_week(today)
    week_label = monday.strftime("%-d %B %Y")
    subject = f"\U0001f3a8 Artist Grants & Open Calls — Week of {week_label}"

    by_cat: dict = {cat_key: [] for cat_key, _, _ in _CATEGORIES}
    for opp in opportunities:
        cat = opp.get("category", "Open Calls")
        if cat not in by_cat:
            cat = "Open Calls"
        by_cat[cat].append(opp)

    h: list = [
        "<!DOCTYPE html><html lang='en'><body>",
        '<div style="font-family: Georgia, serif; max-width: 620px; margin: 0 auto; color: #222; line-height: 1.6;">',
        '<h1 style="font-size: 22px; border-bottom: 2px solid #222; padding-bottom: 8px; margin-top: 32px;">'
        "\U0001f3a8 Artist Grants &amp; Open Calls</h1>",
        f'<p style="color: #666; font-size: 13px; margin-top: -4px;">Week of {week_label}</p>',
        "<p>Here are this week&#39;s artist grant and open call opportunities.</p>",
    ]

    has_content = False

    for cat_key, cat_display, subtitle in _CATEGORIES:
        opps = by_cat.get(cat_key, [])
        if not opps:
            continue
        has_content = True

        h.append(
            f'<h2 style="font-size: 17px; margin-top: 36px; '
            f'border-left: 4px solid #222; padding-left: 12px;">{_esc(cat_display)}</h2>'
        )
        if subtitle:
            h.append(f'<p style="color: #666; font-size: 12px; margin-top: -6px;">{_esc(subtitle)}</p>')

        for opp in opps:
            deadline_raw = opp.get("deadline") or ""
            dl = f"Deadline: {deadline_raw}" if deadline_raw else "Deadline: Not specified"
            apply_url = opp.get("apply_link") or opp.get("url") or "#"
            title = opp.get("title", "")
            description = opp.get("description", "")

            h.append(
                '<div style="margin-bottom: 28px; padding: 16px 18px; '
                'background: #f7f7f7; border-radius: 4px;">'
                f'<p style="margin: 0 0 4px;"><strong>{_esc(title)}</strong>'
                f' &mdash; <span style="color: #555; font-size: 13px;">{_esc(dl)}</span></p>'
                f'<p style="margin: 8px 0;">{_esc(description)}</p>'
                f'<p style="margin: 8px 0 0;"><a href="{_esc(apply_url)}" '
                'style="color: #1a73e8; text-decoration: none;">Apply here →</a></p>'
                '</div>'
            )

    if not has_content:
        h.append("<p>No new opportunities were found this week. The pipeline ran successfully.</p>")

    h.append(
        '<div style="margin-top: 48px; padding-top: 14px; border-top: 1px solid #ddd; '
        'font-size: 11px; color: #aaa;">Sourced by Charlie | '
        'To add or remove recipients, contact purnelljonathan@gmail.com</div>'
    )
    h.append("</div></body></html>")

    return subject, "\n".join(h)


# ---------------------------------------------------------------------------
# Sending — the other side of the seam
# ---------------------------------------------------------------------------

def send_grants_email(subject: str, html_body: str) -> bool:
    """
    Sends the formatted email via Gmail SMTP.
    Returns True on success, False on failure.
    Reads from env: GRANT_GMAIL_ADDRESS, GRANT_GMAIL_PASSWORD, GRANT_RECIPIENT_EMAIL
    """
    from_addr = os.environ.get("GRANT_GMAIL_ADDRESS", "").strip()
    password  = os.environ.get("GRANT_GMAIL_PASSWORD", "").strip()
    to_addr   = os.environ.get("GRANT_RECIPIENT_EMAIL", "").strip()

    if not all([from_addr, password, to_addr]):
        log.error(
            "send_grants_email: GRANT_GMAIL_ADDRESS, GRANT_GMAIL_PASSWORD, "
            "and GRANT_RECIPIENT_EMAIL must all be set"
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
