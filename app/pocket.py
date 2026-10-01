"""Pocket mode: one shared, radio-style audio stream for phones in pockets.

Normal listeners each play the track file themselves and steer to the room
clock, which needs JavaScript running. A locked iPhone freezes JS, so the next
song never starts. Pocket listeners instead play one endless stream, like an
internet radio station: the phone just keeps playing it.

How it works:
  - One long-lived ffmpeg *encoder* turns raw PCM on stdin into ADTS AAC (or
    MP3) on stdout. It never restarts while the station is up, so the stream
    never breaks across songs.
  - A short-lived ffmpeg *decoder* per track (and per seek) reads the current
    track from the server's own in-memory proxy, starting at the right
    position, and outputs PCM.
  - A pacer writes PCM into the encoder in real time, following the room
    clock: decoded audio while playing, silence while paused, loading or idle.
    If the decoder ends up more than RESYNC seconds away from where the clock
    says the track is (a skip, a seek, a stall), it starts a new decoder at
    the right spot.
  - The encoder's output is split into frames and kept in a small ring. Every
    listener reads the same frames from its own position. A new listener
    starts AHEAD seconds back, so it gets a short burst to fill its buffer.
    One that falls too far behind skips forward.

The station runs only while someone is listening and stops IDLE_STOP seconds
after the last listener leaves.
"""
import asyncio
import collections
import logging
import time

log = logging.getLogger("cesura.pocket")

RATE, CHANNELS = 44100, 2
SAMPLE_BYTES = 2 * CHANNELS     # s16le stereo
AHEAD = 1.0                     # the encoder is fed this far ahead of real time
RESYNC = 0.3                    # restart the decoder when it's this far (s) from the clock
TICK = 0.02
IDLE_STOP = 30.0
RING_SECONDS = 20

CODECS = {
    # name: (encoder args, mime, samples per frame)
    "aac": (["-c:a", "aac", "-b:a", "160k", "-f", "adts"], "audio/aac", 1024),
    "mp3": (["-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"], "audio/mpeg", 1152),
}


# ---------------------------------------------------------------- framing

def _adts_len(b: bytes | bytearray, i: int) -> int:
    """Length of the ADTS frame at i, or 0 if there's no valid header there."""
    if b[i] != 0xFF or (b[i + 1] & 0xF6) != 0xF0:
        return 0
    return ((b[i + 3] & 0x03) << 11) | (b[i + 4] << 3) | (b[i + 5] >> 5)


_MP3_KBPS = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
             2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]}
_MP3_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _mp3_len(b: bytes | bytearray, i: int) -> int:
    """Length of the MPEG layer III frame at i, or 0."""
    if b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return 0
    ver, layer = (b[i + 1] >> 3) & 3, (b[i + 1] >> 1) & 3
    br_i, sr_i, pad = b[i + 2] >> 4, (b[i + 2] >> 2) & 3, (b[i + 2] >> 1) & 1
    if ver == 1 or layer != 1 or sr_i == 3 or br_i in (0, 15):
        return 0
    kbps = _MP3_KBPS[1 if ver == 3 else 2][br_i]
    sr = _MP3_RATES[ver][sr_i]
    return (144000 if ver == 3 else 72000) * kbps // sr + pad


def split_frames(buf: bytearray, codec: str = "aac") -> list[bytes]:
    """Remove and return the complete frames at the front of buf, skipping junk until a sync word."""
    flen = _adts_len if codec == "aac" else _mp3_len
    out, i = [], 0
    while len(buf) - i >= 7:
        n = flen(buf, i)
        if n < 7:
            i += 1  # resync
            continue
        if len(buf) - i < n:
            break
        out.append(bytes(buf[i:i + n]))
        i += n
    del buf[:i]
    return out


class Ring:
    """The most recent encoded frames, numbered. Listeners keep their own frame number."""

    def __init__(self, maxframes: int):
        self.frames: collections.deque[bytes] = collections.deque(maxlen=maxframes)
        self.next = 0  # number of the next frame to arrive
        self._new = asyncio.Event()

    @property
    def first(self) -> int:
        return self.next - len(self.frames)

    def push(self, frames: list[bytes]):
        if not frames:
            return
        self.frames.extend(frames)
        self.next += len(frames)
        ev, self._new = self._new, asyncio.Event()
        ev.set()

    def start(self, backlog: int) -> int:
        return max(self.first, self.next - backlog)

    async def read(self, seq: int) -> tuple[bytes, int]:
        """Frames from seq on (waiting for at least one). Skips forward if seq fell out of the ring."""
        while seq >= self.next:
            await self._new.wait()
        seq = max(seq, self.first)
        out = b"".join(self.frames[i] for i in range(seq - self.first, len(self.frames)))
        return out, self.next


# ---------------------------------------------------------------- the station

