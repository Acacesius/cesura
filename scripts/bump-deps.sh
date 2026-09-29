#!/usr/bin/env bash
# Rewrite requirements.txt pins for yt-dlp, yt-dlp-ejs and ytmusicapi to their latest PyPI releases.
set -euo pipefail
cd "$(dirname "$0")/.."
for p in yt-dlp yt-dlp-ejs ytmusicapi; do
  v=$(curl -fsS "https://pypi.org/pypi/$p/json" | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])')
  sed -i -E "s/^(${p}(\[[a-z]+\])?)==.*/\1==${v}/" requirements.txt
  echo "$p -> $v"
done
