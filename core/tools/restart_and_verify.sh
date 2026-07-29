#!/bin/bash
# Detached restart-and-verify script for BUG-002.
#
# Runs independent of the com.charlie process it restarts (launched with a new session
# via start_new_session=True in restart.py), so it survives that process being killed
# mid-script and can report the result afterward via a direct Telegram API call —
# it does not depend on the Python app being alive or healthy to report back.
#
# Args: $1=bot_token $2=group_id(chat_id) $3=topic_id(message_thread_id) $4=verify_script (optional, relative to charlie root)

set -uo pipefail

BOT_TOKEN="$1"
GROUP_ID="$2"
TOPIC_ID="$3"
VERIFY_SCRIPT="${4:-}"

CHARLIE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG="$CHARLIE_ROOT/charlie.log"

send_telegram() {
    local text="$1"
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${GROUP_ID}" \
        --data-urlencode "message_thread_id=${TOPIC_ID}" \
        --data-urlencode "text=${text}" > /dev/null

    # This send bypasses proactive_send (the running app isn't what's sending it), so
    # save it to charlie.db directly here — otherwise Charlie has no memory of this
    # message if Jonathan replies to it. Same architectural rule proactive_send exists
    # to satisfy (see bugs.md). Text goes through a temp file, not a command-line arg,
    # to avoid shell-quoting issues with arbitrary log content.
    local tmpfile
    tmpfile=$(mktemp)
    printf '%s' "$text" > "$tmpfile"
    (cd "$CHARLIE_ROOT" && venv/bin/python3 -c "
import sys
from core.history import save_message
with open(sys.argv[1]) as f:
    text = f.read()
save_message(int(sys.argv[2]), 'assistant', text)
" "$tmpfile" "$TOPIC_ID")
    rm -f "$tmpfile"
}

MARK_LINE=$(wc -l < "$LOG" 2>/dev/null || echo 0)

launchctl stop com.charlie
sleep 2
launchctl start com.charlie
sleep 3

NEW_PID=$(launchctl list | awk '$3=="com.charlie"{print $1}')

if [ -z "$NEW_PID" ] || [ "$NEW_PID" = "-" ]; then
    send_telegram "Restart FAILED — no PID for com.charlie after start. Check charlie.log on the always-on Mac."
    exit 1
fi

sleep 2
NEW_LOG=$(tail -n +"$((MARK_LINE + 1))" "$LOG" 2>/dev/null || echo "")

if echo "$NEW_LOG" | grep -qi "traceback\|ERROR"; then
    ERRLINES=$(echo "$NEW_LOG" | grep -i "traceback\|error" | head -5)
    send_telegram "Restarted (PID ${NEW_PID}) but errors appeared in the startup log:
${ERRLINES}"
    exit 1
fi

if ! echo "$NEW_LOG" | grep -q "Application started"; then
    send_telegram "Restarted (PID ${NEW_PID}) but did not see the normal startup sequence complete in the log — check charlie.log manually."
    exit 1
fi

RESULT="Restarted — PID ${NEW_PID}, clean startup, no errors in the log. Process-level check passed — I have not tested the feature itself, please try it."

if [ -n "$VERIFY_SCRIPT" ] && [ -f "$CHARLIE_ROOT/$VERIFY_SCRIPT" ]; then
    if (cd "$CHARLIE_ROOT" && venv/bin/python3 "$VERIFY_SCRIPT" > /tmp/charlie_verify_out.txt 2>&1); then
        RESULT="Restarted — PID ${NEW_PID}, clean startup, no errors in the log. Re-ran the build's own test script (${VERIFY_SCRIPT}) — passed."
    else
        VERIFY_OUT=$(tail -5 /tmp/charlie_verify_out.txt)
        RESULT="Restarted — PID ${NEW_PID}, clean startup, no errors in the log. Re-ran the build's own test script (${VERIFY_SCRIPT}) — FAILED:
${VERIFY_OUT}"
    fi
fi

send_telegram "$RESULT"
