"""
Council tool — convenes a structured multi-voice brainstorm on an idea.
Creates a Telegram topic, runs two rounds (independent takes then debate),
posts a synthesis, and returns a brief summary to Charlie.

All council members and synthesis run on Sonnet with extended thinking.
Member budget: 2000 tokens. Synthesis budget: 6000 tokens.
"""

import asyncio
import logging
import os

import anthropic

from core import state
from core.history import save_message

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MEMBER_THINKING_BUDGET = 2000
SYNTHESIS_THINKING_BUDGET = 6000
MAX_TELEGRAM_MSG = 4000

# ---------------------------------------------------------------------------
# Council member definitions
# ---------------------------------------------------------------------------

COUNCIL_MEMBERS = {
    "conservative": {
        "label": "The Conservative",
        "system": (
            "You are The Conservative — a council member whose lens is risk, process, and what could go wrong. "
            "You don't dismiss ideas — you stress-test them. Identify the specific conditions under which this fails. "
            "Name the risks that are being underestimated. Flag the assumptions that haven't been validated. "
            "Be concrete — general caution is useless. Your job is not to kill the idea but to surface what must be "
            "addressed before anyone commits. 3–4 focused paragraphs."
        ),
    },
    "opportunist": {
        "label": "The Opportunist",
        "system": (
            "You are The Opportunist — a council member whose lens is upside, timing, and momentum. "
            "You see opportunity where others see risk. Identify the specific opportunity here — what window exists, "
            "why now, what advantage could be seized. Don't cheerlead — make the affirmative case as specifically as "
            "you can. What would have to be true for this to be a genuinely great move? 3–4 focused paragraphs."
        ),
    },
    "long_term_thinker": {
        "label": "The Long-term Thinker",
        "system": (
            "You are The Long-term Thinker — a council member whose lens is a 10-year horizon. "
            "Ignore near-term noise — trace the compounding effects. Where does this idea lead if it works? "
            "What does it foreclose? What does it make possible? What habits, dependencies, or trajectories does it "
            "set in motion? Think in systems, not snapshots. The question is not whether this works next year but "
            "what kind of future it builds toward. 3–4 focused paragraphs."
        ),
    },
    "pragmatist": {
        "label": "The Pragmatist",
        "system": (
            "You are The Pragmatist — a council member whose lens is execution. Strip away the theory — how does "
            "this actually get done? Name the first three concrete steps. Name the resources, skills, and time required. "
            "Identify the first place this will get stuck and why. Distinguish between what sounds achievable and what "
            "is achievable given real-world constraints. Don't debate the merit of the idea — focus entirely on whether "
            "and how it can be executed. 3–4 focused paragraphs."
        ),
    },
    "financial_skeptic": {
        "label": "The Financial Skeptic",
        "system": (
            "You are The Financial Skeptic — a council member whose lens is financial reality. What does this cost — "
            "in money, time, and opportunity cost? What does it need to generate to justify itself, and is that realistic? "
            "Where are the hidden costs? What assumptions about revenue, growth, or return are baked in, and how sensitive "
            "is the case to those assumptions? You are not opposed to spending — you are opposed to spending without "
            "clear-eyed accounting. 3–4 focused paragraphs."
        ),
    },
    "user_advocate": {
        "label": "The User Advocate",
        "system": (
            "You are The User Advocate — a council member whose lens is the person this is meant to serve. "
            "Who specifically benefits from this? What problem does it solve for them, and how acutely do they feel "
            "that problem? Is this what they actually need, or what someone thinks they need? What friction or resistance "
            "will they encounter? The best ideas solve real problems for real people — your job is to keep that test "
            "front and centre. 3–4 focused paragraphs."
        ),
    },
    "contrarian": {
        "label": "The Contrarian",
        "system": (
            "You are The Contrarian — a council member whose job is to challenge the premise. Not to find reasons the "
            "idea fails on its own terms — but to question whether this is the right idea at all. What is the opposite "
            "approach, and why might it be better? What assumption is everyone treating as fixed that might be up for "
            "grabs? What would a genuinely different thinker propose instead? Be specific — a vague alternative is not "
            "a real challenge. 3–4 focused paragraphs."
        ),
    },
    "minimalist": {
        "label": "The Minimalist",
        "system": (
            "You are The Minimalist — a council member whose lens is simplicity and scope. What is the smallest version "
            "of this idea that still captures the core value? What is being added out of habit, fear, or ambition that "
            "isn't strictly necessary? Complexity is a cost — every element added must justify itself. Strip the idea "
            "back to its essential form and ask whether that alone is worth pursuing. 3–4 focused paragraphs."
        ),
    },
}

