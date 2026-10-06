"""Album art and artist images: fetched from YouTube once, kept on disk, served from here.

Clients never load images from Google. A track's "thumb" is /art/<id>, where the
id stands for an image URL the server saw in a YouTube Music response, so the
endpoint can't be pointed at anything else. ?s=<px> picks a size from SIZES
(Google's image hosts resize on request); each size is fetched once and kept in
CACHE_DIR/art, oldest files dropped past ART_CACHE_MB.
"""
import asyncio
import hashlib
import logging
import os
import re
import tempfile
from collections import OrderedDict

import httpx

from .netstats import net

log = logging.getLogger("cesura.art")

SIZES = (120, 240, 544, 1200)
MAX_BYTES = 4 * 1024 * 1024
BUDGET = int(float(os.environ.get("ART_CACHE_MB", "300")) * 1024 * 1024)
RESIZABLE = re.compile(r"^https://(lh\d|yt\d)\.(googleusercontent|ggpht)\.com/")
SIZE_SPEC = re.compile(r"=(w\d+(-h\d+)?|s\d+)[^/=]*$")
ID = re.compile(r"[0-9a-f]{20}")
MAGIC = {b"\xff\xd8": "image/jpeg", b"\x89P": "image/png", b"RI": "image/webp", b"GI": "image/gif"}


def _writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        return True
    except OSError:
        return False


class ArtCache:
    def __init__(self, cache_dir: str, prefix: str = "/art/"):
        self.dir = os.path.join(cache_dir, "art")
        if not _writable(self.dir):
            self.dir = os.path.join(tempfile.gettempdir(), "art-cache")
            os.makedirs(self.dir, exist_ok=True)
            log.warning("%s is not writable: caching artwork in %s instead (lost on restart)", cache_dir, self.dir)
        self.prefix = prefix
        self.urls: OrderedDict[str, tuple[str, bool]] = OrderedDict()  # id -> (url, wide)
        self.fetching: dict[str, asyncio.Task] = {}
        self.gate = asyncio.Semaphore(2)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10, read=15), follow_redirects=True)
        self.used = sum(e.stat().st_size for e in os.scandir(self.dir) if e.is_file())

    def register(self, url: str | None, wide: bool = False) -> str | None:
        """The local path for an image URL (None stays None). Wide images (artist banners) keep their shape."""
        if not url or not url.startswith("https://"):
            return None
        aid = hashlib.sha1(url.encode()).hexdigest()[:20]
        self.urls[aid] = (url, wide)
        self.urls.move_to_end(aid)
        while len(self.urls) > 20000:
            self.urls.popitem(last=False)
        return self.prefix + aid

    @staticmethod
    def sized(url: str, wide: bool, s: int) -> str:
        if not RESIZABLE.match(url):
            return url  # i.ytimg.com video stills: one size only
        spec = f"=w{s}-l90-rj" if wide else f"=w{s}-h{s}-l90-rj"
        return SIZE_SPEC.sub(spec, url) if SIZE_SPEC.search(url) else url + spec

    async def get(self, aid: str, s: int) -> tuple[str, str] | None:
        """(path on disk, media type), fetching it first if needed. None if unknown or unavailable."""
        if not ID.fullmatch(aid):
            return None
        s = min(SIZES, key=lambda x: abs(x - s))
        path = os.path.join(self.dir, f"{aid}-{s}")
        if os.path.exists(path):
            return path, self._type(path)
        if aid not in self.urls:
            return None
        key = f"{aid}-{s}"
        task = self.fetching.get(key)
        if task is None:
            task = self.fetching[key] = asyncio.create_task(self._fetch(aid, s, path))
            task.add_done_callback(lambda _: self.fetching.pop(key, None))
        ok = await asyncio.shield(task)
        return (path, self._type(path)) if ok else None

    async def _fetch(self, aid: str, s: int, path: str) -> bool:
        url, wide = self.urls[aid]
        async with self.gate:
            try:
                with net.fetch("artist-art" if wide else "thumb", f"{aid} {s}px") as f:
                    r = await self.http.get(self.sized(url, wide, s))
                    f.got(len(r.content))
                r.raise_for_status()
                if not r.headers.get("content-type", "").startswith("image/") or len(r.content) > MAX_BYTES:
                    return False
            except Exception as e:
                log.info("art %s failed: %s", aid, type(e).__name__)
                return False
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(r.content)
        os.replace(tmp, path)
        self.used += len(r.content)
        if self.used > BUDGET:
            await asyncio.to_thread(self._trim)
        return True

    def _trim(self):
        files = sorted((e for e in os.scandir(self.dir) if e.is_file()), key=lambda e: e.stat().st_mtime)
        for e in files:
            if self.used <= BUDGET * 0.8:
                break
            self.used -= e.stat().st_size
            os.unlink(e.path)

    @staticmethod
    def _type(path: str) -> str:
        with open(path, "rb") as f:
            return MAGIC.get(f.read(2), "image/jpeg")


CACHE_DIR = os.environ.get("CACHE_DIR", "/app/cache")
art = ArtCache(CACHE_DIR)
