// Shared helpers. All text goes through textContent: track titles and names are untrusted.
const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v; // only ever our own ICON strings
    else if (k === "style") n.style.cssText = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k.startsWith("aria") || k === "title" || k === "role" || k === "draggable") n.setAttribute(k.replace(/^aria([A-Z])/, (_, c) => `aria-${c.toLowerCase()}`), v);
    else n[k] = v;
  }
  for (const k of kids) if (k != null && k !== false) n.append(k);
  return n;
}

function fmt(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}
const totalMin = (tracks) => Math.round(tracks.reduce((a, t) => a + (t.duration || 0), 0) / 60);

// Stroke icons on a 24px grid. Static strings, safe for innerHTML.
const P = {
  play: '<path d="M7 4.5v15l12-7.5z" fill="currentColor"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  skip: '<path d="M6 5l10 7-10 7z"/><path d="M18 5v14"/>',
  queue: '<path d="M4 6h16M4 12h10M4 18h10"/><path d="M17 14l4 3-4 3z"/>',
  playing: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  next: '<path d="M4 7h10M4 12h7M4 17h7"/><path d="M16 11l5 3.5-5 3.5z"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  back: '<path d="M15 18l-6-6 6-6"/>',
  chevron: '<path d="M9 18l6-6-6-6"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  radio: '<circle cx="12" cy="12" r="2"/><path d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 16.2a6 6 0 0 0 0-8.4M4.9 4.9a10 10 0 0 0 0 14.2M19.1 19.1a10 10 0 0 0 0-14.2"/>',
  people: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6.5 6.5 0 0 1 3.5 6"/>',
  phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  vol: '<path d="M11 5L6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>',
  mute: '<path d="M11 5L6 9H3v6h3l5 4z"/><path d="M22 9l-6 6M16 9l6 6"/>',
  replay: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.7L20 8.5"/><path d="M20 3.5v5h-5"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  grip: '<circle cx="9" cy="6" r="1" fill="currentColor"/><circle cx="15" cy="6" r="1" fill="currentColor"/><circle cx="9" cy="12" r="1" fill="currentColor"/><circle cx="15" cy="12" r="1" fill="currentColor"/><circle cx="9" cy="18" r="1" fill="currentColor"/><circle cx="15" cy="18" r="1" fill="currentColor"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${P[name]}</svg>`;
const ic = (name) => el("span", { html: icon(name), style: "display:contents" });

// Avatars: initial on a soft color picked from the name.
const AV = ["#9FC2E8", "#E8B9A0", "#C4B5E8", "#B5DDA4", "#E8D59F", "#A6DCD7", "#EEEBE4"];
function avatar(name, cls = "") {
  const n = (name || "?").trim() || "?";
  let h = 0;
  for (const c of n) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return el("span", { class: `avatar ${cls}`, title: n, textContent: n[0].toUpperCase(), style: `background:${AV[h % AV.length]}` });
}
function avatarStack(names, max = 3) {
  const s = el("div", { class: "stack", ariaLabel: `${names.length} in the room` });
  names.slice(0, max).forEach((n) => s.append(avatar(n)));
  if (names.length > max) s.append(el("span", { class: "avatar more", textContent: `+${names.length - max}` }));
  return s;
}

function art(t, cls = "art") {
  if (!t || !t.thumb) return el("div", { class: `${cls} art-ph` });
  return el("img", { class: cls, src: t.thumb, alt: "", loading: "lazy", onerror: (e) => e.target.replaceWith(el("div", { class: `${cls} art-ph` })) });
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
}

// ---- shared clock
// clockOffset = server_time - local_time (seconds). The guest page refines it
// with ping/pong; the host screen just uses each snapshot's server_time.
let clockOffset = 0;
const serverNow = () => Date.now() / 1000 + clockOffset;
// Where the current track is right now, per the server's clock. Negative
// during the short head start before a track begins.
const trackPos = (s) => (s.started_at == null ? s.position : serverNow() - s.started_at);
const posOf = (s) => (s && s.now ? Math.min(Math.max(0, trackPos(s)), s.now.duration || Infinity) : 0);

// Progress bar with knob: <div class="track-bar"><div class="done"></div><div class="knob"></div><div class="rest"></div></div>
function setBar(bar, frac) {
  const f = Math.max(0, Math.min(1, frac || 0));
  bar.querySelector(".done").style.flex = `0 0 ${f * 100}%`;
}

const CLOCK_TEXT = { playing: "Clock running", paused: "Paused", waiting: "Waiting for listeners", loading: "Loading song", idle: "Idle" };
