// Host dashboard: room code + QR, the queue (reorder, remove, add), radio,
// listeners (kick, ban, co-host) with their measured drift, recent actions,
// and engine health. Plays no audio.
const params = new URLSearchParams(location.search);
const key = params.get("key") || localStorage.getItem("cesura.hostkey");
if (params.get("key")) { localStorage.setItem("cesura.hostkey", params.get("key")); history.replaceState(null, "", "/host"); }
const K = encodeURIComponent(key || "");
let state = null, shownCode = null, artQid, renderedQkey = null;

// Host actions carry the key in a header, never in the URL.
const hostPost = (path, body = {}) => fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Host-Key": key }, body: JSON.stringify(body) });
const roomPost = (path, body) => hostPost(path, { code: state.code, ...body });
// PUBLIC_URL (server config) wins; otherwise the address this dashboard was opened on.
const inviteUrl = () => `${(state && state.public_url) || location.origin}/?code=${state ? state.code : ""}`;

let live = null;
function connect() {
  live = liveSocket({
    banner: $("conn"),
    open: () => new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/host?key=${K}`),
    onMessage: (m) => {
      if (m.type !== "state" && m.type !== "patch") return;
      const s = applyPatch(state, m);
      if (s) apply(s); else live.send({ type: "resync" });
    },
    onClose: (e) => {
      if (e.code === 4003) { localStorage.removeItem("cesura.hostkey"); askForKey("That host key didn't work."); return true; }
      if (e.code === 4002) { $("p-live").replaceChildren(el("span", { class: "dot warn" }), "Opened in another tab"); return true; }
      $("p-live").replaceChildren(el("span", { class: "dot warn pulsing" }), "Reconnecting…");
      return false;
    },
  });
}

const mediaHandlers = {
  play: () => state && roomPost("/api/pause", { paused: false }),
  pause: () => state && roomPost("/api/pause", { paused: true }),
  next: () => skipNow(),
};

const HEALTH = {
  ok: ["OK", "ok"], degraded: ["Degraded", "bad"], failed: ["Failed", "bad"], unknown: ["Checking…", ""],
};

function apply(s) {
  const prev = state;
  state = s;
  clockOffset = s.server_time - Date.now() / 1000;
  const h = s.host, n = s.now, e = h.engine, hl = e.health;
  setMediaSession(n, mediaHandlers, s.status === "playing");

  // YouTube link: a persistent banner whenever anything isn't OK (not linked, expired, degraded, failed).
  const ck = e.cookies;
  let banner = null;
  if (ck === "missing") banner = ["YouTube not linked.", "Playback is off until you link a YouTube Music account."];
  else if (ck === "expired") banner = ["YouTube link expired.", "YouTube asked for a sign-in check. Playback may stop until you relink."];
  else if (hl.state === "failed") banner = ["YouTube link check failed.", hl.problem === "bot-check" ? "YouTube asked for a bot check. Relink with a fresh export." : "The saved session didn't pass the check. Relink to fix it."];
  else if (hl.state === "degraded") banner = ["YouTube link degraded.", !hl.aac256 ? "Signed in, but no 256k AAC (no Premium detected): streaming at 128k." : "Signed in, but the quality check couldn't run."];
  $("relink-banner").classList.toggle("hidden", !banner);
  if (banner) { $("relink-title").textContent = banner[0]; $("relink-sub").textContent = banner[1]; }

  // top bar
  $("p-live").replaceChildren(el("span", { class: "dot" }), `Live · ${s.listening} listening`);
  $("p-clock").replaceChildren(ic(s.status === "playing" ? "play" : "pause"), CLOCK_TEXT[s.status] || s.status);

  // now playing
  if (artQid !== (n ? n.qid : null)) { artQid = n ? n.qid : null; $("np-art").replaceChildren(n ? art(n, "") : el("div", { class: "art-ph" })); }
  $("np-title").textContent = n ? n.title : "Nothing playing yet";
  $("np-sub").textContent = n ? [n.artists, n.album].filter(Boolean).join(" · ") : "Add a song from the queue panel";
  $("np-by").replaceChildren(...(n && (n.by || n.auto) ? [avatar(n.by || "Radio"), n.by ? `Added by ${n.by}` : "From radio"] : []));
  $("np-hands").textContent = s.hands ? s.hands.text : "";
  $("np-hands").classList.toggle("hidden", !s.hands);
  $("pause").innerHTML = icon(s.status === "paused" ? "play" : "pause");
  $("pause").setAttribute("aria-label", s.status === "paused" ? "Resume for everyone" : "Pause for everyone");
  $("pause").disabled = !n || s.status === "loading";
  $("skip").disabled = !n;
  $("skip").title = s.votes ? `Skip now (${s.votes}/${s.votes_needed} votes so far)` : "Skip now";
  $("radio-toggle").setAttribute("aria-pressed", String(s.radio));
  // Short label so it never collides with the pause button in a narrow column; state shows in the icon colour.
  $("radio-toggle").replaceChildren(ic("radio"), "Radio");
  $("radio-toggle").title = s.radio ? "Autoplay radio is on" : "Autoplay radio is off";

  // stats + recent actions
  $("st-played").textContent = h.stats.songs_played;
  $("st-drift").textContent = h.stats.median_drift_ms == null ? "–" : `${h.stats.median_drift_ms} ms`;
  $("st-peak").textContent = h.stats.peak_listeners;
  $("feed").replaceChildren(...(h.feed.length ? h.feed.slice(0, 10).map((f) => el("div", { class: "fe" },
    el("span", { class: "ellip" }, el("b", { textContent: f.who }), ` ${f.text}`), el("span", { class: "ago", textContent: ago(serverNow() - f.at) })))
    : [el("div", { class: "empty", textContent: "Who queued, skipped, paused and resumed shows up here." })]));

  // queue
  $("q-count").textContent = `${s.queue.length} song${s.queue.length === 1 ? "" : "s"}`;
  segs($("q-sub"), [s.queue.length ? `${totalMin(s.queue)} min` : null, "guests take turns", s.radio ? "then autoplay radio" : "radio off"]);
  $("q-hdr").classList.toggle("hidden", !s.queue.length);
  // Rebuild the rows only when the queue (or the CACHED tag) changed: patches keep an unchanged queue identical.
  const qkey = `${e.next_qid}|${e.next}`;
  if (!sorter.dragging && (!prev || prev.queue !== s.queue || qkey !== renderedQkey)) {
    renderedQkey = qkey;
    const rows = s.queue.map((t, i) => qrow(t, i, e));
    $("queue").replaceChildren(...(rows.length ? rows : [emptyState("queue", "Nothing queued", "Search above to add songs, or share the code so guests can.")]));
    if (rows.length) touchQueue(rows, { onRemove: (qid) => remove(s.queue.find((t) => t.qid === qid)), onMove: move });
    sorter.restore();
  }
  segs($("radio-sub"), s.radio ? ["Plays when the queue runs out", n ? `seeded from ${n.title}` : null] : ["Off. The room goes quiet when the queue runs out."]);
  $("radio-btn").textContent = s.radio ? "Turn off" : "Turn on";
  if (!prev || prev.auto !== s.auto) $("auto").replaceChildren(...s.auto.slice(0, 6).map((t) => autorow(t, e)));

  // room code, QR, lock
  if (s.code !== shownCode) {
    const rotated = shownCode !== null && h.listeners.length > 0;
    shownCode = s.code;
    $("codebox").replaceChildren(...[...s.code].map((c) => el("span", { textContent: c })));
    $("codebox").classList.toggle("fresh", rotated);
    $("code-note").classList.toggle("hidden", !rotated);
    $("join-url").textContent = inviteUrl();
    $("qr").src = `/api/qr.svg?key=${K}&url=${encodeURIComponent(inviteUrl())}`;
    $("pop-guest").href = inviteUrl();
  }
  const set = h.settings;
  $("lock-room").setAttribute("aria-pressed", String(set.locked));
  $("lock-room").replaceChildren(ic(set.locked ? "lock" : "unlock"), set.locked ? "Locked" : "Lock room");
  $("lock-state").textContent = set.locked ? "Locked: new joins refused" : h.banned ? `${h.banned} banned` : "";

  // listeners
  $("l-count").textContent = h.listeners.filter((l) => l.listening).length;
  $("listeners").replaceChildren(...(h.listeners.length ? h.listeners.map(lrow) : [emptyState("people", "Nobody here yet", "Share the code or the QR.")]));

  // engine
  $("e-stream").textContent = e.stream || "–";
  const NEXT = { cached: ["Prefetched", "ok"], resolving: ["Fetching…", ""], failed: ["Failed", "bad"], idle: ["–", ""] };
  const [nt, nc] = NEXT[e.next] || [e.next, ""];
  $("e-next").textContent = nt; $("e-next").className = `v ${nc}`;
  const [ct, cc] = ck === "missing" ? ["Not linked", "bad"] : ck === "expired" ? ["Expired", "bad"] : HEALTH[hl.state] || [hl.state, ""];
  $("e-cookies").textContent = ct; $("e-cookies").className = `v ${cc}`;
  $("e-cookies").title = hl.checked_at ? `Checked ${new Date(hl.checked_at * 1000).toLocaleString()}` : "";
  $("e-premium").textContent = hl.checked_at == null ? "–" : `${hl.premium ? "Yes" : "No"} · ${hl.aac256 ? "Yes" : "No"}`;
  $("e-premium").className = `v ${hl.checked_at == null ? "" : hl.premium && hl.aac256 ? "ok" : "bad"}`;
  const days = hl.expires ? Math.round((hl.expires * 1000 - Date.now()) / 86400000) : null;
  $("e-expiry").textContent = hl.expires ? `${new Date(hl.expires * 1000).toLocaleDateString()} · ${days} d` : "–";
  $("e-expiry").className = `v ${days != null && days < 14 ? "bad" : ""}`;
  $("e-pocket").textContent = e.pocket.running ? `On · ${e.pocket.listeners} · ${e.pocket.codec.toUpperCase()}` : "Off";
  $("e-pocket").className = `v ${e.pocket.running ? "ok" : ""}`;
  $("e-wrong").textContent = `${e.wrong_codes} / ${e.wrong_code_limit}`;
  $("e-wrong").className = `v ${e.wrong_codes ? "bad" : ""}`;
  renderSettings();
}

// A line of " · "-separated parts that wraps only between parts, never mid-phrase.
function segs(node, parts) {
  const kids = [];
  parts.filter(Boolean).forEach((t, i) => { if (i) kids.push(el("span", { class: "sep", textContent: " · " })); kids.push(el("span", { class: "seg", textContent: t })); });
  node.replaceChildren(...kids);
}

function ago(s) { return s < 60 ? "now" : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h`; }

function qrow(t, i, engine) {
  const row = el("div", { class: `qgrid qrow${i === 0 ? " first" : ""}` },
    gripBtn(t),
    art(t),
    trackCell(t, engine, t.pinned ? el("span", { class: "tag amber", textContent: "PINNED" }) : null),
    el("span", { class: "who", title: t.by || "Guest" }, avatar(t.by || "?"), el("span", { class: "ellip name", textContent: t.by || "Guest" })),
    el("span", { class: "time", textContent: fmt(t.duration) }),
    el("button", { class: "iconbtn bare", html: icon("x"), ariaLabel: `Remove ${t.title}`, onclick: () => remove(t) }));
  row.dataset.qid = t.qid;
  return row;
}

// Title on its own line (ellipsis); artist line carries the badges, so they never cover the title.
function trackCell(t, engine, ...tags) {
  const cached = engine.next_qid === t.qid && engine.next === "cached" ? el("span", { class: "tag", textContent: "CACHED" }) : null;
  return el("div", { class: "cell" },
    el("div", { class: "t ellip", textContent: t.title }),
    el("div", { class: "s" }, el("span", { class: "ellip", textContent: t.artists || "" }), ...tags, cached));
}

// Autoplay radio: [art][title / artist][duration][+]
function autorow(t, engine) {
  return el("div", { class: "agrid qrow" }, art(t), trackCell(t, engine),
    el("span", { class: "time", textContent: fmt(t.duration) }),
    el("button", { class: "iconbtn", html: icon("plus"), ariaLabel: `Add ${t.title} to queue`, onclick: () => addIds([t.videoId], t.title) }));
}

const LST = { in_phase: ["", "In phase"], catching_up: ["warn", "Catching up"], waiting: ["warn", "Joining"], idle: ["idle", "Not listening"], pocket: ["", "Pocket"], away: ["idle", "Reconnecting"] };
let openMenu = null;
function lrow(l) {
  const [cls, label] = LST[l.status] || ["idle", l.status];
  const menu = el("div", { class: "popover hidden", role: "menu" },
    el("button", { role: "menuitem", onclick: () => setCohost(l, l.role !== "cohost") }, ic("star"), l.role === "cohost" ? "Make a guest again" : "Make co-host"),
    el("button", { role: "menuitem", onclick: () => kick(l) }, ic("kick"), "Kick (can rejoin)"),
    el("button", { role: "menuitem", onclick: () => ban(l) }, ic("ban"), "Ban from this room"));
  const btn = el("button", { class: "iconbtn bare", html: icon("more"), ariaLabel: `Actions for ${l.name}`, ariaHaspopup: "menu", ariaExpanded: "false",
    onclick: (e) => {
      e.stopPropagation();
      const wasOpen = openMenu === l.pid;
      closeMenus();
      if (!wasOpen) { menu.classList.remove("hidden"); btn.setAttribute("aria-expanded", "true"); openMenu = l.pid; }
    } });
  if (openMenu === l.pid) { menu.classList.remove("hidden"); btn.setAttribute("aria-expanded", "true"); }
  return el("div", { class: "lrow" }, avatar(l.name),
    el("div", { style: "min-width:0" },
      el("div", { class: "n" }, el("span", { class: "ellip", textContent: l.name }),
        l.role === "cohost" ? el("span", { class: "tag amber", textContent: "CO-HOST" }) : null,
        l.sockets > 1 ? el("span", { class: "tag", title: `${l.sockets} connections from this device`, textContent: `×${l.sockets}` }) : null),
      el("div", { class: "d ellip", textContent: l.device })),
    el("div", { class: "r" }, el("div", { class: "ms", textContent: l.drift_ms == null ? "–" : `${Math.abs(l.drift_ms)} ms` }),
      el("div", { class: `st ${cls}` }, el("span", { class: "dot" }), label)),
    el("div", { class: "lmenu" }, btn, menu));
}
function closeMenus() {
  openMenu = null;
  document.querySelectorAll(".lmenu .popover").forEach((m) => m.classList.add("hidden"));
  document.querySelectorAll(".lmenu > button").forEach((b) => b.setAttribute("aria-expanded", "false"));
}

// ---- actions
async function remove(t) { if (t && (await roomPost("/api/remove", { qid: t.qid })).ok) toast(`Removed ${t.title}`); }
// Drag to reorder (grip: mouse, touch, arrow keys). The server checks it against the queue as it is
// now; if someone else moved it first, it refuses and the list redraws from the broadcast.
async function move(qid, index, from) {
  const r = await roomPost("/api/move", { qid, index, from_index: from });
  if (!r.ok) toast((await r.json().catch(() => ({}))).detail || "Couldn't move that.");
}
const sorter = sortable($("queue"), { onMove: move, onIdle: () => { renderedQkey = null; if (state) apply(state); } });
async function addIds(videoIds, label) {
  const r = await roomPost("/api/add", { videoIds });
  if (r.ok) { const { added } = await r.json(); toast(added > 1 ? `Added ${added} songs` : `Added ${label}`); }
  else toast(r.status === 409 ? "The queue is full." : "Couldn't add that.");
}
function skipNow() { if (state && state.now) roomPost("/api/skip", { qid: state.now.qid }); }
async function kick(l) {
  closeMenus();
  if (!confirm(`Kick ${l.name}? Their connections close; they can rejoin with the code.`)) return;
  const r = await roomPost("/api/mod/kick", { pid: l.pid });
  toast(r.ok ? `Kicked ${l.name}` : "Couldn't kick them");
}
async function ban(l) {
  closeMenus();
  if (!confirm(`Ban ${l.name}? They're removed, their queued songs go, and this device can't rejoin until the server restarts.\n\nTo keep them out for sure, also change the room code.`)) return;
  const r = await hostPost("/api/host/ban", { pid: l.pid });
  if (r.ok) toast(`Banned ${l.name}`);
}
async function setCohost(l, on) {
  closeMenus();
  const r = await hostPost("/api/host/cohost", { pid: l.pid, on });
  if (r.ok) toast(on ? `${l.name} is a co-host: they can kick, skip and reorder` : `${l.name} is a guest again`);
}
$("pause").addEventListener("click", () => state && roomPost("/api/pause", { paused: state.status !== "paused" }));
$("skip").addEventListener("click", skipNow);
const toggleRadio = () => state && hostPost("/api/host/radio", { on: !state.radio });
$("radio-toggle").addEventListener("click", toggleRadio);
$("radio-btn").addEventListener("click", toggleRadio);
$("copy-link").replaceChildren(ic("link"), "Copy invite link");
$("copy-link").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(inviteUrl()); toast("Invite link copied"); }
  catch { prompt("Copy the invite link:", inviteUrl()); }
});
$("new-session").replaceChildren(ic("refresh"), "New session");
$("new-session").addEventListener("click", async () => {
  if (!confirm("Start a new session? This clears the queue, disconnects everyone and changes the room code. Guests will need the new code.")) return;
  await hostPost("/api/new-session");
});
$("rotate-code").replaceChildren(ic("refresh"), "New code");
$("rotate-code").addEventListener("click", async () => {
  if (!confirm("Change the room code? Everyone connected stays in and gets the new code. Anyone kicked can't use the old one.")) return;
  const r = await hostPost("/api/host/code");
  if (r.ok) toast(`New room code: ${(await r.json()).code}`);
});
$("lock-room").addEventListener("click", () => state && hostPost("/api/host/lock", { on: !state.host.settings.locked }));

