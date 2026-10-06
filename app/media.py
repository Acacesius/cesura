"""Music source: ytmusicapi for metadata, yt-dlp for stream URLs, httpx for proxying.

Why proxy at all: the googlevideo URL yt-dlp hands back is signed for *this*
host's IP (the `ip=` param) and expires (`expire=`, ~6h). A phone fetching it
directly would get 403. So the browser only ever talks to us, and we fetch
from googlevideo on its behalf, passing HTTP Range through so seeking works.
"""
import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
from yt_dlp import YoutubeDL
from ytmusicapi import OAuthCredentials, YTMusic

from . import cookies as cookiejar
from .artcache import art
from .netstats import net

log = logging.getLogger("cesura.media")

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/app/config")
COOKIES = os.path.join(CONFIG_DIR, "cookies.txt")
BROWSER_AUTH = os.path.join(CONFIG_DIR, "browser.json")
OAUTH_AUTH = os.path.join(CONFIG_DIR, "oauth.json")
# m4a/AAC plays everywhere (incl. iOS Safari). With Premium cookies this picks
# itag 141 (256k AAC); without, itag 140 (128k AAC). Only plain https formats:
# HLS/DASH manifests can't be range-proxied as a single file.
FORMAT = "bestaudio[ext=m4a][protocol=https]/bestaudio[protocol=https]"
# googlevideo throttles/403s large single requests, so fetch upstream in slices.
UPSTREAM_CHUNK = 2 * 1024 * 1024
# Each track is pulled from YouTube once and every listener is served from RAM.
# Very long uploads (hour-long mixes) fall back to per-request proxying.
MEM_MAX_BYTES = 150 * 1024 * 1024
MEM_TRACKS = 4  # current + next + a couple recently played
# Downloads run one at a time, paced to DOWNLOAD_RATE (MB/s, 0 = unlimited) so one track
# can't fill the uplink. Paced downloads use small Range requests: a big one would still
# arrive at full speed into the socket buffer, whatever pace we read it at.
DOWNLOAD_RATE = float(os.environ.get("DOWNLOAD_RATE", "2") or 0) * 1024 * 1024
PACED_CHUNK = 256 * 1024


class Throttle:
    """Paces reads to `rate` bytes per second across every download."""

    def __init__(self, rate: float):
        self.rate = rate
        self.free_at = 0.0  # monotonic time by which the bytes taken so far are "paid for"

    async def take(self, n: int):
        if self.rate <= 0:
            return
        now = time.monotonic()
        self.free_at = max(self.free_at, now) + n / self.rate
        if (wait := self.free_at - now - 0.05) > 0:
            await asyncio.sleep(wait)


# ---------------------------------------------------------------- ytmusicapi

def make_ytmusic() -> YTMusic:
    """Auth only personalises results (radio picks); search works without it."""
    if os.path.exists(OAUTH_AUTH):
        log.info("ytmusicapi: using OAuth from %s", OAUTH_AUTH)
        creds = OAuthCredentials(os.environ["YTM_CLIENT_ID"], os.environ["YTM_CLIENT_SECRET"])
        # Pass contents, not the path: with a path ytmusicapi writes refreshed
        # tokens back to the file, and /config is mounted read-only.
        with open(OAUTH_AUTH) as f:
            return net.instrument(YTMusic(f.read().strip(), oauth_credentials=creds))
    if os.path.exists(BROWSER_AUTH):
        log.info("ytmusicapi: using browser auth from %s", BROWSER_AUTH)
        with open(BROWSER_AUTH) as f:
            return net.instrument(YTMusic(f.read().strip()))
    log.info("ytmusicapi: no auth file in %s, running unauthenticated", CONFIG_DIR)
    return net.instrument(YTMusic())


