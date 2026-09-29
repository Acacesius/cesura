// Host dashboard: room code + QR, the queue (reorder, remove, add), radio,
// listeners with their measured drift, and engine health. Plays no audio.
const params = new URLSearchParams(location.search);
const key = params.get("key") || localStorage.getItem("cesura.hostkey");
if (params.get("key")) localStorage.setItem("cesura.hostkey", params.get("key"));
const K = encodeURIComponent(key || "");
let state = null, shownCode = null, artQid;

const hostPost = (path, q = "") => fetch(`${path}?key=${K}${q}`, { method: "POST" });
const roomPost = (path, body) => fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: state.code, ...body }) });
// PUBLIC_URL (server config) wins; otherwise the address this dashboard was opened on.
const inviteUrl = () => `${(state && state.public_url) || location.origin}/?code=${state ? state.code : ""}`;

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/host?key=${K}`);
  ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.type === "state") apply(m); };
  ws.onclose = (e) => {
    if (e.code === 4003) { localStorage.removeItem("cesura.hostkey"); return askForKey("That host key didn't work."); }
    const why = e.code === 4002 ? "Opened in another tab" : "Reconnecting…";
    $("p-live").replaceChildren(el("span", { class: "dot warn" }), why);
    if (e.code !== 4003 && e.code !== 4002) setTimeout(connect, 2000);
  };
}

function apply(s) {
  state = s;
  clockOffset = s.server_time - Date.now() / 1000;
  const h = s.host, n = s.now;
  // YouTube link state: a persistent Relink banner unless the cookies are valid.
  const ck = h.engine.cookies;
  $("relink-banner").classList.toggle("hidden", ck === "valid");
  $("relink-title").textContent = ck === "missing" ? "YouTube not linked." : "YouTube link expired.";
  $("relink-sub").textContent = ck === "missing" ? "Playback is off until you link a YouTube Music account." : "YouTube asked for a sign-in check. Playback may stop until you relink.";

  // top bar
  $("p-live").replaceChildren(el("span", { class: "dot" }), `Live · ${s.listening} listening`);
  $("p-clock").replaceChildren(ic(s.status === "playing" ? "play" : "pause"), CLOCK_TEXT[s.status] || s.status);

  // now playing
  if (artQid !== (n ? n.qid : null)) { artQid = n ? n.qid : null; $("np-art").replaceChildren(n ? art(n, "") : el("div", { class: "art-ph" })); }
  $("np-title").textContent = n ? n.title : "Nothing playing yet";
  $("np-sub").textContent = n ? [n.artists, n.album].filter(Boolean).join(" · ") : "Add a song from the queue panel";
  $("np-by").replaceChildren(...(n && (n.by || n.auto) ? [avatar(n.by || "Radio"), n.by ? `Added by ${n.by}` : "From radio"] : []));
  $("pause").innerHTML = icon(s.status === "paused" ? "play" : "pause");
  $("pause").setAttribute("aria-label", s.status === "paused" ? "Resume for everyone" : "Pause for everyone");
  $("pause").disabled = !n || s.status === "loading";
  $("skip").disabled = !n;
  $("radio-toggle").setAttribute("aria-pressed", String(s.radio));
  $("radio-toggle").replaceChildren(ic("radio"), s.radio ? "Radio on" : "Radio off");

  // stats
  $("st-played").textContent = h.stats.songs_played;
  $("st-drift").textContent = h.stats.median_drift_ms == null ? "–" : `${h.stats.median_drift_ms} ms`;
  $("st-peak").textContent = h.stats.peak_listeners;

  // queue
  $("q-sub").textContent = `${s.queue.length} song${s.queue.length === 1 ? "" : "s"} · ${totalMin(s.queue)} min${s.radio ? " · then autoplay radio" : ""}`;
  $("queue").replaceChildren(...(s.queue.length ? s.queue.map((t, i) => qrow(t, i, h.engine)) :
    [el("div", { class: "empty", style: "padding-left:10px", textContent: "Nothing queued. Search above to add songs." })]));
  $("radio-sub").textContent = s.radio ? `Plays when the queue runs out${n ? ` · seeded from ${n.title}` : ""}` : "Off. The room goes quiet when the queue runs out.";
  $("radio-btn").textContent = s.radio ? "Turn off" : "Turn on";
  $("auto").replaceChildren(...s.auto.slice(0, 6).map((t) => autorow(t, h.engine)));

  // room code, QR
  if (s.code !== shownCode) {
    shownCode = s.code;
    $("codebox").replaceChildren(...[...s.code].map((c) => el("span", { textContent: c })));
    $("join-url").textContent = inviteUrl();
    $("qr").src = `/api/qr.svg?key=${K}&url=${encodeURIComponent(inviteUrl())}`;
    $("pop-guest").href = inviteUrl();
  }

  // listeners
  $("l-count").textContent = h.listeners.filter((l) => l.listening).length;
  $("listeners").replaceChildren(...(h.listeners.length ? h.listeners.map(lrow) : [el("div", { class: "empty", textContent: "Nobody here yet. Share the code." })]));

  // engine
  const e = h.engine;
  $("e-stream").textContent = e.stream || "–";
  const NEXT = { cached: ["Prefetched", "ok"], resolving: ["Fetching…", ""], failed: ["Failed", "bad"], idle: ["–", ""] };
  const [nt, nc] = NEXT[e.next] || [e.next, ""];
  $("e-next").textContent = nt; $("e-next").className = `v ${nc}`;
  const CK = { valid: ["Valid", "ok"], expired: ["Expired", "bad"], missing: ["Not linked", "bad"] };
  const [ct, cc] = CK[e.cookies] || [e.cookies, ""];
  $("e-cookies").textContent = ct; $("e-cookies").className = `v ${cc}`;
  $("e-wrong").textContent = `${e.wrong_codes} / ${e.wrong_code_limit}`;
  $("e-wrong").className = `v ${e.wrong_codes ? "bad" : ""}`;
}

function qrow(t, i, engine) {
  const row = el("div", { class: `qgrid qrow${i === 0 ? " first" : ""}`, draggable: "true" },
    el("span", { class: "handle", html: icon("grip"), title: "Drag to reorder" }),
    art(t),
    el("div", { style: "min-width:0" },
      el("div", { class: "t" }, el("span", { textContent: t.title }), engine.next_qid === t.qid && engine.next === "cached" ? el("span", { class: "tag", textContent: "CACHED" }) : null),
      el("div", { class: "s ellip", textContent: t.artists })),
    el("span", { class: "who" }, avatar(t.by || "?"), el("span", { class: "ellip", textContent: t.by || "Guest" })),
    el("span", { class: "time", textContent: fmt(t.duration) }),
    el("button", { class: "iconbtn bare", html: icon("x"), ariaLabel: `Remove ${t.title}`, onclick: () => remove(t) }));
  row.dataset.qid = t.qid;
  row.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", t.qid); e.dataTransfer.effectAllowed = "move"; row.classList.add("dragging"); });
  row.addEventListener("dragend", () => row.classList.remove("dragging"));
  row.addEventListener("dragover", (e) => { e.preventDefault(); row.classList.add("drop-above"); });
  row.addEventListener("dragleave", () => row.classList.remove("drop-above"));
  row.addEventListener("drop", (e) => {
    e.preventDefault();
    row.classList.remove("drop-above");
    const qid = e.dataTransfer.getData("text/plain");
    const from = state.queue.findIndex((x) => x.qid === qid);
    if (from < 0 || qid === t.qid) return;
    hostPost("/api/host/move", `&qid=${encodeURIComponent(qid)}&index=${from < i ? i - 1 : i}`);
  });
  return row;
}

function autorow(t, engine) {
  return el("div", { class: "qgrid qrow" },
    el("span"), art(t),
    el("div", { style: "min-width:0" },
      el("div", { class: "t", style: "font-weight:500" }, el("span", { textContent: t.title }), engine.next_qid === t.qid && engine.next === "cached" ? el("span", { class: "tag", textContent: "CACHED" }) : null),
      el("div", { class: "s ellip", textContent: t.artists })),
    el("span", { class: "who faint", style: "font-size:13px", textContent: "Suggested" }),
    el("span", { class: "time", textContent: fmt(t.duration) }),
    el("button", { class: "iconbtn", html: icon("plus"), ariaLabel: `Add ${t.title} to queue`, onclick: () => addIds([t.videoId], t.title) }));
}

function lrow(l) {
  const st = l.status === "in_phase" ? ["", "In phase"] : l.status === "catching_up" ? ["warn", "Catching up"] : l.status === "waiting" ? ["warn", "Joining"] : ["idle", "Not listening"];
  return el("div", { class: "lrow" }, avatar(l.name),
    el("div", { style: "min-width:0" }, el("div", { class: "n" }, el("span", { class: "ellip", textContent: l.name })), el("div", { class: "d ellip", textContent: l.device })),
    el("div", { class: "r" }, el("div", { class: "ms", textContent: l.drift_ms == null ? "–" : `${Math.abs(l.drift_ms)} ms` }),
      el("div", { class: `st ${st[0]}` }, el("span", { class: "dot" }), st[1])));
}

// ---- actions
async function remove(t) { if ((await hostPost("/api/host/remove", `&qid=${encodeURIComponent(t.qid)}`)).ok) toast(`Removed ${t.title}`); }
async function addIds(videoIds, label) {
  const r = await roomPost("/api/add", { videoIds, cid: "host", name: "Host" });
  if (r.ok) { const { added } = await r.json(); toast(added > 1 ? `Added ${added} songs` : `Added ${label}`); }
  else toast(r.status === 409 ? "The queue is full." : r.status === 429 ? "Slow down a little." : "Couldn't add that.");
}
$("pause").addEventListener("click", () => state && roomPost("/api/pause", { paused: state.status !== "paused" }));
$("skip").addEventListener("click", () => hostPost("/api/host/skip"));
const toggleRadio = () => state && hostPost("/api/host/radio", `&on=${!state.radio}`);
$("radio-toggle").addEventListener("click", toggleRadio);
$("radio-btn").addEventListener("click", toggleRadio);
$("copy-link").replaceChildren(ic("link"), "Copy invite link");
$("copy-link").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(inviteUrl()); toast("Invite link copied"); }
  catch { prompt("Copy the invite link:", inviteUrl()); }
});
$("new-session").replaceChildren(ic("refresh"), "New session");
$("new-session").addEventListener("click", async () => {
  if (!confirm("Start a new session? This clears the queue and changes the room code. Guests will need the new code.")) return;
  await hostPost("/api/new-session");
});
$("settings").innerHTML = icon("sliders");
$("settings").addEventListener("click", (e) => {
  e.stopPropagation();
  const pop = $("settings-pop"), open = pop.classList.toggle("hidden");
  $("settings").setAttribute("aria-expanded", String(!open));
  $("pop-radio").replaceChildren(ic("radio"), state && state.radio ? "Turn autoplay radio off" : "Turn autoplay radio on");
});
document.addEventListener("click", () => { $("settings-pop").classList.add("hidden"); $("settings").setAttribute("aria-expanded", "false"); });
$("pop-radio").addEventListener("click", toggleRadio);

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

// search-to-add dropdown
$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("add-q").value.trim(), box = $("add-results");
  if (!q || !state) return;
  box.classList.remove("hidden");
  box.replaceChildren(el("div", { class: "empty", textContent: "Searching…" }));
  const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&kind=all&code=${state.code}`);
  if (!r.ok) return box.replaceChildren(el("div", { class: "empty", textContent: "Search failed." }));
  const d = await r.json();
  if (d.open) return addCollection("playlist", d.open.id, "linked playlist");
  box.replaceChildren(...(d.results.length ? d.results.map((x) => el("div", { class: "trow" },
    x.kind === "song" ? art(x) : x.thumb ? el("img", { class: "art", src: x.thumb, alt: "" }) : el("div", { class: "art art-ph" }),
    el("div", { class: "grow" }, el("div", { class: "t ellip", textContent: x.title }),
      el("div", { class: "s ellip", textContent: x.kind === "song" ? [x.artists, x.duration && fmt(x.duration)].filter(Boolean).join(" · ") : `${x.kind === "album" ? "Album" : "Playlist"} · ${x.subtitle}` })),
    el("div", { class: "acts" }, x.kind === "song"
      ? el("button", { class: "iconbtn", html: icon("plus"), ariaLabel: `Add ${x.title}`, onclick: () => addIds([x.videoId], x.title) })
      : el("button", { class: "btn", textContent: "Add all", onclick: () => addCollection(x.kind, x.id, x.title) })))) : [el("div", { class: "empty", textContent: "No results." })]));
});
async function addCollection(kind, id, title) {
  const r = await fetch(`/api/collection?kind=${kind}&id=${encodeURIComponent(id)}&code=${state.code}`);
  if (!r.ok) return toast(`Couldn't open that ${kind}.`);
  const c = await r.json();
  await addIds(c.tracks.map((t) => t.videoId).slice(0, 50), c.title || title);
}
document.addEventListener("click", (e) => { if (!e.target.closest(".addbox")) $("add-results").classList.add("hidden"); });
$("add-q").addEventListener("focus", () => $("add-results").children.length && $("add-results").classList.remove("hidden"));

// progress + session timer
setInterval(() => {
  if (!state) return;
  const n = state.now, p = posOf(state), d = n ? n.duration || 0 : 0;
  setBar($("np-bar"), d ? p / d : 0);
  $("np-pos").textContent = fmt(p);
  $("np-dur").textContent = fmt(d);
  const secs = serverNow() - state.host.stats.session_start;
  $("p-session").textContent = `${Math.floor(secs / 3600)}:${String(Math.floor((secs % 3600) / 60)).padStart(2, "0")}:${String(Math.floor(secs % 60)).padStart(2, "0")}`;
}, 500);

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
