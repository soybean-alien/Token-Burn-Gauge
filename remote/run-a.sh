#!/bin/sh
# Token-burn gauge launcher for cron:  * * * * * /opt/data/scripts/token-gauge/run-a.sh
# collector_a.py takes a lock and exits at once if a copy is already running,
# so cron restarts it within a minute of a crash and never runs two.
# `env -i` starts it with an empty environment: no provider keys can reach it.
cd "$(dirname "$0")" || exit 1
exec env -i PATH="$PATH" HOME="$HOME" python3 collector_a.py >> collector-a.log 2>&1
