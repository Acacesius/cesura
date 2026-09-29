let code = null;
let state = null;
let votedQid = null;
const cid = localStorage.getItem("cesura.cid") || crypto.getRandomValues(new Uint32Array(4)).join("-");
localStorage.setItem("cesura.cid", cid);
let myName = localStorage.getItem("cesura.name") || "";

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, ...body }) } : {});
  if (r.status === 403) { leave("That room code is no longer valid."); throw new Error("bad code"); }
  if (r.status === 429 && path !== "/api/add") { leave("Too many wrong codes from this network. Try again in 10 minutes."); throw new Error("locked"); }
  return r;
}

// ---- join: five code boxes (auto-advance, backspace, paste fills all) + name
const boxes = [];
for (let i = 0; i < 5; i++) {
  const b = el("input", { maxLength: 1, inputMode: "text", autocapitalize: "characters", spellcheck: false, ariaLabel: `Room code letter ${i + 1}` });
  b.addEventListener("input", () => {
    const v = b.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (v.length > 1) return fillCode(v, i);
    b.value = v;
    if (v && i < 4) boxes[i + 1].focus();
  });
  b.addEventListener("keydown", (e) => {
    if (e.key === "Backspace" && !b.value && i > 0) { boxes[i - 1].focus(); boxes[i - 1].value = ""; e.preventDefault(); }
    else if (e.key === "ArrowLeft" && i > 0) boxes[i - 1].focus();
    else if (e.key === "ArrowRight" && i < 4) boxes[i + 1].focus();
  });
  b.addEventListener("paste", (e) => { e.preventDefault(); fillCode((e.clipboardData.getData("text") || "").toUpperCase().replace(/[^A-Z0-9]/g, ""), 0); });
  b.addEventListener("focus", () => b.select());
  boxes.push(b);
  $("codeboxes").append(b);
}
function fillCode(v, from = 0) {
  [...v].slice(0, 5 - from).forEach((c, j) => (boxes[from + j].value = c));
  boxes[Math.min(4, from + v.length)].focus();
}
const typedCode = () => boxes.map((b) => b.value).join("");
$("name-input").value = myName;
$("join-arrow").innerHTML = icon("arrow");
$("note-1").innerHTML = icon("radio");
$("note-2").innerHTML = icon("phone");

$("join").addEventListener("submit", (e) => {
  e.preventDefault();
  myName = $("name-input").value.trim().slice(0, 24);
  localStorage.setItem("cesura.name", myName);
  unlockAudio(); // this tap is the user gesture mobile browsers need before audio can play
  join(typedCode());
});

async function join(c) {
  c = c.trim().toUpperCase();
  if (c.length !== 5) { $("join-err").textContent = "Enter all 5 letters of the code."; return; }
  const r = await fetch("/api/join", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: c }) });
  if (!r.ok) { $("join-err").textContent = r.status === 429 ? "Too many wrong codes. Try again in 10 minutes." : "No room with that code."; return; }
  code = c;
  localStorage.setItem("cesura.code", code);
  history.replaceState(null, "", "/");
  $("join").classList.add("hidden");
  $("room").classList.remove("hidden");
  document.querySelectorAll("[data-code]").forEach((n) => (n.textContent = code));
  connect();
}

function leave(msg) {
  code = null;
  localStorage.removeItem("cesura.code");
  setListening(false);
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  $("room").classList.add("hidden");
  $("join").classList.remove("hidden");
  $("join-err").textContent = msg || "";
}

