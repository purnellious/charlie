"""
Restart tool — restarts the Charlie service to load a completed build, then verifies
and reports back. See BUG-002.

The actual stop/start/verify sequence runs in a detached shell script rather than in
this process, because this process (com.charlie) is what gets killed partway through —
it can't reliably await its own restart and report the result afterward.
"""

import asyncio
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

CHARLIE_ROOT = Path(__file__).parent.parent.parent
RESTART_SCRIPT = CHARLIE_ROOT / "core" / "tools" / "restart_and_verify.sh"


async def trigger_restart(topic_id: int, verify_script: str = "") -> str:
    """Launch the detached restart-and-verify script. Returns immediately — the actual
    result (success/failure) arrives later as a separate Telegram message in topic_id."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()

    if not bot_token or not group_id:
        return "Cannot restart — TELEGRAM_BOT_TOKEN or TELEGRAM_GROUP_ID missing from environment."

    if not RESTART_SCRIPT.exists():
        return f"Cannot restart — {RESTART_SCRIPT} not found."

    try:
        await asyncio.create_subprocess_exec(
            "/bin/bash", str(RESTART_SCRIPT), bot_token, group_id, str(topic_id), verify_script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(CHARLIE_ROOT),
            start_new_session=True,  # detach: must survive this process being killed by the restart
        )
    except Exception as e:
        log.error(f"Failed to launch restart script: {e}")
        return f"Failed to launch restart script: {e}"

    return (
        "Restart initiated — this process will go down shortly. A confirmation message "
        "will arrive in this topic once the new process is up (usually ~5-10 seconds)."
    )
