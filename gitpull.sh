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
cd /Users/purnellious/charlie

before_count=$(git stash list | wc -l)
git stash --include-untracked --message "auto-stash before scheduled pull ($(date '+%Y-%m-%d %H:%M:%S'))"
after_count=$(git stash list | wc -l)
git pull
if [ "$after_count" -gt "$before_count" ]; then
  git stash pop || echo "gitpull.sh: stash pop failed (likely merge conflict) - resolve manually, changes remain in git stash" >&2
fi
