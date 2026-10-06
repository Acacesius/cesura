"""Downloads: one at a time, only the current and next track, paced to DOWNLOAD_RATE."""
import asyncio
import http.server
import socketserver
import threading
import time

import pytest

from app import media
from app.netstats import net

SIZE = 512 * 1024


@pytest.fixture(scope="module")
def upstream():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            a, b = self.headers["Range"][6:].split("-")
            n = int(b) - int(a) + 1
            self.send_response(206)
            self.send_header("Content-Length", str(n))
            self.end_headers()
            self.wfile.write(b"\0" * n)

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def resolver(port, rate):
    r = media.Resolver()
    r.throttle = media.Throttle(rate)
    r.chunk = 64 * 1024

    async def get(vid, fresh=False):
        await asyncio.sleep(0.05)
        return media.Resolved(f"http://127.0.0.1:{port}/{vid}", {}, SIZE, "audio/mp4", time.time() + 3600, "140 mp4a 128", 30.0)
    r.get = get
    return r


def test_stale_prefetch_is_cancelled_and_current_goes_first(upstream):
    async def run():
        r = resolver(upstream, 2 * SIZE)  # each track takes ~0.5 s
        net.peak["audio"] = 0
        a = r.load("aaaaaaaaaaa", prefetch=True)
        await asyncio.sleep(0.1)
        b = r.load("bbbbbbbbbbb", prefetch=True)   # the queue moved on: a is no longer next
        await asyncio.sleep(0.1)
        cur = r.load("ccccccccccc")                # the room needs this one now
        m = await cur
        assert m.size == SIZE
        assert a.cancelled() and b.cancelled()
        assert list(r.media) == ["ccccccccccc"]
        assert net.peak["audio"] == 1
    asyncio.run(run())


def test_downloads_are_paced(upstream):
    async def run():
        r = resolver(upstream, SIZE)  # 1 track per second
        t0 = time.monotonic()
        await r.load("ddddddddddd")
        assert time.monotonic() - t0 > 0.8
    asyncio.run(run())


def test_prefetch_of_current_track_is_reused(upstream):
    async def run():
        r = resolver(upstream, 0)
        p = r.load("eeeeeeeeeee", prefetch=True)
        assert r.load("eeeeeeeeeee") is p  # the track that was next became current: same download
        await p
    asyncio.run(run())
