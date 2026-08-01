"""Small utilities shared across core/ and core/tools/ — kept here specifically to
avoid the same primitive being hand-rolled twice (see run_with_timeout's docstring)."""
import threading


def run_with_timeout(func, timeout_seconds):
    """
    Run func() in a fresh daemon thread, bounded by timeout_seconds. Python cannot
    forcibly kill a running thread, so a hung call is abandoned (still consumes
    background CPU) rather than killed, but the caller is not blocked waiting on it
    indefinitely. Raises TimeoutError if func() doesn't complete in time; re-raises
    func()'s own exception if it failed for another reason.

    Consolidated here after code review flagged that core/bot.py's _wait_for_network
    and core/tools/email/fetch.py's attachment-parsing timeout had independently
    hand-rolled the identical daemon-thread-plus-join-timeout idiom (BUG-020) — a
    future fix to this primitive (e.g. process-based isolation, so a hung call's
    memory is actually reclaimed on timeout, not just its wall-clock wait bounded)
    now only needs to happen in one place.
    """
    result = {}

    def _target():
        try:
            result["value"] = func()
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)
    if t.is_alive():
        raise TimeoutError(f"Timed out after {timeout_seconds} seconds.")
    if "error" in result:
        raise result["error"]
    return result["value"]