// ---- settings popover
function renderSettings() {
  if (!state) return;
  const set = state.host.settings;
  $("pop-library").setAttribute("aria-pressed", String(set.library));
  $("pop-library").replaceChildren(ic("library"), set.library ? "Host's library: shared" : "Share my library with guests");
  $("pop-radio").setAttribute("aria-pressed", String(set.radio));
  $("pop-radio").replaceChildren(ic("radio"), set.radio ? "Turn autoplay radio off" : "Turn autoplay radio on");
  const label = { 0.25: "¼", 0.5: "½", 0.75: "¾", 1: "All" };
  $("pop-votes").replaceChildren(...set.vote_fractions.map((f) => el("button", { class: "chip", ariaPressed: String(f === set.vote_fraction),
    title: `${f * 100}% of active listeners (pocket listeners excluded)`, textContent: label[f] || f,
    onclick: (e) => { e.stopPropagation(); hostPost(`/api/host/settings?vote_fraction=${f}`); } })));
  $("pop-export").replaceChildren(ic("save"), `Save session as playlist (${state.host.session_tracks})`);
  $("pop-export").disabled = !state.host.session_tracks;
}
$("settings").innerHTML = icon("sliders");
$("settings").addEventListener("click", (e) => {
  e.stopPropagation();
  const pop = $("settings-pop"), open = pop.classList.toggle("hidden");
  $("settings").setAttribute("aria-expanded", String(!open));
  renderSettings();
});
$("settings-pop").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => { $("settings-pop").classList.add("hidden"); $("settings").setAttribute("aria-expanded", "false"); closeMenus(); });
$("pop-radio").addEventListener("click", toggleRadio);
$("pop-library").addEventListener("click", () => {
  const on = !state.host.settings.library;
  if (on && !confirm("Let guests browse your YouTube Music playlists and liked songs for this room? They can see titles and queue from them. It turns off again with a new server start.")) return;
  hostPost(`/api/host/settings?library=${on}`);
});
$("pop-health").replaceChildren(ic("pulse"), "Check YouTube link now");
$("pop-health").addEventListener("click", async () => {
  $("settings-pop").classList.add("hidden");
  toast("Checking with YouTube…");
  const r = await hostPost("/api/host/health");
  if (r.ok) { const h = await r.json(); toast(`YouTube link: ${(HEALTH[h.state] || [h.state])[0]}`); }
});
$("pop-export").addEventListener("click", async () => {
  $("settings-pop").classList.add("hidden");
  const n = state.host.session_tracks, day = new Date().toLocaleDateString("en-CA");
  if (!confirm(`Create a private playlist “cesura — ${day}” in your YouTube Music library with the ${n} song${n === 1 ? "" : "s"} played this session (in order, no repeats)?`)) return;
  toast("Saving to YouTube Music…");
  const r = await hostPost("/api/host/export", { date: day });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) return toast(d.detail || "Couldn't save the playlist.");
  $("export-sub").textContent = `“${d.title}” · ${d.count} songs · private`;
  $("export-link").replaceChildren(ic("external"), d.url);
  $("export-link").href = d.url;
  openSheet("export-sheet");
});

