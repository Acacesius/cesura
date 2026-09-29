# cesura

**A self-hosted listen-along room for YouTube Music.** Friends join from their phones with a 5-letter code and hear the same song at the same moment on their own devices. Everyone can queue, pause for the room and vote to skip. You run it on your own server, with your own YouTube Music account.

<!-- Screenshot placeholder: add docs/screenshot.png (phone "Now playing" + host dashboard) and replace this comment. -->
*Screenshot coming soon.*

> [!WARNING]
> **Unofficial APIs, your account, your risk.** cesura talks to YouTube Music through unofficial, reverse-engineered interfaces ([ytmusicapi](https://github.com/sigma67/ytmusicapi), [yt-dlp](https://github.com/yt-dlp/yt-dlp)) using **your own** signed-in session. It is not affiliated with or endorsed by Google or YouTube, and using it may violate [YouTube's Terms of Service](https://www.youtube.com/t/terms). Your account could be rate-limited or restricted. Use it at your own risk, and keep rooms to people you know.
>
> **Never share your cookies or your cookie file.** It is a login to your Google account.

## What it is

- **Listen-along, not remote control.** Every phone plays the audio itself. The server keeps one clock, and each phone steers its player to match it, typically to within tens of milliseconds of each other.
- **For guests:** join with the code and a name, then search songs, albums and playlists (or paste a YouTube link), queue or play next, pause for everyone, and vote to skip. Lock-screen controls work, and there's a per-device volume.
- **For the host:** a desktop dashboard with the queue (drag to reorder), autoplay radio, room code + QR, every listener with their live drift, and engine health (stream quality, prefetch, YouTube link state).
- **One container, no database, no accounts.** Everything lives in memory except `config/`. There's a first-run setup wizard, prebuilt images for amd64 and arm64, and a weekly rebuild with the latest yt-dlp.

## Quick start

You need Docker with the Compose plugin, and a YouTube Music account. Premium gets 256k AAC; without it, 128k.

```sh
mkdir -p cesura/config && cd cesura
curl -fsSLO https://raw.githubusercontent.com/acacesius/cesura/main/docker-compose.yml
docker compose up -d && docker compose logs cesura
```

The logs show a box with your **one-time setup token**:

```
========================================================================
  cesura is not set up yet.

  Open  /setup  in your browser and enter this one-time setup token:

      pX3...Qa9

  Only someone with this token can configure this server. It stops
  working as soon as setup is finished.
========================================================================
```

Open `http://127.0.0.1:8765/setup` on the same machine, or through the address you expose it on (see [Exposing it](#exposing-it)). Then paste the token. Lost it? Run `docker compose logs cesura` again: it's printed on every start until setup is done.

> Create `config/` **before** the first `up` (the first command above does). If Docker creates it, it's owned by root, and cesura will print how to fix it.

## The setup wizard

The setup token keeps strangers from claiming a fresh instance: nothing can be configured without it, and it stops working once setup is finished.

1. **Host key.** Generated for you and shown **once**. Save it in a password manager; it opens the host dashboard. Setting `HOST_KEY` in `.env` overrides it.
2. **Public address.** The URL guests will use, for the invite link and QR code. It must be `https://` unless it's localhost. `PUBLIC_URL` in `.env` overrides it.
3. **Link YouTube Music.** Upload a cookies.txt export (guide below). cesura checks it against YouTube right away and tells you what it found, e.g. *"Premium detected · 256k AAC"*, or what to fix. You can skip this step, but playback stays off, and the dashboard shows **YouTube not linked** until you link it.
4. **Done.** You land on the host dashboard at `/host`.

## Linking YouTube Music

cesura streams through your own YouTube Music session, as a `cookies.txt` exported from a **private window**. A normal window keeps using and rotating the same cookies, which breaks the export quickly. A private window that you close right after exporting doesn't.

1. **Install an open-source exporter:**
   - **[Get cookies.txt LOCALLY](https://github.com/kairi003/Get-cookies.txt-LOCALLY)**: [Chrome / Edge / Brave](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc), [Firefox](https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/)
   - or, on Firefox, **[cookies.txt](https://github.com/hrdl-github/cookies-txt)**: [Firefox Add-ons](https://addons.mozilla.org/firefox/addon/cookies-txt/)

   ⚠️ Avoid **"Get cookies.txt"** (without "LOCALLY"). It was reported as malware and removed from the Chrome Web Store.
2. **Allow it in private windows.** In Chrome: extension *Details* → **Allow in Incognito**. In Firefox: *Manage extension* → **Run in Private Windows** → Allow.
3. **Open a private (incognito) window** and sign in at [music.youtube.com](https://music.youtube.com).
4. **In the same tab, open `https://www.youtube.com/robots.txt`.** It's a plain page, so YouTube can't rotate your cookies while you export.
5. **Export cookies.txt** from the extension, in Netscape format. Extra sites in the file are fine: cesura keeps only YouTube and Google cookies and drops the rest.
6. **Close the private window. Don't log out**; logging out ends the session you just exported.
7. **Upload the file** in the wizard, or later from the host dashboard (**Relink YouTube**). Then delete your downloaded copy.

What cesura does with the file:
- It accepts only a Netscape cookies.txt up to 256 KB that contains a signed-in session.
- It stores the file as `config/cookies.txt`, readable only by the app (mode 600).
- It never logs, displays or returns cookie values.
- When YouTube rotates cookies during normal use, cesura saves the refreshed ones, but only if the session is still signed in.

The link check reports three things:
- **Account:** YouTube Music confirms the session is signed in.
- **256k AAC:** YouTube offers format 141 for a test song (`PROBE_VIDEO_ID`, default: Queen – *Bohemian Rhapsody*).
- **Premium:** inferred from 256k AAC being available. YouTube has no direct "Premium" flag.

When the link stops working (YouTube's bot check, or the session got signed out), the dashboard shows **YouTube link expired** with a **Relink YouTube** button. Relinking is the same upload and check as in setup.

## Configuration

Put these in a `.env` file next to `docker-compose.yml` (copy [`.env.example`](.env.example)). All are optional.

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `8765` | Port published on the host. |
| `BIND` | `127.0.0.1` | Interface the port is published on. Docker bypasses host firewalls such as UFW, so this is your real exposure control. Keep `127.0.0.1` and put a reverse proxy or tunnel in front. |
| `PUBLIC_URL` | *(from setup)* | Base URL for the invite link and QR code. Overrides the wizard's value. |
| `TRUSTED_PROXY_CIDR` | `172.16.0.0/12` | `X-Forwarded-For` is trusted only from this network (your proxy, as the container sees it). Rate limits and lockouts are per client IP. |
| `HOST_KEY` | *(from setup)* | Host dashboard key. Overrides the wizard's key. |
| `PROBE_VIDEO_ID` | `BSTsnWoslP4` | Song used by the link check. Change it if that one is unavailable where you are. |
| `CESURA_UID` / `CESURA_GID` | `1000` | User the container runs as. It must own `./config`. |
| `YTM_CLIENT_ID` / `YTM_CLIENT_SECRET` | | Only for optional ytmusicapi OAuth (personalised radio). Not needed. |

## Exposing it

cesura listens on `127.0.0.1:8765`. Put something in front that terminates HTTPS and passes websockets, then set the public address in setup (or `PUBLIC_URL`).

**Reverse proxy** on the same machine, e.g. Caddy:

```
cesura.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

If your proxy runs in Docker on its own network, set `TRUSTED_PROXY_CIDR` to that network.

**Tunnel** (Cloudflare Tunnel, ngrok and similar): point it at `http://127.0.0.1:8765` and use the tunnel's HTTPS address as the public URL.

**Tailscale.** `serve` shares it inside your tailnet; `funnel` makes it public (Funnel allows ports 443, 8443 and 10000):

```sh
tailscale serve status --json > serve-backup.json                 # save what's there first
sudo tailscale serve  --bg --https=8765 http://127.0.0.1:8765     # tailnet only
sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8765     # public internet
```

> [!CAUTION]
> **Never run `tailscale serve --https=<port> off`** to remove an entry. On some versions it wipes **all** serve and Funnel configuration on the machine, not just that port. Keep the backup from above so you can recreate anything that gets lost.

While exposed:
- guests need the current room code for everything, including audio;
- the dashboard needs the host key;
- each IP gets 10 wrong codes or keys per 10 minutes before a lockout;
- adds are rate-limited.

Press **New session** after a party to rotate the code.

## Updating

```sh
docker compose pull && docker compose up -d
```

- **`:latest`** is rebuilt **weekly** with the newest yt-dlp, yt-dlp-ejs and ytmusicapi, and only published if its smoke tests pass. YouTube changes break these libraries regularly, so pulling is usually the fix.
- **Versioned tags** (`:0.1.0`, …) are published for each release, if you want to pin one.
- **Source installs** (`docker-compose.dev.yml`): run `./update.sh`. It bumps those three libraries to their latest releases, rebuilds, restarts and runs the self-test.

To check a running install: `docker compose exec cesura python -m app.selftest`

## Troubleshooting

| Symptom | Fix |
|---|---|
| **"YouTube link expired"** on the dashboard | Export fresh cookies from a *new* private window (guide above) and use **Relink YouTube**. |
| **Bot check**: "Sign in to confirm you're not a bot" in the logs, or the link check says so | Same as above. Datacenter/VPS IPs get this far more often than home connections. |
| Everything skips, `could not load` in the logs, `Requested format is not available` | Update: `docker compose pull && docker compose up -d` (source installs: `./update.sh`). |
| Link check: "Signed in · no Premium detected · 128k AAC" | Works, but at 128k. Export from the account that has Premium. |
| "config is not writable" box in the logs | `sudo chown -R 1000:1000 ./config`, or set `CESURA_UID`/`CESURA_GID` to the owner of `./config`. |
| Lost the setup token | `docker compose logs cesura`. It's reprinted on every start until setup finishes. |
| Lost the host key | Stop the container, delete `config/host_key` and `config/setup.json`, and start again. A new setup token is printed; your cookies stay linked. |
| Phones play but drift | Check the Listening card. "Catching up" should settle within seconds. Very slow networks may need a moment after seeks. |

## Known limits

- **Locked iPhones.** iOS may finish the current song but not start the next one while the screen is locked. Keep cesura on screen; over HTTPS it holds a wake lock.
- **iOS volume.** Web pages can't set volume on iOS, so the slider is hidden there; use the hardware buttons.
- **Open controls.** Anyone with the room code can queue, pause for everyone and vote to skip.
- **One room per instance.** State is in memory: a restart ends the session and changes the code.
- **Unofficial APIs.** It breaks when YouTube changes things; update, and relink when asked.

## Development

```sh
git clone https://github.com/acacesius/cesura && cd cesura
mkdir -p config
docker compose -f docker-compose.dev.yml up -d --build
python3 tests/smoke.py --image cesura:dev     # the same smoke test CI runs before publishing
```

- **Server:** FastAPI + uvicorn in `app/`.
- **Frontend:** plain HTML/JS in `static/`, with no build step.
- **Streams:** resolved once by yt-dlp (running YouTube's player JS in Deno), held in memory, and served to every listener with HTTP Range support.
- **Sync:** each phone measures its clock offset to the server with ping/pong. It seeks when it's more than 1 s off, and nudges `playbackRate` by ±5% (inaudible) for smaller drift.

## License

MIT. See [LICENSE](LICENSE). The bundled fonts, Bricolage Grotesque and IBM Plex Mono, are under the SIL Open Font License; see `static/fonts/`.