def cookie_ytmusic(path: str = COOKIES) -> YTMusic:
    """ytmusicapi signed in as the host, from cookies.txt. Read-only: ytmusicapi never writes the file.

    For the host's own library and the session export. Search and browsing use
    the shared client from make_ytmusic(), so guests' lookups never run as the host.
    """
    header = cookiejar.cookie_header(cookiejar.parse(path))
    return net.instrument(YTMusic({"cookie": header, "authorization": "SAPISIDHASH 0_placeholder",  # ytmusicapi recomputes the real hash from the cookie
                    "x-goog-authuser": "0", "origin": "https://music.youtube.com"}))


def _thumb(thumbs) -> str | None:
    """The track's artwork as a local /art/ path: clients load it from us, not from Google (see artcache.py)."""
    if not thumbs:
        return None
    return art.register(thumbs[-1]["url"])


def _secs(text) -> int | None:
    if not text:
        return None
    total = 0
    for part in str(text).split(":"):
        total = total * 60 + int(part)
    return total


def to_track(item: dict, album: str | None = None, thumb: str | None = None) -> dict | None:
    """Normalise search / watch-playlist / album / playlist shapes into one dict.

    Album tracks carry no artwork and a plain-string album, so callers pass those in.
    """
    vid = item.get("videoId")
    if not vid:
        return None
    alb = item.get("album")
    artist_id = next((a["id"] for a in item.get("artists") or [] if str(a.get("id") or "").startswith("UC")), None)
    return {
        "videoId": vid,
        "title": item.get("title") or "Unknown",
        "artists": ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name")),
        "album": alb if isinstance(alb, str) else (alb or {}).get("name") or album,
        "duration": item.get("duration_seconds") or _secs(item.get("duration") or item.get("length")),
        "thumb": _thumb(item.get("thumbnails") or item.get("thumbnail")) or thumb,
        "artistId": artist_id,  # for the artist panel (get_artist)
    }


def song_to_track(song: dict) -> dict | None:
    d = song.get("videoDetails") or {}
    if not d.get("videoId"):
        return None
    return {
        "videoId": d["videoId"],
        "title": d.get("title") or "Unknown",
        "artists": d.get("author") or "",
        "album": None,
        "duration": int(d["lengthSeconds"]) if d.get("lengthSeconds") else None,
        "thumb": _thumb((d.get("thumbnail") or {}).get("thumbnails")),
        "artistId": d.get("channelId") if str(d.get("channelId") or "").startswith("UC") else None,
    }


# ---------------------------------------------------------------- yt-dlp

@dataclass
class Resolved:
    url: str
    headers: dict
    size: int | None
    mime: str
    expires: float
    fmt: str
    duration: float | None


@dataclass
class Media:
    data: bytes | None  # None = too big for RAM, stream via iter_range instead
    size: int
    mime: str
    duration: float | None


def _extract(video_id: str) -> dict:
    """Blocking: run yt-dlp's extractor (no download) and return the chosen format.

    yt-dlp loads the player API as your logged-in session, gets format URLs
    whose signatures are scrambled, downloads YouTube's player JS and runs it
    (via Deno) to descramble them, then applies FORMAT to pick one.
    """
    opts = {
        "format": FORMAT,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    # yt-dlp rewrites its cookiefile on exit; give it a private copy and only
    # keep the rotated result if the lookup worked (see cookies.py).
    tmp = cookiejar.snapshot(COOKIES)
    if tmp:
        opts["cookiefile"] = tmp
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=False)
        if tmp:
            cookiejar.write_back(tmp, COOKIES)
        return info
    finally:
        if tmp:
            os.unlink(tmp)