// ---- Relink YouTube: the same upload + probe flow as setup, authorised by the host key
function openRelink() {
  $("relink-modal").classList.remove("hidden");
  mountLinker($("relink-linker"), { headers: () => ({ "X-Host-Key": key }), onChange: (res) => {
    if (res && res.account) toast(res.premium ? "YouTube linked · Premium" : "YouTube linked");
  } });
  $("relink-close").focus();
}
const closeRelink = () => $("relink-modal").classList.add("hidden");
$("relink-open").addEventListener("click", openRelink);
$("pop-relink").addEventListener("click", openRelink);
$("relink-close").innerHTML = icon("x");
$("relink-close").addEventListener("click", closeRelink);
$("relink-modal").addEventListener("click", (e) => e.target.id === "relink-modal" && closeRelink());
document.addEventListener("keydown", (e) => e.key === "Escape" && closeRelink());
$("pop-relink").replaceChildren(ic("upload"), "Relink YouTube");
$("pop-guest").replaceChildren(ic("external"), "Open guest view");
$("add-ic").innerHTML = icon("search");
$("radio-ic").innerHTML = icon("radio");
$("skip").innerHTML = icon("skip");
document.querySelectorAll("[data-close]").forEach((b) => (b.innerHTML = icon("x")));

// search-to-add dropdown
$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("add-q").value.trim(), box = $("add-results");
  if (!q || !state) return;
  box.classList.remove("hidden");
  box.replaceChildren(skeletonRows(4));
  const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&kind=all&code=${state.code}`);
  if (!r.ok) return box.replaceChildren(emptyState("search", "Search failed", "Try again in a moment."));
  const d = await r.json();
  if (d.open) return addCollection("playlist", d.open.id, "linked playlist");
  box.replaceChildren(...(d.results.length ? d.results.map((x) => el("div", { class: "trow" },
    x.kind === "song" ? art(x) : x.thumb ? el("img", { class: "art", src: artUrl(x.thumb), alt: "", loading: "lazy" }) : el("div", { class: "art art-ph" }),
    el("div", { class: "grow" }, el("div", { class: "t ellip", textContent: x.title }),
      el("div", { class: "s ellip", textContent: x.kind === "song" ? [x.artists, x.duration && fmt(x.duration)].filter(Boolean).join(" · ") : `${x.kind === "album" ? "Album" : "Playlist"} · ${x.subtitle}` })),
    el("div", { class: "acts" }, x.kind === "song"
      ? el("button", { class: "iconbtn", html: icon("plus"), ariaLabel: `Add ${x.title}`, onclick: () => addIds([x.videoId], x.title) })
      : el("button", { class: "btn", textContent: "Add all", onclick: () => addCollection(x.kind, x.id, x.title) })))) : [emptyState("search", `No results for “${q}”`)]));
});
async function addCollection(kind, id, title) {
  const r = await fetch(`/api/collection?kind=${kind}&id=${encodeURIComponent(id)}&code=${state.code}`);
  if (!r.ok) return toast(`Couldn't open that ${kind}.`);
  const c = await r.json();
  await addIds(c.tracks.map((t) => t.videoId).slice(0, 50), c.title || title);
}
document.addEventListener("click", (e) => { if (!e.target.closest(".addbox")) $("add-results").classList.add("hidden"); });
$("add-q").addEventListener("focus", () => $("add-results").children.length && $("add-results").classList.remove("hidden"));

