"""Smoke test run inside the image (CI, and `docker compose exec cesura python -m app.selftest`).

Checks that the pieces YouTube keeps breaking still work, without any cookies:
  0. Deno (yt-dlp's JS runtime) and ffmpeg (pocket mode) are installed;
  1. ytmusicapi search returns songs;
  2. yt-dlp (with Deno + yt-dlp-ejs) can resolve an audio format for PROBE_VIDEO_ID.
Datacenter IPs (e.g. CI runners) often get YouTube's bot check instead of a stream.
That says nothing about the build, so it's reported as "inconclusive" and passes;
any other failure, like a broken signature solver or no formats, fails.
Prints one JSON line; exit code 0 = pass, 1 = fail.
"""
import json
import shutil
import subprocess
import sys

from yt_dlp import YoutubeDL
from ytmusicapi import YTMusic

from .media import FORMAT, PROBE_VIDEO_ID


def main() -> int:
    out = {"deno": bool(shutil.which("deno")), "ffmpeg": bool(shutil.which("ffmpeg")), "search": False, "stream": "fail", "detail": None}
    try:
        out["deno_version"] = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=30).stdout.split()[1]
    except Exception:
        pass
    try:
        out["search"] = len(YTMusic().search("bohemian rhapsody", filter="songs", limit=5)) > 0
    except Exception as e:
        out["detail"] = f"search: {type(e).__name__}"
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "format": FORMAT}) as ydl:
            info = ydl.extract_info(f"https://music.youtube.com/watch?v={PROBE_VIDEO_ID}", download=False)
        out["stream"] = "ok" if info.get("url") else "fail"
        out["format"] = info.get("format_id")
    except Exception as e:
        msg = str(e)
        if "confirm you" in msg and "not a bot" in msg:
            out["stream"] = "inconclusive"
            out["detail"] = "YouTube bot check from this IP (expected on CI runners)"
        else:
            out["detail"] = f"stream: {type(e).__name__}: {msg[:300]}"
    ok = out["deno"] and out["ffmpeg"] and out["search"] and out["stream"] in ("ok", "inconclusive")
    out["result"] = "pass" if ok else "fail"
    print(json.dumps(out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
