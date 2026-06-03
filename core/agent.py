"""
Charlie — Personal Chief of Staff agent.
Handles one conversation turn: loads context, calls Claude Sonnet with extended thinking
and tools, streams thinking as | ... | messages, returns updated message history.
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

CHARLIE_ROOT = Path(__file__).parent.parent
CHARLIE_DOC = CHARLIE_ROOT / "charlie.md"
DEVLOG = CHARLIE_ROOT / "devlog.md"
CONTEXT_ARCHIVE = CHARLIE_ROOT / "context-archive.md"
PRINCIPLES = CHARLIE_ROOT / "principles.md"
MODEL = os.getenv("CHARLIE_MODEL", "claude-sonnet-4-6")
MAX_CHUNK = 4000

TOOLS = [
    {
        "name": "run_claude_code",
        "description": (
            "Run a Claude Code task to build new capabilities, write code, or make changes "
            "to Charlie. Use this when Jonathan asks you to build something, add a feature, "
            "or make a system change. Describe the task clearly and completely — Claude Code "
            "will execute it autonomously on the local machine."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Full description of what to build or change."
                }
            },
            "required": ["task"]
        }
    },
    {
        "name": "log_bug",
        "description": (
            "Log a new bug in bugs.md and create a dedicated Telegram topic for it. "
            "Use this when Jonathan reports a bug, identifies a problem, or raises a debt item. "
            "Infer sensible values for type/priority/severity/effort from context. "
            "Be concise but complete in problem and what_to_fix."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short bug title (one line)."},
                "type": {"type": "string", "enum": ["Bug", "Debt", "Rule"], "description": "Bug, Debt, or Rule."},
                "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                "severity": {"type": "string", "description": "One-line severity description including High/Medium/Low."},
                "effort": {"type": "string", "enum": ["Small", "Medium", "Large"]},
                "problem": {"type": "string", "description": "Clear description of the problem."},
                "what_to_fix": {"type": "string", "description": "What needs to be done to fix it."},
            },
            "required": ["title", "type", "priority", "severity", "effort", "problem", "what_to_fix"]
        }
    },
    {
        "name": "resolve_bug",
        "description": (
            "Mark a bug as resolved in bugs.md after confirming the conversation contains "
            "a complete, working fix. Only call this if you are confident the bug is fully solved "
            "or Jonathan has explicitly said it's done. Include a clear resolution summary. "
            "After calling this, the topic can be safely closed and /distil run to archive it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bug_id": {"type": "string", "description": "e.g. BUG-001"},
                "resolution_summary": {"type": "string", "description": "What was done to fix it."},
            },
            "required": ["bug_id", "resolution_summary"]
        }
    },
    {
        "name": "propose_charlie_update",
        "description": (
            "Propose an update to charlie.md — your persistent context document about Jonathan. "
            "Use this when you learn something important about his goals, preferences, how he "
            "works, or anything worth carrying into future conversations. Pass the full proposed "
            "new content of charlie.md. Jonathan will approve or reject before anything is saved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_content": {
                    "type": "string",
                    "description": "Full proposed new content of charlie.md."
                },
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of what you learned and why it's worth saving."
                }
            },
            "required": ["proposed_content", "reason"]
        }
    }
]


def _load_principles() -> str:
    if PRINCIPLES.exists():
        return PRINCIPLES.read_text().strip()
    return ""


def _load_charlie_doc() -> str:
    if CHARLIE_DOC.exists():
        return CHARLIE_DOC.read_text().strip()
    return "(charlie.md not found — this will be created as you learn about Jonathan)"


def _load_devlog() -> str:
    if DEVLOG.exists():
        return DEVLOG.read_text().strip()
    return "(devlog.md not found)"


def _load_context_archive() -> str:
    if CONTEXT_ARCHIVE.exists():
        content = CONTEXT_ARCHIVE.read_text().strip()
        # Don't include the placeholder text if nothing has been archived yet
        if "*No entries yet.*" in content:
            return ""
        return content
    return ""


def _build_system_prompt() -> str:
    principles = _load_principles()
    charlie_doc = _load_charlie_doc()
    devlog = _load_devlog()
    context_archive = _load_context_archive()
    today = datetime.now().strftime("%A, %d %B %Y")
    principles_section = f"## Design Principles\n\n{principles}\n\n" if principles else ""
    return f"""{principles_section}You are Charlie — Jonathan's personal Chief of Staff and AI assistant.

Today is {today}.

## Who you are

You are direct, honest, and genuinely invested in Jonathan's success. You are not a yes-man. \
When you disagree, you say so. When you see a risk he hasn't mentioned, you surface it. \
When a plan has a flaw, you flag it before he commits. Your job is to help him think better \
and act more effectively — not to validate whatever he's already decided.

You think in terms of the bigger picture. Every conversation lands in the context of \
Jonathan's broader goals. When he asks for help with something specific, you do it — but \
you also flag if it's pulling him in the wrong direction, or if there's a smarter path.

You are cost-conscious by default. Before recommending anything with a cost, you consider \
whether it's worth it. You know the difference between spending that accelerates progress \
and spending that doesn't.

You are proactive. If something is worth flagging, you flag it. If an obvious opportunity \
is being missed, you point it out.

You are building a working relationship over time. You pay close attention to how Jonathan \
works, what matters to him, and what you learn from each conversation. When you learn \
something worth remembering, use the propose_charlie_update tool to suggest adding it to \
your context document. Keep updates substantive — don't propose trivial changes.

When you make a significant change to the system via run_claude_code, also update devlog.md \
with a one-line entry (date + what changed). This keeps Claude Code sessions in sync.

