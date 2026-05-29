"""
/meta command — reviews a topic conversation with a fresh Claude instance.
Reads the full conversation from SQLite history and posts a ruthless review.
"""

import logging
import os

from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

REVIEW_SYSTEM_PROMPT = """\
You are an independent reviewer analysing a conversation between Jonathan (the user) \
and Charlie (an AI chief of staff). Your job is to provide ruthless, specific, actionable \
feedback. Do not hedge. Do not be polite for politeness's sake. Identify real problems \
and real improvements.

Structure your response in exactly three sections:

**What Charlie should do better**
Specific failures or inefficiencies in how Charlie communicated, responded, or handled \
the conversation. Include concrete recommendations.

**What Jonathan should do better**
Specific ways Jonathan could prompt more clearly, provide better context, or structure \
his requests more efficiently. Be direct — this is for his benefit.

**Recommended system changes**
Any changes to the Charlie system itself — prompts, tools, workflows, context documents — \
that would improve future conversations. Be specific about what should change and why.

Do not summarise the conversation. Do not compliment what went well unless it's directly \
relevant to a contrast. Focus entirely on improvement."""


def _extract_text(content) -> str:
    """Pull readable text out of a message content value (string or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content)


def _is_tool_result_message(role: str, content) -> bool:
    """True if this is an internal tool-result user message (not Jonathan's words)."""
    if role != "user":
        return False
    if not isinstance(content, list):
        return False
    return all(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
        if isinstance(b, dict)
    )


async def run_meta_review(topic_id: int) -> str:
    """
    Build a transcript from SQLite history and run a meta-review with Claude.
    Returns the review text to post back to the topic.
    """
    from core.history import load_history

    messages = load_history(topic_id)
    if not messages:
        return "No conversation history found for this topic."

    lines = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        # Skip internal tool-result messages (not Jonathan's words)
        if _is_tool_result_message(role, content):
            continue

        speaker = "Jonathan" if role == "user" else "Charlie"
        text = _extract_text(content).strip()
        if text:
            lines.append(f"{speaker}: {text}")

    if not lines:
        return "No readable conversation content found in this topic."

    transcript = "\n\n".join(lines)
    user_message = f"Here is the conversation to review:\n\n{transcript}"

    model = os.getenv("CHARLIE_MODEL", "claude-sonnet-4-6")
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=REVIEW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text
