#!/usr/bin/env bash
# Source installs: bump yt-dlp (+ its JS solver) and ytmusicapi to the latest
# releases, rebuild, restart and self-test. Prebuilt-image installs just run:
#   docker compose pull && docker compose up -d
set -euo pipefail
cd "$(dirname "$0")"
scripts/bump-deps.sh
docker compose -f docker-compose.dev.yml build --pull
docker compose -f docker-compose.dev.yml up -d
sleep 5
docker compose -f docker-compose.dev.yml exec -T cesura python -m app.selftest || echo "selftest failed: see above"
