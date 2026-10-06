"""Network accounting: every message pushed to clients and every fetch from YouTube.

Each is logged (logger "cesura.net") and counted, so a ping spike can be lined
up with what the server was doing. The host dashboard can read the totals from
/api/host/net.

- push(kind, text, n): a message sent to n sockets. Bytes are counted raw and
  as permessage-deflate would send them (the socket compresses).
- fetch(kind, label): an outbound request (ytmusicapi call, yt-dlp lookup,
  audio download, artwork). Tracks how many run at once and the inbound
  bandwidth per second.
"""
import logging
import time
import zlib
from collections import defaultdict, deque

log = logging.getLogger("cesura.net")
QUIET = {"pong", "hb", "beat"}  # small and frequent: counted, logged at DEBUG only


def deflated(text: str) -> int:
    c = zlib.compressobj(6, zlib.DEFLATED, -15)
    return len(c.compress(text.encode()) + c.flush(zlib.Z_SYNC_FLUSH)) - 4


class Fetch:
    def __init__(self, stats: "NetStats", kind: str, label: str):
        self.stats, self.kind, self.label = stats, kind, label
        self.bytes = 0
        self.t0 = time.monotonic()

    def got(self, n: int):
        self.bytes += n
        self.stats._inbound(n)

    def __enter__(self):
        s = self.stats
        s.running[self.kind] += 1
        s.peak[self.kind] = max(s.peak[self.kind], s.running[self.kind])
        return self

    def __exit__(self, exc, *_):
        s = self.stats
        s.running[self.kind] -= 1
        secs = time.monotonic() - self.t0
        f = s.fetches[self.kind]
        f["n"] += 1
        f["bytes"] += self.bytes
        f["secs"] += secs
        if exc:
            f["failed"] += 1
        rate = f", {self.bytes * 8 / secs / 1e6:.1f} Mbit/s" if self.bytes and secs > 0.05 else ""
        log.info("fetch %s %s: %s in %.2fs%s (%d running)%s", self.kind, self.label, human(self.bytes), secs, rate,
                 s.running[self.kind], " FAILED" if exc else "")
        return False


class NetStats:
    def __init__(self):
        self.started = time.time()
        self.pushes: dict[str, dict] = defaultdict(lambda: {"msgs": 0, "sockets": 0, "bytes": 0, "wire": 0, "max": 0})
        self.fetches: dict[str, dict] = defaultdict(lambda: {"n": 0, "bytes": 0, "secs": 0.0, "failed": 0})
        self.running: dict[str, int] = defaultdict(int)
        self.peak: dict[str, int] = defaultdict(int)
        self.per_sec: deque[list] = deque(maxlen=3600)  # [second, inbound bytes]

    def push(self, kind: str, text: str, sockets: int):
        if not sockets:
            return
        n = len(text.encode())
        wire = deflated(text) if n > 200 else n
        p = self.pushes[kind]
        p["msgs"] += 1
        p["sockets"] += sockets
        p["bytes"] += n * sockets
        p["wire"] += wire * sockets
        p["max"] = max(p["max"], n)
        (log.debug if kind in QUIET else log.info)("push %s: %s (%s deflated) to %d socket%s", kind, human(n), human(wire),
                                                   sockets, "" if sockets == 1 else "s")

    def push_batch(self, kind: str, texts: list[str]):
        """One broadcast: per-socket messages (they may differ per client), logged as one line."""
        if not texts:
            return
        sizes = {}
        for t in texts:  # identical texts compress identically: measure each distinct one once
            if t not in sizes:
                n = len(t.encode())
                sizes[t] = (n, deflated(t) if n > 200 else n)
        p = self.pushes[kind]
        raw = sum(sizes[t][0] for t in texts)
        wire = sum(sizes[t][1] for t in texts)
        p["msgs"] += 1
        p["sockets"] += len(texts)
        p["bytes"] += raw
        p["wire"] += wire
        p["max"] = max(p["max"], max(n for n, _ in sizes.values()))
        (log.debug if kind in QUIET else log.info)("push %s: %s (%s deflated) to %d socket%s, %s total", kind,
                                                   human(raw // len(texts)), human(wire // len(texts)), len(texts),
                                                   "" if len(texts) == 1 else "s", human(raw))

    def fetch(self, kind: str, label: str = "") -> Fetch:
        return Fetch(self, kind, label)

    def ytmusic_hook(self, resp, *_, **__):
        """requests response hook for ytmusicapi sessions: one line per YouTube Music API call."""
        n = len(resp.content or b"")
        endpoint = resp.url.split("?")[0].rsplit("/", 1)[-1]
        f = self.fetches["ytmusic"]
        f["n"] += 1
        f["bytes"] += n
        f["secs"] += resp.elapsed.total_seconds()
        self._inbound(n)
        log.info("fetch ytmusic %s: %s in %.2fs", endpoint, human(n), resp.elapsed.total_seconds())

    def instrument(self, yt):
        """Count every HTTP call a YTMusic client makes."""
        session = getattr(yt, "_session", None)
        if session is not None:
            session.hooks.setdefault("response", []).append(self.ytmusic_hook)
        return yt

    def _inbound(self, n: int):
        sec = int(time.time())
        if self.per_sec and self.per_sec[-1][0] == sec:
            self.per_sec[-1][1] += n
        else:
            self.per_sec.append([sec, n])

    def snapshot(self) -> dict:
        peak = max(self.per_sec, key=lambda x: x[1], default=[None, 0])
        return {
            "since": self.started,
            "pushes": dict(self.pushes),
            "fetches": dict(self.fetches),
            "running": dict(self.running),
            "peak_concurrency": dict(self.peak),
            "peak_inbound_mbit": round(peak[1] * 8 / 1e6, 1),
            "peak_inbound_at": peak[0],
        }


def human(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1048576 else f"{n / 1048576:.1f} MB"


net = NetStats()
