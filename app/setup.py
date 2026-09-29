"""First-run setup state: the one-time setup token, host key and public URL, all in config/.

Threat model: a fresh instance may already be reachable by strangers. Nothing
can be configured without the setup token, which only appears in the
container logs (i.e. to whoever runs the container). The token is stored so
a restart mid-setup doesn't lock the owner out, and deleted when setup finishes.
"""
import json
import os
import secrets
from urllib.parse import urlsplit

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/app/config")
STATE = os.path.join(CONFIG_DIR, "setup.json")
TOKEN = os.path.join(CONFIG_DIR, "setup_token")
HOST_KEY = os.path.join(CONFIG_DIR, "host_key")


def _write_private(path: str, text: str):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def load_state() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict):
    _write_private(STATE, json.dumps(state, indent=1))


def is_complete() -> bool:
    return bool(load_state().get("complete"))


def setup_token() -> str | None:
    """The current token, creating one if setup hasn't finished. None once setup is complete."""
    if is_complete():
        return None
    tok = _read(TOKEN)
    if not tok:
        tok = secrets.token_urlsafe(24)
        _write_private(TOKEN, tok + "\n")
    return tok


def finish():
    state = load_state()
    state["complete"] = True
    save_state(state)
    try:
        os.unlink(TOKEN)
    except FileNotFoundError:
        pass


def stored_host_key() -> str | None:
    return _read(HOST_KEY)


def new_host_key() -> str:
    key = secrets.token_urlsafe(24)
    _write_private(HOST_KEY, key + "\n")
    return key


def check_public_url(url: str) -> str:
    """Normalise and validate. Raises ValueError with a message safe to show the user."""
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Enter a full address such as https://music.example.com")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("Use just the base address, without a path")
    local = parts.hostname in ("localhost", "127.0.0.1", "::1") or parts.hostname.endswith(".localhost")
    if parts.scheme != "https" and not local:
        raise ValueError("Use https:// (plain http is only allowed for localhost). Phones need HTTPS for a reliable connection.")
    return f"{parts.scheme}://{parts.netloc}"
