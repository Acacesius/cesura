"""Run cesura with synthetic audio and a fake host library, for end-to-end tests
and screenshots without a linked YouTube account.

Search, albums, lyrics and radio still use the real ytmusicapi (signed out).
Each track's audio becomes a sine tone (pitch from its videoId), and the
"host's library" is a few made-up playlists built from real search results.
Not part of the image (.dockerignore drops tests/); mount it in:

    docker run --rm -p 127.0.0.1:18765:8765 -v "$PWD/tests:/app/tests:ro" -v "$CFG:/app/config" \\
        -e HOST_KEY=test -e FAKE_SECONDS=30 cesura:dev python -m tests.fakeserver

FAKE_NO_ART=1 drops album art (for screenshots); FAKE_CODE=XXXXX pins the room code.
"""
import asyncio
import io
import math
import os
import struct
import time
import wave
import zlib

import uvicorn

from app import cookies, media

SECONDS = float(os.environ.get("FAKE_SECONDS", "30"))
RATE = 22050


def tone(video_id: str) -> bytes:
    freq = 220 * 2 ** ((zlib.crc32(video_id.encode()) % 24) / 12)  # two octaves above A3
    n = int(SECONDS * RATE)
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / 2000, (n - i) / 2000)
        frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / RATE)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


async def _download(self, video_id: str):
    await asyncio.sleep(0.3)  # a little "fetch" time, so loading states show
    data = await asyncio.to_thread(tone, video_id)
    return media.Media(data, len(data), "audio/wav", SECONDS)


class FakeLibrary:
    """Stands in for ytmusicapi signed in as the host."""
    LISTS = [("PLfakeRoadTrip000001", "Road trip"), ("PLfakeLateNight00002", "Late night"), ("PLfakeSundayMorning3", "Sunday morning")]
    QUERIES = {"PLfakeRoadTrip000001": "driving songs", "PLfakeLateNight00002": "late night jazz", "PLfakeSundayMorning3": "acoustic morning", "LM": "classic soul"}
    exported: list = []

    def get_library_playlists(self, limit=25):
        return [{"playlistId": "LM", "title": "Liked Music"}] + [{"playlistId": i, "title": t, "count": 12, "thumbnails": None} for i, t in self.LISTS]

    def _tracks(self, pid):
        from app.main import ytm
        return ytm.search(self.QUERIES[pid], filter="songs", limit=12)

    def get_playlist(self, pid, limit=100):
        return {"title": dict(self.LISTS)[pid], "author": "Host", "trackCount": 12, "tracks": self._tracks(pid)}

    def get_liked_songs(self, limit=100):
        return {"title": "Liked Music", "trackCount": 12, "tracks": self._tracks("LM")}

    def create_playlist(self, title, description, privacy_status="PRIVATE", video_ids=None, source_playlist=None):
        self.exported.append((title, list(video_ids or [])))
        return "PLfakeExport" + str(len(self.exported)).zfill(8)


def fake_upstream(size: int) -> int:
    """FAKE_UPSTREAM=1 (tests/netbench.py): keep the real download path (prefetch, RAM cache, network
    accounting) and fake only YouTube: yt-dlp "resolves" in a second to a local server that serves
    `size` bytes per track (a tone, padded) with Range support. Returns the port."""
    import http.server
    import socketserver
    import threading

    import functools
    body_of = functools.lru_cache(8)(lambda vid: tone(vid).ljust(size, b"\0"))

    class Upstream(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, like googlevideo

        def do_GET(self):
            body = body_of(self.path.split("?")[0].strip("/"))
            start, end = 0, size - 1
            if (r := self.headers.get("Range", "")).startswith("bytes="):
                a, b = r[6:].split("-")
                start, end = int(a or 0), min(int(b or size - 1), size - 1)
            self.send_response(206 if r else 200)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self.wfile.write(body[start:end + 1])

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def extract(video_id):
        time.sleep(1.0)  # yt-dlp takes a few seconds per lookup in real life
        return {"url": f"http://127.0.0.1:{port}/{video_id}?clen={size}&expire={int(time.time()) + 3600}&dur={SECONDS}",
                "ext": "wav", "format_id": "0", "acodec": "pcm", "abr": 352}

    media._extract = extract
    return port


if os.environ.get("FAKE_UPSTREAM"):
    fake_upstream(int(os.environ.get("FAKE_BYTES", str(7 * 1024 * 1024))))
else:
    media.Resolver._download = _download
if os.environ.get("FAKE_NO_ART"):  # screenshots: no real album art, the UI's placeholder tiles instead
    media._thumb = lambda thumbs: None
media.Resolver.cookie_health = lambda self: "valid"
media.Resolver.stream_format = lambda self, vid: "WAV test tone · itag 0" if vid in self.media else None
media.probe_account = lambda: {"account": True, "name": "Test", "premium": True, "aac256": True, "problem": None, "video": "x"}
media.cookie_ytmusic = lambda path=None: FakeLibrary()
cookies.soonest_auth_expiry = lambda path: int(time.time() + 300 * 86400)

if __name__ == "__main__":
    from app import setup as setupstate
    if not setupstate.is_complete():  # skip the wizard: a test instance, keyed by HOST_KEY
        setupstate.save_state({"complete": True, "public_url": os.environ.get("PUBLIC_URL") or "https://cesura.example.com"})
    from app import main
    if os.environ.get("FAKE_CODE"):  # screenshots: fixed, made-up room codes (also after "New code")
        codes = iter([os.environ["FAKE_CODE"], "RVKMN", "HPLWD", "MGTBX"] * 100)
        main.new_code = lambda: next(codes)
        main.room.code = main.new_code()
    uvicorn.run(main.app, host="0.0.0.0", port=8765, log_level="warning")