class Resolver:
    def __init__(self):
        self.cache: dict[str, Resolved] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(15, read=30), follow_redirects=True)
        self.media: OrderedDict[str, asyncio.Task] = OrderedDict()
        self.current: str | None = None   # the track the room is playing: never cancelled, never waits on a prefetch
        self.slot = asyncio.Lock()        # one download at a time
        self.throttle = Throttle(DOWNLOAD_RATE)
        self.chunk = PACED_CHUNK if DOWNLOAD_RATE > 0 else UPSTREAM_CHUNK
        # Cookies stopped working: a lookup hit YouTube's "Sign in to confirm you're
        # not a bot", or the link probe found the session signed out. Cleared by a new upload.
        self.bot_check = False

    # ---- engine status for the host dashboard
    def cookie_health(self) -> str:
        """missing / valid / expired."""
        if not os.path.exists(COOKIES):
            return "missing"
        return "expired" if self.bot_check else "valid"

    def stream_format(self, video_id: str) -> str | None:
        r = self.cache.get(video_id)
        if not r:
            return None
        itag, codec, kbps = (r.fmt.split() + ["", "", ""])[:3]
        name = "AAC" if codec.startswith("mp4a") else "Opus" if codec == "opus" else codec
        return f"{name} {kbps} · itag {itag}"

    def prefetch_state(self, video_id: str) -> str:
        task = self.media.get(video_id)
        if not task:
            return "idle"
        if not task.done():
            return "resolving"
        return "failed" if task.cancelled() or task.exception() else "cached"

    def load(self, video_id: str, prefetch: bool = False) -> asyncio.Task:
        """Resolve + download a track into RAM once; every caller awaits the same task.

        prefetch=False is the track the room plays now. Any other unfinished download
        (a prefetch the queue has moved past) is cancelled, so at most the current
        track and the one after it are ever fetched, and the current one never waits.
        """
        if not prefetch:
            self.current = video_id
        for vid, t in list(self.media.items()):
            if vid not in (video_id, self.current) and not t.done():
                t.cancel()
                del self.media[vid]
                log.info("cancelled download of %s: no longer next", vid)
        task = self.media.get(video_id)
        if task and not (task.done() and (task.cancelled() or task.exception())):
            self.media.move_to_end(video_id)
            return task
        task = asyncio.create_task(self._download_one(video_id))
        self.media[video_id] = task
        while len(self.media) > MEM_TRACKS:
            _, old = self.media.popitem(last=False)
            old.cancel()
        return task

    async def _download_one(self, video_id: str) -> Media:
        async with self.slot:
            return await self._download(video_id)

    async def _download(self, video_id: str) -> Media:
        r = await self.get(video_id)
        if r.size > MEM_MAX_BYTES:
            return Media(None, r.size, r.mime, r.duration)
        t0 = time.monotonic()
        buf = bytearray()
        async for chunk in self.iter_range(video_id, 0, r.size - 1):
            buf += chunk
        log.info("downloaded %s: %d bytes in %.1fs", video_id, len(buf), time.monotonic() - t0)
        return Media(bytes(buf), r.size, r.mime, r.duration)

    async def get(self, video_id: str, fresh: bool = False) -> Resolved:
        lock = self.locks.setdefault(video_id, asyncio.Lock())
        async with lock:
            r = self.cache.get(video_id)
            if r and not fresh and r.expires - 600 > time.time():
                return r
            t0 = time.monotonic()
            try:
                with net.fetch("resolve", video_id):
                    info = await asyncio.to_thread(_extract, video_id)
            except Exception as e:
                if "confirm you" in str(e) and "not a bot" in str(e):
                    self.bot_check = True
                raise
            self.bot_check = False
            q = parse_qs(urlparse(info["url"]).query)
            size = int(q["clen"][0]) if "clen" in q else info.get("filesize")
            r = Resolved(
                url=info["url"],
                headers=info.get("http_headers") or {},
                size=size,
                mime="audio/mp4" if info.get("ext") == "m4a" else f"audio/{info.get('ext')}",
                expires=float(q["expire"][0]) if "expire" in q else time.time() + 3600,
                fmt=f"{info.get('format_id')} {info.get('acodec')} {round(info.get('abr') or 0)}k",
                duration=float(q["dur"][0]) if "dur" in q else info.get("duration"),
            )
            if r.size is None:
                r.size = await self._probe_size(r)
            log.info("resolved %s -> itag %s, %s bytes, %.1fs", video_id, r.fmt, r.size, time.monotonic() - t0)
            now = time.time()
            self.cache = {k: v for k, v in self.cache.items() if v.expires > now}
            self.cache[video_id] = r
            return r

    async def _probe_size(self, r: Resolved) -> int:
        resp = await self.http.get(r.url, headers={**r.headers, "Range": "bytes=0-0"})
        resp.raise_for_status()
        return int(resp.headers["content-range"].rsplit("/", 1)[1])

    async def iter_range(self, video_id: str, start: int, end: int):
        """Yield bytes [start, end] from upstream, re-resolving once on 403/expiry."""
        with net.fetch("audio", f"{video_id} bytes {start}-{end}") as f:
            async for chunk in self._iter_range(video_id, start, end):
                f.got(len(chunk))
                yield chunk

    async def _iter_range(self, video_id: str, start: int, end: int):
        pos, retried = start, False
        while pos <= end:
            r = await self.get(video_id)
            stop = min(pos + self.chunk - 1, end)
            await self.throttle.take(stop - pos + 1)
            before = pos
            try:
                async with self.http.stream("GET", r.url, headers={**r.headers, "Range": f"bytes={pos}-{stop}"}) as resp:
                    if resp.status_code in (403, 404, 410) and not retried:
                        log.warning("upstream %s for %s, re-resolving", resp.status_code, video_id)
                        retried = True
                        await self.get(video_id, fresh=True)
                        continue
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        chunk = chunk[: stop - pos + 1]
                        if not chunk:
                            break
                        yield chunk
                        pos += len(chunk)
                if pos == before:
                    raise RuntimeError(f"upstream returned no data for {video_id} at {pos}")
                retried = False
            except httpx.TransportError as e:
                if retried:
                    raise
                log.warning("upstream transport error for %s: %s, retrying", video_id, e)
                retried = True


