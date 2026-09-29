# cesura

A self-hosted, listen-along music room in the style of Spotify Jam, backed by **your own YouTube Music account**.

Everyone joins from their phone with a 5-letter room code and hears the same song at the same moment on their own device. Anyone in the room can search, queue songs, pause for everyone and vote to skip. The host gets a desktop dashboard with the queue, the listeners and how in phase each one is, plus engine health.

- One container: FastAPI + uvicorn, [ytmusicapi](https://github.com/sigma67/ytmusicapi), [yt-dlp](https://github.com/yt-dlp/yt-dlp) + Deno.
- No database and no accounts. State lives in memory; only `config/` persists.
- Plain HTML/JS frontend, no build step. Fonts are bundled (OFL), and there are no third-party requests except YouTube album art.

> **Unofficial APIs, your account, your risk.** cesura uses unofficial, reverse-engineered YouTube Music interfaces (ytmusicapi, yt-dlp) with *your* logged-in session. This is not affiliated with or endorsed by Google or YouTube, and it may violate YouTube's Terms of Service. Your account could be rate-limited or restricted. Things will break whenever YouTube changes something. Use at your own risk, and keep rooms to people you know.

## Requirements

- Docker with the Compose plugin (`docker compose version`), on Linux, macOS or WSL2.
- A YouTube Music account. Premium gets 256 kbps AAC; free accounts and no cookies get lower quality, if YouTube serves anything at all.
- A way for guests to reach the server over HTTPS: a reverse proxy, a tunnel, or Tailscale (see [Exposing it](#exposing-it)). Plain http on a LAN works too, but phones only allow screen wake lock on HTTPS.

## Setup

```sh
git clone <this repo> cesura && cd cesura
cp .env.example .env          # optional, every setting has a default
docker compose up -d --build
docker compose logs cesura       # shows the generated host key and the room code
```

On first start with no `HOST_KEY` set, cesura generates one, saves it to `config/host_key` and prints it once:

```
  cesura host key: q3J...xyz
  (saved to /config/host_key)
  Open /host?key=q3J...xyz
```

- **Host dashboard:** `http://127.0.0.1:8765/host?key=<host key>`. It shows the room code, QR code, queue, listeners and engine status.
- **Guests:** open `/`, enter the code and a name, and tap **Join**. That tap also unlocks audio on phones.

The room code changes on every restart and whenever the host presses **New session**, which also clears the queue and disconnects anyone on the old code.

### Configuration (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `8765` | Port published on the host. |
| `BIND` | `127.0.0.1` | Interface the port is published on. Docker bypasses host firewalls such as UFW, so this is your exposure control. Use `0.0.0.0` only if you mean it. |
| `PUBLIC_URL` | *(blank)* | Public base URL used for the invite link and QR code, e.g. `https://cesura.example.com`. Blank = the address the host dashboard was opened on. |
| `TRUSTED_PROXY_CIDR` | `172.16.0.0/12` | `X-Forwarded-For` is only trusted from this network, which is your reverse proxy as seen from the container. Rate limits and lockouts are per client IP. |
| `HOST_KEY` | *(generated)* | Key for `/host`. Blank = generate once and save to `config/host_key`. |
| `CESURA_UID` / `CESURA_GID` | `1000` | User the container runs as. It needs write access to `./config`. |
| `YTM_CLIENT_ID` / `YTM_CLIENT_SECRET` | *(blank)* | Only for ytmusicapi OAuth (optional, below). |

If `config/` isn't writable by `CESURA_UID`, the generated key can't be saved. It would then change on every restart, so either `chown` the folder or set `HOST_KEY`.

## YouTube cookies (for yt-dlp)

Streams are resolved with yt-dlp using your YouTube Music session, exported as a Netscape-format `cookies.txt`. Without it, cesura still starts, but shows a "No YouTube cookies" notice to the host and guests, and playback may be low quality or blocked by YouTube's bot check.

**Export them from a private window**, so your normal browser session doesn't rotate the cookies out from under you:

1. Open a **private/incognito window** and sign in at https://music.youtube.com.
2. In that same window, export cookies for `youtube.com` in Netscape format with a cookies.txt extension. Firefox has "cookies.txt"; Chrome has "Get cookies.txt LOCALLY". Only use well-reviewed extensions, since this is your Google session.
3. **Close the private window right away and don't reuse it.** A session kept open in a browser gets its cookies rotated within hours; a closed one keeps working much longer.
4. Save the file as `config/cookies.txt` and restrict it: `chmod 600 config/cookies.txt`.

No restart is needed; the file is read on every lookup. The dashboard's **Engine** card shows the cookie state: *Valid*, *Missing*, or *Bot check, re-export*. Check the stream line: `itag 141` means Premium 256k AAC.

Treat `cookies.txt` like a password. It is excluded from git by `.gitignore`.

### Optional: ytmusicapi auth

Search, albums, playlists and autoplay radio all work unauthenticated. Auth only personalises radio picks.

- **Browser headers:** `docker compose run --rm --entrypoint ytmusicapi cesura browser --file /config/browser.json`, then paste the request headers from a `music.youtube.com` `/browse` request (DevTools → Network).
- **OAuth:** needs your own Google Cloud OAuth client ("TVs and Limited Input devices"). Run `docker compose run --rm --entrypoint ytmusicapi cesura oauth --file /config/oauth.json --client-id … --client-secret …`, and set `YTM_CLIENT_ID` / `YTM_CLIENT_SECRET` in `.env`.

Restart after adding either file.

## Exposing it

cesura listens on `127.0.0.1:8765` by default. Put something in front of it that terminates HTTPS and passes websockets:

- **Reverse proxy** (Caddy, nginx, Traefik) on the same machine. Example Caddyfile:
  ```
  cesura.example.com {
      reverse_proxy 127.0.0.1:8765
  }
  ```
  Then set `PUBLIC_URL=https://cesura.example.com`. If the proxy runs in Docker on a custom network, set `TRUSTED_PROXY_CIDR` to that network.
- **Tunnel** (Cloudflare Tunnel, ngrok, and similar): point it at `http://127.0.0.1:8765` and set `PUBLIC_URL` to the tunnel's address.
- **Tailscale.** `tailscale serve` shares it with your tailnet only; Funnel makes it public:
  ```sh
  tailscale serve status                                   # look at what's already configured first
  sudo tailscale serve  --bg --https=8765 http://127.0.0.1:8765   # tailnet only
  sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8765   # public internet (Funnel: 443, 8443 or 10000)
  ```
  Your HTTPS name is shown by `tailscale serve status`. Set `PUBLIC_URL` to it, including the port.

  ⚠️ **Never run `tailscale serve --https=<port> off`** to remove an entry. On some versions it wipes **all** serve/Funnel config on the machine, not just that port. Save `tailscale serve status --json` before making changes, so you can recreate anything that gets lost.

While public, cesura's protection is:
- every guest action (including fetching audio) needs the current room code;
- `/host` needs the host key;
- each IP gets 10 wrong codes/keys per 10 minutes before a 429 lockout;
- `/add` is rate-limited to 10 per IP per minute.

Rotate the code with **New session** after a party.

## Updating yt-dlp / ytmusicapi

YouTube changes break these regularly. Versions are pinned in `requirements.txt`. To bump yt-dlp, yt-dlp-ejs and ytmusicapi to their latest releases and rebuild (also pulling newer Python/Deno base images):

```sh
./update.sh
```

Run it when you see `could not load`, `Requested format is not available`, or every song being skipped. To roll back, restore the previous pins and run `docker compose up -d --build`.

## How it works (short version)

- **Server-owned clock.** For the current song the server stores `started_at` (server time at position 0), or a frozen position while paused, loading or when nobody is listening. It ends songs on its own timer.
- **Sync.** Each phone measures its clock offset with ping/pong (lowest round-trip wins). Every 500 ms it compares `audio.currentTime` with where the song should be. More than 1 s off → seek; 60 ms–1 s → nudge `playbackRate` to 0.95/1.05, which is inaudible. Phones typically land within tens of ms of each other. They report their drift to the host dashboard, for display only.
- **Streams.** yt-dlp resolves each song once, running YouTube's player JS in Deno. The resolved googlevideo URLs are signed for the server's IP and expire, so cesura downloads the song into memory once and serves every listener from there, with HTTP Range support. The next song is prefetched.
- **Autoplay radio.** When the queue runs out, cesura seeds YouTube Music's "up next" radio from the last song. The host can turn it off.

## Known limits

- **Locked phones.** iPhones especially may finish the current song but not start the next one when the screen is locked. Keep cesura on screen; over HTTPS it holds a wake lock.
- **iOS volume.** Web pages can't set volume on iOS, so the slider is hidden there.
- **Open control.** Anyone with the room code can pause for everyone, add songs and vote to skip.
- **One room per instance.** State is in memory, so a restart ends the session.
- **Memory.** Songs are cached in RAM (about 4 at a time). Tracks over 150 MB are proxied per request instead.
- **Fragility.** Everything depends on unofficial YouTube interfaces. Expect breakage, then run `./update.sh`.

## License

See [LICENSE](LICENSE). The bundled fonts (Bricolage Grotesque, IBM Plex Mono) are under the SIL Open Font License; see `static/fonts/`.
