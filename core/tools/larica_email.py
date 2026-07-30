"""
Larica daily news email sender.
Reuses the Gmail SMTP setup from grants_email.py (same credentials).
"""
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

log = logging.getLogger(__name__)

_LARICA_EMAIL = "laricalschnell@gmail.com"


def send_larica_email(html_content: str, subject: str) -> bool:
    """
    Send the daily briefing email to Larica via Gmail SMTP.
    Returns True on success, False on failure.
    Reads from env: GRANT_GMAIL_ADDRESS, GRANT_GMAIL_PASSWORD
    """
    from_addr = os.environ.get("GRANT_GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GRANT_GMAIL_PASSWORD", "").strip()

    if not all([from_addr, password]):
        log.error(
            "send_larica_email: GRANT_GMAIL_ADDRESS and GRANT_GMAIL_PASSWORD must be set"
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = _LARICA_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(from_addr, password)
            server.sendmail(from_addr, [_LARICA_EMAIL], msg.as_string())
        log.info(f"Larica briefing sent: {subject!r} → {_LARICA_EMAIL}")
        return True
    except Exception as e:
        log.error(f"send_larica_email failed: {e}")
        return False
