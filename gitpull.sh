#!/bin/bash
# Safe wrapper around the daily auto-pull for the main Mac's charlie repo.
# Plain `git pull` aborts the moment the working tree is dirty, which then
# fails silently on every subsequent scheduled run until someone notices.
# Stashing first means the job always reaches a clean state before merging.
set -e
cd /Users/purnellious/charlie

git stash --include-untracked --message "auto-stash before scheduled pull ($(date '+%Y-%m-%d %H:%M:%S'))"
git pull