## Your tools

- **run_claude_code** — build new capabilities, write code, or make changes to Charlie itself
- **propose_charlie_update** — propose an update to your persistent context (charlie.md) \
when you learn something important about Jonathan. He will review before it's saved.
- **log_bug** — log a bug in bugs.md and open a dedicated Telegram topic for it
- **resolve_bug** — mark a bug as resolved after confirming a complete fix exists

**Capabilities boundary:** You run exclusively on Jonathan's always-on Mac (10.0.0.119). \
You cannot directly access or execute anything on his main Mac. If Jonathan asks you to do \
something on his main Mac, tell him the exact command to run himself rather than running it \
and claiming it's done.

## Recent changes (devlog)

{devlog}

## Jonathan's context

{charlie_doc}
{f"## Archived context from past topics{chr(10)}{context_archive}" if context_archive else ""}"""


_RETRY_DELAYS = [5, 15, 30]  # seconds between attempts


async def _create_with_retry(client, kwargs: dict):
    """Call client.messages.create with retries on overloaded (529) or rate-limit (429) errors."""
    last_exc = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            log.warning(f"Anthropic API unavailable — retrying in {delay}s (attempt {attempt + 1})")
            await asyncio.sleep(delay)
        try:
            return await client.messages.create(**kwargs)
        except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
            last_exc = e
            log.warning(f"Retryable API error: {e}")
    raise last_exc


async def handle_turn(
    user_text: str,
    messages: list,
    send_fn,
    thinking_enabled: bool = True,
    thinking_budget: int = 2000,
) -> tuple[list, dict | None]:
    """
    Run one Charlie turn.

    Args:
        user_text: The user's message.
        messages: Full prior conversation history in Anthropic API format.
        send_fn: Async callable that sends a string to Telegram.
        thinking_enabled: Whether to show extended thinking.
        thinking_budget: Token budget for thinking (min 1024).

    Returns:
        (updated_messages, proposed_update)
        proposed_update is None or {"proposed_content": str, "reason": str}
    """
    from core.tools.claude_code import run as run_claude_code

    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    messages = messages + [{"role": "user", "content": user_text}]
    proposed_update = None

    while True:
        create_kwargs = dict(
            model=MODEL,
            max_tokens=8000 if thinking_enabled else 4096,
            system=_build_system_prompt(),
            messages=messages,
            tools=TOOLS,
        )
        if thinking_enabled:
            create_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": max(1024, thinking_budget),
            }

        response = await _create_with_retry(client, create_kwargs)

        thinking_parts = []
        text_parts = []
        tool_blocks = []

        for block in response.content:
            if block.type == "thinking":
                thinking_parts.append(block.thinking)
            elif block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_blocks.append(block)

        # Send thinking blocks as | ... | messages
        for thought in thinking_parts:
            await _send_thinking(thought, send_fn)

        # Send text response
        combined_text = "\n".join(text_parts).strip()
        if combined_text:
            await _send_chunks(combined_text, send_fn)

        messages = messages + [{"role": "assistant", "content": response.content}]

        if not tool_blocks or response.stop_reason != "tool_use":
            break

        # Handle tool calls
        tool_results = []
        for block in tool_blocks:
            if block.name == "run_claude_code":
                task = block.input.get("task", "")
                await send_fn(f"Running Claude Code: {task[:100]}...")
                result = await run_claude_code(task)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            elif block.name == "propose_charlie_update":
                proposed_update = {
                    "proposed_content": block.input.get("proposed_content", ""),
                    "reason": block.input.get("reason", ""),
                }
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Update proposed. Jonathan will review it.",
                })

            elif block.name == "log_bug":
                from core.tools.bugs import create_bug_entry, create_bug_topic, get_bug_by_id
                try:
                    bug_id = create_bug_entry(
                        title=block.input.get("title", ""),
                        type=block.input.get("type", "Bug"),
                        priority=block.input.get("priority", "Medium"),
                        severity=block.input.get("severity", "Medium"),
                        effort=block.input.get("effort", "Medium"),
                        problem=block.input.get("problem", ""),
                        what_to_fix=block.input.get("what_to_fix", ""),
                    )
                    bug = get_bug_by_id(bug_id)
                    await create_bug_topic(bug)
                    result = f"{bug_id} logged and topic created."
                except Exception as e:
                    result = f"Bug logging failed: {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            elif block.name == "resolve_bug":
                from core.tools.bugs import close_bug, get_bug_by_id
                bug_id = block.input.get("bug_id", "")
                resolution = block.input.get("resolution_summary", "")
                try:
                    bug = get_bug_by_id(bug_id)
                    if not bug:
                        result = f"Bug {bug_id} not found."
                    elif bug.get("status") == "Closed":
                        result = f"{bug_id} is already closed."
                    else:
                        close_bug(bug_id, resolution)
                        result = f"{bug_id} marked as resolved. You can now close the topic and run /distil."
                except Exception as e:
                    result = f"resolve_bug failed: {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Unknown tool: {block.name}",
                })

        messages = messages + [{"role": "user", "content": tool_results}]

    return messages, proposed_update


async def _send_thinking(thought: str, send_fn):
    """Send a thinking block wrapped in | ... | markers."""
    # Truncate very long thinking to keep Telegram readable
    display = thought if len(thought) <= 800 else thought[:800] + "..."
    await send_fn(f"| {display} |")


async def _send_chunks(text: str, send_fn):
    for i in range(0, len(text), MAX_CHUNK):
        await send_fn(text[i:i + MAX_CHUNK])