// Seek: drag or click the bar, or arrow keys on it. The server moves the clock; everyone follows.
const seeker = seekBar($("np-bar"), {
  duration: () => (state && state.now && state.now.duration) || 0,
  position: () => posOf(state),
  enabled: () => !!(state && state.now && !["loading", "idle", "unlinked"].includes(state.status)),
  onSeek: async (pos) => {
    const r = await roomPost("/api/seek", { qid: state.now.qid, position: pos });
    if (!r.ok) { seeker.preview = null; toast((await r.json().catch(() => ({}))).detail || "Couldn't jump there."); }
  },
});

// progress + session timer
function tick() {
  if (!state) return;
  const n = state.now, real = posOf(state), d = n ? n.duration || 0 : 0;
  seeker.settle(real);
  const p = seeker.preview ?? real;
  seeker.show(p, d);
  setBar($("np-bar"), d ? p / d : 0);
  $("np-pos").textContent = fmt(p);
  $("np-dur").textContent = fmt(d);
  const secs = serverNow() - state.host.stats.session_start;
  $("p-session").textContent = `${Math.floor(secs / 3600)}:${String(Math.floor((secs % 3600) / 60)).padStart(2, "0")}:${String(Math.floor(secs % 60)).padStart(2, "0")}`;
}
setInterval(tick, 500);
$("np-bar").addEventListener("pointermove", () => seeker.preview != null && tick());
$("np-bar").addEventListener("keydown", () => setTimeout(tick));

function askForKey(msg = "") {
  document.querySelector(".host main").classList.add("hidden");
  $("keygate").classList.remove("hidden");
  $("keygate-err").textContent = msg;
  $("keygate-in").focus();
}
$("keygate").addEventListener("submit", (e) => {
  e.preventDefault();
  const k = $("keygate-in").value.trim();
  if (!k) return;
  localStorage.setItem("cesura.hostkey", k);
  location.replace("/host");
});

if (key) connect();
else askForKey();