# ---- linking probe (setup wizard / relink)

PROBE_VIDEO_ID = os.environ.get("PROBE_VIDEO_ID", "").strip() or "BSTsnWoslP4"  # Queen - Bohemian Rhapsody (Queen Official)


def probe_account() -> dict:
    """Blocking. Check the saved cookies against YouTube itself.

    - account: YouTube Music's account endpoint answers for these cookies
      (server-side confirmation, not just "the right cookie names exist").
    - aac256: format 141 (256k AAC) is offered for PROBE_VIDEO_ID. YouTube only
      offers it to Premium sessions; without cookies the same track has 140 only.
    - premium: inferred as account + aac256. YouTube exposes no Premium flag, and
      yt-dlp's "Premium" video labels appear even when signed out, so they can't be used.
    Returns booleans and a display name only; never cookie values or raw errors.
    """
    res = {"account": False, "name": None, "premium": False, "aac256": False, "problem": None, "video": PROBE_VIDEO_ID}
    tmp = cookiejar.snapshot(COOKIES)
    if not tmp:
        res["problem"] = "missing"
        return res
    try:
        try:
            info = cookie_ytmusic(tmp).get_account_info()
            res["account"] = True
            res["name"] = (info.get("accountName") or "")[:80] or None
        except Exception as e:
            log.info("probe: account check failed (%s)", type(e).__name__)
        try:
            opts = {"quiet": True, "no_warnings": True, "skip_download": True, "format": "bestaudio/best", "cookiefile": tmp}
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://music.youtube.com/watch?v={PROBE_VIDEO_ID}", download=False)
            res["aac256"] = any(f.get("format_id") == "141" for f in info.get("formats") or [])
            if res["account"]:
                cookiejar.write_back(tmp, COOKIES)
        except Exception as e:
            msg = str(e)
            res["problem"] = "bot-check" if ("confirm you" in msg and "not a bot" in msg) else "unavailable"
            log.info("probe: stream check failed (%s, %s)", type(e).__name__, res["problem"])
        res["premium"] = res["account"] and res["aac256"]
        if not res["account"] and not res["problem"]:
            res["problem"] = "signed-out"
        return res
    finally:
        os.unlink(tmp)
