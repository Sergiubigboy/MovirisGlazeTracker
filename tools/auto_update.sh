#!/usr/bin/env bash
# Pulls origin/main every 5 minutes and restarts the glaze service if
# anything changed. Meant to run as its own systemd service (see
# glaze-autoupdate.service) so the tracker service itself stays simple.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="main"
INTERVAL=300

cd "$REPO_DIR" || exit 1

while true; do
    git fetch origin "$BRANCH" >/dev/null 2>&1

    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse "origin/$BRANCH")

    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "$(date '+%F %T') update found, pulling..."
        git reset --hard "origin/$BRANCH"
        sudo systemctl restart glaze
        echo "$(date '+%F %T') updated to $(git rev-parse --short HEAD) and restarted"
    fi

    sleep "$INTERVAL"
done
