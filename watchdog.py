"""
Charlie watchdog — a standalone, independent health check.

Deliberately NOT part of the core/ package and NOT run inside Charlie's own
process: if Charlie's process is stuck (not crashed — see below), it needs a
completely separate process to notice and act. stdlib only, no venv/imports
from core/ — this must keep working even if Charlie's own dependencies are
broken.

Checks two things, both required for "healthy":
  1. com.charlie has a running PID (launchctl list).
  2. data/heartbeat.txt was written recently (Charlie's own scheduler writes
     it every 3 minutes, but only right after a genuinely successful
     Telegram API call — see core/scheduler.py's _heartbeat job). PID-only
     checking (Bartie's watchdog.py approach) would NOT catch a process that
     is alive but internally stuck — e.g. a stale DNS resolver state after a
     reboot, which is exactly what happened on 2026-07-30 and caused a
     ~12.5 hour silent outage.

On newly-detected unhealthy: alert once and attempt one automatic restart
(launchctl kickstart -k). If still unhealthy on the next check despite the
restart, alert again and stop auto-restarting for this outage (to avoid a
restart loop masking a deeper problem) — keep alerting on a capped interval
until healthy again. On recovery: one "back up" message, reset state.

Run via com.charlie.watchdog.plist, StartInterval=300 (every 5 minutes).
"""
import json
import os
import subprocess
import time
import urllib.request
import urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")
HEARTBEAT_PATH = os.path.join(SCRIPT_DIR, "data", "heartbeat.txt")
STATE_PATH = os.path.join(SCRIPT_DIR, "data", "watchdog_state.json")

HEARTBEAT_MAX_AGE_SECONDS = 10 * 60  # generous buffer over the 3-min write interval
STARTUP_GRACE_SECONDS = 6 * 60  # covers at least one heartbeat cycle after a fresh (re)start
MAX_ALERTS_PER_OUTAGE = 10
LABEL = "com.charlie"


def _load_env():
    values = {}
    try:
        with open(ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


def _load_state():
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"unhealthy_since": None, "already_restarted": False, "alert_count": 0, "restart_succeeded": None}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def _send_alert(token, group_id, text):
    if not token or not group_id:
        print("watchdog: cannot send alert, TELEGRAM_BOT_TOKEN/TELEGRAM_GROUP_ID missing from .env")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": group_id, "text": f"[watchdog] {text}"}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=15)
    except Exception as e:
        print(f"watchdog: alert send failed: {e}")


def _process_status():
    """
    Returns (running: bool, pid: str|None). PID comes straight out of
    `launchctl list`'s own text output rather than a second lookup, since
    that's the exact process instance being asked about.
    """
    try:
        result = subprocess.run(
            ["launchctl", "list", LABEL],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('"PID"'):
                pid = line.split("=")[1].strip().rstrip(";").strip()
                return True, pid
        return True, None
    except Exception:
        return False, None


def _process_uptime_seconds(pid):
    """
    Seconds since the process at `pid` started. macOS's BSD `ps` has no
    `etimes` (seconds) keyword — that's GNU/Linux-only — so this parses the
    portable `etime` format instead: "[[dd-]hh:]mm:ss". None if unavailable.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        days = 0
        if "-" in raw:
            days_str, raw = raw.split("-", 1)
            days = int(days_str)
        parts = [int(p) for p in raw.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        hours, minutes, seconds = parts
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    except Exception:
        return None


def _heartbeat_age_seconds():
    try:
        with open(HEARTBEAT_PATH, "r") as f:
            last = float(f.read().strip())
        return time.time() - last
    except (FileNotFoundError, ValueError):
        return None


def _restart_charlie():
    """
    Returns (True, "") if `launchctl kickstart` actually succeeded, or
    (False, detail) otherwise — e.g. com.charlie was fully unloaded (not just
    crashed), in which case kickstart fails immediately and no restart
    happens. Checking returncode matters here: silently treating a failed
    kickstart as a successful restart would consume this outage's one
    auto-restart attempt for nothing and tell Jonathan Charlie "was
    restarted" when it wasn't.
    """
    uid = os.getuid()
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return True, ""
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        print(f"watchdog: restart attempt failed: {detail}")
        return False, detail
    except Exception as e:
        print(f"watchdog: restart attempt failed: {e}")
        return False, str(e)


def main():
    env = _load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    group_id = env.get("TELEGRAM_GROUP_ID")

    process_running, pid = _process_status()
    uptime = _process_uptime_seconds(pid) if pid else None
    in_startup_grace = uptime is not None and uptime < STARTUP_GRACE_SECONDS

    heartbeat_age = _heartbeat_age_seconds()
    heartbeat_fresh = heartbeat_age is not None and heartbeat_age < HEARTBEAT_MAX_AGE_SECONDS
    # Right after com.charlie (re)starts, no heartbeat has been written yet —
    # the job only fires every 3 minutes. Without a grace period, the watchdog
    # (which also runs at its own boot, ~90s after a fresh start in practice)
    # would treat that gap as a genuine outage and false-alarm/restart on
    # every single restart. Only the process-running check applies during
    # the grace window; heartbeat freshness is enforced once it elapses.
    healthy = process_running and (heartbeat_fresh or in_startup_grace)

    state = _load_state()

    if healthy:
        if state["unhealthy_since"] is not None:
            _send_alert(token, group_id, "Charlie is back to healthy.")
            state = {"unhealthy_since": None, "already_restarted": False, "alert_count": 0}
            _save_state(state)
        return

    reason = []
    if not process_running:
        reason.append("process not running")
    if not heartbeat_fresh:
        age_desc = "no heartbeat file" if heartbeat_age is None else f"heartbeat is {int(heartbeat_age)}s old"
        reason.append(age_desc)
    reason_str = ", ".join(reason)

    newly_unhealthy = state["unhealthy_since"] is None
    if newly_unhealthy:
        state["unhealthy_since"] = time.time()
        state["already_restarted"] = False
        state["alert_count"] = 0

    if newly_unhealthy or not state["already_restarted"]:
        # The `not already_restarted` branch shouldn't normally trigger (a
        # restart is always attempted immediately on first detection), but
        # guards against a future unhealthy tick leaving the outage stuck
        # with no restart attempt at all.
        restarted, detail = _restart_charlie()
        state["already_restarted"] = True
        state["restart_succeeded"] = restarted
        if restarted:
            msg = f"Charlie appears unhealthy ({reason_str}). Attempted one automatic restart."
        else:
            msg = (
                f"Charlie appears unhealthy ({reason_str}). Automatic restart FAILED to even "
                f"run ({detail}) — needs manual attention now, no further auto-restart will be tried."
            )
        _send_alert(token, group_id, msg)
        state["alert_count"] += 1
    else:
        if state["alert_count"] == 1:
            if state.get("restart_succeeded"):
                msg = f"Charlie was restarted but is still unhealthy ({reason_str}). Needs manual attention."
            else:
                msg = f"Charlie is still unhealthy ({reason_str}). The earlier automatic restart attempt did not run successfully."
            _send_alert(token, group_id, msg)
            state["alert_count"] += 1
        elif state["alert_count"] < MAX_ALERTS_PER_OUTAGE:
            _send_alert(token, group_id, f"Charlie is still unhealthy ({reason_str}).")
            state["alert_count"] += 1

    _save_state(state)


if __name__ == "__main__":
    main()
