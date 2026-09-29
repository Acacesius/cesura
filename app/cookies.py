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


# ---- uploads (setup wizard / relink)

MAX_UPLOAD = 256 * 1024
KEEP_DOMAINS = ("youtube.com", "google.com")  # everything else in an export is dropped


class Rejected(Exception):
    """Upload refused. The message is fixed text written here: safe to show, never contains file content."""


def _domain_ok(domain: str, suffixes=KEEP_DOMAINS) -> bool:
    d = domain.lstrip(".").lower()
    return any(d == s or d.endswith("." + s) for s in suffixes)


def ingest(data: bytes, dest: str) -> dict:
    """Validate an uploaded cookies.txt, keep only YouTube/Google cookies, save atomically (600).

    Returns counts only. Python's cookie parser puts the offending *line* in its
    exception text, so parser errors are replaced with fixed messages here and
    never chained (`from None`) into anything that could be logged.
    """
    if len(data) > MAX_UPLOAD:
        raise Rejected("That file is larger than 256 KB, which is too big for a cookies.txt export. Export again with the extension.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Rejected("That isn't a text file. Export cookies.txt with the extension.") from None
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first:
        raise Rejected("That file is empty.")
    if first.startswith(("[", "{")):
        raise Rejected("That's a JSON export. In the extension, choose the Netscape (cookies.txt) format and export again.")
    if first not in ("# Netscape HTTP Cookie File", "# HTTP Cookie File"):
        raise Rejected("That isn't a Netscape cookies.txt file (the first line should be '# Netscape HTTP Cookie File').")

    fd, raw = tempfile.mkstemp(prefix="upload-", suffix=".txt")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    out = None
    try:
        try:
            jar = parse(raw)
        except Exception:
            raise Rejected("Couldn't read that cookies file. Export it again with the extension.") from None
        cookies = list(jar)
        kept = [c for c in cookies if _domain_ok(c.domain)]
        if not any(_domain_ok(c.domain, ("youtube.com",)) for c in kept):
            raise Rejected("No YouTube cookies in that file. Export while on a youtube.com page, signed in.")
        clean = YoutubeDLCookieJar()
        for c in kept:
            clean.set_cookie(c)
        if not has_session(clean):
            raise Rejected("Signed out: this export has no YouTube Music session. Sign in at music.youtube.com in a private window, then export again (close the window without logging out).")
        fd, out = tempfile.mkstemp(prefix="clean-", suffix=".txt")
        os.close(fd)
        clean.save(out, ignore_discard=True, ignore_expires=True)
        with open(out, "rb") as f:
            cleaned = f.read()
        with LOCK:
            write_atomic(dest, cleaned)
        return {"youtube_cookies": len(youtube_names(clean)), "kept": len(kept), "dropped_other_sites": len(cookies) - len(kept)}
    finally:
        for p in (raw, out):
            if p:
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


def cookie_header(jar) -> str:
    """Cookie header for youtube.com requests. In-memory use only; never log or return it."""
    return "; ".join(f"{c.name}={c.value}" for c in jar if _domain_ok(c.domain, ("youtube.com",)))
