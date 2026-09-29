"""cesura: listen-along. Everyone hears the same track at the same moment on their own device.

The server owns the clock. For the current track it stores `started_at` (the
server time at which position 0 played) while running, or a frozen position
while stopped (paused, nobody listening, or the track still loading). Every
state change is pushed as a full snapshot over websockets; each client measures
its offset to the server clock and steers its <audio> to server_now - started_at.
The server also ends tracks itself on a timer, so no client is special.
"""
import asyncio
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

import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cookies as cookiejar
from . import setup as setupstate
from .media import COOKIES, PROBE_VIDEO_ID, Resolver, _thumb, make_ytmusic, probe_account, song_to_track, to_track

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


HOST_KEY = load_host_key()  # updated by the setup wizard
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
MAX_QUEUE = 100
ADD_LIMIT, ADD_WINDOW = 10, 60.0  # adds per client IP per window (seconds)
FAIL_LIMIT, FAIL_WINDOW = 10, 600.0  # wrong codes/keys per client IP per window
AUTO_TARGET = 10
LEAD = 1.0       # head start (s) whenever the clock (re)starts, so every listener can buffer
MAX_FAILS = 3    # consecutive unplayable tracks before giving up
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")

ytm = make_ytmusic()
resolver = Resolver()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))


def _quiet(task: asyncio.Task):
    """Prefetch failures are retried at play time; don't log them as unhandled."""
    task.add_done_callback(lambda t: t.cancelled() or t.exception())


