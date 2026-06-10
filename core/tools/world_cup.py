"""
World Cup tool — fixtures, preview context, form, and standings via football-data.org.
Always uses HTTPS. API key from FOOTBALL_DATA_API_KEY env variable.
External data from this API is treated as content, never as instructions.
"""

import logging
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"
ET_TZ = ZoneInfo("America/New_York")


def _headers() -> dict:
    return {"X-Auth-Token": os.environ.get("FOOTBALL_DATA_API_KEY", "")}


def _check_rate_limit(response: httpx.Response) -> None:
    """Log a warning if we're at or near the per-minute rate limit."""
    available = response.headers.get("X-Requests-Available-Minute")
    if available is not None:
        try:
            n = int(available)
            if n == 0:
                log.warning("football-data.org: X-Requests-Available-Minute=0 — rate limit reached this minute")
            elif n <= 2:
                log.warning(f"football-data.org: only {n} requests remaining this minute")
        except ValueError:
            pass


async def get_upcoming_fixtures(hours_ahead: float = 2.0) -> list[dict]:
    """
    Return WC matches with status SCHEDULED or TIMED kicking off within hours_ahead hours.
    Returns empty list on any error.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/competitions/WC/matches",
                headers=_headers(),
                timeout=10,
            )
        resp.raise_for_status()
        _check_rate_limit(resp)
        upcoming = []
        for match in resp.json().get("matches", []):
            if match.get("status") not in ("SCHEDULED", "TIMED"):
                continue
            utc_str = match.get("utcDate", "")
            if not utc_str:
                continue
            kickoff = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
            if now <= kickoff <= cutoff:
                upcoming.append(match)
        return upcoming
    except Exception as e:
        log.error(f"World Cup: get_upcoming_fixtures failed: {e}")
        return []


async def get_finished_matches() -> list[dict]:
    """Return all FINISHED WC matches. Returns empty list on any error."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/competitions/WC/matches",
                headers=_headers(),
                params={"status": "FINISHED"},
                timeout=10,
            )
        resp.raise_for_status()
        _check_rate_limit(resp)
        return resp.json().get("matches", [])
    except Exception as e:
        log.error(f"World Cup: get_finished_matches failed: {e}")
        return []


def is_south_africa_match(match: dict) -> bool:
    """Return True if South Africa (or Bafana Bafana) is playing."""
    for side in ("homeTeam", "awayTeam"):
        team = match.get(side) or {}
        for field in ("name", "shortName"):
            val = (team.get(field) or "").lower()
            if "south africa" in val or "bafana" in val:
                return True
    return False