// ---- live state over websocket
let ws = null;
const send = (m) => ws && ws.readyState === 1 && ws.send(JSON.stringify(m));

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?code=${encodeURIComponent(code)}&cid=${encodeURIComponent(cid)}`);
  ws.onopen = () => {
    bestRtt = Infinity;
    for (let i = 0; i < 5; i++) setTimeout(ping, i * 300);
    send({ type: "hello", name: myName });
    if (listening) send({ type: "listen", on: true });
  };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "state") render(m);
    else if (m.type === "pong") onPong(m);
  };
  ws.onclose = (e) => {
    ws = null;
    if (e.code === 4001) return leave("The host started a new session. Get the new code from the host's screen.");
    if (e.code === 4029) return leave("Too many wrong codes. Try again in 10 minutes.");
    setTimeout(() => code && connect(), 2000);
  };
}

// ---- clock sync
// Offset = server clock - our clock. Each ping gives server time at (roughly)
// the midpoint of the round trip; the lowest-RTT sample is the most accurate.
let bestRtt = Infinity;
function ping() { send({ type: "ping", t: Date.now() / 1000 }); }
function onPong(m) {
  const now = Date.now() / 1000, rtt = now - m.t;
  if (rtt <= bestRtt) {
    bestRtt = rtt;
    clockOffset = m.server - (m.t + rtt / 2);
  }
}
setInterval(() => { bestRtt *= 1.2; ping(); }, 20000); // let fresher samples win over time

// ---- views
function show(tab) {
  document.querySelectorAll("[data-view]").forEach((v) => v.classList.toggle("hidden", v.dataset.view !== tab));
  document.querySelectorAll("#room > header").forEach((h) => h.classList.toggle("hidden", h.dataset.for !== tab));
  document.querySelectorAll(".tabbar button").forEach((b) => (b.dataset.tab === tab ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current")));
  if (tab === "search" && !$("q").value) $("q").focus();
}
document.querySelectorAll(".tabbar button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
document.querySelectorAll("[data-ic]").forEach((n) => (n.innerHTML = icon(n.dataset.ic)));

const queuedIds = () => new Set([...(state ? state.queue : []), ...(state && state.now ? [state.now] : [])].map((t) => t.videoId));

function render(s) {
  if (!state) clockOffset = s.server_time - Date.now() / 1000; // until the first pong
  const trackChanged = !state || (state.now && state.now.qid) !== (s.now && s.now.qid);
  state = s;
  document.querySelectorAll("[data-people]").forEach((n) => n.replaceChildren(avatarStack(s.people)));
  $("notice").textContent = s.notice || "";
  $("notice").classList.toggle("hidden", !s.notice);

  // Now playing
  const n = s.now;
  if (trackChanged) {
    $("np-art").replaceChildren(n ? art(n, "") : el("div", { class: "art-ph" }));
    updateMediaSession(s);
  }
  $("np-title").textContent = n ? n.title : "Nothing playing yet";
  $("np-sub").textContent = n ? [n.artists, n.mine ? "added by you" : n.by ? `added by ${n.by}` : n.auto ? "from radio" : null].filter(Boolean).join(" · ") : "Add a song to get the room going";
  $("pause").disabled = !n || s.status === "loading";
  $("pause").innerHTML = icon(s.status === "paused" ? "play" : "pause");
  $("pause").setAttribute("aria-label", s.status === "paused" ? "Resume for everyone" : "Pause for everyone");
  $("skip").disabled = !n || votedQid === (n && n.qid);
  $("skip").replaceChildren(...[ic("skip"), s.votes ? el("span", { class: "count", textContent: `${s.votes}/${s.votes_needed}` }) : null].filter(Boolean));
  $("skip").setAttribute("aria-label", s.votes ? `Vote to skip, ${s.votes} of ${s.votes_needed}` : "Vote to skip");
  const nx = s.queue[0] || s.auto[0];
  $("upnext").classList.toggle("hidden", !nx);
  if (nx) $("upnext").replaceChildren(art(nx), el("div", { class: "grow" },
    el("div", { class: "k ellip", textContent: `Up next · ${nx.mine ? "you" : nx.by || (nx.auto ? "radio" : "")}` }),
    el("div", { class: "t ellip", textContent: nx.title }), el("div", { class: "s ellip", textContent: nx.artists })), ic("chevron"));

  // Queue
  $("q-count").replaceChildren(ic("people"), ` ${s.listeners}`);
  $("q-now").classList.toggle("hidden", !n);
  if (n) {
    $("q-now").replaceChildren(
      el("div", { class: "row" }, art(n), el("div", { class: "grow" }, el("div", { class: "k", textContent: s.status === "paused" ? "PAUSED" : "NOW PLAYING" }),
        el("div", { class: "t ellip", textContent: n.title }), el("div", { class: "s ellip", textContent: n.artists })),
        el("span", { class: "mono faint", style: "font-size:12px", id: "q-now-time" })),
      el("div", { class: "thin" }, el("div", { id: "q-now-bar" })));
  }
  $("q-head").textContent = s.queue.length ? `Up next · ${s.queue.length} song${s.queue.length > 1 ? "s" : ""} · ${totalMin(s.queue)} min` : "Up next";
  $("queue").replaceChildren(...(s.queue.length ? s.queue.map((t, i) => trow(t, {
    tag: i === 0 ? el("span", { class: "tag", textContent: "NEXT" }) : null,
    right: t.mine ? el("button", { class: "iconbtn", html: icon("x"), ariaLabel: `Remove ${t.title}`, onclick: () => removeTrack(t) }) : avatar(t.by || (t.auto ? "Radio" : "?")),
  })) : [el("div", { class: "empty", textContent: "Nothing queued. Search for something, or let the radio pick." })]));
  $("radio-block").classList.toggle("hidden", !s.radio && !s.auto.length);
  $("radio-sub").textContent = s.radio ? "after the queue" : "off";
  $("auto").replaceChildren(...s.auto.slice(0, 6).map((t) => trow(t, { weight: 500, right: addBtn(t) })));
  $("played-block").classList.toggle("hidden", !s.played.length);
  $("played").replaceChildren(...s.played.slice(0, 8).map((t) => trow(t, { weight: 500, right: addBtn(t, "replay") })));

  refreshQueuedMarks();
  steer();
}

function trow(t, { tag = null, right = null, weight = 600, extra = null } = {}) {
  return el("div", { class: "trow" }, art(t),
    el("div", { class: "grow" },
      el("div", { style: "display:flex;align-items:center;gap:8px;min-width:0" }, el("span", { class: "t ellip", style: `font-weight:${weight}`, textContent: t.title }), tag),
      el("div", { class: "s ellip", textContent: [t.artists, t.duration && fmt(t.duration)].filter(Boolean).join(" · ") })),
    el("div", { class: "acts" }, extra, right));
}

function addBtn(t, iconName = "plus", opts = {}) {
  return el("button", { class: "iconbtn", html: icon(iconName), ariaLabel: `Add ${t.title} to queue`, onclick: (e) => addTracks([t.videoId], { btn: e.currentTarget, label: t.title, ...opts }) });
}
function nextBtn(t) {
  return el("button", { class: "iconbtn bare", html: icon("next"), ariaLabel: `Play ${t.title} next`, title: "Play next", onclick: (e) => addTracks([t.videoId], { btn: e.currentTarget, label: t.title, next: true }) });
}
function queuedChip() { return el("span", { class: "queued", html: icon("check") + "QUEUED" }); }

// Search / collection rows show QUEUED for anything already in the queue.
function refreshQueuedMarks() {
  const ids = queuedIds();
  document.querySelectorAll("[data-vid]").forEach((row) => {
    const acts = row.querySelector(".acts");
    const want = ids.has(row.dataset.vid);
    if (want && !acts.querySelector(".queued")) acts.replaceChildren(queuedChip());
    else if (!want && acts.querySelector(".queued")) acts.replaceChildren(...songActs(row._track));
  });
}
function songActs(t) { return [nextBtn(t), addBtn(t)]; }
function songRow(t) {
  const row = trow(t, { right: null });
  row.dataset.vid = t.videoId;
  row._track = t;
  row.querySelector(".acts").replaceChildren(...(queuedIds().has(t.videoId) ? [queuedChip()] : songActs(t)));
  return row;
}

// Progress + times, every half second
function tick() {
  if (!state) return;
  const n = state.now, p = posOf(state), d = n ? n.duration || 0 : 0;
  setBar($("np-bar"), d ? p / d : 0);
  $("np-pos").textContent = fmt(p);
  $("np-left").textContent = `-${fmt(Math.max(0, d - p))}`;
  if ($("q-now-bar")) { $("q-now-bar").style.width = `${d ? (p / d) * 100 : 0}%`; $("q-now-time").textContent = `${fmt(p)} / ${fmt(d)}`; }
  renderSync();
}
setInterval(tick, 500);

// ---- local playback, steered to the server clock
const audio = $("audio");
let listening = false;
let loadedQid = null;
let holdUntil = 0; // after a seek, give the browser time to buffer before judging drift
let lastDrift = null, lastCatchingUp = false, driftSentAt = 0;

function silentWav() {
  // 50 ms of silence; played inside a tap to unlock audio on mobile.
  const n = 400, b = new DataView(new ArrayBuffer(44 + n));
  const s = (o, t) => [...t].forEach((c, i) => b.setUint8(o + i, c.charCodeAt(0)));
  s(0, "RIFF"); b.setUint32(4, 36 + n, true); s(8, "WAVEfmt "); b.setUint32(16, 16, true);
  b.setUint16(20, 1, true); b.setUint16(22, 1, true); b.setUint32(24, 8000, true); b.setUint32(28, 8000, true);
  b.setUint16(32, 1, true); b.setUint16(34, 8, true); s(36, "data"); b.setUint32(40, n, true);
  for (let i = 0; i < n; i++) b.setUint8(44 + i, 128);
  return URL.createObjectURL(new Blob([b], { type: "audio/wav" }));
}

function unlockAudio() {
  // play() must be called inside a tap for mobile browsers to allow audio later.
  if (!audio.src) audio.src = silentWav();
  audio.play().catch(() => {});
  setListening(true);
  requestWakeLock();
}
function setListening(on) {
  listening = on;
  if (!on) { audio.pause(); lastDrift = null; }
  send({ type: "listen", on });
}
$("unlock").addEventListener("click", () => { unlockAudio(); steer(); });

function steer() {
  const s = state;
  if (!listening || !s) return;
  if (!s.now) {
    loadedQid = null;
    audio.pause();
    return;
  }
  if (s.now.qid !== loadedQid && s.status !== "loading") {
    loadedQid = s.now.qid;
    audio.src = `/stream/${s.now.videoId}?code=${encodeURIComponent(code)}`;
    holdUntil = 0;
  }
  if (loadedQid !== s.now.qid) return;
  const t = trackPos(s);
  if (s.status !== "playing") {
    audio.pause();
    audio.playbackRate = 1;
    if (audio.readyState >= 1 && Math.abs(audio.currentTime - s.position) > 0.5) audio.currentTime = s.position;
    return;
  }
  if (audio.readyState < 1) return; // metadata not loaded yet, can't seek
  // Clock hasn't reached where we're parked (track head start, or just resumed): wait.
  if (audio.paused && t < audio.currentTime && audio.currentTime - t < 3) return;
  const drift = audio.currentTime - t;
  if (performance.now() > holdUntil) {
    if (Math.abs(drift) > 1) {
      audio.currentTime = Math.max(0, t + 0.2);
      audio.playbackRate = 1;
      holdUntil = performance.now() + 2500;
    } else if (Math.abs(drift) > 0.06) {
      audio.playbackRate = drift > 0 ? 0.95 : 1.05; // inaudible nudge, pitch preserved
    } else {
      audio.playbackRate = 1;
    }
  }
  if (audio.paused) {
    // Starting from a standstill: land exactly on the clock, allowing for play() startup latency.
    if (Math.abs(audio.currentTime - t) > 0.05) audio.currentTime = Math.max(0, t + 0.15);
    audio.play().catch((err) => {
      if (err.name === "NotAllowedError") { setListening(false); renderSync(); }
    });
  }
  reportDrift(drift);
}
setInterval(steer, 500);
audio.addEventListener("error", () => {
  if (listening && audio.src.includes("/stream/")) loadedQid = null; // retry on next tick
});

// Report the measured drift (display only; the sync loop above is unchanged).
function reportDrift(drift) {
  if (audio.paused) return;
  lastDrift = drift * 1000;
  lastCatchingUp = performance.now() < holdUntil || Math.abs(drift) > 0.06;
  if (performance.now() - driftSentAt > 2000) {
    driftSentAt = performance.now();
    send({ type: "drift", ms: Math.round(lastDrift * 10) / 10, catching_up: lastCatchingUp });
  }
}

function renderSync() {
  const s = state, pill = $("sync");
  $("unlock").classList.toggle("hidden", listening);
  pill.classList.toggle("hidden", !listening);
  if (!listening || !s) return;
  let dot = "", text;
  if (!s.now) text = "Nothing playing";
  else if (s.status === "loading") { dot = "warn"; text = "Loading the next song"; }
  else if (s.status === "paused") { dot = "warn"; text = "Paused for everyone"; }
  else if (lastDrift == null) { dot = "warn"; text = "Syncing…"; }
  else if (lastCatchingUp) { dot = "warn"; text = `Catching up · ${Math.round(Math.abs(lastDrift))} ms`; }
  else text = `In phase · ${Math.round(Math.abs(lastDrift))} ms · pause affects everyone`;
  pill.replaceChildren(el("span", { class: `dot ${dot}` }), text);
}

let wakeLock = null;
async function requestWakeLock() {
  try { if ("wakeLock" in navigator && !wakeLock) wakeLock = await navigator.wakeLock.request("screen"); } catch {}
  if (wakeLock) wakeLock.addEventListener("release", () => (wakeLock = null), { once: true });
}
document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && listening && requestWakeLock());

// ---- volume (this device only). iOS ignores audio.volume, so the row is hidden there.
const vol = $("vol");
function applyVolume(v, muted) {
  audio.volume = v / 100;
  audio.muted = muted;
  vol.value = v;
  vol.style.setProperty("--fill", `${v}%`);
  $("mute").innerHTML = icon(muted || v == 0 ? "mute" : "vol");
  $("mute").setAttribute("aria-label", muted ? "Unmute" : "Mute");
  try { localStorage.setItem("cesura.vol", JSON.stringify({ v, muted })); } catch {}
}
{
  let saved = { v: 80, muted: false };
  try { saved = { ...saved, ...JSON.parse(localStorage.getItem("cesura.vol") || "{}") }; } catch {}
  audio.volume = 0.5;
  if (audio.volume !== 0.5) $("volume").classList.add("hidden");
  applyVolume(saved.v, saved.muted);
}
vol.addEventListener("input", () => applyVolume(+vol.value, false));
$("mute").addEventListener("click", () => applyVolume(+vol.value, !audio.muted));

// ---- lock screen / notification controls
function updateMediaSession(s) {
  if (!("mediaSession" in navigator)) return;
  const n = s.now;
  navigator.mediaSession.metadata = n ? new MediaMetadata({
    title: n.title, artist: n.artists, album: n.album || "cesura",
    artwork: n.thumb ? [{ src: n.thumb, sizes: "400x400" }] : [],
  }) : null;
}
if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("play", () => (listening ? setPaused(false) : unlockAudio()));
  navigator.mediaSession.setActionHandler("pause", () => setPaused(true));
  navigator.mediaSession.setActionHandler("nexttrack", () => voteSkip());
}

// ---- room controls
async function setPaused(paused) {
  if (!state || !state.now) return;
  await api("/api/pause", { paused });
}
$("pause").addEventListener("click", () => setPaused(state && state.status !== "paused"));

async function voteSkip() {
  if (!state || !state.now || votedQid === state.now.qid) return;
  votedQid = state.now.qid;
  $("skip").disabled = true;
  await api("/api/skip", { qid: state.now.qid, cid });
}
$("skip").addEventListener("click", voteSkip);
$("to-queue").innerHTML = icon("queue");
$("to-queue").addEventListener("click", () => show("queue"));
$("upnext").addEventListener("click", () => show("queue"));
$("q-now").addEventListener("click", () => show("playing"));
$("q-search").replaceChildren(ic("search"), "Add songs, albums, playlists");
$("q-search").addEventListener("click", () => show("search"));
$("radio-ic").innerHTML = icon("radio");

async function addTracks(videoIds, { next = false, btn = null, label = "" } = {}) {
  if (btn) btn.disabled = true;
  const r = await api("/api/add", { videoIds, cid, name: myName, next });
  if (r.ok) {
    const { added } = await r.json();
    toast(added > 1 ? `Added ${added} songs` : next ? `${label || "Song"} plays next` : `Added ${label || "song"}`);
    return true;
  }
  if (btn) btn.disabled = false;
  toast(r.status === 429 ? "Slow down a little. Try again in a minute." : r.status === 409 ? "The queue is full." : "Couldn't add that one.");
  return false;
}

async function removeTrack(t) {
  const r = await api("/api/remove", { qid: t.qid, cid });
  if (r.ok) toast(`Removed ${t.title}`);
}

// ---- search: all / songs / albums / playlists, or a pasted link
let kind = "all";
$("s-ic").innerHTML = icon("search");
$("q-clear").innerHTML = icon("x");
$("q").addEventListener("input", () => $("q-clear").classList.toggle("hidden", !$("q").value));
$("q-clear").addEventListener("click", () => { $("q").value = ""; $("q-clear").classList.add("hidden"); $("results").replaceChildren(); showCollection(false); $("q").focus(); });
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
  kind = c.dataset.kind;
  document.querySelectorAll(".chip").forEach((x) => x.setAttribute("aria-pressed", String(x === c)));
  if ($("q").value.trim()) runSearch();
}));
$("search-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });

const info = (text) => el("div", { class: "empty", textContent: text });

async function runSearch() {
  const q = $("q").value.trim();
  if (!q) return;
  $("q").blur(); // drop the phone keyboard so results are visible
  showCollection(false);
  $("results").replaceChildren(info("Searching…"));
  const r = await api(`/api/search?q=${encodeURIComponent(q)}&kind=${kind}&code=${encodeURIComponent(code)}`);
  if (!r.ok) return $("results").replaceChildren(info("Search failed. Try again."));
  const data = await r.json();
  if (data.open) return openCollection(data.open.kind, data.open.id);
  if (!data.results.length) return $("results").replaceChildren(info("No results."));
  const songs = data.results.filter((x) => x.kind === "song");
  const albums = data.results.filter((x) => x.kind === "album");
  const lists = data.results.filter((x) => x.kind === "playlist");
  const out = [];
  if (songs.length) out.push(el("div", { class: "sechead", textContent: "SONGS" }), el("div", { class: "rows" }, ...songs.slice(0, kind === "all" ? 6 : 20).map(songRow)));
  for (const [label, items] of [["ALBUMS", albums], ["PLAYLISTS", lists]]) {
    if (!items.length) continue;
    out.push(el("div", { class: "sechead", textContent: label }), el("div", { class: "cards" }, ...items.slice(0, kind === "all" ? 4 : 20).map((a) =>
      el("button", { class: "acard", onclick: () => openCollection(a.kind, a.id) },
        a.thumb ? el("img", { class: "cover", src: a.thumb, alt: "", loading: "lazy" }) : el("div", { class: "cover art-ph" }),
        el("div", { class: "t ellip", textContent: a.title }), el("div", { class: "s ellip", textContent: a.subtitle })))));
  }
  $("results").replaceChildren(...out);
}

function showCollection(on) {
  $("s-coll-view").classList.toggle("hidden", !on);
  $("s-results-view").classList.toggle("hidden", on);
}
$("coll-back").replaceChildren(ic("back"), "Back");
$("coll-back").addEventListener("click", () => showCollection(false));

async function openCollection(k, id) {
  show("search");
  showCollection(true);
  $("coll-title").textContent = "Loading…";
  $("coll-sub").textContent = "";
  $("coll-art").removeAttribute("src");
  $("coll-add").classList.add("hidden");
  $("coll-tracks").replaceChildren();
  const r = await api(`/api/collection?kind=${k}&id=${encodeURIComponent(id)}&code=${encodeURIComponent(code)}`);
  if (!r.ok) { $("coll-title").textContent = `Couldn't open that ${k}.`; return; }
  const c = await r.json();
  $("coll-title").textContent = c.title;
  $("coll-sub").textContent = c.subtitle;
  if (c.thumb) $("coll-art").src = c.thumb;
  const ids = c.tracks.map((t) => t.videoId).slice(0, 50);
  const addAll = $("coll-add");
  addAll.classList.toggle("hidden", !ids.length);
  addAll.disabled = false;
  addAll.replaceChildren(ic("plus"), c.tracks.length > 50 ? "Add first 50 songs" : ids.length === 1 ? "Add 1 song" : `Add all ${ids.length} songs`);
  addAll.onclick = async () => { if (await addTracks(ids)) { addAll.disabled = true; addAll.replaceChildren(ic("check"), "Added to the queue"); } };
  $("coll-tracks").replaceChildren(...(c.tracks.length ? c.tracks.map(songRow) : [info("This one's empty.")]));
}

// ---- boot: ?code= from the QR link wins, then a remembered code
const initial = (new URLSearchParams(location.search).get("code") || localStorage.getItem("cesura.code") || "").toUpperCase();
if (initial) fillCode(initial);
if (initial && myName) join(initial); // returning guest: straight in (audio needs one tap: the sync pill)
else (initial ? $("name-input") : boxes[0]).focus();