class Station:
    """now_playing() -> None (nothing to play), or (key, source, position):
    key identifies the track (changes on every track change), source is what
    ffmpeg should read, position is seconds into the track right now (negative
    during a lead-in), or None while paused or loading.
    """

    def __init__(self, now_playing, codec: str = "aac", ffmpeg: str = "ffmpeg", idle_stop: float = IDLE_STOP):
        self.now_playing = now_playing
        self.codec = codec if codec in CODECS else "aac"
        self.enc_args, self.mime, spf = CODECS[self.codec]
        self.frame_secs = spf / RATE
        self.ffmpeg = ffmpeg
        self.idle_stop = idle_stop
        self.ring = Ring(int(RING_SECONDS / self.frame_secs))
        self.listeners = 0
        self.task: asyncio.Task | None = None
        self._stop: asyncio.TimerHandle | None = None
        self.decoder_starts = 0  # for tests and the engine panel

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    # ---- lifecycle
    def join(self):
        self.listeners += 1
        if self._stop:
            self._stop.cancel()
            self._stop = None
        if not self.running:
            self.ring = Ring(self.ring.frames.maxlen)
            self.task = asyncio.create_task(self._run())

    def leave(self):
        self.listeners = max(0, self.listeners - 1)
        if not self.listeners and self.running and not self._stop:
            self._stop = asyncio.get_running_loop().call_later(self.idle_stop, self.stop)

    def stop(self) -> asyncio.Task | None:
        self._stop = None
        task, self.task = self.task, None
        if task:
            task.cancel()
            log.info("pocket: stopped")
        return task

    async def aclose(self):
        if self._stop:
            self._stop.cancel()
        if task := self.stop():
            await asyncio.gather(task, return_exceptions=True)

    async def listen(self):
        """Async generator of stream bytes for one listener."""
        self.join()
        try:
            while not self.ring.next:  # first frames after a cold start
                await self.ring.read(0)
            seq = self.ring.start(int(AHEAD / self.frame_secs))
            while True:
                data, seq = await self.ring.read(seq)
                yield data
        finally:
            self.leave()

    # ---- the pipeline
    async def _spawn(self, *args):
        return await asyncio.create_subprocess_exec(
            self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *args,
            stdin=asyncio.subprocess.PIPE if "pipe:0" in args else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def _run(self):
        log.info("pocket: starting (%s)", self.codec)
        # -probesize/-analyzeduration: raw PCM needs no probing, and the default probe holds back the first ~5 s.
        enc = await self._spawn("-probesize", "32", "-analyzeduration", "0", "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS),
                                "-i", "pipe:0", *self.enc_args, "-flush_packets", "1", "pipe:1")
        reader = self._reader = asyncio.create_task(self._read_encoder(enc))
        self._enc = enc
        dec: Decoder | None = None
        try:
            t0, written = time.monotonic(), 0
            while True:
                due = int((time.monotonic() - t0 + AHEAD) * RATE) - written
                if due < TICK * RATE:  # write in ~20 ms batches and let the reader and listeners run
                    await asyncio.sleep(TICK)
                    continue
                cur = self.now_playing()
                key, src, pos = cur if cur else (None, None, None)
                if key is None or pos is None:
                    dec = await _close(dec)
                    await self._write(enc, bytes(due * SAMPLE_BYTES))
                    written += due
                    continue
                # Where the next sample we write should be in the track (a listener hears it at t0 + written/RATE).
                want = pos + (t0 + written / RATE - time.monotonic())
                if want < 0:  # lead-in before the track starts
                    n = min(due, int(-want * RATE) + 1)
                    await self._write(enc, bytes(n * SAMPLE_BYTES))
                    written += n
                    continue
                if dec is None or dec.key != key or abs(dec.pos - want) > RESYNC:
                    await _close(dec)
                    dec = Decoder(key, want, await self._spawn("-ss", f"{want:.3f}", "-i", src, "-vn", "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "pipe:1"))
                    self.decoder_starts += 1
                pcm = dec.take(due)
                if pcm:
                    await self._write(enc, pcm)
                    written += len(pcm) // SAMPLE_BYTES
                elif dec.eof or (written - (time.monotonic() - t0) * RATE < 0.15 * RATE and dec.age > 0.5):
                    # Track over, or the decoder is starving us: fill with silence rather than let listeners run
                    # dry. (A starving decoder falls behind the clock and gets restarted at the right spot.)
                    await self._write(enc, bytes(due * SAMPLE_BYTES))
                    written += due
                    if dec.eof:
                        dec.pos += due / RATE
                else:
                    await asyncio.sleep(TICK)  # decoder still starting; there's buffer to spare
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("pocket: station failed")
        finally:
            await _close(dec)
            reader.cancel()
            await _kill(enc)

    async def _write(self, enc, data: bytes):
        enc.stdin.write(data)
        await enc.stdin.drain()

    async def _read_encoder(self, enc):
        buf = bytearray()
        while chunk := await enc.stdout.read(16384):
            buf += chunk
            self.ring.push(split_frames(buf, self.codec))


class Decoder:
    """One track's PCM, read ahead a little (backpressure keeps ffmpeg from decoding the whole song into RAM)."""

    MAX_BUFFER = 3 * RATE * SAMPLE_BYTES

    def __init__(self, key, pos: float, proc):
        self.key, self.pos, self.proc = key, pos, proc
        self.born = time.monotonic()
        self.buf = bytearray()
        self.eof = False
        self.task = asyncio.create_task(self._fill())

    async def _fill(self):
        while True:
            while len(self.buf) > self.MAX_BUFFER:
                await asyncio.sleep(0.05)
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                self.eof = True
                return
            self.buf += chunk

    @property
    def age(self) -> float:
        return time.monotonic() - self.born

    def take(self, samples: int) -> bytes:
        n = min(samples * SAMPLE_BYTES, len(self.buf) // SAMPLE_BYTES * SAMPLE_BYTES)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        self.pos += n / SAMPLE_BYTES / RATE
        return out


async def _kill(proc):
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    if proc:
        # communicate(), not wait(): a pipe we stopped reading (backpressure) never reports EOF, and
        # asyncio only counts the process as finished once its pipes are closed.
        await asyncio.wait_for(proc.communicate(), 5)


async def _close(dec: Decoder | None) -> None:
    if dec:
        dec.task.cancel()
        await _kill(dec.proc)
    return None
