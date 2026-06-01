"""
Distillation tool — extracts minimum useful context from a completed topic conversation.
Called by the /distil command. Output is proposed for context-archive.md before anything is deleted.
"""

import logging
import os
from datetime import datetime

import anthropic

from core.history import load_history

log = logging.getLogger(__name__)

MODEL = os.getenv("CHARLIE_MODEL", "claude-sonnet-4-6")

DISTIL_PROMPT = """You are extracting the minimum useful context from a conversation between \
Jonathan and his AI Chief of Staff, Charlie.

Your job is ruthless distillation. Read the full conversation and extract ONLY what is \
genuinely worth carrying forward into future conversations. Be extremely selective.

Worth keeping:
- Decisions made
- Things learned about how Jonathan thinks, works, or what matters to him
- Commitments or action items that are not yet resolved
- Important context about ongoing projects or situations

Not worth keeping:
- Transactional exchanges (greetings, acknowledgements, clarifications)
- Information that was time-specific and is now stale
- Anything that is already obvious from context or prior knowledge
- Process discussions that led nowhere

Output format — a short titled entry suitable for an archive:

**[Inferred topic name] — [date]**
[2-5 bullet points of distilled context]

If there is genuinely nothing worth keeping, output only the single line:
NOTHING_TO_KEEP

Be honest. Most conversations produce little or nothing worth archiving. Default to nothing."""


async def run_distil(topic_id: int) -> str:
    """
    Run distillation on a topic's full conversation history.
    Returns the distilled text, or "NOTHING_TO_KEEP" if nothing is worth saving.
    """
    messages = load_history(topic_id)
    if not messages:
        return "NOTHING_TO_KEEP"

    conversation_text = "\n\n".join(
        f"{'Jonathan' if m['role'] == 'user' else 'Charlie'}: "
        f"{_extract_text(m['content'])}"
        for m in messages
    )

    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    response = await client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=DISTIL_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Today is {datetime.now().strftime('%A, %d %B %Y')}.\n\n"
                f"Conversation:\n\n{conversation_text}"
            )
        }],
    )

    result = response.content[0].text.strip()
    if result == "NOTHING_TO_KEEP" or "nothing_to_keep" in result.lower():
        return "NOTHING_TO_KEEP"
    return result


def _extract_text(content) -> str:
    """Extract plain text from a string or list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block["text"])
            elif hasattr(block, "type") and block.type == "text":
                parts.append(block.text)
        return " ".join(parts)
    return str(content)
