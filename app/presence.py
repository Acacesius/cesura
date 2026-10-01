"""Who's in the room, keyed by device rather than by socket.

Each browser makes up a random device ID on first join and keeps it in
localStorage. Every socket it opens (a reconnect, a second tab) attaches to
the same Device, so the host sees one row per phone with a ×N badge rather
than a row per socket.

On first attach the server issues the device a random session token. The
token is what authorises everything after joining: audio (/stream, /pocket),
queueing, pausing, voting, and co-host powers. Kicking revokes it. A socket
that claims an existing device ID without its token is refused, so knowing
someone's device ID isn't enough to act as them.

Liveness: the server pings each socket every HB_INTERVAL seconds and closes a
socket that leaves HB_MISSES pings unanswered. A device with no live sockets
and no open pocket stream is dropped after GRACE seconds, so a brief reconnect
doesn't make it flicker off the list. An open pocket stream counts as live,
because iOS freezes a locked page's JS (and with it the socket) while the
stream keeps playing.

Bans are by device ID for the life of the server process. Clearing site data
gets a new ID, so a ban only stops casual rejoining.
"""
import re
import secrets
import time

HB_INTERVAL = 10.0
HB_MISSES = 3
GRACE = 30.0
DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


class Device:
    def __init__(self, did: str, now: float):
        self.id = did
        self.pid = secrets.token_hex(5)  # what other clients see; the device ID itself never leaves the server
        self.token = secrets.token_urlsafe(18)
        self.name = ""
        self.ua = ""
        self.sockets: set = set()
        self.role = "guest"           # or "cohost"
        self.listening = False        # tapped Listen (audio unlocked)
        self.pocket = False           # pocket mode on
        self.streams = 0              # open /pocket responses
        self.drift: tuple[float, bool, float] | None = None  # ms, catching up, reported at (wall clock)
        self.gone_at: float | None = None
        self.joined = now

    @property
    def alive(self) -> bool:
        return bool(self.sockets) or self.streams > 0

    @property
    def cohost(self) -> bool:
        return self.role == "cohost"

    @property
    def in_pocket(self) -> bool:
        return self.pocket or self.streams > 0


class Presence:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.devices: dict[str, Device] = {}
        self.by_ws: dict = {}            # socket -> device id
        self.missed: dict = {}           # socket -> pings sent without a pong
        self.tokens: dict[str, str] = {}  # token -> device id
        self.banned: set[str] = set()
        self.known: set[str] = set()     # devices admitted this session; may reconnect while locked
        self.locked = False

    # ---- joining
    def refusal(self, did: str) -> str | None:
        """Why this device can't join right now, or None."""
        if did in self.banned:
            return "banned"
        if self.locked and did not in self.devices and did not in self.known:
            return "locked"
        return None

    def attach(self, ws, did: str, token: str = "") -> tuple[Device | None, str]:
        """Add a socket to its device, creating the device on first sight. -> (device, "ok" | refusal reason)."""
        if why := self.refusal(did):
            return None, why
        d = self.devices.get(did)
        if d is not None and not secrets.compare_digest(token.encode(), d.token.encode()):
            return None, "mismatch"
        if d is None:
            d = self.devices[did] = Device(did, self.clock())
            self.tokens[d.token] = did
            self.known.add(did)
        d.sockets.add(ws)
        d.gone_at = None
        self.by_ws[ws] = did
        self.missed[ws] = 0
        return d, "ok"

    def detach(self, ws) -> Device | None:
        did = self.by_ws.pop(ws, None)
        self.missed.pop(ws, None)
        d = self.devices.get(did) if did else None
        if d is not None:
            d.sockets.discard(ws)
            self._maybe_gone(d)
        return d

    def device(self, ws) -> Device | None:
        did = self.by_ws.get(ws)
        return self.devices.get(did) if did else None

    def by_pid(self, pid: str | None) -> Device | None:
        return next((d for d in self.devices.values() if d.pid == pid), None)

    def by_token(self, token: str | None) -> Device | None:
        did = self.tokens.get(token or "")
        return self.devices.get(did) if did else None

    # ---- pocket streams count as presence
    def stream_opened(self, d: Device):
        d.streams += 1
        d.gone_at = None

    def stream_closed(self, d: Device):
        d.streams = max(0, d.streams - 1)
        self._maybe_gone(d)

    def _maybe_gone(self, d: Device):
        if not d.alive and d.gone_at is None:
            d.gone_at = self.clock()

    # ---- liveness
    def heartbeat(self) -> tuple[list, list]:
        """Call every HB_INTERVAL. -> (sockets to ping now, sockets to close as dead)."""
        ping, dead = [], []
        for ws, n in list(self.missed.items()):
            if n >= HB_MISSES:
                dead.append(ws)
            else:
                self.missed[ws] = n + 1
                ping.append(ws)
        return ping, dead

    def pong(self, ws):
        if ws in self.missed:
            self.missed[ws] = 0

    def reap(self) -> list[Device]:
        """Drop devices that have had nothing live for GRACE seconds."""
        now, gone = self.clock(), []
        for d in list(self.devices.values()):
            if not d.alive and d.gone_at is not None and now - d.gone_at >= GRACE:
                self._forget(d)
                gone.append(d)
        return gone

    # ---- moderation
    def kick(self, did: str) -> Device | None:
        """Remove the device and revoke its token; the caller closes d.sockets. It may rejoin (unless locked)."""
        d = self.devices.get(did)
        if d is None:
            return None
        self._forget(d)
        self.known.discard(did)
        return d

    def ban(self, did: str) -> Device | None:
        self.banned.add(did)
        return self.kick(did)

    def _forget(self, d: Device):
        self.devices.pop(d.id, None)
        self.tokens.pop(d.token, None)
        for ws in d.sockets:
            self.by_ws.pop(ws, None)
            self.missed.pop(ws, None)

    def reset(self):
        """New session: everyone out. Bans survive (they last for the server's life)."""
        self.devices.clear()
        self.by_ws.clear()
        self.missed.clear()
        self.tokens.clear()
        self.known.clear()
        self.locked = False

    # ---- views
    def listening(self) -> list[Device]:
        return [d for d in self.devices.values() if d.listening or d.streams]

    def voters(self) -> list[Device]:
        """Active non-pocket listeners: the base for the vote-skip threshold."""
        return [d for d in self.devices.values() if d.listening and not d.in_pocket]

    def sockets(self) -> list[tuple]:
        return [(ws, d) for d in self.devices.values() for ws in d.sockets]
