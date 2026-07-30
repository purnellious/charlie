"""
Claude Haiku triage for the email monitor tool. Produces a structured
verdict (actionability, urgency, confidence, one-sentence suggested action)
for a single email. Charlie's evolving email-preferences.md (proposed and
approved the same way charlie.md is) is folded into the prompt so the
verdict calibrates over time — kept local to this tool file, per CLAUDE.md's
"keep the base system prompt lean" rule, same as it worked before.
"""
import logging
import os
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

EMAIL_PREFS_DOC = Path(__file__).parent.parent.parent.parent / "email-preferences.md"


def _load_email_preferences() -> str:
    if EMAIL_PREFS_DOC.exists():
        return EMAIL_PREFS_DOC.read_text().strip()
    return ""


def triage_email(email: dict) -> dict:
    """
    email: dict with sender_name, sender_email, subject, body (in-memory only).
    Returns {actionability, urgent, confidence, summary}.
    """
    preferences_text = _load_email_preferences()

    prompt = (
        f"From: {email['sender_name']} <{email['sender_email']}>\n"
        f"Subject: {email['subject']}\n"
        f"Body: {email['body']}\n\n"
        "Respond in EXACTLY this format, no other text:\n"
        "ACTIONABILITY: ACTIONABLE or RECOMMENDATION or NONE\n"
        "URGENT: yes or no\n"
        "CONFIDENCE: high or medium or low\n"
        "SUMMARY: one sentence describing what this email is and what action is needed\n\n"
        "ACTIONABILITY meanings:\n"
        "- ACTIONABLE: the user must do something (reply needed, decision required, deadline present)\n"
        "- RECOMMENDATION: worth reading but no action strictly required\n"
        "- NONE: purely informational, automated, promotional, or low-value"
    )

    system = (
        "You are an email triage assistant. You only read and summarise — you never "
        "suggest sending, replying to, forwarding, or contacting anyone on the user's behalf."
        + (f"\n\nStanding preferences for how Jonathan wants email handled:\n{preferences_text}" if preferences_text else "")
    )

    result = {
        "actionability": "NONE",
        "urgent": False,
        "confidence": "low",
        "summary": "",
    }

    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=MODEL,
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if hasattr(b, "text")), "")

        for line in text.splitlines():
            line = line.strip()
            if line.startswith("ACTIONABILITY:"):
                val = line.split(":", 1)[1].strip().upper()
                if val in ("ACTIONABLE", "RECOMMENDATION", "NONE"):
                    result["actionability"] = val
            elif line.startswith("URGENT:"):
                result["urgent"] = "yes" in line.lower()
            elif line.startswith("CONFIDENCE:"):
                val = line.split(":", 1)[1].strip().lower()
                if val in ("high", "medium", "low"):
                    result["confidence"] = val
            elif line.startswith("SUMMARY:"):
                result["summary"] = line.split(":", 1)[1].strip()

        if not result["summary"]:
            log.warning(f"Malformed triage response for {email.get('gmail_message_id')}: {text!r}")
            result["summary"] = "Triage response was malformed — review this email directly."
    except Exception as e:
        log.error(f"Triage failed for {email.get('gmail_message_id')}: {e}")
        result["summary"] = "Triage failed — could not generate a suggested action for this email."

    return result
