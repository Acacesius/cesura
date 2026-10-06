"""cesura: listen-along. Everyone hears the same track at the same moment on their own device.

The server owns the clock. For the current track it stores `started_at` (the
server time at which position 0 played) while running, or a frozen position
while stopped (paused, nobody listening, or the track still loading). Every
state change is pushed as a full snapshot over websockets; each client measures
its offset to the server clock and steers its <audio> to server_now - started_at.
The server also ends tracks itself on a timer, so no client is special.

Listeners are devices (presence.py), not sockets. Joining needs the room code;
after that a per-device session token authorises audio and every action.
Pocket listeners (pocket.py) hear one shared stream that follows the clock.
"""
import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass

import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ytmusicapi import YTMusic

from . import cookies as cookiejar
from . import setup as setupstate
from .artcache import art
from .delta import Sync, dumps
from .fair import FairQueue
from .library import TTLCache, fetch_lyrics, normalize_playlist, session_tracks
from .netstats import net
from .media import COOKIES, PROBE_VIDEO_ID, Resolver, _thumb, cookie_ytmusic, make_ytmusic, probe_account, song_to_track, to_track
from .pocket import Station
from .presence import DEVICE_ID, HB_INTERVAL, Device, Presence

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cesura")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain signed googlevideo URLs

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/app/config")
ENV_PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")


def public_url() -> str:
    """Invite link / QR base: PUBLIC_URL env wins, then the wizard's choice, else blank (= the host page's address)."""
    return ENV_PUBLIC_URL or setupstate.load_state().get("public_url", "")


def load_host_key() -> str | None:
    """HOST_KEY from the environment, else config/host_key (written by the setup wizard)."""
    return os.environ.get("HOST_KEY", "").strip() or setupstate.stored_host_key()


VERSION = "0.2.0"
HOST_KEY = load_host_key()  # updated by the setup wizard
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
MAX_QUEUE = 100
ADD_LIMIT, ADD_WINDOW = 10, 60.0  # adds per client IP per window (seconds)
FAIL_LIMIT, FAIL_WINDOW = 10, 600.0  # wrong codes/keys per client IP per window
AUTO_TARGET = 10
LEAD = 1.0       # head start (s) whenever the clock (re)starts, so every listener can buffer
MAX_FAILS = 3    # consecutive unplayable tracks before giving up
BACK_RESTART = 3.0  # Back restarts the current song once it has played this long, like any player
VOTE_FRACTIONS = (0.25, 0.5, 0.75, 1.0)
HEALTH_EVERY = 6 * 3600   # re-run the YouTube link check this often...
HEALTH_DEBOUNCE = 600     # ...and when the dashboard opens, unless it ran this recently
POCKET_MAX, POCKET_PER_DEVICE = 40, 2
SESSION_LOG_MAX = 500
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")
# The pocket station reads the current track from our own /stream (the in-memory proxy) over loopback.
INTERNAL_PORT = int(os.environ.get("CESURA_INTERNAL_PORT", "8765"))
INTERNAL_TOKEN = secrets.token_urlsafe(24)
DEBOUNCE = 0.1         # a burst of changes (an album's worth of adds) goes out as one push
PREFETCH_DELAY = 3.0   # fetch the next track once the queue has stopped changing for this long
OWNER_SALT = secrets.token_bytes(16)


def owner_id(owner: str | None) -> str | None:
    """Who added a track, as clients see it: "host", or an opaque per-device id (never the device id itself)."""
    if not owner or owner == "host":
        return owner
    return hashlib.blake2s(owner.encode(), key=OWNER_SALT, digest_size=5).hexdigest()

ytm = make_ytmusic()
public_ytm = net.instrument(YTMusic())  # never signed in: pasted playlist links can't reach the host's private playlists
resolver = Resolver()


@asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(f()) for f in (heartbeat_loop, reap_loop, health_loop)]
    yield
    for t in tasks:
        t.cancel()
    await station.aclose()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))


def _quiet(task: asyncio.Task):
    """Prefetch failures are retried at play time; don't log them as unhandled."""
    task.add_done_callback(lambda t: t.cancelled() or t.exception())


@dataclass
class Actor:
    """Whoever did something: the host, or a guest device (maybe a co-host)."""
    key: str                  # device id, or "host"
    name: str
    role: str                 # host | cohost | guest
    device: Device | None = None

    @property
    def label(self) -> str:
        return "Host" if self.role == "host" else f"{self.name} (co-host)" if self.role == "cohost" else self.name

    @property
    def mod(self) -> bool:
        return self.role in ("host", "cohost")


HOST = Actor("host", "Host", "host")


