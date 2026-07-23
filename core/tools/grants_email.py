"""
Artist grant finder — email formatting and sending.

Clean seam: format_email() and send_email() are intentionally separate functions.
A human approval gate can be inserted between them later with minimal disruption.

Usage:
    result = run_pipeline()
    formatted = format_email(result["opportunities"])
    # --- future approval gate here ---
    send_email(formatted)
"""
import logging
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

_CATEGORIES = [
    "Local / Municipal",
    "State-level",
    "Open Calls",
    "National Grants",
]

_CATEGORY_SUBTITLES = {
    "Local / Municipal": "Jersey City, Hudson County, and nearby municipal programmes",
    "State-level":       "New Jersey state-wide grant programmes",
    "Open Calls":        "Exhibitions, residencies, and juried shows — any geography",
    "National Grants":   "Open to US artists nationally",
}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_email(opportunities: list) -> dict:
    """
    Format a list of categorised Opportunity objects into an HTML (and plain-text) email.

    Returns:
        {"subject": str, "html": str, "plain": str}
    """
    today = date.today()
    week_of = today.strftime("%-d %B %Y")
    subject = f"\U0001f3a8 Artist Grants & Open Calls — Week of {week_of}"

    # Group by category, preserving display order
    by_cat: dict = {cat: [] for cat in _CATEGORIES}
    for opp in opportunities:
        cat = opp.category if opp.category in by_cat else "Open Calls"
        by_cat[cat].append(opp)

    # ---- HTML ----
    h: list = [
        "<!DOCTYPE html><html lang='en'><body>",
        '<div style="font-family: Georgia, serif; max-width: 620px; margin: 0 auto; color: #222; line-height: 1.6;">',
        f'<h1 style="font-size: 22px; border-bottom: 2px solid #222; padding-bottom: 8px; margin-top: 32px;">'
        f'\U0001f3a8 Artist Grants &amp; Open Calls</h1>',
        f'<p style="color: #666; font-size: 13px; margin-top: -4px;">Week of {week_of}</p>',
        "<p>Here are this week’s opportunities for visual artists.</p>",
    ]

    # ---- Plain text ----
    p: list = [
        f"Artist Grants & Open Calls — Week of {week_of}",
        "",
        "Here are this week's opportunities for visual artists.",
        "",
    ]

    has_content = False

    for cat in _CATEGORIES:
        opps = by_cat.get(cat, [])
        if not opps:
            continue
        has_content = True
        subtitle = _CATEGORY_SUBTITLES.get(cat, "")

        h.append(
            f'<h2 style="font-size: 17px; margin-top: 36px; '
            f'border-left: 4px solid #222; padding-left: 12px;">{cat}</h2>'
        )
        if subtitle:
            h.append(f'<p style="color: #666; font-size: 12px; margin-top: -6px;">{subtitle}</p>')

        p.append(f"{'='*len(cat)}")
        p.append(cat.upper())
        if subtitle:
            p.append(f"({subtitle})")
        p.append("")

        for opp in opps:
            dl = f"Deadline: {opp.deadline}" if opp.deadline else "Deadline: See link"
            apply_url = opp.apply_url or opp.url or "#"

            h.append(
                '<div style="margin-bottom: 28px; padding: 16px 18px; '
                'background: #f7f7f7; border-radius: 4px;">'
                f'<p style="margin: 0 0 4px;"><strong>{_esc(opp.title)}</strong>'
                f' &mdash; <span style="color: #555; font-size: 13px;">{_esc(dl)}</span></p>'
                f'<p style="margin: 8px 0;">{_esc(opp.description)}</p>'
                f'<p style="margin: 8px 0 0;"><a href="{_esc(apply_url)}" '
                f'style="color: #1a73e8; text-decoration: none;">Apply here →</a></p>'
                '</div>'
            )

            p.append(f"  {opp.title}")
            p.append(f"  {dl}")
            p.append(f"  {opp.description}")
            p.append(f"  Apply: {apply_url}")
            p.append("")

    if not has_content:
        h.append("<p>No new opportunities found this week.</p>")
        p.append("No new opportunities found this week.")

    footer_html = (
        '<div style="margin-top: 48px; padding-top: 14px; border-top: 1px solid #ddd; '
        'font-size: 11px; color: #aaa;">Sourced by Charlie | Unsubscribe instructions coming soon</div>'
    )
    h.append(footer_html)
    h.append("</div></body></html>")

    p += ["", "--", "Sourced by Charlie | Unsubscribe instructions coming soon"]

    return {
        "subject": subject,
        "html":    "\n".join(h),
        "plain":   "\n".join(p),
    }


def _esc(text: str) -> str:
    """Minimal HTML escaping for user-supplied strings."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Sending  — the clean seam
# ---------------------------------------------------------------------------

def send_email(formatted: dict) -> None:
    """
    Send the formatted grant email via Gmail SMTP.

    This is the clean seam for the future human approval gate.
    It is a standalone callable that takes the output of format_email() as input.
    A future approval step can sit between format_email() and this function
    without requiring any change to either side.

    Args:
        formatted: dict with "subject", "html", and "plain" keys.

    Raises:
        ValueError: if required environment variables are missing.
        smtplib.SMTPException: on send failure.
    """
    from_addr = os.environ.get("GRANT_GMAIL_ADDRESS", "").strip()
    password  = os.environ.get("GRANT_GMAIL_PASSWORD", "").strip()
    to_addr   = os.environ.get("GRANT_RECIPIENT_EMAIL", "").strip()

    if not all([from_addr, password, to_addr]):
        raise ValueError(
            "GRANT_GMAIL_ADDRESS, GRANT_GMAIL_PASSWORD, and GRANT_RECIPIENT_EMAIL must all be set"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = formatted["subject"]
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    msg.attach(MIMEText(formatted["plain"], "plain", "utf-8"))
    msg.attach(MIMEText(formatted["html"],  "html",  "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(from_addr, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())

    log.info(f"Grant email sent: {formatted['subject']!r} → {to_addr}")