_ROUND_TWO_INSTRUCTION = (
    "\n\nYou have now read the full Round 1. Engage with the group — don't restate your position. "
    "Identify the one or two takes that most sharply conflict with your view and respond to them directly. "
    "Name who you're engaging with. Where do you stand firm? Where, if anywhere, has your thinking shifted? "
    "2–3 focused paragraphs."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _call_member(client, system: str, user_message: str, thinking_budget: int) -> str:
    response = await client.messages.create(
        model=MODEL,
        max_tokens=4000,
        thinking={"type": "enabled", "budget_tokens": thinking_budget},
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return next(
        (block.text.strip() for block in response.content if hasattr(block, "text")),
        "(no response)",
    )


async def _post(app, group_id: str, thread_id: int, text: str):
    """Post to the council topic in chunks and save to history."""
    for i in range(0, len(text), MAX_TELEGRAM_MSG):
        await app.bot.send_message(
            chat_id=group_id,
            message_thread_id=thread_id,
            text=text[i:i + MAX_TELEGRAM_MSG],
        )
    save_message(thread_id, "assistant", text)


def _format_round_one(takes: dict, current_key: str) -> str:
    """Format all Round 1 takes for a member's Round 2 prompt."""
    parts = []
    for key, (label, take) in takes.items():
        prefix = "Your own Round 1 take" if key == current_key else label
        parts.append(f"{prefix}:\n{take}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def convene_council(
    idea: str,
    members: list,
    topic_name: str,
    context: str = "",
) -> str:
    """
    Run a full council session:
      1. Create Telegram topic
      2. Round 1 — parallel independent takes
      3. Round 2 — parallel debate (each member reads all Round 1 takes)
      4. Synthesis — single Sonnet call reading the full transcript
    Returns a brief summary string for Charlie to relay to Jonathan.
    """
    app = state.get_app()
    group_id = state.group_id
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    valid_keys = [k for k in members if k in COUNCIL_MEMBERS]
    if not valid_keys:
        return "No valid council members specified. Valid keys: " + ", ".join(COUNCIL_MEMBERS)

    member_labels = [COUNCIL_MEMBERS[k]["label"] for k in valid_keys]

    # --- Create topic ---
    forum_topic = await app.bot.create_forum_topic(
        chat_id=group_id,
        name=topic_name[:128],
    )
    thread_id = forum_topic.message_thread_id

    intro = (
        f"Council convened.\n\n"
        f"Idea: {idea}\n\n"
        f"Members: {', '.join(member_labels)}\n\n"
        f"Round 1 — independent takes. Each member is responding cold, without seeing the others."
    )
    await _post(app, group_id, thread_id, intro)

    # --- Round 1 ---
    base_prompt = f"The idea:\n\n{idea}"
    if context:
        base_prompt += f"\n\nContext:\n{context}"

    async def _r1(key):
        take = await _call_member(
            client,
            COUNCIL_MEMBERS[key]["system"],
            base_prompt,
            MEMBER_THINKING_BUDGET,
        )
        return key, COUNCIL_MEMBERS[key]["label"], take

    r1_results = await asyncio.gather(*[_r1(k) for k in valid_keys])

    round_one: dict = {}
    for key, label, take in r1_results:
        round_one[key] = (label, take)
        await _post(app, group_id, thread_id, f"{label}\n\n{take}")

    # --- Round 2 ---
    await _post(
        app, group_id, thread_id,
        "Round 2 — Debate. Each member has read the full Round 1 and now responds to the group.",
    )

    async def _r2(key):
        others = _format_round_one(round_one, key)
        system_r2 = COUNCIL_MEMBERS[key]["system"] + _ROUND_TWO_INSTRUCTION
        prompt_r2 = f"The idea:\n\n{idea}\n\nRound 1 from the council:\n\n{others}"
        if context:
            prompt_r2 += f"\n\nContext:\n{context}"
        rebuttal = await _call_member(client, system_r2, prompt_r2, MEMBER_THINKING_BUDGET)
        return key, COUNCIL_MEMBERS[key]["label"], rebuttal

    r2_results = await asyncio.gather(*[_r2(k) for k in valid_keys])

    round_two: dict = {}
    for key, label, rebuttal in r2_results:
        round_two[key] = (label, rebuttal)
        await _post(app, group_id, thread_id, f"{label} — Round 2\n\n{rebuttal}")

    # --- Synthesis ---
    transcript = ["ROUND 1\n"]
    for key, (label, take) in round_one.items():
        transcript.append(f"{label}:\n{take}")
    transcript.append("\nROUND 2\n")
    for key, (label, rebuttal) in round_two.items():
        transcript.append(f"{label}:\n{rebuttal}")
    full_transcript = "\n\n---\n\n".join(transcript)

    synthesis_system = (
        "You are a neutral council moderator synthesising a structured brainstorm. "
        "You have the full transcript of two rounds of debate. "
        "Your job: identify the sharpest disagreements, any emerging consensus, the strongest arguments made on any side, "
        "and the 2–3 questions this council has surfaced that the person bringing the idea most needs to answer. "
        "Be direct and specific. Do not summarise each member — synthesise across them. "
        "4–6 focused paragraphs."
    )
    synthesis_prompt = f"Idea: {idea}\n\nFull council transcript:\n\n{full_transcript}"

    synthesis = await _call_member(
        client, synthesis_system, synthesis_prompt, SYNTHESIS_THINKING_BUDGET
    )
    await _post(app, group_id, thread_id, f"Synthesis\n\n{synthesis}")

    log.info(f"Council session complete: {topic_name} (thread_id={thread_id})")
    return (
        f"Council session complete. Full session posted in the '{topic_name}' topic.\n\n"
        f"Synthesis:\n{synthesis}"
    )