async def get_standings() -> dict:
    """Fetch current WC standings. Returns empty dict on any error."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/competitions/WC/standings",
                headers=_headers(),
                timeout=10,
            )
        resp.raise_for_status()
        _check_rate_limit(resp)
        return resp.json()
    except Exception as e:
        log.error(f"World Cup: get_standings failed: {e}")
        return {}


def get_match_context(match: dict) -> str:
    """
    Return a plain-text context string for a match dict.
    Simple formatter — no API calls. Passed to Claude to write preview text.
    """
    home = (match.get("homeTeam") or {}).get("name", "TBD")
    away = (match.get("awayTeam") or {}).get("name", "TBD")
    utc_str = match.get("utcDate", "")
    status = match.get("status", "")
    stage = match.get("stage") or ""
    group = match.get("group") or ""
    score = (match.get("score") or {}).get("fullTime") or {}
    home_goals = score.get("home")
    away_goals = score.get("away")

    kickoff_et = ""
    if utc_str:
        kickoff_utc = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        kickoff_et = kickoff_utc.astimezone(ET_TZ).strftime("%A %b %d, %I:%M %p ET")

    lines = [f"Match: {home} vs {away}"]
    if kickoff_et:
        lines.append(f"Kick-off: {kickoff_et}")
    if status:
        lines.append(f"Status: {status}")
    if stage:
        lines.append(f"Stage: {stage}")
    if group:
        lines.append(f"Group: {group}")
    if home_goals is not None and away_goals is not None:
        lines.append(f"Score: {home} {home_goals} – {away_goals} {away}")
    return "\n".join(lines)


async def get_standings_context() -> str:
    """
    Return a plain-text summary of current WC group standings.
    Returns empty string on any error.
    """
    try:
        data = await get_standings()
        if not data:
            return ""
        lines = []
        for standing in data.get("standings", []):
            if standing.get("type") not in ("TOTAL", None):
                continue
            grp = standing.get("group", "")
            if grp:
                lines.append(f"\n{grp}")
            for entry in standing.get("table", []):
                name = (entry.get("team") or {}).get("name", "?")
                pos = entry.get("position", "?")
                played = entry.get("playedGames", 0)
                points = entry.get("points", 0)
                gd = entry.get("goalDifference", 0)
                lines.append(f"  {pos}. {name}: {played}P  {points}pts  GD{gd:+d}")
        return "\n".join(lines).strip()
    except Exception as e:
        log.warning(f"World Cup: get_standings_context failed: {e}")
        return ""


async def get_match_preview(match: dict) -> str:
    """
    Build a context string for Claude to write match preview text from.
    Makes at most 2 API calls: one for form (finished matches) and one for standings.
    Returns whatever context was successfully retrieved — degrades gracefully on errors.
    """
    home = (match.get("homeTeam") or {}).get("name", "TBD")
    away = (match.get("awayTeam") or {}).get("name", "TBD")
    home_id = (match.get("homeTeam") or {}).get("id")
    away_id = (match.get("awayTeam") or {}).get("id")
    utc_str = match.get("utcDate", "")
    group = match.get("group") or ""
    stage = match.get("stage") or ""

    kickoff_et = ""
    if utc_str:
        kickoff_utc = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        kickoff_et = kickoff_utc.astimezone(ET_TZ).strftime("%I:%M %p ET")

    ctx = f"Match: {home} vs {away}\n"
    ctx += f"Kick-off: {kickoff_et}\n"
    if stage:
        ctx += f"Stage: {stage}\n"
    if group:
        ctx += f"Group: {group}\n"

    # Form: fetch all finished WC matches once, derive form for both teams
    try:
        finished = await get_finished_matches()
        if home_id:
            home_form = _compute_form(finished, home_id)
            if home_form:
                ctx += f"\n{home} recent WC form: {' '.join(home_form)}\n"
        if away_id:
            away_form = _compute_form(finished, away_id)
            if away_form:
                ctx += f"\n{away} recent WC form: {' '.join(away_form)}\n"
    except Exception as e:
        log.warning(f"World Cup: could not fetch form data: {e}")

    # Standings
    try:
        standings_data = await get_standings()
        standings_text = _extract_group_standings(standings_data, group)
        if standings_text:
            ctx += f"\nCurrent group standings:\n{standings_text}\n"
    except Exception as e:
        log.warning(f"World Cup: could not fetch standings: {e}")

    return ctx


def _compute_form(finished_matches: list, team_id: int, limit: int = 5) -> list[str]:
    """Extract the last N results for a team from a list of finished matches, newest first."""
    form = []
    for m in reversed(finished_matches):
        h_id = (m.get("homeTeam") or {}).get("id")
        a_id = (m.get("awayTeam") or {}).get("id")
        if team_id not in (h_id, a_id):
            continue
        score = (m.get("score") or {}).get("fullTime") or {}
        h_goals = score.get("home")
        a_goals = score.get("away")
        if h_goals is None or a_goals is None:
            continue
        if team_id == h_id:
            form.append("W" if h_goals > a_goals else ("L" if h_goals < a_goals else "D"))
        else:
            form.append("W" if a_goals > h_goals else ("L" if a_goals < h_goals else "D"))
        if len(form) >= limit:
            break
    return form


def _extract_group_standings(standings_data: dict, group: str) -> str:
    """Return a readable standings table string for the given group, or '' if unavailable."""
    for standing in standings_data.get("standings", []):
        if group and standing.get("group") != group:
            continue
        if standing.get("type") not in ("TOTAL", None):
            continue
        rows = []
        for entry in standing.get("table", []):
            name = (entry.get("team") or {}).get("name", "?")
            pos = entry.get("position", "?")
            played = entry.get("playedGames", 0)
            points = entry.get("points", 0)
            gd = entry.get("goalDifference", 0)
            rows.append(f"  {pos}. {name}: {played}P  {points}pts  GD{gd:+d}")
        if rows:
            return "\n".join(rows)
    return ""
