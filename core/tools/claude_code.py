"""
Claude Code tool — runs a Claude Code task on the local machine.
Charlie calls this to build new capabilities, modify the system, or write code.
Auto-commits any changes to git on completion.
"""

import asyncio
import logging
import os
import shlex
import time
from pathlib import Path

log = logging.getLogger(__name__)

CHARLIE_ROOT = Path(__file__).parent.parent.parent
CLAUDE_CMD = "claude"
BUILD_TIMEOUT = 600  # 10 minutes
# Use Sonnet by default — Opus hits rate limits and is overkill for build tasks.
# Override with CLAUDE_CODE_MODEL env var if needed.
CLAUDE_CODE_MODEL = os.getenv("CLAUDE_CODE_MODEL", "claude-sonnet-4-6")


MAX_RETRIES = 3
RETRY_DELAY = 65  # seconds — wait for the token bucket to refill


async def run(task: str) -> str:
    """Run a Claude Code task. Returns a result string for Charlie to relay."""
    log.info(f"Claude Code task: {task[:80]}")

    cmd = f"{CLAUDE_CMD} --dangerously-skip-permissions --model {CLAUDE_CODE_MODEL} -p {shlex.quote(task)}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/zsh", "-l", "-c", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(CHARLIE_ROOT),
            )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=BUILD_TIMEOUT)
            output = stdout.decode("utf-8", errors="replace").strip()
            error = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                detail = error or output or "No output."
                # Retry on rate limit errors
                if "429" in detail or "rate limit" in detail.lower():
                    if attempt < MAX_RETRIES:
                        log.warning(f"Rate limit hit (attempt {attempt}/{MAX_RETRIES}), retrying in {RETRY_DELAY}s...")
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    return f"Rate limit — all {MAX_RETRIES} attempts failed. Try again in a minute."
                log.error(f"Task failed (exit {proc.returncode}): {detail[:200]}")
                return f"Task failed (exit {proc.returncode}).\n\n{_truncate(detail)}"

            commit_note = await _auto_commit(task)
            result = _truncate(output) if output else "Task completed — no output."
            if commit_note:
                result += f"\n\n{commit_note}"
            return result

        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return "Task timed out after 10 minutes. The process has been stopped."

        except FileNotFoundError:
            return (
                "Claude Code not found. Make sure it is installed and nvm is set up in ~/.zprofile.\n"
                "Install with: npm install -g @anthropic-ai/claude-code"
            )

        except Exception as e:
            log.error(f"Claude Code error: {e}")
            return f"Unexpected error: {e}"


async def _auto_commit(task: str) -> str:
    """Stage and commit any changes made by the task. Returns a note or empty string."""
    try:
        status = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(CHARLIE_ROOT),
        )
        stdout, _ = await status.communicate()
        if not stdout.strip():
            return ""

        add = await asyncio.create_subprocess_exec(
            "git", "add", "-A",
            cwd=str(CHARLIE_ROOT),
        )
        await add.wait()

        summary = task[:72].replace("\n", " ")
        commit = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", f"build: {summary}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(CHARLIE_ROOT),
        )
        await commit.wait()

        if commit.returncode != 0:
            return ""

        push = await asyncio.create_subprocess_exec(
            "git", "push", "origin", "main",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(CHARLIE_ROOT),
        )
        await push.wait()

        return "Changes committed and pushed to GitHub." if push.returncode == 0 else "Committed locally (push failed)."

    except Exception as e:
        log.warning(f"Auto-commit failed: {e}")
        return ""


def _truncate(text: str, limit: int = 3800) -> str:
    if len(text) <= limit:
        return text
    return f"[...output truncated...]\n\n{text[-limit:]}"
