#!/usr/bin/env bash
# Bump yt-dlp (+ its JS solver) and ytmusicapi to the latest PyPI releases, rebuild, restart.
# --pull also refreshes the Python base and Deno images.
set -euo pipefail
cd "$(dirname "$0")"
for p in yt-dlp yt-dlp-ejs ytmusicapi; do
  v=$(curl -fsS "https://pypi.org/pypi/$p/json" | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])')
  sed -i -E "s/^(${p}(\[[a-z]+\])?)==.*/\1==${v}/" requirements.txt
  echo "$p -> $v"
done
docker compose build --pull
docker compose up -d
docker compose logs --tail 5
