#!/bin/bash
# Safe wrapper around the daily auto-pull for the main Mac's charlie repo.
# Plain `git pull` aborts the moment the working tree is dirty, which then
# fails silently on every subsequent scheduled run until someone notices.
# Stashing first means the job always reaches a clean state before merging.
#
# BUG-038: this used to stash and never pop, silently burying any uncommitted
# work (e.g. cv/) for days until someone noticed it missing. Only pop back a
# stash this run actually created — an unconditional pop would instead reapply
# an old leftover stash from a *previous* run when today's tree was already
# clean, corrupting the working tree with stale content.
set -e
# Resolve to the directory this script actually lives in, rather than a
# hardcoded absolute path — the previous hardcoded /Users/purnellious/charlie
# only worked on the primary Mac; running this script as-is on any other
# machine (a fresh checkout elsewhere, or a differently-named account) failed
# immediately with "No such file or directory".
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

before_count=$(git stash list | wc -l)
git stash --include-untracked --message "auto-stash before scheduled pull ($(date '+%Y-%m-%d %H:%M:%S'))"
after_count=$(git stash list | wc -l)
git pull
if [ "$after_count" -gt "$before_count" ]; then
  git stash pop || echo "gitpull.sh: stash pop failed (likely merge conflict) - resolve manually, changes remain in git stash" >&2
fi