class Room:
    def __init__(self):
        self.code = new_code()
        self.fq = FairQueue()         # guest-added, plays first; round-robin by device
        self.auto: list[dict] = []    # radio suggestions, plays when queue is empty
        self.now: dict | None = None
        self.started_at: float | None = None  # set while the clock runs
        self.frozen = 0.0                     # position while the clock is stopped
        self.loading = False                  # current track not yet in RAM
        self.user_paused = False
        self.host: WebSocket | None = None    # optional QR/admin screen, plays nothing
        self.people = Presence()
        self.radio = True
        self.library_on = False               # guests can browse the host's library
        self.vote_fraction = 0.5
        self.session_start = time.time()
        self.songs_played = 0
        self.peak_listeners = 0
        self.votes: set[str] = set()          # device ids
        self.history: deque[str] = deque(maxlen=200)   # videoIds, keeps radio from repeating
        self.played: deque[dict] = deque(maxlen=30)    # for the Played tab
        self.back_stack: deque[dict] = deque(maxlen=30)  # tracks that played, newest last: what Back goes to
        self.session_log: list[dict] = []              # every track that played, for the export
        self.feed: deque[dict] = deque(maxlen=40)      # recent actions, for the host
        self.hands: dict | None = None                 # last action on the current track ("Paused by Sam")
        self.add_log: dict[str, deque] = {}
        self.lock = asyncio.Lock()
        self.auto_lock = asyncio.Lock()
        self.timer: asyncio.Task | None = None
        self.fails = 0
        self.liked: dict[str, bool] = {}  # videoId -> liked on the host's YouTube Music (host only)
        self.sync = Sync()
        self._flush: asyncio.Task | None = None
        self._prefetch: asyncio.Task | None = None

    @property
    def queue(self) -> list[dict]:
        return self.fq.items

    # ---- derived values
    def listeners(self) -> int:
        return len(self.people.devices)

    def n_listening(self) -> int:
        return len(self.people.listening())

    def votes_needed(self) -> int:
        """Share of active non-pocket listeners (host's setting, default half), rounded up, at least 1."""
        return max(1, math.ceil(self.vote_fraction * len(self.people.voters()) - 1e-9))

    def position(self) -> float:
        return time.time() - self.started_at if self.started_at is not None else self.frozen

    def status(self) -> str:
        if not self.now:
            return "idle"
        if resolver.cookie_health() == "missing":
            return "unlinked"
        if self.loading:
            return "loading"
        if self.user_paused:
            return "paused"
        if not self.n_listening():
            return "waiting"
        return "playing"

    @staticmethod
    def public(it: dict | None) -> dict | None:
        """Replace the adder's device id with an opaque owner id. The same for every client, so one
        message fits all; each guest knows its own oid (state.me) and so which tracks are theirs."""
        if it is None:
            return None
        d = {k: v for k, v in it.items() if k not in ("owner", "pin")}
        d["oid"] = owner_id(it.get("owner"))
        d["pinned"] = it.get("pin") is not None
        return d

    def listener_rows(self) -> list[dict]:
        now, rows = time.time(), []
        for d in sorted(self.people.devices.values(), key=lambda d: d.joined):
            ms, catching_up, at = d.drift or (None, False, 0.0)
            fresh = ms is not None and now - at < 10
            if d.in_pocket:
                status = "pocket"
            elif not d.sockets:
                status = "away"
            elif not d.listening:
                status = "idle"
            elif fresh and (catching_up or abs(ms) > 60):
                status = "catching_up"
            elif fresh:
                status = "in_phase"
            else:
                status = "waiting"
            rows.append({"pid": d.pid, "name": d.name or "Guest", "device": d.ua, "sockets": len(d.sockets),
                         "listening": d.listening or d.in_pocket, "role": d.role, "status": status,
                         "drift_ms": round(ms) if fresh and status in ("in_phase", "catching_up") else None})
        return rows

    def host_panel(self) -> dict:
        rows = self.listener_rows()
        drifts = sorted(abs(r["drift_ms"]) for r in rows if r["drift_ms"] is not None)
        nxt = self.queue[0] if self.queue else self.auto[0] if self.auto else None
        cutoff = time.monotonic() - FAIL_WINDOW
        now = time.time()
        return {
            "listeners": rows,
            "stats": {"session_start": self.session_start, "songs_played": self.songs_played,
                      "peak_listeners": self.peak_listeners,
                      "median_drift_ms": drifts[len(drifts) // 2] if drifts else None},
            "engine": {"stream": resolver.stream_format(self.now["videoId"]) if self.now else None,
                       "next_qid": nxt["qid"] if nxt else None,
                       "next": resolver.prefetch_state(nxt["videoId"]) if nxt else "idle",
                       "cookies": resolver.cookie_health(),
                       "health": health,
                       "pocket": {"running": station.running, "listeners": station.listeners, "codec": station.codec},
                       "wrong_codes": sum(1 for q in fail_log.values() for t in q if t > cutoff),
                       "wrong_code_limit": FAIL_LIMIT},
            "feed": list(reversed(self.feed))[:15],  # "at" is server time: the dashboard shows how long ago
            "settings": {"vote_fraction": self.vote_fraction, "vote_fractions": VOTE_FRACTIONS,
                         "library": self.library_on, "locked": self.people.locked, "radio": self.radio},
            "banned": len(self.people.banned),
            "session_tracks": len(session_tracks(self.session_log)),
        }

    def snapshot(self, d: Device | None = None, for_host=False) -> dict:
        """Everything a client shows, except the queue (which travels as edits, see delta.py)."""
        me = d.id if d else "host" if for_host else None
        s = {
            "version": VERSION,
            "now": self.public(self.now),
            "hands": self.hands and {k: self.hands[k] for k in ("kind", "text")},
            "status": self.status(),
            "started_at": self.started_at,
            "position": round(self.position(), 3),
            "server_time": time.time(),
            "auto": [self.public(t) for t in self.auto],
            "played": list(reversed(self.played)),
            "can_back": bool(self.back_stack),
            "votes": len(self.votes),
            "votes_needed": self.votes_needed(),
            "voted": bool(me) and me in self.votes,
            "listeners": self.listeners(),
            "listening": self.n_listening(),
            "radio": self.radio,
            "library": self.library_on,
            "locked": self.people.locked,
            "people": sorted({x.name or "Guest" for x in self.people.devices.values()}, key=str.lower),
            "notice": NOTICES.get(resolver.cookie_health()),
        }
        if d:
            s["me"] = {"role": d.role, "pocket": d.in_pocket, "pid": d.pid, "oid": owner_id(d.id)}
            if d.cohost:
                s["roster"] = self.listener_rows()
        if for_host:
            s["code"] = self.code
            s["public_url"] = public_url()
            s["host"] = self.host_panel()
            s["me"] = {"oid": "host"}
            s["liked"] = self.liked.get(self.now["videoId"]) if self.now else None  # None = not known yet
        return s

    # ---- attribution ("hands on it")
    def record(self, actor: Actor, kind: str, text: str):
        self.feed.append({"who": actor.label, "role": actor.role, "kind": kind, "text": text, "at": time.time()})
        log.info("%s: %s", actor.label, text)

    # ---- the clock
    def sync_clock(self):
        """Start or stop the clock to match status(). Call after any change. Holds self.lock."""
        run = self.status() == "playing"
        if run and self.started_at is None:
            self.started_at = time.time() + LEAD - self.frozen
        elif not run and self.started_at is not None:
            self.frozen = max(0.0, self.position())
            self.started_at = None
        cur = asyncio.current_task()
        if self.timer and self.timer is not cur:
            self.timer.cancel()
        self.timer = None
        if self.started_at is not None and self.now and self.now.get("duration"):
            self.timer = asyncio.create_task(self._end_after(self.now["qid"], self.now["duration"] - self.position()))

    def seek_to(self, pos: float):
        """Move the clock to `pos` seconds into the current track. Holds self.lock.
        Listeners re-steer to the new clock; the pocket station starts a new decoder there."""
        if self.started_at is not None:
            self.started_at = time.time() - pos
        else:
            self.frozen = pos
        self.sync_clock()  # reschedules the end-of-track timer

    async def _end_after(self, qid: str, secs: float):
        await asyncio.sleep(max(0.0, secs) + 0.25)
        async with self.lock:
            if not self.now or self.now["qid"] != qid or self.started_at is None:
                return
            await self.advance("ended")
        await self.broadcast()

    # ---- broadcasting
    @staticmethod
    async def _send_text(ws: WebSocket, text: str):
        try:
            await ws.send_text(text)
        except Exception:
            pass  # disconnect handler cleans up

    async def _send(self, ws: WebSocket, msg: dict):
        text = json.dumps(msg, separators=(",", ":"))
        net.push(msg.get("type", "?"), text, 1)
        await self._send_text(ws, text)

    async def _send_all(self, kind: str, targets: list[tuple[WebSocket, dict]]):
        texts = [(ws, dumps(m)) for ws, m in targets]
        net.push_batch(kind, [t for _, t in texts])
        await asyncio.gather(*(self._send_text(ws, t) for ws, t in texts))

    def targets(self) -> list[tuple[WebSocket, Device | None]]:
        out = [(ws, d) for d in list(self.people.devices.values()) for ws in list(d.sockets)]
        if self.host:
            out.append((self.host, None))
        return out

    async def flush(self, only=None):
        """Bring every socket (or those `only` picks) up to date: a patch, or the full state if it has none."""
        self.sync.set_queue([self.public(t) for t in self.queue])
        snaps: dict = {}
        msgs = []
        for ws, d in self.targets():
            if only and not only(ws, d):
                continue
            key = d.id if d else "host"
            if key not in snaps:
                snaps[key] = self.snapshot(d, for_host=d is None)
            if (m := self.sync.message(ws, snaps[key])) is not None:
                msgs.append((ws, m))
        for kind in ("state", "patch"):
            await self._send_all(kind, [(ws, m) for ws, m in msgs if m["type"] == kind])

    async def push_host(self):
        """Refresh the host dashboard and co-hosts (drift updates), at most once a second."""
        now = time.monotonic()
        if now - getattr(self, "_host_pushed", 0) < 1:
            return
        self._host_pushed = now
        if self.sync.queue != [self.public(t) for t in self.queue]:
            return await self.broadcast()  # queue edits must reach everyone, not just these
        await self.flush(only=lambda ws, d: d is None or d.cohost)

    async def broadcast(self):
        """Push the current state to everyone, DEBOUNCE from now: a burst of changes goes out as one push."""
        if self._flush is None or self._flush.done():
            self._flush = asyncio.create_task(self._flush_later())

    async def _flush_later(self):
        await asyncio.sleep(DEBOUNCE)
        self._flush = None  # changes made while this push is sending schedule the next one
        await self.flush()

    # ---- queue mechanics
    def item(self, track: dict, auto=False, by: str | None = None) -> dict:
        return {**track, "qid": uuid.uuid4().hex[:10], "auto": auto, "by": by}

    def prefetch_next(self):
        """Fetch the next track ahead of time, once the queue has settled for PREFETCH_DELAY seconds.
        Someone clearing or reordering the queue song by song starts one download, not one per change."""
        if self._prefetch and not self._prefetch.done():
            self._prefetch.cancel()
        self._prefetch = asyncio.create_task(self._prefetch_later())

    async def _prefetch_later(self):
        await asyncio.sleep(PREFETCH_DELAY)
        nxt = self.queue[0] if self.queue else self.auto[0] if self.auto else None
        if nxt and not self.loading:  # while the current track loads, its _prepare prefetches afterwards
            _quiet(resolver.load(nxt["videoId"], prefetch=True))

    def check_votes(self) -> bool:
        """The threshold can drop when listeners leave; skip if the votes already cast now suffice. Holds self.lock."""
        return bool(self.now and self.votes and len(self.votes) >= self.votes_needed())

    async def advance(self, reason: str, hands: dict | None = None):
        """Move to the next track. Caller holds self.lock."""
        prev = self.now
        if prev:
            self.history.append(prev["videoId"])
            self.played.append(self.public(prev))
            self.back_stack.append(prev)
        if self.queue:
            self.now = self.fq.pop()
        elif self.auto:
            self.now = self.auto.pop(0)
        elif prev and self.radio:
            await self.refill_auto(prev)
            self.now = self.auto.pop(0) if self.auto else None
        else:
            self.now = None
        self._start(hands)
        log.info("advance (%s): %s", reason, self.now and f"{self.now['artists']} - {self.now['title']}")
        if self.now:
            # Queue ran dry: seed radio from the track that's now playing. If that
            # track is itself a radio pick, keep the existing radio list going.
            if self.radio and not self.queue and (not self.now["auto"] or len(self.auto) < 3):
                if not self.now["auto"]:
                    self.auto.clear()
                asyncio.create_task(self.refill_auto_and_broadcast(self.now))
        self.sync_clock()

    def _start(self, hands: dict | None):
        """self.now just changed: reset the clock and votes, and get the track into RAM. Holds self.lock."""
        self.started_at, self.frozen = None, 0.0
        self.loading = self.now is not None
        self.votes.clear()
        self.hands = hands if self.now else None
        if self.now:
            self.songs_played += 1
            asyncio.create_task(self._prepare(self.now))

    def go_back(self, hands: dict):
        """Play the previous track again. The current one goes back to the front of the queue (or of
        the radio list, if it was a radio pick), so skipping forward again returns to it. Holds self.lock."""
        prev, cur = self.back_stack.pop(), self.now
        if cur:
            if cur.get("auto"):
                self.auto.insert(0, cur)
            else:
                self.fq.add([cur], cur.get("owner") or "host", "next")
        if self.played and self.played[-1]["videoId"] == prev["videoId"]:
            self.played.pop()  # it's playing again, not "recently played"
        self.now = {**prev, "qid": uuid.uuid4().hex[:10]}  # a new qid: clients reload it, stale skips miss
        self._start(hands)
        log.info("back: %s - %s", self.now["artists"], self.now["title"])
        self.sync_clock()
        self.prefetch_next()

    async def _prepare(self, item: dict):
        """Get the track into RAM, then let the clock start."""
        if resolver.cookie_health() == "missing":
            return  # stays "unlinked"; relinked() picks it up once cookies arrive
        try:
            media, err = await resolver.load(item["videoId"]), None
        except Exception as e:
            media, err = None, e
        async with self.lock:
            if self.now is not item:
                return
            if err:
                self.fails += 1
                log.warning("could not load %s (%d in a row): %s", item["videoId"], self.fails, err)
                if self.fails >= MAX_FAILS:
                    log.error("giving up after %d failures; check cookies or run ./update.sh", self.fails)
                    self.fails, self.now, self.loading = 0, None, False
                    self.sync_clock()
                else:
                    await self.advance("load failed")
            else:
                self.fails = 0
                if media.duration:
                    item["duration"] = media.duration
                self.loading = False
                self.session_log.append({k: item.get(k) for k in ("videoId", "title", "artists")})
                del self.session_log[:-SESSION_LOG_MAX]
                self.sync_clock()
                self.prefetch_next()
                if self.host:
                    asyncio.create_task(fetch_like(item["videoId"]))
        await self.broadcast()

    async def relinked(self):
        """New cookies were saved: start the current song if it was waiting on them."""
        resolver.bot_check = False
        library_cache.clear()
        async with self.lock:
            if self.now and self.loading:
                asyncio.create_task(self._prepare(self.now))
            self.sync_clock()
        await self.broadcast()

    async def refill_auto(self, seed: dict):
        async with self.auto_lock:
            if not self.radio or len(self.auto) >= AUTO_TARGET:
                return
            try:
                wp = await asyncio.to_thread(ytm.get_watch_playlist, videoId=seed["videoId"], limit=30)
            except Exception as e:
                log.warning("autoplay fetch failed for %s: %s", seed["videoId"], e)
                return
            skip = set(self.history) | {t["videoId"] for t in self.queue + self.auto} | {seed["videoId"]}
            if self.now:
                skip.add(self.now["videoId"])
            for t in filter(None, map(to_track, wp.get("tracks", []))):
                if len(self.auto) >= AUTO_TARGET:
                    break
                search_cache[t["videoId"]] = t  # so "add to queue" from Autoplay keeps metadata
                if t["videoId"] not in skip:
                    skip.add(t["videoId"])
                    self.auto.append(self.item(t, auto=True))
            log.info("autoplay: %d radio tracks seeded from %s", len(self.auto), seed["videoId"])

    async def refill_auto_and_broadcast(self, seed: dict):
        await self.refill_auto(seed)
        self.prefetch_next()
        await self.broadcast()

    # ---- people
    async def close_device(self, d: Device, code: int, reason: str):
        await asyncio.gather(*(_close_ws(ws, code, reason) for ws in list(d.sockets)))

    async def remove_device(self, d: Device, actor: Actor, ban: bool = False):
        """Kick (may rejoin) or ban (may not). Closes every socket of that device; a pocket stream ends on its next chunk."""
        async with self.lock:
            (self.people.ban if ban else self.people.kick)(d.id)
            self.votes.discard(d.id)
            if ban:
                self.fq.retain(lambda t: t.get("owner") != d.id)  # a banned guest's queued songs go too
            self.record(actor, "ban" if ban else "kick", f"{'banned' if ban else 'removed'} {d.name or 'a guest'}")
            if self.check_votes():
                await self.advance("vote skip")
            self.sync_clock()
        await self.close_device(d, 4005 if ban else 4004, "removed by host")
        await self.broadcast()

    async def rotate_code(self, actor: Actor):
        """New room code without ending the session: everyone connected gets it over their socket and stays."""
        async with self.lock:
            self.code = new_code()
            self.record(actor, "code", "changed the room code")
        msg = {"type": "code", "code": self.code}
        await self._send_all("code", [(ws, msg) for ws, _ in self.people.sockets()])
        await self.broadcast()

    async def rotate(self):
        """New session: fresh code, empty room, kick guests holding the old code."""
        async with self.lock:
            self.code = new_code()
            self.fq.clear()
            self.auto.clear()
            self.now = None
            self.started_at, self.frozen = None, 0.0
            self.loading = self.user_paused = False
            self.votes.clear()
            self.history.clear()
            self.played.clear()
            self.back_stack.clear()
            self.session_log.clear()
            self.feed.clear()
            self.hands = None
            self.session_start, self.songs_played, self.peak_listeners = time.time(), 0, 0
            old = list(self.people.devices.values())
            self.people.reset()
            self.sync_clock()
        for d in old:
            await self.close_device(d, 4001, "room code changed")
        log.info("new session")
        await self.broadcast()


async def _close_ws(ws: WebSocket, code: int, reason: str):
    try:
        await asyncio.wait_for(ws.close(code=code, reason=reason), 2)
    except Exception:
        pass


# Shown to guests and on the host dashboard when YouTube auth needs attention.
NOTICES = {
    "missing": "Playback is off until the host links a YouTube Music account.",
    "expired": "The host's YouTube link has expired. Playback may stop until they relink it.",
}
room = Room()
fail_log: dict[str, deque] = {}
search_cache: dict[str, dict] = {}  # videoId -> track, from searches/albums/playlists/radio
library_cache = TTLCache(600, 60)   # the host's playlist list and playlists, 10 minutes
lyrics_cache = TTLCache(6 * 3600, 200)
# YouTube link health: OK (Premium, 256k), degraded (signed in, no Premium or no 256k), failed.
health: dict = {"state": "unknown", "checked_at": None, "account": None, "premium": None, "aac256": None,
                "problem": None, "expires": None}
health_lock = asyncio.Lock()


def pocket_source():
    """What the pocket station should be playing right now (see Station)."""
    n = room.now
    if not n or room.status() in ("idle", "unlinked"):
        return None
    src = f"http://127.0.0.1:{INTERNAL_PORT}/stream/{n['videoId']}?code={room.code}&token={INTERNAL_TOKEN}"
    return n["qid"], src, (time.time() - room.started_at if room.started_at is not None else None)


station = Station(pocket_source, codec=os.environ.get("POCKET_CODEC", "aac"))
log.info("cesura %s ready", VERSION)
def _config_writable() -> bool:
    try:
        probe = os.path.join(CONFIG_DIR, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        return True
    except OSError:
        return False


if not _config_writable():
    bar = "!" * 72
    print(f"\n{bar}\n  {CONFIG_DIR} is not writable by this container (uid {os.getuid()}).\n"
          "  cesura stores its setup, host key and cookies there. Fix one of:\n"
          f"    sudo chown -R {os.getuid()}:{os.getgid()} ./config\n"
          "    or set CESURA_UID / CESURA_GID in .env to the owner of ./config\n"
          f"{bar}\n", flush=True)
    _token = None
else:
    _token = setupstate.setup_token()
if _token:
    bar = "=" * 72
    print(f"\n{bar}\n  cesura is not set up yet.\n\n  Open  /setup  in your browser and enter this one-time setup token:\n\n"
          f"      {_token}\n\n  Only someone with this token can configure this server. It stops\n"
          f"  working as soon as setup is finished.\n{bar}\n", flush=True)
elif resolver.cookie_health() == "missing":  # (expired can only be known after a lookup)
    log.warning("YouTube is not linked: playback is off. Open /host and use Relink YouTube.")


# ------------------------------------------------------------------ helpers

def device_name(ua: str) -> str:
    """"iPhone · Safari" style label for the host's Listening list."""
    os_ = next((n for k, n in [("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "Mac"),
                               ("Windows", "Windows"), ("CrOS", "ChromeOS"), ("Linux", "Linux")] if k in ua), "Device")
    if os_ == "Android" and "Pixel" in ua:
        os_ = "Pixel"
    br = next((n for k, n in [("Edg/", "Edge"), ("Firefox/", "Firefox"), ("FxiOS", "Firefox"), ("CriOS", "Chrome"),
                              ("Chrome/", "Chrome"), ("Safari/", "Safari")] if k in ua), "Browser")
    return f"{os_} · {br}"


def client_ip(conn: Request | WebSocket) -> str:
    return conn.client.host if conn.client else "?"


def check_secret(given: str | None, real: str | None, ip: str) -> int:
    """0 if ok, else the HTTP status to answer with. Wrong guesses are counted
    per IP; after FAIL_LIMIT in FAIL_WINDOW that IP is locked out, which keeps
    the 5-letter room code from being brute-forced over the public Funnel."""
    now = time.monotonic()
    q = fail_log.setdefault(ip, deque())
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    if len(q) >= FAIL_LIMIT:
        return 429
    if given and real and secrets.compare_digest(given.encode(), real.encode()):
        return 0
    q.append(now)
    if len(fail_log) > 10000:
        for k in [k for k, v in fail_log.items() if not v]:
            del fail_log[k]
    log.warning("wrong code/key from %s (%d/%d)", ip, len(q), FAIL_LIMIT)
    return 403


CODE_SHAPE = re.compile(f"[{CODE_ALPHABET}]{{5}}")


def require_code(conn: Request, code: str | None):
    code = (code or "").upper()
    # Only a well-formed code is a guess. A missing or garbled one (a client bug such as
    # "?code=null") is refused without counting, so it can't lock out a whole network.
    if not CODE_SHAPE.fullmatch(code):
        raise HTTPException(403, "bad room code")
    status = check_secret(code, room.code, client_ip(conn))
    if status:
        raise HTTPException(status, "too many wrong codes, try again later" if status == 429 else "bad room code")


def require_key(conn: Request, key: str | None):
    status = check_secret(key, HOST_KEY, client_ip(conn))
    if status:
        raise HTTPException(status, "too many attempts" if status == 429 else "bad host key")


def rate_limited(ip: str, log_: dict | None = None, limit: int = ADD_LIMIT) -> bool:
    now = time.monotonic()
    q = (room.add_log if log_ is None else log_).setdefault(ip, deque())
    while q and now - q[0] > ADD_WINDOW:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


lookup_log: dict[str, deque] = {}  # lyrics + pasted playlists: each is a YouTube round trip
LOOKUP_LIMIT = 30


def limit_lookups(request: Request):
    if rate_limited(client_ip(request), lookup_log, LOOKUP_LIMIT):
        raise HTTPException(429, "slow down")


# ------------------------------------------------------------------ pages

@app.get("/")
async def guest_page():
    return FileResponse(os.path.join(STATIC, "guest.html"))


@app.get("/art/{aid}")
async def artwork(aid: str, s: int = 120):
    """Album art and artist images, from the disk cache (see artcache.py). Public pictures: no code needed."""
    got = await art.get(aid, s)
    if not got:
        raise HTTPException(404)
    return FileResponse(got[0], media_type=got[1], headers={"Cache-Control": "public, max-age=604800, immutable"})


@app.get("/healthz")
async def healthz():
    """Container healthcheck: the web server is up. No auth, no state, no secrets."""
    return {"ok": True, "version": VERSION, "setup_complete": setupstate.is_complete(), "youtube": resolver.cookie_health()}


@app.get("/host")
async def host_page():
    if not setupstate.is_complete():
        return RedirectResponse("/setup", status_code=303)
    return FileResponse(os.path.join(STATIC, "host.html"))


@app.get("/setup")
async def setup_page():
    if setupstate.is_complete():
        return RedirectResponse("/host", status_code=303)
    return FileResponse(os.path.join(STATIC, "setup.html"), headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ guest API
# Every endpoint needs the room code. Actions also need either the device's
# session token (from its websocket welcome) or, for the host dashboard, the
# host key in X-Host-Key.

class CodeBody(BaseModel):
    code: str


class JoinBody(CodeBody):
    device: str = ""


class ActBody(CodeBody):
    token: str = ""


class AddBody(ActBody):
    videoIds: list[str]   # one track, or a whole album/playlist ("add all")
    next: bool = False    # guests: front of their own turns; host/co-host: very next


class QidBody(ActBody):
    qid: str


class PauseBody(ActBody):
    paused: bool


class SeekBody(QidBody):
    position: float


class MoveBody(QidBody):
    index: int                     # where it goes, counted without it
    from_index: int | None = None  # where the client saw it: if it's moved since, the drag is refused


class PidBody(ActBody):
    pid: str


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_BULK = 50  # tracks per "add all"
REFUSALS = {"banned": "The host removed you from this room.", "locked": "This room is locked. Ask the host to unlock it."}


def remember(tracks: list[dict]):
    for t in tracks:
        search_cache[t["videoId"]] = t
    while len(search_cache) > 3000:
        search_cache.pop(next(iter(search_cache)))


def artist_names(item: dict) -> str:
    return ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name"))


def host_key_ok(request: Request) -> bool:
    """True if X-Host-Key is present and right; raises if present and wrong."""
    key = request.headers.get("x-host-key")
    if key is None:
        return False
    require_key(request, key)
    return True


def actor_for(request: Request, body: ActBody) -> Actor:
    """Room code first, then who: the host (X-Host-Key) or the device holding this session token."""
    require_code(request, body.code)
    if host_key_ok(request):
        return HOST
    d = room.people.by_token(body.token)
    if d is None:
        raise HTTPException(401, "session expired, reconnecting")
    return Actor(d.id, d.name or "Guest", d.role, d)


def mod_for(request: Request, body: ActBody) -> Actor:
    actor = actor_for(request, body)
    if not actor.mod:
        raise HTTPException(403, "only the host or a co-host can do that")
    return actor


@app.post("/api/join")
async def join(body: JoinBody, request: Request):
    require_code(request, body.code)
    if DEVICE_ID.fullmatch(body.device) and (why := room.people.refusal(body.device)):
        raise HTTPException(409, REFUSALS[why])
    return {"ok": True}


@app.get("/api/search")
async def search(q: str, code: str, request: Request, kind: str = "songs"):
    require_code(request, code)
    q = q.strip()[:300]
    if not q:
        return {"results": []}
    # A pasted YouTube / YouTube Music link goes straight to that playlist or song.
    if m := re.search(r"[?&]list=([A-Za-z0-9_-]+)", q):
        return {"results": [], "open": {"kind": "playlist", "id": m[1]}}
    if m := re.search(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})", q):
        try:
            t = song_to_track(await asyncio.to_thread(ytm.get_song, m[1]))
        except Exception:
            t = None
        if t:
            remember([t])
        return {"results": [{**t, "kind": "song"}] if t else []}
    kind = kind if kind in ("all", "songs", "albums", "playlists") else "songs"
    # "All" = the three filtered searches in parallel; unfiltered results lack artist/album data.
    kinds = ("songs", "albums", "playlists") if kind == "all" else (kind,)
    limits = {"songs": 10, "albums": 6, "playlists": 6} if kind == "all" else {kind: 20}
    try:
        raws = await asyncio.gather(*(asyncio.to_thread(ytm.search, q, filter=k, limit=limits[k]) for k in kinds))
    except Exception as e:
        log.warning("search failed: %s", e)
        raise HTTPException(502, "search failed")
    return {"results": [r for k, raw in zip(kinds, raws) for r in format_results(k, raw)]}


def format_results(kind: str, raw: list[dict]) -> list[dict]:
    if kind == "songs":
        tracks = [t for t in map(to_track, raw) if t]
        remember(tracks)
        return [{**t, "kind": "song"} for t in tracks]
    out = []
    for r in raw:
        rid = r.get("browseId") or r.get("playlistId")
        if not rid:
            continue
        if kind == "albums":
            sub = [artist_names(r), r.get("year"), r.get("type")]
        else:
            sub = [r.get("author"), f"{r['itemCount']} songs" if r.get("itemCount") else None]
        out.append({"kind": kind[:-1], "id": rid, "title": r.get("title") or "Untitled",
                    "subtitle": " · ".join(str(x) for x in sub if x), "thumb": _thumb(r.get("thumbnails"))})
    return out


def playlist_view(p: dict, pid: str, title: str = "Playlist") -> dict:
    title, thumb = p.get("title") or title, _thumb(p.get("thumbnails"))
    author = p.get("author")
    author = author.get("name") if isinstance(author, dict) else author
    count = p.get("trackCount")
    subtitle = " · ".join(str(x) for x in [author, f"{count} songs" if count else None] if x)
    tracks = [t for t in (to_track(t, thumb=thumb) for t in p.get("tracks") or []) if t]
    remember(tracks)
    thumb = thumb or next((t["thumb"] for t in tracks if t["thumb"]), None)
    return {"kind": "playlist", "id": pid, "title": title, "subtitle": subtitle, "thumb": thumb, "tracks": tracks}


@app.get("/api/collection")
async def collection(kind: str, id: str, code: str, request: Request):
    """An album or playlist with its tracks, for browsing and "add all"."""
    require_code(request, code)
    if kind not in ("album", "playlist") or not re.fullmatch(r"[A-Za-z0-9_-]{2,80}", id):
        raise HTTPException(400)
    try:
        if kind == "playlist":
            return playlist_view(await asyncio.to_thread(ytm.get_playlist, id, limit=200), id)
        a = await asyncio.to_thread(ytm.get_album, id)
    except Exception as e:
        log.warning("%s %s failed: %s", kind, id, e)
        raise HTTPException(502, f"could not load {kind}")
    title, thumb = a.get("title") or "Album", _thumb(a.get("thumbnails"))
    subtitle = " · ".join(str(x) for x in [artist_names(a), a.get("year")] if x)
    tracks = [t for t in (to_track(t, album=title, thumb=thumb) for t in a.get("tracks", [])) if t]
    remember(tracks)
    thumb = thumb or next((t["thumb"] for t in tracks if t["thumb"]), None)
    return {"kind": kind, "id": id, "title": title, "subtitle": subtitle, "thumb": thumb, "tracks": tracks}


@app.get("/api/playlist")
async def pasted_playlist(url: str, code: str, request: Request):
    """A guest pasted a YouTube / YT Music playlist link. Public and unlisted only: fetched signed out."""
    require_code(request, code)
    limit_lookups(request)
    pid = normalize_playlist(url)
    if not pid:
        raise HTTPException(400, "That isn't a playlist link.")
    try:
        p = await asyncio.to_thread(public_ytm.get_playlist, pid, limit=200)
    except Exception as e:
        log.info("pasted playlist %s failed: %s", pid, type(e).__name__)
        raise HTTPException(404, "Couldn't open that playlist. It may be private.")
    return playlist_view(p, pid)


@app.post("/api/add")
async def add(body: AddBody, request: Request):
    actor = actor_for(request, body)
    ids = [v for v in body.videoIds if VIDEO_ID.fullmatch(v)][:MAX_BULK]
    if not ids:
        raise HTTPException(400, "bad videoId")
    if not actor.mod and rate_limited(client_ip(request)):
        raise HTTPException(429, "slow down")
    space = MAX_QUEUE - len(room.queue)
    if space <= 0:
        raise HTTPException(409, "queue full")
    tracks = []
    for vid in ids[:space]:
        t = search_cache.get(vid)
        if not t and len(ids) == 1:
            try:
                t = song_to_track(await asyncio.to_thread(ytm.get_song, vid))
            except Exception:
                t = None
        if t:
            tracks.append(t)
    if not tracks:
        raise HTTPException(404, "unknown track")
    how = ("next" if actor.mod else "mine_first") if body.next else "end"
    async with room.lock:
        items = [room.item(t, by=actor.label) for t in tracks]
        room.fq.add(items, actor.key, how)
        added = {t["videoId"] for t in tracks}
        room.auto = [a for a in room.auto if a["videoId"] not in added]  # promoted from Autoplay
        room.record(actor, "queue", f"queued {tracks[0]['title']}" if len(tracks) == 1 else f"queued {len(tracks)} songs")
        if room.now is None:
            await room.advance("first add")
        else:
            room.prefetch_next()
    await room.broadcast()
    return {"ok": True, "added": len(items)}


@app.post("/api/remove")
async def remove(body: QidBody, request: Request):
    """Guests can take back tracks they added; the host and co-hosts can remove anything, Autoplay included."""
    actor = actor_for(request, body)
    async with room.lock:
        t = room.fq.find(body.qid)
        if t is None and actor.mod:
            t = next((a for a in room.auto if a["qid"] == body.qid), None)
            if t:
                room.auto.remove(t)
        elif t is not None:
            if not actor.mod and t.get("owner") != actor.key:
                raise HTTPException(409, "not your track")
            room.fq.remove(body.qid)
        if not t:
            return {"ok": False}
        room.record(actor, "remove", f"removed {t['title']}")
        room.prefetch_next()
    await room.broadcast()
    return {"ok": True}


@app.post("/api/skip")
async def skip(body: QidBody, request: Request):
    """The host and co-hosts skip right away. A guest's skip is a vote."""
    actor = actor_for(request, body)
    async with room.lock:
        if not room.now or room.now["qid"] != body.qid:
            return {"ok": False, "stale": True}
        title = room.now["title"]
        if actor.mod:
            room.record(actor, "skip", f"skipped {title}")
            await room.advance("skip", {"kind": "skip", "text": f"{actor.label} skipped to this"})
        else:
            room.votes.add(actor.key)
            room.record(actor, "vote", f"voted to skip {title} ({len(room.votes)}/{room.votes_needed()})")
            if room.check_votes():
                n = len(room.votes)
                room.feed.append({"who": "Room", "role": "room", "kind": "skip", "text": f"voted off {title} ({n} vote{'s' if n > 1 else ''})", "at": time.time()})
                await room.advance("vote skip", {"kind": "skip", "text": f"The room voted to skip to this ({n})"})
    await room.broadcast()
    return {"ok": True}


@app.post("/api/back")
async def back(body: QidBody, request: Request):
    """Back, like any player: restart the song once it has played BACK_RESTART seconds (or when
    there's nothing before it), otherwise go to the previous one. Works after a skip: the server keeps
    the play history. The host's and co-hosts' call, like skip."""
    actor = mod_for(request, body)
    async with room.lock:
        if room.now and room.now["qid"] != body.qid:
            return {"ok": False, "stale": True}
        if room.now and (room.position() > BACK_RESTART or not room.back_stack):
            room.seek_to(0.0)
            room.record(actor, "back", f"restarted {room.now['title']}")
            room.hands = {"kind": "back", "text": f"Restarted by {actor.label}"}
            did = "restarted"
        elif room.back_stack:
            room.go_back({"kind": "back", "text": f"{actor.label} went back to this"})
            room.record(actor, "back", f"went back to {room.now['title']}")
            did = "back"
        else:
            return {"ok": False}
    await room.broadcast()
    return {"ok": True, "did": did}


@app.post("/api/pause")
async def pause(body: PauseBody, request: Request):
    """Anyone in the room can pause or resume for everyone; the room sees who."""
    actor = actor_for(request, body)
    async with room.lock:
        if room.user_paused != body.paused and room.now:
            room.user_paused = body.paused
            verb = "paused" if body.paused else "resumed"
            room.record(actor, "pause" if body.paused else "resume", verb)
            room.hands = {"kind": "pause" if body.paused else "resume", "text": f"{verb.capitalize()} by {actor.label}"}
            room.sync_clock()
    await room.broadcast()
    return {"ok": True}


def mmss(secs: float) -> str:
    return f"{int(secs) // 60}:{int(secs) % 60:02d}"


@app.post("/api/seek")
async def seek(body: SeekBody, request: Request):
    """Jump to a position in the current track. Same rights as pause: anyone in the room, and the
    room sees who. The server moves the clock; every listener and the pocket stream follow it."""
    actor = actor_for(request, body)
    if not math.isfinite(body.position):
        raise HTTPException(400, "bad position")
    async with room.lock:
        if not room.now or room.now["qid"] != body.qid:
            return {"ok": False, "stale": True}
        if room.loading:
            raise HTTPException(409, "The song is still loading.")
        dur = room.now.get("duration") or 0
        pos = max(0.0, min(body.position, dur) if dur else body.position)
        room.seek_to(pos)
        room.record(actor, "seek", f"jumped to {mmss(pos)} in {room.now['title']}")
        room.hands = {"kind": "seek", "text": f"{actor.label} jumped to {mmss(pos)}"}
    await room.broadcast()
    return {"ok": True, "position": pos}


@app.get("/api/lyrics")
async def lyrics(videoId: str, code: str, request: Request):
    require_code(request, code)
    if not VIDEO_ID.fullmatch(videoId):
        raise HTTPException(400)
    got = lyrics_cache.get(videoId)
    if got is None:
        limit_lookups(request)
        try:
            got = await asyncio.to_thread(fetch_lyrics, ytm, videoId)
        except Exception as e:
            log.info("lyrics for %s failed: %s", videoId, type(e).__name__)
            raise HTTPException(502, "couldn't load lyrics")
        lyrics_cache.put(videoId, got)
    return got


# ------------------------------------------------------------------ the host's library (opt-in per room)

def require_library(request: Request, code: str):
    require_code(request, code)
    if not room.library_on:
        raise HTTPException(409, "The host's library is off for this room.")
    if resolver.cookie_health() == "missing":
        raise HTTPException(409, "The host hasn't linked YouTube Music.")


async def library_lists() -> list[dict]:
    lists = library_cache.get("lists")
    if lists is None:
        def fetch():
            return cookie_ytmusic().get_library_playlists(limit=100)
        raw = await asyncio.to_thread(fetch)
        lists = [{"id": p["playlistId"], "title": p.get("title") or "Playlist",
                  "subtitle": f"{p['count']} songs" if p.get("count") else "",
                  "thumb": _thumb(p.get("thumbnails"))}
                 for p in raw if p.get("playlistId") and p["playlistId"] not in ("LM", "SE")]
        library_cache.put("lists", lists)
    return lists


@app.get("/api/library")
async def library(code: str, request: Request):
    """The host's playlists (signed in as the host). Only while the host has the library tab on."""
    require_library(request, code)
    try:
        lists = await library_lists()
    except Exception as e:
        log.warning("library list failed: %s", type(e).__name__)
        raise HTTPException(502, "Couldn't load the host's library.")
    return {"liked": {"id": "LM", "title": "Liked songs"}, "playlists": lists}


@app.get("/api/library/playlist")
async def library_playlist(id: str, code: str, request: Request):
    require_library(request, code)
    try:
        allowed = id == "LM" or any(p["id"] == id for p in await library_lists())
    except Exception:
        raise HTTPException(502, "Couldn't load the host's library.")
    if not allowed:
        raise HTTPException(404, "Not in the host's library.")
    view = library_cache.get(f"pl:{id}")
    if view is None:
        def fetch():
            yt = cookie_ytmusic()
            return yt.get_liked_songs(limit=200) if id == "LM" else yt.get_playlist(id, limit=200)
        try:
            view = playlist_view(await asyncio.to_thread(fetch), id, "Liked songs" if id == "LM" else "Playlist")
        except Exception as e:
            log.warning("library playlist failed: %s", type(e).__name__)
            raise HTTPException(502, "Couldn't open that playlist.")
        if id == "LM":
            view["title"] = "Liked songs"
        library_cache.put(f"pl:{id}", view)
    remember(view["tracks"])
    return view


# ------------------------------------------------------------------ host + co-host (moderation)

@app.post("/api/move")
@app.post("/api/mod/move")
async def move(body: MoveBody, request: Request):
    """Drag to reorder. The host and co-hosts move anything (it's pinned there; round-robin no
    longer moves it). Guests move their own songs among their own turns (see FairQueue.reorder_own).
    Every drag runs under the room lock against the queue as it is now, so two people dragging at
    once can't corrupt it: a drag based on a stale view (from_index) is refused and the client
    redraws from the broadcast."""
    actor = actor_for(request, body)
    async with room.lock:
        t = room.fq.find(body.qid)
        if t is None:
            raise HTTPException(409, "That song isn't in the queue any more.")
        if body.from_index is not None and room.queue.index(t) != body.from_index:
            raise HTTPException(409, "The queue changed while you were dragging.")
        if actor.mod:
            room.fq.move(body.qid, body.index)
        elif t.get("owner") != actor.key:
            raise HTTPException(403, "You can only move songs you added.")
        elif not room.fq.reorder_own(body.qid, body.index):
            raise HTTPException(409, "The host pinned that song.")
        at = room.queue.index(t)
        room.record(actor, "move", f"moved {t['title']} to #{at + 1}")
        room.prefetch_next()
    await room.broadcast()
    return {"ok": True, "index": at}


@app.post("/api/mod/kick")
async def mod_kick(body: PidBody, request: Request):
    actor = mod_for(request, body)
    d = room.people.by_pid(body.pid)
    if d is None:
        return {"ok": False}
    if actor.role == "cohost" and d.cohost:
        raise HTTPException(403, "co-hosts can't remove other co-hosts")
    await room.remove_device(d, actor)
    return {"ok": True}


# ------------------------------------------------------------------ host API (host key only)
# The key travels in X-Host-Key, except for the QR image (an <img> can't send headers).

class HostBody(BaseModel):
    pid: str = ""
    on: bool = False
    qid: str = ""
    index: int = 0
    value: float = 0.0
    date: str = ""


def require_host(request: Request):
    require_key(request, request.headers.get("x-host-key"))


@app.get("/api/qr.svg")
async def qr(key: str, url: str, request: Request):
    require_key(request, key)
    if not re.match(r"https?://", url) or len(url) > 300:
        raise HTTPException(400)
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=10, border=2, dark="#000", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.post("/api/host/ban")
async def host_ban(body: HostBody, request: Request):
    require_host(request)
    d = room.people.by_pid(body.pid)
    if d is None:
        return {"ok": False}
    await room.remove_device(d, HOST, ban=True)
    return {"ok": True}


@app.post("/api/host/cohost")
async def host_cohost(body: HostBody, request: Request):
    require_host(request)
    d = room.people.by_pid(body.pid)
    if d is None:
        return {"ok": False}
    d.role = "cohost" if body.on else "guest"
    room.record(HOST, "cohost", f"made {d.name or 'a guest'} {'a co-host' if body.on else 'a guest again'}")
    await room.broadcast()
    return {"ok": True}


@app.post("/api/host/code")
async def host_rotate_code(request: Request):
    """New room code, same session: connected listeners get it and stay; anyone kicked can't use the old one."""
    require_host(request)
    await room.rotate_code(HOST)
    return {"ok": True, "code": room.code}


@app.post("/api/host/lock")
async def host_lock(body: HostBody, request: Request):
    require_host(request)
    room.people.locked = body.on
    room.record(HOST, "lock", "locked the room" if body.on else "unlocked the room")
    await room.broadcast()
    return {"ok": True}


@app.post("/api/host/settings")
async def host_settings(request: Request, vote_fraction: float | None = None, library: bool | None = None):
    require_host(request)
    if vote_fraction is not None:
        if vote_fraction not in VOTE_FRACTIONS:
            raise HTTPException(400, "unsupported vote threshold")
        room.vote_fraction = vote_fraction
        async with room.lock:
            if room.check_votes():
                await room.advance("vote skip")
    if library is not None:
        room.library_on = library
        room.record(HOST, "library", "shared their library" if library else "stopped sharing their library")
    await room.broadcast()
    return {"ok": True}


@app.post("/api/host/clear")
async def host_clear(request: Request):
    """Remove every upcoming song people queued. The current song keeps playing, the play history
    (Back) stays, and so do the radio picks, which play next as with any empty queue. Host key only:
    a guest or co-host is refused here, not just by a hidden button."""
    require_host(request)
    async with room.lock:
        n = len(room.queue)
        room.fq.clear()
        room.record(HOST, "clear", f"cleared the queue ({n} song{'' if n == 1 else 's'})")
        room.prefetch_next()
        if room.radio and room.now and not room.auto:
            asyncio.create_task(room.refill_auto_and_broadcast(room.now))
    await room.broadcast()
    return {"ok": True, "removed": n}


LIKE_CHECKS: dict[str, asyncio.Task] = {}


async def fetch_like(video_id: str):
    """Whether the host has liked this song, for the dashboard's heart. One lookup per song, host only."""
    if video_id in room.liked or video_id in LIKE_CHECKS or resolver.cookie_health() == "missing":
        return

    def lookup():
        tracks = cookie_ytmusic().get_watch_playlist(videoId=video_id, limit=1).get("tracks") or []
        return next((t.get("likeStatus") for t in tracks if t.get("videoId") == video_id), None)
    LIKE_CHECKS[video_id] = asyncio.current_task()
    try:
        status = await asyncio.to_thread(lookup)
    except Exception as e:
        log.info("like status for %s failed: %s", video_id, type(e).__name__)
        return
    finally:
        LIKE_CHECKS.pop(video_id, None)
    if status in ("LIKE", "INDIFFERENT", "DISLIKE") and video_id not in room.liked:
        room.liked[video_id] = status == "LIKE"
        await room.broadcast()


class LikeBody(BaseModel):
    videoId: str
    on: bool


@app.post("/api/host/like")
async def host_like(body: LikeBody, request: Request):
    """Like (or un-like: INDIFFERENT) a song on the host's YouTube Music account, which feeds their
    radio. Host key only: guests and co-hosts are refused by the server. A YouTube error is
    reported as-is (502) and the liked state stays as it was."""
    require_host(request)
    if not VIDEO_ID.fullmatch(body.videoId):
        raise HTTPException(400, "bad videoId")
    if resolver.cookie_health() == "missing":
        raise HTTPException(409, "Link YouTube Music first.")
    rating = "LIKE" if body.on else "INDIFFERENT"

    def rate():
        return cookie_ytmusic().rate_song(body.videoId, rating)
    try:
        res = await asyncio.to_thread(rate)
    except Exception as e:
        log.warning("rate_song %s %s failed: %s: %s", body.videoId, rating, type(e).__name__, str(e)[:200])
        raise HTTPException(502, f"YouTube Music didn't accept that ({type(e).__name__}). Nothing was changed.")
    if not isinstance(res, dict):
        raise HTTPException(502, "YouTube Music sent an unexpected answer. Nothing was changed.")
    room.liked[body.videoId] = body.on
    title = room.now["title"] if room.now and room.now["videoId"] == body.videoId else body.videoId
    room.record(HOST, "like", f"{'liked' if body.on else 'un-liked'} {title}")
    await room.broadcast()
    return {"ok": True, "liked": body.on}


@app.post("/api/host/radio")
async def host_radio(body: HostBody, request: Request):
    require_host(request)
    async with room.lock:
        room.radio = body.on
        if not body.on:
            room.auto.clear()
        elif room.now and not room.queue:
            asyncio.create_task(room.refill_auto_and_broadcast(room.now))
    await room.broadcast()
    return {"ok": True}


@app.post("/api/host/export")
async def host_export(body: HostBody, request: Request):
    """Save this session's songs (in play order, no repeats) as a private playlist in the host's YT Music library."""
    require_host(request)
    ids = session_tracks(room.session_log)
    if not ids:
        raise HTTPException(409, "Nothing has played yet.")
    if resolver.cookie_health() == "missing":
        raise HTTPException(409, "Link YouTube Music first.")
    day = body.date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", body.date) else time.strftime("%Y-%m-%d")
    title = f"cesura — {day}"

    def create():
        return cookie_ytmusic().create_playlist(title, "Played in a cesura listening room.", "PRIVATE", ids)
    try:
        res = await asyncio.to_thread(create)
    except Exception as e:
        log.warning("export failed: %s", type(e).__name__)
        raise HTTPException(502, "YouTube Music didn't accept the playlist.")
    if not isinstance(res, str):
        raise HTTPException(502, "YouTube Music didn't accept the playlist.")
    library_cache.clear()
    room.record(HOST, "export", f"saved {len(ids)} songs as “{title}”")
    await room.broadcast()
    return {"ok": True, "title": title, "count": len(ids), "url": f"https://music.youtube.com/playlist?list={res}"}


@app.post("/api/host/health")
async def host_health(request: Request):
    require_host(request)
    await check_health(force=True)
    return health


@app.get("/api/host/net")
async def host_net(request: Request):
    """Network totals since start: pushes to clients, fetches from YouTube, peak concurrency and bandwidth."""
    require_host(request)
    return net.snapshot()


@app.post("/api/new-session")
async def new_session(request: Request):
    require_host(request)
    await room.rotate()
    return {"ok": True, "code": room.code}


# ------------------------------------------------------------------ YouTube link health

def apply_probe(res: dict):
    resolver.bot_check = res["problem"] in ("bot-check", "signed-out")  # -> "expired"
    ok = res["account"] and res["aac256"]
    state = "ok" if ok else "degraded" if res["account"] and res["problem"] in (None, "unavailable") else "failed"
    health.update(state=state, checked_at=time.time(), account=res["account"], premium=res["premium"],
                  aac256=res["aac256"], problem=res["problem"])


async def check_health(force: bool = False):
    """Re-run the link check (account, Premium, 256k). Debounced unless forced; one at a time."""
    if health_lock.locked() or (not force and health["checked_at"] and time.time() - health["checked_at"] < HEALTH_DEBOUNCE):
        return
    async with health_lock:
        if resolver.cookie_health() == "missing":
            health.update(state="failed", checked_at=time.time(), account=False, premium=False, aac256=False,
                          problem="missing", expires=None)
        else:
            try:
                apply_probe(await asyncio.to_thread(probe_account))
            except Exception as e:
                log.warning("health check failed: %s", type(e).__name__)
                health.update(state="failed", checked_at=time.time(), problem="unavailable")
            health["expires"] = await asyncio.to_thread(cookiejar.soonest_auth_expiry, COOKIES)
        log.info("YouTube link health: %s", health["state"])
    await room.broadcast()


# ------------------------------------------------------------------ setup wizard + YouTube linking
# Credentials travel in headers (X-Setup-Token / X-Host-Key), never in URLs,
# so they can't end up in access logs or browser history.

def require_setup_token(request: Request):
    if setupstate.is_complete():
        raise HTTPException(410, "Setup is already complete.")
    status = check_secret(request.headers.get("x-setup-token", ""), setupstate.setup_token(), client_ip(request))
    if status:
        raise HTTPException(status, "Too many attempts. Try again later." if status == 429 else "That setup token isn't right. Copy it from the container logs.")


def require_linker(request: Request):
    """Cookie upload/probe: the setup token during setup, the host key afterwards."""
    key = request.headers.get("x-host-key")
    if key is not None:
        status = check_secret(key, HOST_KEY, client_ip(request))
        if status:
            raise HTTPException(status, "Too many attempts." if status == 429 else "Bad host key.")
        return
    require_setup_token(request)


class SetupKeyBody(BaseModel):
    regenerate: bool = False


class SetupUrlBody(BaseModel):
    url: str


@app.post("/api/setup/verify")
async def setup_verify(request: Request):
    require_setup_token(request)
    host = request.headers.get("host", "")
    return {
        "host_key": "env" if os.environ.get("HOST_KEY", "").strip() else "stored" if setupstate.stored_host_key() else None,
        "public_url": public_url(),
        "public_url_env": bool(ENV_PUBLIC_URL),
        "suggested_url": f"{request.url.scheme}://{host}" if host else "",
        "cookies": resolver.cookie_health(),
        "probe_video": PROBE_VIDEO_ID,
    }


@app.post("/api/setup/host-key")
async def setup_host_key(body: SetupKeyBody, request: Request):
    """Generate the host key and return it this once. Later calls only say it exists."""
    global HOST_KEY
    require_setup_token(request)
    if os.environ.get("HOST_KEY", "").strip():
        return {"source": "env"}
    if setupstate.stored_host_key() and not body.regenerate:
        return {"source": "stored"}
    HOST_KEY = setupstate.new_host_key()
    log.info("setup: host key generated and saved")
    return {"source": "generated", "key": HOST_KEY}


@app.post("/api/setup/public-url")
async def setup_public_url(body: SetupUrlBody, request: Request):
    require_setup_token(request)
    try:
        url = setupstate.check_public_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    state = setupstate.load_state()
    state["public_url"] = url
    setupstate.save_state(state)
    return {"public_url": url, "overridden_by_env": bool(ENV_PUBLIC_URL)}


@app.post("/api/setup/finish")
async def setup_finish(request: Request):
    require_setup_token(request)
    if not HOST_KEY:
        raise HTTPException(409, "Create the host key first.")
    setupstate.finish()
    log.info("setup: complete; the setup token is now invalid")
    await room.broadcast()
    return {"ok": True, "redirect": "/host"}


@app.post("/api/youtube/cookies")
async def youtube_cookies(request: Request):
    """Upload cookies.txt as the raw request body (max 256 KB). Returns counts only."""
    require_linker(request)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > cookiejar.MAX_UPLOAD:
            break
    try:
        counts = await asyncio.to_thread(cookiejar.ingest, bytes(data), COOKIES)
    except cookiejar.Rejected as e:
        log.info("cookie upload rejected (%d bytes)", len(data))
        raise HTTPException(413 if len(data) > cookiejar.MAX_UPLOAD else 400, str(e))
    log.info("cookies saved: %d YouTube cookies, %d from other sites dropped", counts["youtube_cookies"], counts["dropped_other_sites"])
    await room.relinked()
    return {"ok": True, **counts}


@app.post("/api/youtube/probe")
async def youtube_probe(request: Request):
    """Check the saved cookies against YouTube: account, Premium, 256k AAC."""
    require_linker(request)
    res = await asyncio.to_thread(probe_account)
    apply_probe(res)
    health["expires"] = await asyncio.to_thread(cookiejar.soonest_auth_expiry, COOKIES)
    await room.broadcast()
    return res


# ------------------------------------------------------------------ audio

def stream_device(request: Request, code: str, token: str) -> Device | None:
    """Audio needs the room code and a live session token (or, for the pocket station, the internal one)."""
    require_code(request, code)
    if secrets.compare_digest(token.encode(), INTERNAL_TOKEN.encode()) and client_ip(request) in ("127.0.0.1", "::1"):
        return None
    d = room.people.by_token(token)
    if d is None:
        raise HTTPException(401, "session expired")
    return d


@app.get("/stream/{video_id}")
async def stream(video_id: str, request: Request, code: str, token: str = ""):
    """Serve the current track with HTTP Range support, from RAM when possible.

    Proxying is required: googlevideo URLs are signed for the server's IP and
    expire, so listeners can't fetch them directly.
    """
    stream_device(request, code, token)
    if resolver.cookie_health() == "missing":
        raise HTTPException(409, "YouTube is not linked")
    if not room.now or room.now["videoId"] != video_id:
        raise HTTPException(409, "not the current track")
    try:
        m = await resolver.load(video_id)
    except Exception as e:
        log.warning("load failed for %s: %s", video_id, e)
        raise HTTPException(502, "could not load stream")
    size = m.size
    start, end, status = 0, size - 1, 200
    rng = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", "").strip())
    if rng and (rng[1] or rng[2]):
        if rng[1]:
            start = int(rng[1])
            end = min(int(rng[2]), size - 1) if rng[2] else size - 1
        else:  # suffix range: last N bytes
            start = max(0, size - int(rng[2]))
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if m.data is not None:
        return Response(m.data[start:end + 1], status_code=status, headers=headers, media_type=m.mime)
    return StreamingResponse(resolver.iter_range(video_id, start, end), status_code=status,
                             headers=headers, media_type=m.mime)


@app.get("/pocket")
async def pocket_stream(request: Request, code: str, token: str = ""):
    """Pocket mode: the room's shared, never-ending stream (see pocket.py)."""
    d = stream_device(request, code, token)
    if d is None:
        raise HTTPException(403)
    if d.streams >= POCKET_PER_DEVICE or station.listeners >= POCKET_MAX:
        raise HTTPException(429, "too many pocket streams")

    async def body():
        room.people.stream_opened(d)
        d.pocket = True
        await on_presence_change()
        try:
            async for chunk in station.listen():
                if room.people.devices.get(d.id) is not d:
                    return  # kicked, banned or new session: end the stream
                yield chunk
        finally:
            room.people.stream_closed(d)
            asyncio.create_task(on_presence_change())

    return StreamingResponse(body(), media_type=station.mime,
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


# ------------------------------------------------------------------ websockets

async def on_presence_change():
    """Listening counts changed: the clock may start or stop, and the vote threshold may have dropped."""
    async with room.lock:
        if room.check_votes():
            await room.advance("vote skip")
        room.sync_clock()
    room.peak_listeners = max(room.peak_listeners, room.listeners())
    await room.broadcast()


CLOSE = {"banned": (4005, "removed by host"), "locked": (4006, "room locked"), "mismatch": (4009, "device in use")}


@app.websocket("/ws")
async def guest_ws(ws: WebSocket, code: str = "", device: str = "", token: str = ""):
    """Guests: receive state. Send hello / listen / pocket / drift / ping (clock sync); answer hb (liveness).

    A device that already holds a session token may reconnect with it even if the
    code changed meanwhile (it was in the room when the host rotated the code).
    """
    await ws.accept()
    if not DEVICE_ID.fullmatch(device):
        await ws.close(code=4001, reason="update needed")
        return
    known = room.people.devices.get(device)
    if not (known and token and secrets.compare_digest(token.encode(), known.token.encode())):
        status = check_secret(code.upper(), room.code, client_ip(ws)) if CODE_SHAPE.fullmatch(code.upper()) else 403
        if status:
            await ws.close(code=4029 if status == 429 else 4001, reason="bad room code")
            return
    d, why = room.people.attach(ws, device, token)
    if d is None:
        await ws.close(*CLOSE[why])
        return
    d.ua = device_name(ws.headers.get("user-agent", ""))
    await ws.send_json({"type": "welcome", "token": d.token, "code": room.code})
    await on_presence_change()
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
            except ValueError:
                continue
            room.people.pong(ws)  # any message proves the socket is alive
            if room.people.device(ws) is not d:
                break  # kicked while this message was in flight
            kind = msg.get("type")
            if kind == "hello":
                d.name = re.sub(r"\s+", " ", str(msg.get("name") or "")).strip()[:24]
                await room.broadcast()
            elif kind == "drift":
                # Measured by the client (audio.currentTime vs the server clock); display only.
                try:
                    d.drift = (float(msg.get("ms")), bool(msg.get("catching_up")), time.time())
                except (TypeError, ValueError):
                    continue
                await room.push_host()
            elif kind == "ping":
                # Client computes its clock offset from this: server - (t_sent + rtt/2).
                await room._send(ws, {"type": "pong", "t": msg.get("t"), "server": time.time()})
            elif kind == "listen":
                d.listening = bool(msg.get("on"))
                await on_presence_change()
            elif kind == "pocket":
                d.pocket = bool(msg.get("on"))
                d.drift = None
                await on_presence_change()
            elif kind == "resync":  # it missed a patch: send the full state again
                room.sync.forget(ws)
                await room.broadcast()
    except WebSocketDisconnect:
        pass
    finally:
        room.people.detach(ws)
        room.sync.forget(ws)
        await on_presence_change()


@app.websocket("/ws/host")
async def host_ws(ws: WebSocket, key: str = ""):
    """Host screen: same state plus the room code. Plays no audio."""
    await ws.accept()
    if check_secret(key, HOST_KEY, client_ip(ws)):
        await ws.close(code=4003, reason="bad host key")
        return
    if room.host:  # newest screen wins
        old, room.host = room.host, None
        try:
            await old.close(code=4002, reason="another host connected")
        except Exception:
            pass
    room.host = ws
    await room.broadcast()
    if room.now:
        asyncio.create_task(fetch_like(room.now["videoId"]))
    asyncio.create_task(check_health())  # debounced: at most every HEALTH_DEBOUNCE
    try:
        while True:
            try:
                if json.loads(await ws.receive_text()).get("type") == "resync":
                    room.sync.forget(ws)
                    await room.broadcast()
            except (ValueError, AttributeError):
                continue
    except WebSocketDisconnect:
        pass
    finally:
        room.sync.forget(ws)
        if room.host is ws:
            room.host = None


# ------------------------------------------------------------------ background loops

async def heartbeat_loop():
    """Ping every socket every HB_INTERVAL; close the ones that stopped answering."""
    while True:
        await asyncio.sleep(HB_INTERVAL)
        ping, dead = room.people.heartbeat()
        for ws in dead:
            room.people.detach(ws)
            room.sync.forget(ws)
            asyncio.create_task(_close_ws(ws, 4008, "no heartbeat"))
        await room._send_all("hb", [(ws, {"type": "hb"}) for ws in ping])
        if dead:
            await on_presence_change()


async def reap_loop():
    """Drop devices that have been gone longer than the grace period."""
    while True:
        await asyncio.sleep(2)
        if room.people.reap():
            await on_presence_change()


async def health_loop():
    await asyncio.sleep(60)
    while True:
        await check_health(force=True)
        await asyncio.sleep(HEALTH_EVERY)