class Room:
    def __init__(self):
        self.code = new_code()
        self.queue: list[dict] = []   # guest-added, plays first
        self.auto: list[dict] = []    # radio suggestions, plays when queue is empty
        self.now: dict | None = None
        self.started_at: float | None = None  # set while the clock runs
        self.frozen = 0.0                     # position while the clock is stopped
        self.loading = False                  # current track not yet in RAM
        self.user_paused = False
        self.host: WebSocket | None = None    # optional QR/admin screen, plays nothing
        self.guests: dict[WebSocket, str] = {}  # socket -> client id
        self.listening: set[WebSocket] = set()  # guests who tapped Listen
        self.names: dict[WebSocket, str] = {}     # from the guest's hello
        self.devices: dict[WebSocket, str] = {}   # parsed from User-Agent
        self.drift: dict[WebSocket, tuple[float, bool, float]] = {}  # ms, catching up, reported at
        self.radio = True
        self.session_start = time.time()
        self.songs_played = 0
        self.peak_listeners = 0
        self.votes: set[str] = set()
        self.history: deque[str] = deque(maxlen=200)   # videoIds, keeps radio from repeating
        self.played: deque[dict] = deque(maxlen=30)    # for the Played tab
        self.add_log: dict[str, deque] = {}
        self.lock = asyncio.Lock()
        self.auto_lock = asyncio.Lock()
        self.timer: asyncio.Task | None = None
        self.fails = 0

    # ---- derived values
    def listeners(self) -> int:
        return len(set(self.guests.values()))

    def n_listening(self) -> int:
        return len({self.guests[w] for w in self.listening if w in self.guests})

    def votes_needed(self) -> int:
        return max(1, math.ceil(self.listeners() / 2))

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
    def public(it: dict | None, cid: str | None = None) -> dict | None:
        """Strip the adder's client id; tell each guest which tracks are theirs."""
        if it is None:
            return None
        d = {k: v for k, v in it.items() if k != "owner"}
        d["mine"] = bool(cid) and it.get("owner") == cid
        return d

    def listener_rows(self) -> list[dict]:
        now, rows = time.time(), []
        for ws in list(self.guests):
            ms, catching_up, at = self.drift.get(ws, (None, False, 0.0))
            fresh = ms is not None and now - at < 10
            listening = ws in self.listening
            status = "idle" if not listening else "catching_up" if fresh and (catching_up or abs(ms) > 60) else "in_phase" if fresh else "waiting"
            rows.append({"name": self.names.get(ws) or "Guest", "device": self.devices.get(ws, ""), "listening": listening,
                         "drift_ms": round(ms) if fresh else None, "status": status})
        return rows

    def host_panel(self) -> dict:
        rows = self.listener_rows()
        drifts = sorted(abs(r["drift_ms"]) for r in rows if r["drift_ms"] is not None and r["listening"])
        nxt = self.queue[0] if self.queue else self.auto[0] if self.auto else None
        cutoff = time.monotonic() - FAIL_WINDOW
        return {
            "listeners": rows,
            "stats": {"session_start": self.session_start, "songs_played": self.songs_played,
                      "peak_listeners": self.peak_listeners,
                      "median_drift_ms": drifts[len(drifts) // 2] if drifts else None},
            "engine": {"stream": resolver.stream_format(self.now["videoId"]) if self.now else None,
                       "next_qid": nxt["qid"] if nxt else None,
                       "next": resolver.prefetch_state(nxt["videoId"]) if nxt else "idle",
                       "cookies": resolver.cookie_health(),
                       "wrong_codes": sum(1 for q in fail_log.values() for t in q if t > cutoff),
                       "wrong_code_limit": FAIL_LIMIT},
        }

    def snapshot(self, for_host=False, cid: str | None = None) -> dict:
        s = {
            "type": "state",
            "now": self.public(self.now, cid),
            "status": self.status(),
            "started_at": self.started_at,
            "position": round(self.position(), 3),
            "server_time": time.time(),
            "queue": [self.public(t, cid) for t in self.queue],
            "auto": self.auto,
            "played": list(reversed(self.played)),
            "votes": len(self.votes),
            "votes_needed": self.votes_needed(),
            "listeners": self.listeners(),
            "listening": self.n_listening(),
            "radio": self.radio,
            "people": sorted({self.names.get(w) or "Guest" for w in self.guests}, key=str.lower),
            "notice": NOTICES.get(resolver.cookie_health()),
        }
        if for_host:
            s["code"] = self.code
            s["public_url"] = public_url()
            s["host"] = self.host_panel()
        return s

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

    async def _end_after(self, qid: str, secs: float):
        await asyncio.sleep(max(0.0, secs) + 0.25)
        async with self.lock:
            if not self.now or self.now["qid"] != qid or self.started_at is None:
                return
            await self.advance("ended")
        await self.broadcast()

    # ---- broadcasting
    async def _send(self, ws: WebSocket, msg: dict):
        try:
            await ws.send_json(msg)
        except Exception:
            pass  # disconnect handler cleans up

    async def push_host(self):
        """Refresh only the host dashboard (drift updates), at most once a second."""
        now = time.monotonic()
        if self.host and now - getattr(self, "_host_pushed", 0) >= 1:
            self._host_pushed = now
            await self._send(self.host, self.snapshot(for_host=True))

    async def broadcast(self):
        targets = [(ws, self.snapshot(cid=cid)) for ws, cid in list(self.guests.items())]
        if self.host:
            targets.append((self.host, self.snapshot(for_host=True)))
        await asyncio.gather(*(self._send(ws, m) for ws, m in targets))

    # ---- queue mechanics
    def item(self, track: dict, auto=False, by: str | None = None, owner: str | None = None) -> dict:
        return {**track, "qid": uuid.uuid4().hex[:10], "auto": auto, "by": by, "owner": owner}

    def prefetch_next(self):
        nxt = self.queue[0] if self.queue else self.auto[0] if self.auto else None
        if nxt:
            _quiet(resolver.load(nxt["videoId"]))

    async def advance(self, reason: str):
        """Move to the next track. Caller holds self.lock."""
        prev = self.now
        if prev:
            self.history.append(prev["videoId"])
            self.played.append(self.public(prev))
        if self.queue:
            self.now = self.queue.pop(0)
        elif self.auto:
            self.now = self.auto.pop(0)
        elif prev and self.radio:
            await self.refill_auto(prev)
            self.now = self.auto.pop(0) if self.auto else None
        else:
            self.now = None
        self.started_at, self.frozen = None, 0.0
        self.loading = self.now is not None
        self.votes.clear()
        log.info("advance (%s): %s", reason, self.now and f"{self.now['artists']} - {self.now['title']}")
        if self.now:
            self.songs_played += 1
            asyncio.create_task(self._prepare(self.now))
            # Queue ran dry: seed radio from the track that's now playing. If that
            # track is itself a radio pick, keep the existing radio list going.
            if self.radio and not self.queue and (not self.now["auto"] or len(self.auto) < 3):
                if not self.now["auto"]:
                    self.auto.clear()
                asyncio.create_task(self.refill_auto_and_broadcast(self.now))
        self.sync_clock()

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
                self.sync_clock()
                self.prefetch_next()
        await self.broadcast()

    async def relinked(self):
        """New cookies were saved: start the current song if it was waiting on them."""
        resolver.bot_check = False
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

    async def rotate(self):
        """New session: fresh code, empty room, kick guests holding the old code."""
        async with self.lock:
            self.code = new_code()
            self.queue.clear()
            self.auto.clear()
            self.now = None
            self.started_at, self.frozen = None, 0.0
            self.loading = self.user_paused = False
            self.votes.clear()
            self.history.clear()
            self.played.clear()
            self.session_start, self.songs_played, self.peak_listeners = time.time(), 0, 0
            old = list(self.guests)
            self.guests.clear()
            self.listening.clear()
            self.sync_clock()
        for ws in old:
            try:
                await ws.close(code=4001, reason="room code changed")
            except Exception:
                pass
        log.info("new session, code %s", self.code)
        await self.broadcast()


# Shown to guests and on the host dashboard when YouTube auth needs attention.
NOTICES = {
    "missing": "Playback is off until the host links a YouTube Music account.",
    "bot-check": "YouTube is asking the host to sign in again. Playback may stop until they relink.",
}
room = Room()
fail_log: dict[str, deque] = {}
search_cache: dict[str, dict] = {}  # videoId -> track, from searches/albums/playlists/radio
log.info("room code %s", room.code)
_token = setupstate.setup_token()
if _token:
    bar = "=" * 72
    print(f"\n{bar}\n  cesura is not set up yet.\n\n  Open  /setup  in your browser and enter this one-time setup token:\n\n"
          f"      {_token}\n\n  Only someone with this token can configure this server. It stops\n"
          f"  working as soon as setup is finished.\n{bar}\n", flush=True)
elif resolver.cookie_health() == "missing":
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


def require_code(conn: Request, code: str | None):
    status = check_secret((code or "").upper(), room.code, client_ip(conn))
    if status:
        raise HTTPException(status, "too many wrong codes, try again later" if status == 429 else "bad room code")


def require_key(conn: Request, key: str | None):
    status = check_secret(key, HOST_KEY, client_ip(conn))
    if status:
        raise HTTPException(status, "too many attempts" if status == 429 else "bad host key")


def rate_limited(ip: str) -> bool:
    now = time.monotonic()
    q = room.add_log.setdefault(ip, deque())
    while q and now - q[0] > ADD_WINDOW:
        q.popleft()
    if len(q) >= ADD_LIMIT:
        return True
    q.append(now)
    return False


# ------------------------------------------------------------------ pages

@app.get("/")
async def guest_page():
    return FileResponse(os.path.join(STATIC, "guest.html"))


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

class CodeBody(BaseModel):
    code: str


class AddBody(CodeBody):
    videoIds: list[str]   # one track, or a whole album/playlist ("add all")
    cid: str = ""
    name: str = ""
    next: bool = False    # "play next" instead of the end of the queue


class SkipBody(CodeBody):
    qid: str
    cid: str


class RemoveBody(CodeBody):
    qid: str
    cid: str


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_BULK = 50  # tracks per "add all"


def remember(tracks: list[dict]):
    for t in tracks:
        search_cache[t["videoId"]] = t
    while len(search_cache) > 3000:
        search_cache.pop(next(iter(search_cache)))


def artist_names(item: dict) -> str:
    return ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name"))


@app.post("/api/join")
async def join(body: CodeBody, request: Request):
    require_code(request, body.code)
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


@app.get("/api/collection")
async def collection(kind: str, id: str, code: str, request: Request):
    """An album or playlist with its tracks, for browsing and "add all"."""
    require_code(request, code)
    if kind not in ("album", "playlist") or not re.fullmatch(r"[A-Za-z0-9_-]{2,80}", id):
        raise HTTPException(400)
    try:
        if kind == "album":
            a = await asyncio.to_thread(ytm.get_album, id)
            title, thumb = a.get("title") or "Album", _thumb(a.get("thumbnails"))
            subtitle = " · ".join(str(x) for x in [artist_names(a), a.get("year")] if x)
            tracks = [to_track(t, album=title, thumb=thumb) for t in a.get("tracks", [])]
        else:
            p = await asyncio.to_thread(ytm.get_playlist, id, limit=200)
            title, thumb = p.get("title") or "Playlist", _thumb(p.get("thumbnails"))
            author = p.get("author")
            author = author.get("name") if isinstance(author, dict) else author
            subtitle = " · ".join(str(x) for x in [author, f"{p['trackCount']} songs" if p.get("trackCount") else None] if x)
            tracks = [to_track(t, thumb=thumb) for t in p.get("tracks", [])]
    except Exception as e:
        log.warning("%s %s failed: %s", kind, id, e)
        raise HTTPException(502, f"could not load {kind}")
    tracks = [t for t in tracks if t]
    remember(tracks)
    thumb = thumb or next((t["thumb"] for t in tracks if t["thumb"]), None)
    return {"kind": kind, "id": id, "title": title, "subtitle": subtitle, "thumb": thumb, "tracks": tracks}


@app.post("/api/add")
async def add(body: AddBody, request: Request):
    require_code(request, body.code)
    ids = [v for v in body.videoIds if VIDEO_ID.fullmatch(v)][:MAX_BULK]
    if not ids:
        raise HTTPException(400, "bad videoId")
    if rate_limited(client_ip(request)):
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
    by = re.sub(r"\s+", " ", body.name).strip()[:24] or None
    async with room.lock:
        items = [room.item(t, by=by, owner=body.cid[:64] or None) for t in tracks]
        if body.next:
            room.queue[0:0] = items
        else:
            room.queue.extend(items)
        added = {t["videoId"] for t in tracks}
        room.auto = [a for a in room.auto if a["videoId"] not in added]  # promoted from Autoplay
        if room.now is None:
            await room.advance("first add")
        else:
            room.prefetch_next()
    await room.broadcast()
    return {"ok": True, "added": len(items)}


@app.post("/api/remove")
async def remove(body: RemoveBody, request: Request):
    """Guests can take back tracks they added."""
    require_code(request, body.code)
    async with room.lock:
        t = next((t for t in room.queue if t["qid"] == body.qid), None)
        if not t:
            return {"ok": False}
        if t.get("owner") != body.cid[:64]:
            raise HTTPException(409, "not your track")
        room.queue.remove(t)
        room.prefetch_next()
    await room.broadcast()
    return {"ok": True}


@app.post("/api/skip")
async def vote_skip(body: SkipBody, request: Request):
    require_code(request, body.code)
    async with room.lock:
        if not room.now or room.now["qid"] != body.qid:
            return {"ok": False, "stale": True}
        room.votes.add(body.cid[:64])
        if len(room.votes) >= room.votes_needed():
            await room.advance("vote skip")
    await room.broadcast()
    return {"ok": True}


class PauseBody(CodeBody):
    paused: bool


@app.post("/api/pause")
async def pause(body: PauseBody, request: Request):
    """Anyone in the room can pause or resume for everyone."""
    require_code(request, body.code)
    async with room.lock:
        if room.user_paused != body.paused:
            room.user_paused = body.paused
            log.info("%s by %s", "paused" if body.paused else "resumed", client_ip(request))
            room.sync_clock()
    await room.broadcast()
    return {"ok": True}


# ------------------------------------------------------------------ host API

@app.get("/api/qr.svg")
async def qr(key: str, url: str, request: Request):
    require_key(request, key)
    if not re.match(r"https?://", url) or len(url) > 300:
        raise HTTPException(400)
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=10, border=2, dark="#000", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.post("/api/host/remove")
async def host_remove(key: str, qid: str, request: Request):
    """The host screen can remove anything from the queue or Autoplay."""
    require_key(request, key)
    async with room.lock:
        room.queue = [t for t in room.queue if t["qid"] != qid]
        room.auto = [t for t in room.auto if t["qid"] != qid]
        room.prefetch_next()
    await room.broadcast()
    return {"ok": True}


@app.post("/api/host/move")
async def host_move(key: str, qid: str, index: int, request: Request):
    """Reorder the queue (drag handle on the host dashboard)."""
    require_key(request, key)
    async with room.lock:
        t = next((t for t in room.queue if t["qid"] == qid), None)
        if t:
            room.queue.remove(t)
            room.queue.insert(max(0, min(index, len(room.queue))), t)
            room.prefetch_next()
    await room.broadcast()
    return {"ok": bool(t)}


@app.post("/api/host/radio")
async def host_radio(key: str, on: bool, request: Request):
    require_key(request, key)
    async with room.lock:
        room.radio = on
        if not on:
            room.auto.clear()
        elif room.now and not room.queue:
            asyncio.create_task(room.refill_auto_and_broadcast(room.now))
    await room.broadcast()
    return {"ok": True}


@app.post("/api/host/skip")
async def host_skip(key: str, request: Request):
    """The host skips immediately; guests vote."""
    require_key(request, key)
    async with room.lock:
        if room.now:
            await room.advance("host skip")
    await room.broadcast()
    return {"ok": True}


@app.post("/api/new-session")
async def new_session(key: str, request: Request):
    require_key(request, key)
    await room.rotate()
    return {"ok": True, "code": room.code}


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
    resolver.bot_check = res["problem"] == "bot-check"
    await room.broadcast()
    return res


# ------------------------------------------------------------------ audio

@app.get("/stream/{video_id}")
async def stream(video_id: str, request: Request, code: str):
    """Serve the current track with HTTP Range support, from RAM when possible.

    Proxying is required: googlevideo URLs are signed for the server's IP and
    expire, so listeners can't fetch them directly.
    """
    require_code(request, code)
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


# ------------------------------------------------------------------ websockets

@app.websocket("/ws")
async def guest_ws(ws: WebSocket, code: str = "", cid: str = ""):
    """Guests: receive state; send {"type":"ping"} for clock sync and {"type":"listen"}."""
    await ws.accept()
    status = check_secret(code.upper(), room.code, client_ip(ws))
    if status:
        await ws.close(code=4029 if status == 429 else 4001, reason="bad room code")
        return
    room.guests[ws] = cid[:64] or uuid.uuid4().hex
    room.devices[ws] = device_name(ws.headers.get("user-agent", ""))
    room.peak_listeners = max(room.peak_listeners, room.listeners())
    await room.broadcast()  # listener count changed
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "hello":
                room.names[ws] = re.sub(r"\s+", " ", str(msg.get("name") or "")).strip()[:24]
                await room.broadcast()
            elif kind == "drift":
                # Measured by the client (audio.currentTime vs the server clock); display only.
                try:
                    room.drift[ws] = (float(msg.get("ms")), bool(msg.get("catching_up")), time.time())
                except (TypeError, ValueError):
                    continue
                await room.push_host()
            elif kind == "ping":
                # Client computes its clock offset from this: server - (t_sent + rtt/2).
                await ws.send_json({"type": "pong", "t": msg.get("t"), "server": time.time()})
            elif kind == "listen":
                async with room.lock:
                    if msg.get("on"):
                        room.listening.add(ws)
                    else:
                        room.listening.discard(ws)
                    room.sync_clock()
                await room.broadcast()
    except WebSocketDisconnect:
        pass
    finally:
        room.guests.pop(ws, None)
        room.listening.discard(ws)
        for d in (room.names, room.devices, room.drift):
            d.pop(ws, None)
        async with room.lock:
            room.sync_clock()
        await room.broadcast()


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
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if room.host is ws:
            room.host = None
