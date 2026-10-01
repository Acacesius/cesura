"""Pocket station end to end with real ffmpeg and generated tones (no YouTube)."""
import array
import asyncio
import math
import subprocess

import pytest

from app import pocket
from app.pocket import Ring, Station, split_frames


@pytest.fixture(scope="module")
def tones(tmp_path_factory):
    d = tmp_path_factory.mktemp("tones")
    out = {}
    for name, hz in (("a", 440), ("b", 880)):
        p = d / f"{name}.m4a"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration=20",
                        "-c:a", "aac", str(p)], check=True)
        out[name] = str(p)
    return out


def decode(data: bytes, fmt="aac") -> array.array:
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "aac" if fmt == "aac" else "mp3", "-i", "pipe:0",
                          "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1"], input=data, capture_output=True, check=True).stdout
    return array.array("h", pcm)


def rms(a) -> float:
    return math.sqrt(sum(x * x for x in a) / max(1, len(a)))


def dominant_hz(a, rate=8000) -> float:
    zc = sum(1 for i in range(1, len(a)) if (a[i - 1] < 0) != (a[i] < 0))
    return zc / 2 / (len(a) / rate)


class Room:
    """A fake room clock."""
    def __init__(self, src):
        self.key, self.src, self.started, self.paused_at = "t1", src, None, None

    def play(self, key, src, at=0.0):
        import time
        self.key, self.src, self.started, self.paused_at = key, src, time.monotonic() - at, None

    def pause(self):
        import time
        self.paused_at = time.monotonic() - self.started

    def __call__(self):
        import time
        if self.started is None:
            return None
        if self.paused_at is not None:
            return (self.key, self.src, None)
        return (self.key, self.src, time.monotonic() - self.started)


async def collect(st: Station, secs: float) -> bytes:
    out = bytearray()
    gen = st.listen()

    async def run():
        async for chunk in gen:
            out.extend(chunk)
    task = asyncio.create_task(run())
    await asyncio.sleep(secs)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await gen.aclose()
    return bytes(out)


def test_frame_splitter_handles_partial_frames_and_junk(tones):
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", tones["a"], "-t", "1", "-c:a", "aac", "-f", "adts", "pipe:1"],
                         capture_output=True, check=True).stdout
    buf = bytearray(b"junk" + raw[:1000])
    frames = split_frames(buf, "aac")
    buf += raw[1000:]
    frames += split_frames(buf, "aac")
    assert b"".join(frames) == raw and not buf


def test_ring_skips_slow_reader_forward():
    async def go():
        r = Ring(4)
        r.push([b"1", b"2", b"3", b"4", b"5", b"6"])
        data, nxt = await r.read(0)
        assert data == b"3456" and nxt == 6
    asyncio.run(go())


def test_stream_follows_clock_track_change_pause_and_stops_when_idle(tones):
    async def go():
        room = Room(tones["a"])
        room.play("t1", tones["a"], at=5.0)
        st = Station(room, idle_stop=0.5)
        # Two listeners share one encoder.
        first, second = await asyncio.gather(collect(st, 3.0), collect(st, 3.0))
        assert st.decoder_starts == 1, st.decoder_starts
        for data in (first, second):
            pcm = decode(data)
            secs = len(pcm) / 8000
            assert 3.0 <= secs <= 3.0 + pocket.AHEAD + 0.6   # real time, plus the join burst
            assert abs(dominant_hz(pcm[4000:]) - 440) < 25
        # Track change: the stream switches source without ending.
        room.play("t2", tones["b"], at=2.0)
        pcm = decode(await collect(st, 3.0))
        assert abs(dominant_hz(pcm[-8000:]) - 880) < 25
        # Paused: silence, but bytes keep flowing.
        room.pause()
        await asyncio.sleep(pocket.AHEAD + 0.3)
        pcm = decode(await collect(st, 2.0))
        assert len(pcm) > 8000 and rms(pcm[-6000:]) < 50
        # Last listener left: the encoder stops after idle_stop.
        await asyncio.sleep(0.8)
        assert not st.running
        await st.aclose()
    asyncio.run(go())


def test_mp3_fallback(tones):
    async def go():
        room = Room(tones["a"])
        room.play("t1", tones["a"])
        st = Station(room, codec="mp3", idle_stop=0.1)
        assert st.mime == "audio/mpeg"
        data = await collect(st, 2.0)
        await st.aclose()
        pcm = decode(data, "mp3")
        assert abs(dominant_hz(pcm[4000:]) - 440) < 25
    asyncio.run(go())
