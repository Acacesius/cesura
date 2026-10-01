"""Playlists and lyrics: pasted playlist links, the host's library, small caches."""
import re
import time
from collections import OrderedDict
from urllib.parse import parse_qs, urlsplit

# Playlist IDs YouTube hands out: user playlists (PL), album playlists (OLAK5uy_),
# mixes and radio (RD…), channel uploads (UU), favourites (FL). Browse IDs carry a VL prefix.
PLAYLIST_ID = re.compile(r"(?:PL|OLAK5uy_|RD|UU|FL)[A-Za-z0-9_-]{10,72}")
PLAYLIST_HOSTS = ("music.youtube.com", "www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be")


def normalize_playlist(text: str) -> str | None:
    """A playlist ID from a YouTube / YouTube Music link or a bare ID, else None.

    Liked songs ("LM", "LL") and watch-later ("WL") are private by nature and rejected.
    """
    text = (text or "").strip()[:500]
    if not text:
        return None
    if "://" not in text and "/" not in text and "?" not in text:
        cand = text
    else:
        if "://" not in text:
            text = "https://" + text
        try:
            parts = urlsplit(text)
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in PLAYLIST_HOSTS:
            return None
        q = parse_qs(parts.query)
        cand = (q.get("list") or [""])[0]
        if not cand and parts.path.startswith("/browse/"):
            cand = parts.path.split("/")[2]
    if cand.startswith("VL"):
        cand = cand[2:]
    return cand if PLAYLIST_ID.fullmatch(cand) else None


class TTLCache:
    """Small LRU with expiry. Values are whatever the caller stores; keys are short strings."""

    def __init__(self, ttl: float, size: int = 200, clock=time.monotonic):
        self.ttl, self.size, self.clock = ttl, size, clock
        self.data: OrderedDict[str, tuple[float, object]] = OrderedDict()

    def get(self, key: str):
        hit = self.data.get(key)
        if not hit or self.clock() - hit[0] > self.ttl:
            self.data.pop(key, None)
            return None
        self.data.move_to_end(key)
        return hit[1]

    def put(self, key: str, value):
        self.data[key] = (self.clock(), value)
        self.data.move_to_end(key)
        while len(self.data) > self.size:
            self.data.popitem(last=False)

    def clear(self):
        self.data.clear()


def fetch_lyrics(yt, video_id: str) -> dict:
    """Blocking. {"lyrics": text | None, "source": str | None}. The lyrics browseId comes from the watch playlist."""
    wp = yt.get_watch_playlist(videoId=video_id, limit=1)
    browse = wp.get("lyrics")
    if not browse:
        return {"lyrics": None, "source": None}
    got = yt.get_lyrics(browse) or {}
    text = got.get("lyrics") if isinstance(got, dict) else None
    if isinstance(text, list):  # timed lyrics shape; not requested, but be safe
        text = "\n".join(getattr(line, "text", "") for line in text)
    return {"lyrics": (text or "").strip() or None, "source": (got.get("source") if isinstance(got, dict) else None)}


def session_tracks(played: list[dict]) -> list[str]:
    """Video IDs played this session, in order, first occurrence only."""
    seen, out = set(), []
    for t in played:
        v = t.get("videoId")
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out
