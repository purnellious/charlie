"""
World Cup 2026 scheduler jobs.
Runs every 5 minutes during the tournament.

Jobs:
  check_upcoming_matches — 60-min SA preview + 30-min standard notifications
  check_sa_results       — post-game result messages for SA matches

Notifications are sent to a dedicated World Cup Telegram topic (auto-created on first use,
thread_id persisted in data/wc_notified.json). Uses proactive_send so Charlie has memory
of every notification when Jonathan replies.

State file: data/wc_notified.json
  notified_30min: [match_id, ...]      — 30-min notifications sent
  notified_60min: [match_id, ...]      — 60-min SA notifications sent
  result_sent:    [match_id, ...]      — post-game notifications sent
  wc_topic_id:    int | null           — Telegram topic thread_id for WC notifications
  _seeded:        bool                 — True after startup seed of old finished matches
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from telegram.ext import Application

from core.scheduler import proactive_send

log = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")
DATA_DIR = Path(__file__).parent.parent / "data"
NOTIFIED_PATH = DATA_DIR / "wc_notified.json"

MODEL = os.getenv("CHARLIE_MODEL", "claude-sonnet-4-6")

_EMPTY_STATE = {
    "notified_30min": [],
    "notified_60min": [],
    "result_sent": [],
    "wc_topic_id": None,
    "_seeded": False,
}


def _load_state() -> dict:
    if NOTIFIED_PATH.exists():
        try:
            return json.loads(NOTIFIED_PATH.read_text())
        except Exception as e:
            log.warning(f"World Cup: could not load state file, using empty state: {e}")
    return dict(_EMPTY_STATE)


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    NOTIFIED_PATH.write_text(json.dumps(state, indent=2))


async def _get_or_create_wc_topic(app: Application, group_id: str) -> int | None:
    """
    Return the World Cup notification topic thread_id.
    Checks WORLD_CUP_TOPIC_ID env var first, then state file, then creates one.
    """
    env_val = os.getenv("WORLD_CUP_TOPIC_ID", "").strip()
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            log.warning(f"World Cup: WORLD_CUP_TOPIC_ID={env_val!r} is not an integer — ignoring")

    state = _load_state()
    if state.get("wc_topic_id"):
        return state["wc_topic_id"]

    try:
        topic = await app.bot.create_forum_topic(
            chat_id=group_id,
            name="⚽ World Cup 2026",
        )
        thread_id = topic.message_thread_id
        state["wc_topic_id"] = thread_id
        _save_state(state)
        log.info(f"World Cup: created notification topic (thread_id={thread_id})")
        return thread_id
    except Exception as e:
        log.error(f"World Cup: could not create notification topic: {e}")
        return None


async def _call_claude(system: str, user: str, max_tokens: int = 300) -> str:
    """Minimal Claude call — generates preview/result text from context data."""
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    response = await client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text.strip()


async def _seed_finished_results() -> None:
    """
    On first startup, record all currently-FINISHED SA matches in result_sent
    to avoid sending stale post-game notifications for matches played before
    the tracker was installed.
    """
    from core.tools.world_cup import get_finished_matches, is_south_africa_match

    state = _load_state()
    if state.get("_seeded"):
        return

    try:
        finished = await get_finished_matches()
        seeded = 0
        for m in finished:
            if is_south_africa_match(m):
                match_id = str(m.get("id", ""))
                if match_id and match_id not in state["result_sent"]:
                    state["result_sent"].append(match_id)
                    seeded += 1
        state["_seeded"] = True
        _save_state(state)
        log.info(f"World Cup: seeded result_sent with {seeded} pre-existing finished SA match(es)")
    except Exception as e:
        log.warning(f"World Cup: seeding failed (will retry next tick): {e}")


async def check_upcoming_matches(app: Application) -> None:
    """
    Every 5 minutes: check for WC matches within 1.1 hours.
    - SA match 55–65 min out → rich 60-min Bafana Bafana notification
    - Any match 25–35 min out → standard 30-min notification
    Deduplicates via wc_notified.json.
    """
    from core.tools.world_cup import get_upcoming_fixtures, is_south_africa_match, get_match_preview

    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return

    try:
        fixtures = await get_upcoming_fixtures(hours_ahead=1.1)
    except Exception as e:
        log.error(f"World Cup: get_upcoming_fixtures failed: {e}")
        return

    if not fixtures:
        return

    state = _load_state()
    now = datetime.now(timezone.utc)

    for match in fixtures:
        match_id = str(match.get("id", ""))
        if not match_id:
            continue

        utc_str = match.get("utcDate", "")
        if not utc_str:
            continue
        kickoff = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        mins_to_ko = (kickoff - now).total_seconds() / 60

        home = (match.get("homeTeam") or {}).get("name", "TBD")
        away = (match.get("awayTeam") or {}).get("name", "TBD")
        kickoff_et = kickoff.astimezone(ET_TZ).strftime("%I:%M %p ET")
        is_sa = is_south_africa_match(match)

        # 60-minute SA window: 55–65 mins to kick-off
        if is_sa and 55 <= mins_to_ko <= 65 and match_id not in state["notified_60min"]:
            try:
                context = await get_match_preview(match)
                preview = await _call_claude(
                    system=(
                        "You are writing a pre-match hype message for a passionate South African "
                        "football fan (Jonathan). Be energetic, warm, and emotionally engaged. "
                        "Bafana Bafana are his team and he cares deeply. Use the data provided. "
                        "Write plain text only — no markdown, no headers, no bullet points."
                    ),
                    user=(
                        f"Write a 4-6 sentence rich match preview covering: what's at stake in the group, "
                        f"recent form, and one or two things to watch. Data:\n\n{context}"
                    ),
                    max_tokens=450,
                )
                thread_id = await _get_or_create_wc_topic(app, group_id)
                if thread_id:
                    msg = (
                        f"🇿🇦 BAFANA BAFANA IN 60 MINUTES!\n"
                        f"{home} vs {away}\n\n"
                        f"{preview}\n\n"
                        f"Kick-off: {kickoff_et}"
                    )
                    await proactive_send(app, group_id, thread_id, msg)
                    state["notified_60min"].append(match_id)
                    _save_state(state)
                    log.info(f"World Cup: sent 60-min SA notification for match {match_id}")
            except Exception as e:
                log.error(f"World Cup: SA 60-min notification failed for {match_id}: {e}")

        # 30-minute window: 25–35 mins to kick-off (all matches)
        elif 25 <= mins_to_ko <= 35 and match_id not in state["notified_30min"]:
            try:
                context = await get_match_preview(match)
                preview = await _call_claude(
                    system=(
                        "You are writing a quick pre-match preview for a football fan. "
                        "Be concise, energetic, and topical. Use the data provided. "
                        "Write plain text only — no markdown, no headers, no bullet points."
                    ),
                    user=(
                        f"Write 2-3 sentences of match preview. Data:\n\n{context}"
                    ),
                    max_tokens=200,
                )
                thread_id = await _get_or_create_wc_topic(app, group_id)
                if thread_id:
                    msg = (
                        f"⚽ Kick-off in 30 minutes: {home} vs {away}\n\n"
                        f"{preview}\n\n"
                        f"Kick-off: {kickoff_et}"
                    )
                    await proactive_send(app, group_id, thread_id, msg)
                    state["notified_30min"].append(match_id)
                    _save_state(state)
                    log.info(f"World Cup: sent 30-min notification for match {match_id}")
            except Exception as e:
                log.error(f"World Cup: 30-min notification failed for {match_id}: {e}")


async def check_sa_results(app: Application) -> None:
    """
    Every 5 minutes: check for newly-finished SA matches and send post-game result messages.
    Seeds result_sent on first run to avoid replaying old results.
    """
    from core.tools.world_cup import get_finished_matches, is_south_africa_match

    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_id:
        return

    # Seed on first run so we don't replay results from before the tracker was installed
    await _seed_finished_results()

    state = _load_state()

    try:
        finished_matches = await get_finished_matches()
    except Exception as e:
        log.error(f"World Cup: check_sa_results API call failed: {e}")
        return

    for match in finished_matches:
        match_id = str(match.get("id", ""))
        if not match_id or match_id in state["result_sent"]:
            continue
        if not is_south_africa_match(match):
            continue

        home = (match.get("homeTeam") or {}).get("name", "TBD")
        away = (match.get("awayTeam") or {}).get("name", "TBD")
        score = (match.get("score") or {}).get("fullTime") or {}
        home_goals = score.get("home", "?")
        away_goals = score.get("away", "?")

        context = (
            f"Match: {home} vs {away}\n"
            f"Final score: {home} {home_goals} – {away_goals} {away}\n"
        )

        try:
            summary = await _call_claude(
                system=(
                    "You are writing a post-game message for Jonathan, a passionate South African "
                    "football fan. Be warm, honest, and heartfelt — whether it's a win, draw, or "
                    "loss. Capture the emotion without being over-the-top. "
                    "Write plain text only — no markdown, no headers, no bullet points."
                ),
                user=(
                    f"Write 2-3 sentences summarising this result with heart. Data:\n\n{context}"
                ),
                max_tokens=200,
            )
            thread_id = await _get_or_create_wc_topic(app, group_id)
            if thread_id:
                msg = (
                    f"🇿🇦 Full time: {home} {home_goals} – {away_goals} {away}\n\n"
                    f"{summary}"
                )
                await proactive_send(app, group_id, thread_id, msg)
                state["result_sent"].append(match_id)
                _save_state(state)
                log.info(f"World Cup: sent SA result notification for match {match_id}")
        except Exception as e:
            log.error(f"World Cup: SA result notification failed for {match_id}: {e}")
