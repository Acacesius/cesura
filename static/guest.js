let code = null;
let state = null;
// Device identity: made up once, kept in localStorage, sent on every connect. The server hands
// this device a session token on connect; the token authorises audio and actions.
const newDevice = () => "d-" + [...crypto.getRandomValues(new Uint8Array(12))].map((b) => b.toString(16).padStart(2, "0")).join("");
let device = localStorage.getItem("cesura.cid");
if (!device || !/^[A-Za-z0-9_-]{8,64}$/.test(device)) { device = newDevice(); localStorage.setItem("cesura.cid", device); }
let token = localStorage.getItem("cesura.token") || "";
let myName = localStorage.getItem("cesura.name") || "";
const setToken = (t) => { token = t || ""; t ? localStorage.setItem("cesura.token", t) : localStorage.removeItem("cesura.token"); };

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, token, ...body }) } : {});
  if (r.status === 403 && !body) { leave("That room code is no longer valid."); throw new Error("bad code"); }
  if (r.status === 403 && body) {
    const d = await r.clone().json().catch(() => ({}));
    if (d.detail === "bad room code") { leave("That room code is no longer valid."); throw new Error("bad code"); }
  }
  if (r.status === 401 && live) live.reconnect(); // session expired: get a fresh token
  if (r.status === 429 && path === "/api/join") { leave("Too many wrong codes from this network. Try again in 10 minutes."); throw new Error("locked"); }
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
  const r = await fetch("/api/join", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: c, device }) });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    $("join-err").textContent = r.status === 429 ? "Too many wrong codes. Try again in 10 minutes." : r.status === 409 ? d.detail : "No room with that code.";
    return;
  }
  setCode(c);
  history.replaceState(null, "", "/");
  $("join").classList.add("hidden");
  $("room").classList.remove("hidden");
  connect();
}
function setCode(c) {
  code = c;
  localStorage.setItem("cesura.code", code);
  document.querySelectorAll("[data-code]").forEach((n) => (n.textContent = code));
}

function leave(msg) {
  code = null;
  localStorage.removeItem("cesura.code");
  setPocket(false, { quiet: true });
  setListening(false);
  // Forget the room: a stale state must never drive audio requests without a code.
  state = null;
  loadedQid = null;
  audio.removeAttribute("src");
  if (live) { live.stop(); live = null; }
  $("room").classList.add("hidden");
  $("join").classList.remove("hidden");
  $("join-err").textContent = msg || "";
}

// ---- live state over websocket (reconnects with backoff and shows a banner meanwhile)
let live = null;
const send = (m) => live && live.send(m);
const LEAVE = {
  4001: "The host started a new session. Get the new code from the host's screen.",
  4004: "The host removed you from the room. You can rejoin with the code.",
  4005: "The host removed you from this room.",
  4006: "This room is locked. Ask the host to unlock it.",
  4029: "Too many wrong codes. Try again in 10 minutes.",
};

