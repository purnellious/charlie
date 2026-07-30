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
            "or make a system change. Claude Code will execute it autonomously on the local "
            "machine. Before calling this, work through the Principle 11 Pre-Build Checklist "
            "in principles.md — tier, scope, and checklist below are how you show that work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Full description of what to build or change."
                },
                "tier": {
                    "type": "string",
                    "enum": ["1", "2", "3"],
                    "description": (
                        "Build Tier per Principle 11. 1=Low: copy/prompt/log changes, no "
                        "persistence or behaviour change. 2=Standard: new tool, new scheduled "
                        "job, edits to agent.py/bot.py/scheduler.py, or anything touching "
                        "bugs.md/principles.md/charlie.md. 3=High: touches .env or "
                        "credentials, shares data with a third party, deletes data, touches "
                        "financial/legal data, or changes deployment/launchd config. "
                        "If unclear, use the higher tier."
                    )
                },
                "scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Files or paths this build is expected to touch, e.g. "
                        "['core/agent.py', 'bugs.md']. Checked against what Claude Code "
                        "actually changes — anything outside this list blocks the "
                        "auto-commit and is surfaced to Jonathan instead of being pushed."
                    )
                },
                "checklist": {
                    "type": "string",
                    "description": (
                        "Your answers to the remaining Principle 11 questions: what problem "
                        "this solves, whether a simpler solution exists, what data (if any) "
                        "is stored and why, the cost/complexity, and the testing plan."
                    )
                },
                "jonathan_confirmed_risk": {
                    "type": "boolean",
                    "description": (
                        "Tier 3 only. Set true only if Jonathan has explicitly confirmed, in "
                        "this conversation, that he wants to proceed given the specific risk "
                        "named to him. Leave false/omitted for Tier 1-2."
                    )
                }
            },
            "required": ["task", "tier", "scope", "checklist"]
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
        "name": "convene_council",
        "description": (
            "Convene the Council — a structured multi-voice brainstorm on an idea. "
            "Call this only after you have had a composition discussion with Jonathan and he has confirmed the member list. "
            "The tool creates a new Telegram topic, runs two rounds (independent takes then debate), posts a synthesis, "
            "and returns the synthesis for you to respond to. "
            "Valid member keys: conservative, opportunist, long_term_thinker, pragmatist, "
            "financial_skeptic, user_advocate, contrarian, minimalist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "idea": {
                    "type": "string",
                    "description": "Full description of the idea being evaluated.",
                },
                "members": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Member keys to include, e.g. ['contrarian', 'pragmatist', 'financial_skeptic'].",
                },
                "topic_name": {
                    "type": "string",
                    "description": "Short Telegram topic name, e.g. 'Council — Freelance Platform'.",
                },
                "context": {
                    "type": "string",
                    "description": "Any additional context about Jonathan's situation relevant to this idea.",
                },
            },
            "required": ["idea", "members", "topic_name"],
        },
    },
    {
        "name": "get_news_briefing",
        "description": (
            "Fetch today's news and return a formatted briefing, grouped by topic. "
            "Call this when Jonathan asks for news, the latest headlines, or a news briefing. "
            "The briefing is generated fresh each time — articles are fetched, summarised, and "
            "returned as clean text for you to relay."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "news_add_source",
        "description": (
            "Add a new RSS feed to the news sources. Use when Jonathan wants to add a new "
            "publication or topic. Confirm the name, URL, and topic before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":  {"type": "string", "description": "Display name, e.g. 'BBC World'"},
                "url":   {"type": "string", "description": "RSS feed URL"},
                "topic": {"type": "string", "description": "Topic group, e.g. 'World News', 'South Africa'"},
            },
            "required": ["name", "url", "topic"],
        },
    },
    {
        "name": "news_remove_source",
        "description": "Remove a news source by its ID. Use list_sources first to confirm the ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "integer", "description": "Source ID from news_list_sources"},
            },
            "required": ["source_id"],
        },
    },
    {
        "name": "news_list_sources",
        "description": "List all configured RSS news sources with their IDs, topics, and URLs.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "restart_charlie",
        "description": (
            "Restart the Charlie service to load a build that touched code (see the "
            "RESTART REQUIRED note from run_claude_code). Only call this after Jonathan "
            "has explicitly said to restart now — never proactively. Returns immediately; "
            "the actual result (success/failure) arrives a few seconds later as a "
            "separate message in this topic, sent independently of this process."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jonathan_confirmed": {
                    "type": "boolean",
                    "description": "Must be true — set only if Jonathan has just explicitly confirmed he wants to restart now."
                },
                "verify_script": {
                    "type": "string",
                    "description": (
                        "Optional: path (relative to the charlie root) to a standalone "
                        "script you wrote and ran during the build to test the new "
                        "feature. If provided, it's re-run after restart as an extra "
                        "check. Omit if no such script exists — do not invent one."
                    )
                }
            },
            "required": ["jonathan_confirmed"]
        }
    },
    {
        "name": "record_email_feedback",
        "description": (
            "Record a correction to an email triage verdict, keyed by the [#id] shown in "
            "an Email topic digest message. Use this when Jonathan corrects a verdict "
            "(e.g. 'that one wasn't actually urgent', 'always treat invoices from X as "
            "just FYI'). Recent corrections are fed into future triage so it calibrates "
            "over time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "integer", "description": "The [#id] from the digest message."},
                "original_verdict": {"type": "string", "description": "What the triage originally said."},
                "user_correction": {"type": "string", "description": "What Jonathan actually wants."},
                "context_snippet": {"type": "string", "description": "Sender and subject, for context."},
            },
            "required": ["email_id", "original_verdict", "user_correction"],
        },
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

- **run_claude_code** — build new capabilities, write code, or make changes to Charlie itself. \
Requires a declared tier, scope, and Principle 11 checklist. Tier 3 builds also require \
Jonathan's explicit confirmation of the named risk before you call it. If the result says \
RESTART REQUIRED, tell Jonathan plainly it's committed but not live, and ask before \
restarting — never claim something is live when a restart is pending.
- **restart_charlie** — restart the service to load a build that touched code. Only call \
this after Jonathan has explicitly said to restart now. It returns immediately; the real \
result (restarted cleanly, or failed) arrives moments later as a separate message — don't \
assume success from the immediate tool result alone
- **propose_charlie_update** — propose an update to your persistent context (charlie.md) \
when you learn something important about Jonathan. He will review before it's saved.
- **log_bug** — log a bug in bugs.md and open a dedicated Telegram topic for it
- **resolve_bug** — mark a bug as resolved after confirming a complete fix exists
- **get_news_briefing** — fetch and summarise today's news, grouped by topic
- **news_add_source** / **news_remove_source** / **news_list_sources** — manage RSS feeds
- **convene_council** — run a structured multi-voice brainstorm; have a composition discussion first \
to determine which members are relevant, confirm with Jonathan, then call this tool
- **record_email_feedback** — record a correction to an email triage verdict (keyed by the \
[#id] in an Email topic digest) so future triage calibrates over time

**Capabilities boundary:** You run exclusively on Jonathan's always-on Mac (10.0.0.119). \
You cannot directly access or execute anything on his main Mac. If Jonathan asks you to do \
something on his main Mac, tell him the exact command to run himself rather than running it \
and claiming it's done.

## Recent changes (devlog)

{devlog}

## Jonathan's context

{charlie_doc}
{f"## Archived context from past topics{chr(10)}{context_archive}" if context_archive else ""}"""


def _validate_claude_code_call(tier: str, scope: list, checklist: str, risk_confirmed: bool) -> str | None:
    """Return an error string if the run_claude_code call is missing required Principle 11
    fields, or None if it's ready to dispatch."""
    if tier not in ("1", "2", "3"):
        return (
            "Invalid or missing tier — must be '1', '2', or '3'. See Principle 11 (Build "
            "Tiers) in principles.md. Retry with a valid tier."
        )
    if not scope:
        return (
            "Missing declared scope — list the files/areas this build is expected to touch, "
            "per Principle 11. Retry with a non-empty scope."
        )
    if not checklist or not checklist.strip():
        return (
            "Missing checklist — answer the remaining Principle 11 Pre-Build Checklist "
            "questions before calling run_claude_code. Retry with a completed checklist."
        )
    if tier == "3" and not risk_confirmed:
        return (
            "Tier 3 build requires Jonathan's explicit confirmation of the named risk before "
            "proceeding. Ask him directly in this conversation, then retry with "
            "jonathan_confirmed_risk=true only once he has actually confirmed."
        )
    return None


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
    topic_id: int = 0,
    thinking_enabled: bool = True,
    thinking_budget: int = 2000,
) -> tuple[list, dict | None]:
    """
    Run one Charlie turn.

    Args:
        user_text: The user's message.
        messages: Full prior conversation history in Anthropic API format.
        send_fn: Async callable that sends a string to Telegram.
        topic_id: Telegram thread_id of the current topic — needed by restart_charlie
            so the detached restart script can report back into the right topic.
        thinking_enabled: Whether to show extended thinking.
        thinking_budget: Token budget for thinking (min 1024).

    Returns:
        (updated_messages, proposed_update)
        proposed_update is None or {"proposed_content": str, "reason": str}
    """
    from core.tools.claude_code import run as run_claude_code
    from core.tools.restart import trigger_restart

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
                tier = block.input.get("tier", "")
                scope = block.input.get("scope", [])
                checklist = block.input.get("checklist", "")
                risk_confirmed = block.input.get("jonathan_confirmed_risk", False)

                validation_error = _validate_claude_code_call(tier, scope, checklist, risk_confirmed)
                if validation_error:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": validation_error,
                        "is_error": True,
                    })
                    continue

                await send_fn(f"Running Claude Code (Tier {tier}): {task[:100]}...")
                result = await run_claude_code(task, tier=tier, scope=scope)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            elif block.name == "restart_charlie":
                if not block.input.get("jonathan_confirmed"):
                    result = "Cannot restart — Jonathan must explicitly confirm before this is called."
                else:
                    result = await trigger_restart(
                        topic_id=topic_id,
                        verify_script=block.input.get("verify_script", ""),
                    )
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

            elif block.name == "convene_council":
                await send_fn("Convening the council — this will take a few minutes...")
                from core.tools.council import convene_council
                result = await convene_council(
                    idea=block.input.get("idea", ""),
                    members=block.input.get("members", []),
                    topic_name=block.input.get("topic_name", "Council"),
                    context=block.input.get("context", ""),
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            elif block.name == "get_news_briefing":
                await send_fn("Fetching the news...")
                from core.tools.news import tool_get_news_briefing
                result = tool_get_news_briefing()
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            elif block.name == "news_add_source":
                from core.tools.news import tool_add_source
                result = tool_add_source(
                    name=block.input.get("name", ""),
                    url=block.input.get("url", ""),
                    topic=block.input.get("topic", ""),
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            elif block.name == "news_remove_source":
                from core.tools.news import tool_remove_source
                result = tool_remove_source(source_id=block.input.get("source_id", 0))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            elif block.name == "news_list_sources":
                from core.tools.news import tool_list_sources
                result = tool_list_sources()
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            elif block.name == "record_email_feedback":
                from core.tools.email import record_feedback
                result = record_feedback(
                    email_id=block.input.get("email_id", 0),
                    original_verdict=block.input.get("original_verdict", ""),
                    user_correction=block.input.get("user_correction", ""),
                    context_snippet=block.input.get("context_snippet", ""),
                )
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
