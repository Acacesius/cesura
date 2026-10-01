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


media.Resolver._download = _download
if os.environ.get("FAKE_NO_ART"):  # screenshots: no real album art, the UI's placeholder tiles instead
    media._thumb = lambda thumbs: None
media.Resolver.cookie_health = lambda self: "valid"
media.Resolver.stream_format = lambda self, vid: "WAV test tone · itag 0" if vid in self.media else None
media.probe_account = lambda: {"account": True, "name": "Test", "premium": True, "aac256": True, "problem": None, "video": "x"}
media.cookie_ytmusic = lambda path=None: FakeLibrary()
cookies.soonest_auth_expiry = lambda path: int(time.time() + 300 * 86400)

if __name__ == "__main__":
    from app import main
    if os.environ.get("FAKE_CODE"):  # screenshots: a fixed, obviously fake room code
        main.room.code = os.environ["FAKE_CODE"]
    uvicorn.run(main.app, host="0.0.0.0", port=8765, log_level="warning")
