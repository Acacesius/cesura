"""The YouTube cookie file: parse, validate, and write it safely.

Rules: cookie *values* never leave this module. Nothing here logs, returns or
raises with a value in it; callers only ever see names, counts and booleans.

Write-back: yt-dlp saves its whole jar back to `cookiefile` when it closes,
including cookies YouTube rotated during the request. Letting it write
config/cookies.txt directly could collide between concurrent lookups, or clobber a
good session with a signed-out one. So each lookup works on a private temp
copy (`snapshot`), and afterwards `write_back` copies the updated jar back only
if the lookup succeeded and the jar still holds a signed-in session. The copy
back is atomic (temp file + rename, mode 600) and happens under the same lock
the uploader uses.
"""
import os
import tempfile
import threading

from yt_dlp.cookies import YoutubeDLCookieJar

LOCK = threading.Lock()

# Cookie *names* that mark a signed-in Google session (no values here).
ACCOUNT_COOKIE = "LOGIN_INFO"
SAPISID_FAMILY = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")


def parse(path: str) -> YoutubeDLCookieJar:
    """Load with yt-dlp's own jar, which understands #HttpOnly_ lines. Raises on malformed files."""
    jar = YoutubeDLCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def youtube_names(jar) -> set[str]:
    return {c.name for c in jar if c.domain.lstrip(".").endswith("youtube.com")}


def has_session(jar) -> bool:
    names = youtube_names(jar)
    return ACCOUNT_COOKIE in names and any(n in names for n in SAPISID_FAMILY)


def write_atomic(path: str, data: bytes):
    """Replace `path` with `data`: temp file in the same dir, chmod 600, fsync, rename. Caller holds LOCK."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cookies-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def snapshot(path: str) -> str | None:
    """Private temp copy of the cookie file for one yt-dlp run (None if there is no file)."""
    with LOCK:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return None
    fd, tmp = tempfile.mkstemp(prefix="cookies-", suffix=".txt")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return tmp


def write_back(tmp: str, path: str) -> bool:
    """After a successful lookup: keep YouTube's rotated cookies, but never a signed-out jar."""
    try:
        jar = parse(tmp)
    except Exception:
        return False
    if not has_session(jar):
        return False
    with open(tmp, "rb") as f:
        data = f.read()
    with LOCK:
        try:
            with open(path, "rb") as f:
                if f.read() == data:
                    return False
        except FileNotFoundError:
            return False  # removed meanwhile (e.g. unlinked by the host): don't resurrect it
        write_atomic(path, data)
    return True