function connect() {
  if (live) live.stop();
  live = liveSocket({
    banner: $("conn"),
    open: () => new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?code=${encodeURIComponent(code)}&device=${encodeURIComponent(device)}&token=${encodeURIComponent(token)}`),
    onOpen: () => {
      bestRtt = Infinity;
      for (let i = 0; i < 5; i++) setTimeout(ping, i * 300);
      send({ type: "hello", name: myName });
      if (listening) send({ type: "listen", on: true });
      if (pocket) send({ type: "pocket", on: true });
    },
    onMessage: (m) => {
      if (m.type === "state" || m.type === "patch") {
        const s = applyPatch(state, m);
        if (s) render(s); else send({ type: "resync" });
      }
      else if (m.type === "pong") onPong(m);
      else if (m.type === "hb") send({ type: "hb" });
      else if (m.type === "welcome") { setToken(m.token); if (m.code !== code) setCode(m.code); }
      else if (m.type === "code") onNewCode(m.code);
    },
    onClose: (e) => {
      if (e.code === 4009) { device = newDevice(); localStorage.setItem("cesura.cid", device); setToken(""); return false; }
      if (LEAVE[e.code]) {
        if (e.code !== 4029) setToken("");
        leave(LEAVE[e.code]);
        return true;
      }
      return false;
    },
  });
}

// The host changed the room code. We stay in; re-point audio at URLs with the new code.
function onNewCode(c) {
  setCode(c);
  if (!pocket) loadedQid = null; // steer() reloads the stream URL and seeks back to the clock
  toast(`New room code: ${c}`);
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
  if (tab === "library") loadLibrary();
}
document.querySelectorAll(".tabbar button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
document.querySelectorAll("[data-ic]").forEach((n) => (n.innerHTML = icon(n.dataset.ic)));

const queuedIds = () => new Set([...(state ? state.queue : []), ...(state && state.now ? [state.now] : [])].map((t) => t.videoId));

const isMod = () => !!(state && state.me && state.me.role === "cohost");
// Tracks carry an opaque owner id; this device's is in state.me.
const mine = (t) => !!(t && t.oid && state && state.me && t.oid === state.me.oid);

function render(s) {
  if (!state) clockOffset = s.server_time - Date.now() / 1000; // until the first pong
  const prev = state;
  const trackChanged = !prev || (prev.now && prev.now.qid) !== (s.now && s.now.qid);
  const libChanged = !prev || prev.library !== s.library;
  state = s;
  document.querySelectorAll("[data-people]").forEach((n) => n.replaceChildren(avatarStack(s.people)));
  $("notice").textContent = s.notice || "";
  $("notice").classList.toggle("hidden", !s.notice);
  const mod = isMod();

  // Now playing
  const n = s.now;
  if (trackChanged) {
    $("np-art").replaceChildren(n ? art(n, "") : el("div", { class: "art-ph" }));
    if (!$("lyrics-sheet").classList.contains("hidden")) showLyrics();
  }
  setMediaSession(n, mediaHandlers, s.status === "playing");
  $("np-title").textContent = n ? n.title : "Nothing playing yet";
  $("np-sub").textContent = n ? [n.artists, mine(n) ? "added by you" : n.by ? `added by ${n.by}` : n.auto ? "from radio" : null].filter(Boolean).join(" · ") : "Add a song to get the room going";
  $("np-hands").textContent = s.hands ? s.hands.text : "";
  $("np-hands").classList.toggle("hidden", !s.hands);
  $("pause").disabled = !n || s.status === "loading";
  $("pause").innerHTML = icon(s.status === "paused" ? "play" : "pause");
  $("pause").setAttribute("aria-label", s.status === "paused" ? "Resume for everyone" : "Pause for everyone");
  $("skip").disabled = !n || (!mod && s.voted);
  const showVotes = !mod && s.votes;
  $("skip").replaceChildren(...[ic("skip"), showVotes ? el("span", { class: "count", textContent: `${s.votes}/${s.votes_needed}` }) : null].filter(Boolean));
  $("skip").setAttribute("aria-label", mod ? "Skip song" : s.votes ? `Vote to skip, ${s.votes} of ${s.votes_needed}` : `Vote to skip (${s.votes_needed} vote${s.votes_needed > 1 ? "s" : ""} needed)`);
  $("skip").title = mod ? "Skip (co-host)" : `Vote to skip · ${s.votes_needed} needed`;
  $("lyrics-btn").disabled = !n;
  $("pocket").checked = pocket;
  const nx = s.queue[0] || s.auto[0];
  $("upnext").classList.toggle("hidden", !nx);
  if (nx && (!prev || (prev.queue[0] || prev.auto[0]) !== nx || prev.me !== s.me)) $("upnext").replaceChildren(art(nx), el("div", { class: "grow" },
    el("div", { class: "k ellip", textContent: `Up next · ${mine(nx) ? "you" : nx.by || (nx.auto ? "radio" : "")}` }),
    el("div", { class: "t ellip", textContent: nx.title }), el("div", { class: "s ellip", textContent: nx.artists })), ic("chevron"));

  // Queue
  $("q-count").replaceChildren(ic("people"), ` ${s.listeners}`);
  $("q-now").classList.toggle("hidden", !n);
  if (n && (trackChanged || prev.status !== s.status || prev.me !== s.me)) {
    $("q-now").replaceChildren(
      el("div", { class: "row" }, art(n), el("div", { class: "grow" }, el("div", { class: "k", textContent: s.status === "paused" ? "PAUSED" : "NOW PLAYING" }),
        el("div", { class: "t ellip", textContent: n.title }), el("div", { class: "s ellip", textContent: [n.artists, n.by ? `added by ${mine(n) ? "you" : n.by}` : null].filter(Boolean).join(" · ") })),
        el("span", { class: "mono faint", style: "font-size:12px", id: "q-now-time" })),
      el("div", { class: "thin" }, el("div", { id: "q-now-bar" })));
  }
  // The lists below only rebuild when what they show changed (patches keep unchanged parts identical).
  const listsChanged = !prev || prev.queue !== s.queue || prev.auto !== s.auto || prev.played !== s.played || prev.radio !== s.radio || prev.me !== s.me;
  if (listsChanged) renderLists(s, mod);
  if (!$("people-sheet").classList.contains("hidden")) renderPeople();
  if (libChanged && !$("lib-list-view").classList.contains("hidden")) renderHostLib();
  if (listsChanged) refreshQueuedMarks();
  steer();
}

function renderLists(s, mod) {
  $("q-head").textContent = s.queue.length ? `Up next · ${s.queue.length} song${s.queue.length > 1 ? "s" : ""} · ${totalMin(s.queue)} min${mod ? " · swipe to remove, hold to move" : ""}` : "Up next";
  const qrows = s.queue.map((t, i) => {
    const row = trow(t, {
      tag: i === 0 ? el("span", { class: "tag", textContent: "NEXT" }) : t.pinned ? el("span", { class: "tag amber", textContent: "PINNED" }) : null,
      right: mine(t) || mod ? el("button", { class: "iconbtn", html: icon("x"), ariaLabel: `Remove ${t.title}`, onclick: () => removeTrack(t) }) : avatar(t.by || (t.auto ? "Radio" : "?")),
      sub: t.by ? `${mine(t) ? "you" : t.by}` : null,
    });
    row.dataset.qid = t.qid;
    return row;
  });
  $("queue").replaceChildren(...(qrows.length ? qrows : [emptyState("queue", "Nothing queued yet", s.radio ? "Search for something, or let the radio pick." : "Search for a song to get things going.",
    el("button", { class: "btn fill", textContent: "Search", onclick: () => show("search") }))]));
  if (mod && qrows.length) touchQueue(qrows, { onRemove: (qid) => removeTrack(s.queue.find((t) => t.qid === qid)), onMove: moveTrack });
  $("radio-block").classList.toggle("hidden", !s.radio && !s.auto.length);
  $("radio-sub").textContent = s.radio ? "after the queue" : "off";
  $("auto").replaceChildren(...s.auto.slice(0, 6).map((t) => trow(t, { weight: 500, right: addBtn(t) })));
  $("played-block").classList.toggle("hidden", !s.played.length);
  $("played").replaceChildren(...s.played.slice(0, 8).map((t) => trow(t, { weight: 500, right: addBtn(t, "replay") })));
}

function trow(t, { tag = null, right = null, weight = 600, extra = null, sub = null } = {}) {
  return el("div", { class: "trow" }, art(t),
    el("div", { class: "grow" },
      el("div", { style: "display:flex;align-items:center;gap:8px;min-width:0" }, el("span", { class: "t ellip", style: `font-weight:${weight}`, textContent: t.title }), tag),
      el("div", { class: "s ellip", textContent: [t.artists, t.duration && fmt(t.duration), sub].filter(Boolean).join(" · ") })),
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
  if (!listening || !s || pocket || !code) return; // pocket mode: the shared stream, no sync correction
  if (!s.now) {
    loadedQid = null;
    audio.pause();
    return;
  }
  if (s.now.qid !== loadedQid && s.status !== "loading") {
    loadedQid = s.now.qid;
    audio.src = `/stream/${s.now.videoId}?code=${encodeURIComponent(code)}&token=${encodeURIComponent(token)}`;
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
  if (pocket) return pocketRetry();
  if (listening && audio.src.includes("/stream/")) loadedQid = null; // retry on next tick
});

// ---- pocket mode: one shared radio-style stream that keeps playing on a locked phone.
// No sync correction; it trails the room by a second or two.
let pocket = false, pocketTimer = null;
function pocketUrl() { return `/pocket?code=${encodeURIComponent(code)}&token=${encodeURIComponent(token)}`; }
function setPocket(on, { quiet = false } = {}) {
  if (pocket === on) return;
  pocket = on;
  $("pocket").checked = on;
  clearTimeout(pocketTimer);
  send({ type: "pocket", on });
  if (on) {
    if (!listening) { listening = true; send({ type: "listen", on: true }); requestWakeLock(); }
    audio.playbackRate = 1;
    audio.src = pocketUrl();
    audio.play().catch(() => {});
    loadedQid = null;
    lastDrift = null;
  } else {
    audio.pause();
    audio.removeAttribute("src");
    loadedQid = null;
    if (!quiet) steer();
  }
  renderSync();
}
function pocketRetry() {
  clearTimeout(pocketTimer);
  pocketTimer = setTimeout(() => { if (pocket && code) { audio.src = pocketUrl(); audio.play().catch(() => {}); } }, 2000);
}
audio.addEventListener("ended", () => pocket && pocketRetry());
audio.addEventListener("stalled", () => pocket && audio.paused && pocketRetry());
$("pocket").addEventListener("change", (e) => setPocket(e.target.checked));

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
  if (pocket) text = s.status === "paused" ? "Pocket mode · paused for everyone" : "Pocket mode · loosely synced";
  else if (!s.now) text = "Nothing playing";
  else if (s.status === "unlinked") { dot = "warn"; text = "Playback is off: YouTube not linked"; }
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

// ---- lock screen / notification controls: the normal room commands
const mediaHandlers = {
  play: () => (listening ? setPaused(false) : unlockAudio()),
  pause: () => setPaused(true),
  next: () => voteSkip(),
};

// ---- room controls
async function setPaused(paused) {
  if (!state || !state.now) return;
  await api("/api/pause", { paused });
}
$("pause").addEventListener("click", () => setPaused(state && state.status !== "paused"));

async function voteSkip() {
  if (!state || !state.now || (state.voted && !isMod())) return;
  $("skip").disabled = true;
  const r = await api("/api/skip", { qid: state.now.qid });
  if (r.ok && !isMod()) toast(state.votes + 1 >= state.votes_needed ? "Skipping…" : `Vote counted · ${state.votes + 1} of ${state.votes_needed}`);
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
  const r = await api("/api/add", { videoIds, next });
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
  if (!t) return;
  const r = await api("/api/remove", { qid: t.qid });
  if (r.ok) toast(`Removed ${t.title}`);
}
async function moveTrack(qid, index) {
  const r = await api("/api/mod/move", { qid, index });
  if (r.ok) toast("Moved and pinned");
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


async function runSearch() {
  const q = $("q").value.trim();
  if (!q) return;
  $("q").blur(); // drop the phone keyboard so results are visible
  showCollection(false);
  $("results").replaceChildren(skeletonRows(6));
  const r = await api(`/api/search?q=${encodeURIComponent(q)}&kind=${kind}&code=${encodeURIComponent(code)}`);
  if (!r.ok) return $("results").replaceChildren(emptyState("search", "Search failed", "Check your connection and try again."));
  const data = await r.json();
  if (data.open) return openCollection(data.open.kind, data.open.id);
  if (!data.results.length) return $("results").replaceChildren(emptyState("search", `No results for “${q}”`, "Try fewer words, the artist's name, or paste a YouTube link."));
  const songs = data.results.filter((x) => x.kind === "song");
  const albums = data.results.filter((x) => x.kind === "album");
  const lists = data.results.filter((x) => x.kind === "playlist");
  const out = [];
  if (songs.length) out.push(el("div", { class: "sechead", textContent: "SONGS" }), el("div", { class: "rows" }, ...songs.slice(0, kind === "all" ? 6 : 20).map(songRow)));
  for (const [label, items] of [["ALBUMS", albums], ["PLAYLISTS", lists]]) {
    if (!items.length) continue;
    out.push(el("div", { class: "sechead", textContent: label }), el("div", { class: "cards" }, ...items.slice(0, kind === "all" ? 4 : 20).map((a) =>
      el("button", { class: "acard", onclick: () => openCollection(a.kind, a.id) },
        a.thumb ? el("img", { class: "cover", src: artUrl(a.thumb, 240), alt: "", loading: "lazy" }) : el("div", { class: "cover art-ph" }),
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
  $("coll-title").replaceChildren(el("div", { class: "sk sk-t", style: "width:70%;height:18px" }));
  $("coll-sub").replaceChildren(el("div", { class: "sk sk-s", style: "width:40%" }));
  $("coll-art").removeAttribute("src");
  $("coll-add").classList.add("hidden");
  $("coll-tracks").replaceChildren(skeletonRows(6));
  const r = await api(`/api/collection?kind=${k}&id=${encodeURIComponent(id)}&code=${encodeURIComponent(code)}`);
  if (!r.ok) { $("coll-title").textContent = `Couldn't open that ${k}.`; $("coll-sub").textContent = ""; $("coll-tracks").replaceChildren(); return; }
  const c = await r.json();
  $("coll-title").textContent = c.title;
  $("coll-sub").textContent = c.subtitle;
  if (c.thumb) $("coll-art").src = artUrl(c.thumb, 240);
  const ids = c.tracks.map((t) => t.videoId).slice(0, 50);
  const addAll = $("coll-add");
  addAll.classList.toggle("hidden", !ids.length);
  addAll.disabled = false;
  addAll.replaceChildren(ic("plus"), c.tracks.length > 50 ? "Add first 50 songs" : ids.length === 1 ? "Add 1 song" : `Add all ${ids.length} songs`);
  addAll.onclick = async () => { if (await addTracks(ids)) { addAll.disabled = true; addAll.replaceChildren(ic("check"), "Added to the queue"); } };
  $("coll-tracks").replaceChildren(...(c.tracks.length ? c.tracks.map(songRow) : [emptyState("queue", "This one's empty")]));
}

// ---- lyrics (from YouTube Music; cached on the server per song)
const lyricsCache = new Map();
$("lyrics-btn").addEventListener("click", () => { openSheet("lyrics-sheet"); showLyrics(); });
async function showLyrics() {
  const n = state && state.now;
  if (!n) return closeSheet("lyrics-sheet");
  $("lyrics-h").textContent = n.title;
  $("lyrics-sub").textContent = n.artists;
  const body = $("lyrics-body");
  let got = lyricsCache.get(n.videoId);
  if (!got) {
    body.replaceChildren(el("div", { class: "lyr-skel" }, ...[80, 62, 74, 50, 0, 70, 58, 77, 44].map((w) => w ? el("div", { class: "sk sk-t", style: `width:${w}%` }) : el("br"))));
    const r = await api(`/api/lyrics?videoId=${encodeURIComponent(n.videoId)}&code=${encodeURIComponent(code)}`).catch(() => null);
    if (!r || !r.ok) return body.replaceChildren(emptyState("lyrics", "Couldn't load lyrics", "Try again in a moment."));
    got = await r.json();
    lyricsCache.set(n.videoId, got);
    if (!state.now || state.now.videoId !== n.videoId) return;
  }
  body.replaceChildren(...(got.lyrics
    ? [el("div", { class: "lyr", textContent: got.lyrics }), got.source ? el("div", { class: "lyr-src", textContent: got.source }) : null].filter(Boolean)
    : [emptyState("lyrics", "No lyrics for this one", "YouTube Music doesn't have lyrics for this song.")]));
}

// ---- who's here (co-hosts can remove people)
document.querySelectorAll("[data-people]").forEach((b) => b.addEventListener("click", () => { renderPeople(); openSheet("people-sheet"); }));
$("q-count").addEventListener("click", () => { renderPeople(); openSheet("people-sheet"); });
function renderPeople() {
  const s = state;
  if (!s) return;
  const list = $("people-list");
  if (s.roster) {
    const ST = { in_phase: "In phase", catching_up: "Catching up", waiting: "Joining", idle: "Not listening", pocket: "Pocket", away: "Reconnecting" };
    list.replaceChildren(el("div", { class: "faint", style: "font-size:13px;margin-bottom:6px", textContent: "You're a co-host: you can remove people. They can rejoin with the code." }),
      ...s.roster.map((l) => el("div", { class: "prow" }, avatar(l.name),
        el("div", { class: "grow" }, el("div", { class: "n" }, el("span", { class: "ellip", textContent: l.name }),
          l.role === "cohost" ? el("span", { class: "tag amber", textContent: "CO-HOST" }) : null,
          l.sockets > 1 ? el("span", { class: "tag", textContent: `×${l.sockets}` }) : null),
          el("div", { class: "d ellip", textContent: `${l.device} · ${ST[l.status] || l.status}` })),
        l.pid === s.me.pid || l.role === "cohost" ? null
          : el("button", { class: "btn", textContent: "Remove", onclick: async () => {
            if (!confirm(`Remove ${l.name} from the room? They can rejoin with the code.`)) return;
            const r = await api("/api/mod/kick", { pid: l.pid });
            toast(r.ok ? `Removed ${l.name}` : "Couldn't remove them");
          } }))));
  } else {
    list.replaceChildren(...s.people.map((n) => el("div", { class: "prow" }, avatar(n), el("div", { class: "grow n", textContent: n }))),
      el("div", { class: "faint", style: "font-size:13px;margin-top:10px", textContent: `${s.listening} listening now${s.locked ? " · the room is locked" : ""}` }));
  }
}

// ---- library: the host's playlists (when shared) + paste a playlist link
let libLoaded = false, libData = null;
async function loadLibrary(force = false) {
  libCollection(false);
  if (libLoaded && !force) return renderHostLib();
  libLoaded = true;
  renderHostLib();
}
async function renderHostLib() {
  const box = $("host-lib");
  if (!state || !state.library) {
    libData = null;
    return box.replaceChildren(el("div", { class: "sechead", textContent: "HOST'S LIBRARY" }),
      emptyState("library", "The host isn't sharing their library", "Paste a playlist link above, or search for songs."));
  }
  if (!libData) {
    box.replaceChildren(el("div", { class: "sechead", textContent: "HOST'S LIBRARY" }), skeletonRows(5));
    const r = await api(`/api/library?code=${encodeURIComponent(code)}`).catch(() => null);
    if (!r || !r.ok) {
      const d = r ? await r.json().catch(() => ({})) : {};
      return box.replaceChildren(el("div", { class: "sechead", textContent: "HOST'S LIBRARY" }), emptyState("library", d.detail || "Couldn't load the host's library", "",
        el("button", { class: "btn fill", textContent: "Try again", onclick: () => { libData = null; renderHostLib(); } })));
    }
    libData = await r.json();
  }
  const row = (p, iconName) => el("button", { class: "lrow-btn", onclick: () => openLibPlaylist(p.id) },
    p.thumb ? el("img", { class: "art", src: artUrl(p.thumb), alt: "", loading: "lazy" }) : el("span", { class: "art art-ic", html: icon(iconName) }),
    el("div", { class: "grow" }, el("div", { class: "t ellip", textContent: p.title }), p.subtitle ? el("div", { class: "s ellip", textContent: p.subtitle }) : null), ic("chevron"));
  box.replaceChildren(...[el("div", { class: "sechead", textContent: "HOST'S LIBRARY" }),
    el("div", { class: "rows" }, row({ ...libData.liked, subtitle: "Songs the host liked" }, "heart"), ...libData.playlists.map((p) => row(p, "library"))),
    libData.playlists.length ? null : el("div", { class: "empty", textContent: "No playlists in the host's library." })].filter(Boolean));
}

function libCollection(on) {
  $("lib-coll-view").classList.toggle("hidden", !on);
  $("lib-list-view").classList.toggle("hidden", on);
}
$("lib-back").replaceChildren(ic("back"), "Library");
$("lib-back").addEventListener("click", () => libCollection(false));

async function openLibPlaylist(id) {
  await showSheetFrom(api(`/api/library/playlist?id=${encodeURIComponent(id)}&code=${encodeURIComponent(code)}`));
}
$("paste-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("paste-url").value.trim();
  if (!url) return $("paste-url").focus();
  $("paste-url").blur();
  const ok = await showSheetFrom(api(`/api/playlist?url=${encodeURIComponent(url)}&code=${encodeURIComponent(code)}`));
  if (ok) $("paste-url").value = "";
});

const shuffled = (a) => { a = [...a]; for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };
async function showSheetFrom(request) {
  libCollection(true);
  $("lib-title").replaceChildren(el("div", { class: "sk sk-t", style: "width:70%;height:18px" }));
  $("lib-sub").replaceChildren(el("div", { class: "sk sk-s", style: "width:40%" }));
  $("lib-art").removeAttribute("src");
  $("lib-add").classList.add("hidden");
  $("lib-tracks").replaceChildren(skeletonRows(7));
  const r = await request.catch(() => null);
  if (!r || !r.ok) {
    const d = r ? await r.json().catch(() => ({})) : {};
    libCollection(false);
    $("paste-hint").textContent = d.detail || "Couldn't open that playlist.";
    $("paste-hint").classList.add("bad");
    setTimeout(() => { $("paste-hint").textContent = "Public or unlisted playlists. Private ones won't open."; $("paste-hint").classList.remove("bad"); }, 5000);
    return false;
  }
  const c = await r.json();
  $("lib-title").textContent = c.title;
  $("lib-sub").textContent = c.subtitle;
  if (c.thumb) $("lib-art").src = artUrl(c.thumb, 240);
  const all = c.tracks.map((t) => t.videoId);
  const btn = $("lib-add");
  btn.classList.toggle("hidden", !all.length);
  btn.disabled = false;
  btn.replaceChildren(ic("shuffle"), all.length > 50 ? "Queue 50, shuffled" : `Queue all ${all.length}, shuffled`);
  btn.onclick = async () => { if (await addTracks(shuffled(all).slice(0, 50))) { btn.disabled = true; btn.replaceChildren(ic("check"), "Added to the queue"); } };
  $("lib-tracks").replaceChildren(c.tracks.length ? el("div", { class: "rows" }, ...c.tracks.map(songRow)) : emptyState("queue", "This playlist is empty"));
  return true;
}

// ---- boot: ?code= from the QR link wins, then a remembered code
const initial = (new URLSearchParams(location.search).get("code") || localStorage.getItem("cesura.code") || "").toUpperCase();
if (initial) fillCode(initial);
document.querySelectorAll("[data-close]").forEach((b) => (b.innerHTML = icon("x")));
document.querySelectorAll("[data-ic]").forEach((n) => (n.innerHTML = icon(n.dataset.ic)));
if (initial && myName) join(initial); // returning guest: straight in (audio needs one tap: the sync pill)
else (initial ? $("name-input") : boxes[0]).focus();
