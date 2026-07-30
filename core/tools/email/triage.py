"""
Claude Haiku triage for the email monitor tool. Produces a structured
verdict (actionability, urgency, confidence, one-sentence suggested action)
for a single email. Recent corrections are folded into the prompt so the
verdict calibrates over time.
"""
import logging
import os

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"


def format_feedback_examples(rows: list) -> str:
    if not rows:
        return ""
    lines = ["Past corrections — use these to calibrate your judgement:"]
    for f in rows:
        if f["context_snippet"]:
            ctx = f["context_snippet"]
        elif f["sender_email"]:
            ctx = f"{f['sender_email']} / {f['subject']}"
        else:
            ctx = "(original email no longer available)"  # pruned since this feedback was recorded
        lines.append(f"  Email: {ctx}")
        lines.append(f"  Verdict was: {f['original_verdict']}")
        lines.append(f"  Correction: {f['user_correction']}")
        lines.append("  ---")
    return "\n".join(lines)


def triage_email(email: dict, feedback_rows: list) -> dict:
    """
    email: dict with sender_name, sender_email, subject, body (in-memory only).
    Returns {actionability, urgent, confidence, summary}.
    """
    feedback_text = format_feedback_examples(feedback_rows)

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
        + (f"\n\n{feedback_text}" if feedback_text else "")
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
