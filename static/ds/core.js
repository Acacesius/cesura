// Shared design system: DOM helpers, icons, avatars, toasts, sheets, skeletons, the
// reconnecting socket, drag to reorder and seeking. Used by cesura and rubato (see README.md).
// All text goes through textContent: track titles and names are untrusted.
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
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  lyrics: '<path d="M4 6h16M4 10h12M4 14h16M4 18h9"/>',
  pocket: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M10 9.5c1.2-1 2.8-1 4 0M9 12.5c1.8-1.5 4.2-1.5 6 0"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  unlock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.5-2"/>',
  more: '<circle cx="12" cy="5" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><circle cx="12" cy="19" r="1.2" fill="currentColor"/>',
  kick: '<path d="M14 4h5a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-5"/><path d="M10 16l-4-4 4-4M6 12h10"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>',
  star: '<path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/>',
  shuffle: '<path d="M4 7h3.5c4.5 0 4.5 10 9 10H20M4 17h3.5c1.6 0 2.6-1.3 3.4-3M14 10c.8-1.8 1.8-3 3.5-3H20"/><path d="M17.5 4.5L20 7l-2.5 2.5M17.5 14.5L20 17l-2.5 2.5"/>',
  library: '<path d="M5 4v16M9 4v16"/><path d="M13 5l4 15M17 4.5l3.5 1"/>',
  heart: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/>',
  save: '<path d="M12 4v11M7 10l5 5 5-5"/><path d="M4 18v1a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-1"/>',
  pulse: '<path d="M3 12h4l2.5-6 5 12L17 12h4"/>',
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

// Artwork comes from the server's cache (/art/<id>) at the size asked for: small for list rows.
const artUrl = (thumb, px = 120) => (thumb ? `${thumb}${thumb.includes("?") ? "&" : "?"}s=${px}` : null);
function art(t, cls = "art", px = cls === "art" ? 120 : 544) {
  if (!t || !t.thumb) return el("div", { class: `${cls} art-ph` });
  return el("img", { class: cls, src: artUrl(t.thumb, px), alt: "", loading: "lazy", decoding: "async", onerror: (e) => e.target.replaceWith(el("div", { class: `${cls} art-ph` })) });
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
}

// ---- lock screen / notification controls (every client)
// handlers: { play, pause, next } -> the normal room commands.
function setMediaSession(n, handlers, playing) {
  if (!("mediaSession" in navigator)) return;
  const ms = navigator.mediaSession;
  ms.metadata = n ? new MediaMetadata({ title: n.title, artist: n.artists || "", album: n.album || "cesura",
    artwork: n.thumb ? [{ src: new URL(artUrl(n.thumb, 544), location.href).href, sizes: "544x544", type: "image/jpeg" }] : [] }) : null;
  try { ms.playbackState = !n ? "none" : playing ? "playing" : "paused"; } catch {}
  if (handlers && !setMediaSession.wired) {
    setMediaSession.wired = true;
    for (const [action, fn] of [["play", handlers.play], ["pause", handlers.pause], ["nexttrack", handlers.next]]) {
      try { ms.setActionHandler(action, fn); } catch {}
    }
  }
}

// ---- websocket with reconnect + a visible "Reconnecting" banner (never a silently frozen screen)
// open(): returns a new WebSocket. onMessage(msg). onClose(event) -> true to stop retrying.
function liveSocket({ open, onOpen = () => {}, onMessage, onClose = () => false, banner }) {
  let ws = null, tries = 0, timer = null, shownAt = null, countdown = null, stopped = false, next = 0;
  const bar = banner;
  const showBanner = (on) => {
    bar.classList.toggle("hidden", !on);
    clearInterval(countdown);
    if (!on) return;
    const paint = () => {
      const secs = Math.max(0, Math.ceil((next - Date.now()) / 1000));
      bar.replaceChildren(el("span", { class: "dot warn pulsing" }),
        el("span", { class: "grow", textContent: secs ? `Connection lost. Reconnecting in ${secs}s…` : "Reconnecting…" }),
        el("button", { class: "btn fill", textContent: "Retry now", onclick: () => { clearTimeout(timer); connect(); } }));
    };
    paint();
    countdown = setInterval(paint, 500);
  };
  function connect() {
    if (stopped) return;
    clearTimeout(timer);
    ws = open();
    ws.onopen = () => { tries = 0; clearTimeout(shownAt); shownAt = null; showBanner(false); onOpen(ws); };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = (e) => {
      ws = null;
      if (stopped || onClose(e)) return;
      const wait = Math.min(15000, 1000 * 2 ** tries++);
      next = Date.now() + wait;
      // Don't flash the banner for a reconnect that succeeds right away.
      if (!shownAt) shownAt = setTimeout(() => showBanner(true), tries > 1 ? 0 : 1500);
      timer = setTimeout(connect, wait);
    };
  }
  window.addEventListener("online", () => { if (!ws && !stopped) { clearTimeout(timer); connect(); } });
  connect();
  return {
    send: (m) => ws && ws.readyState === 1 && ws.send(JSON.stringify(m)),
    reconnect: () => { if (ws) ws.close(4100); else { clearTimeout(timer); connect(); } },
    stop: () => { stopped = true; clearTimeout(timer); clearTimeout(shownAt); showBanner(false); if (ws) { ws.onclose = null; ws.close(); ws = null; } },
    get open() { return !!ws && ws.readyState === 1; },
  };
}

// ---- state over the socket: a full "state" on connect, then "patch" messages (see app/delta.py).
// Returns the new state, or null when this client missed a version and must ask for a resync.
// Unchanged parts keep their identity (s.queue === old.queue when the queue didn't change).
function applyPatch(state, m) {
  if (m.type === "state") return m;
  if (!state) return null;
  const s = { ...state, ...(m.set || {}) };
  for (const k of m.del || []) delete s[k];
  if (m.q) {
    if (m.q.base !== state.qver) return null;
    s.queue = applyQueue(state.queue, m.q);
  } else if (m.qver !== state.qver) return null;
  s.qver = m.qver;
  return s;
}
// Queue edits: drop rm + every qid in put, then insert the put entries in order.
function applyQueue(old, { rm, put }) {
  const by = new Map(old.map((t) => [t.qid, t]));
  const gone = new Set([...rm, ...put.map(([, x]) => (typeof x === "string" ? x : x.qid))]);
  const out = old.filter((t) => !gone.has(t.qid));
  for (const [i, x] of put) out.splice(i, 0, typeof x === "string" ? by.get(x) : x);
  return out;
}

// ---- loading + empty states
function skeletonRows(n = 6) {
  return el("div", { class: "rows", ariaBusy: "true", ariaLabel: "Loading" }, ...Array.from({ length: n }, (_, i) =>
    el("div", { class: "trow skel" }, el("div", { class: "art sk" }), el("div", { class: "grow" },
      el("div", { class: "sk sk-t", style: `width:${55 + ((i * 37) % 35)}%` }), el("div", { class: "sk sk-s", style: `width:${30 + ((i * 23) % 30)}%` })))));
}
function emptyState(iconName, title, sub = "", action = null) {
  return el("div", { class: "emptystate" }, el("span", { class: "es-ic", html: icon(iconName) }),
    el("div", { class: "es-t", textContent: title }), sub ? el("div", { class: "es-s", textContent: sub }) : null, action);
}

// ---- sheets (bottom sheet on phones, centred card on desktop)
function openSheet(id) {
  const m = $(id);
  m.classList.remove("hidden");
  const f = m.querySelector("[data-close]");
  if (f) f.focus();
}
function closeSheet(id) { $(id).classList.add("hidden"); }
document.addEventListener("click", (e) => {
  const c = e.target.closest("[data-close]");
  if (c) closeSheet(c.closest(".sheet").id);
  else if (e.target.classList && e.target.classList.contains("sheet")) closeSheet(e.target.id);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".sheet:not(.hidden)").forEach((s) => s.classList.add("hidden")); });

// ---- queue gestures for touch: swipe left to remove, long-press then drag to reorder.
// rows: the row elements in queue order (each with dataset.qid). onRemove(qid), onMove(qid, index).
function touchQueue(rows, { onRemove, onMove }) {
  rows.forEach((row, i) => {
    let sx = 0, sy = 0, mode = null, timer = null, target = i;
    const reset = () => {
      clearTimeout(timer);
      row.style.transform = "";
      row.classList.remove("lifted", "swiping");
      rows.forEach((r) => r.classList.remove("drop-above", "drop-below"));
      mode = null;
    };
    row.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" || e.target.closest("button")) return;
      sx = e.clientX; sy = e.clientY; mode = "pending"; target = i;
      timer = setTimeout(() => { if (mode === "pending") { mode = "drag"; row.classList.add("lifted"); navigator.vibrate && navigator.vibrate(10); } }, 450);
    });
    row.addEventListener("pointermove", (e) => {
      if (!mode) return;
      const dx = e.clientX - sx, dy = e.clientY - sy;
      if (mode === "pending") {
        if (Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy)) { mode = "swipe"; clearTimeout(timer); row.classList.add("swiping"); try { row.setPointerCapture(e.pointerId); } catch {} }
        else if (Math.abs(dy) > 10) { reset(); return; } // a scroll
      }
      if (mode === "swipe") row.style.transform = `translateX(${Math.min(0, dx)}px)`;
      if (mode === "drag") {
        row.style.transform = `translateY(${dy}px)`;
        rows.forEach((r) => r.classList.remove("drop-above", "drop-below"));
        target = i;
        rows.forEach((r, j) => {
          if (r === row) return;
          const b = r.getBoundingClientRect(), mid = b.top + b.height / 2;
          if (j < i && e.clientY < mid) target = Math.min(target, j);
          if (j > i && e.clientY > mid) target = Math.max(target, j);
        });
        if (target !== i) rows[target].classList.add(target < i ? "drop-above" : "drop-below");
      }
    });
    const end = (e) => {
      if (mode === "swipe" && e.clientX - sx < -90) { row.style.transform = "translateX(-100%)"; onRemove(row.dataset.qid); setTimeout(reset, 300); mode = null; return; }
      if (mode === "drag" && target !== i) onMove(row.dataset.qid, target);
      reset();
    };
    row.addEventListener("pointerup", end);
    row.addEventListener("pointercancel", reset);
    // While lifted, the page must not scroll under the finger.
    row.addEventListener("touchmove", (e) => { if (mode === "drag" || mode === "swipe") e.preventDefault(); }, { passive: false });
    row.addEventListener("contextmenu", (e) => { if (mode) e.preventDefault(); });
  });
}

// ---- drag to reorder. Each movable row has a grip button (.grip). Drag it with a mouse, pen or
// finger, or focus it and press ArrowUp / ArrowDown. The server decides: onMove(qid, to, from)
// sends the request (`to` counts without the moved row), and the list redraws from the broadcast.
// While a drag is on, ctl.dragging is true and the app should hold off redrawing the list; onIdle()
// runs when it ends so the app can catch up. After a redraw, call ctl.restore() to keep keyboard focus.
function sortable(list, { onMove, onIdle = () => {} }) {
  const ctl = { dragging: false, focusQid: null };
  const rows = () => [...list.querySelectorAll(":scope > [data-qid]")];
  const scroller = () => {
    for (let n = list.parentElement; n && n !== document.body; n = n.parentElement) {
      const o = getComputedStyle(n).overflowY;
      if ((o === "auto" || o === "scroll") && n.scrollHeight > n.clientHeight) return n;
    }
    return document.scrollingElement;
  };
  list.addEventListener("pointerdown", (e) => {
    const grip = e.target.closest(".grip");
    if (!grip || e.button > 0) return;
    const row = grip.closest("[data-qid]"), all = rows(), from = all.indexOf(row);
    if (from < 0) return;
    e.preventDefault();
    try { grip.setPointerCapture(e.pointerId); } catch {}
    ctl.dragging = true;
    const sc = scroller(), top0 = sc.scrollTop, y0 = e.clientY, h = row.getBoundingClientRect().height;
    const others = all.filter((r) => r !== row);
    const mids = others.map((r) => { const b = r.getBoundingClientRect(); return b.top + b.height / 2; });
    const rb = row.getBoundingClientRect(), mid0 = rb.top + rb.height / 2;
    let y = y0, target = from, raf = 0;
    list.classList.add("sorting");
    row.classList.add("dragging");
    const place = () => {
      const scrolled = sc.scrollTop - top0;
      row.style.transform = `translateY(${y - y0 + scrolled}px)`;
      const mid = mid0 + (y - y0);
      target = mids.filter((m) => m - scrolled < mid).length;
      others.forEach((r, j) => { r.style.transform = j >= from && j < target ? `translateY(${-h}px)` : j < from && j >= target ? `translateY(${h}px)` : ""; });
    };
    const tick = () => { // scroll when the finger is near the top or bottom edge
      const b = sc === document.scrollingElement ? { top: 0, bottom: innerHeight } : sc.getBoundingClientRect();
      const step = y < b.top + 56 ? -12 : y > b.bottom - 56 ? 12 : 0;
      if (step) { sc.scrollTop += step; place(); }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    const move = (ev) => { y = ev.clientY; place(); };
    const up = (ev) => {
      cancelAnimationFrame(raf);
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
      grip.removeEventListener("pointercancel", up);
      all.forEach((r) => (r.style.transform = ""));
      list.classList.remove("sorting");
      row.classList.remove("dragging");
      ctl.dragging = false;
      if (ev.type === "pointerup" && target !== from) onMove(row.dataset.qid, target, from);
      onIdle();
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
    grip.addEventListener("pointercancel", up);
  });
  list.addEventListener("keydown", (e) => {
    const grip = e.target.closest(".grip");
    if (!grip || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
    e.preventDefault();
    const row = grip.closest("[data-qid]"), all = rows(), from = all.indexOf(row), to = from + (e.key === "ArrowUp" ? -1 : 1);
    if (to < 0 || to >= all.length) return;
    ctl.focusQid = row.dataset.qid;
    onMove(row.dataset.qid, to, from);
  });
  ctl.restore = () => {
    if (!ctl.focusQid) return;
    const g = list.querySelector(`[data-qid="${CSS.escape(ctl.focusQid)}"] .grip`);
    if (g) g.focus();
    ctl.focusQid = null;
  };
  return ctl;
}
const gripBtn = (t) => el("button", { class: "grip", html: icon("grip"), title: "Drag to reorder", ariaLabel: `Move ${t.title}. Drag, or use the arrow keys.` });

// ---- seeking: click or drag a progress bar, or arrow keys on it. While dragging, the target time
// shows locally (ctl.preview) and nothing is sent; one onSeek(seconds) goes on release. Keys step
// 5 s (PageUp/PageDown 30 s, Home/End) and send once they settle. After a seek, ctl.preview holds the
// target until the server's broadcast catches up (ctl.settle(pos)), so the bar doesn't jump back.
function seekBar(bar, { duration, position, onSeek, enabled = () => true, label = "Seek" }) {
  const ctl = { preview: null };
  let keyTimer = null, holdUntil = 0;
  bar.setAttribute("role", "slider");
  bar.setAttribute("aria-label", label);
  bar.setAttribute("aria-valuemin", "0");
  bar.tabIndex = 0;
  const clamp = (p) => Math.max(0, Math.min(duration() || 0, p));
  const at = (x) => { const b = bar.getBoundingClientRect(); return clamp(((x - b.left) / b.width) * duration()); };
  const commit = (p) => { holdUntil = performance.now() + 2500; ctl.preview = p; onSeek(p); };
  bar.addEventListener("pointerdown", (e) => {
    if (!enabled() || !duration() || e.button > 0) return;
    e.preventDefault();
    try { bar.setPointerCapture(e.pointerId); } catch {}
    bar.focus({ preventScroll: true });
    bar.classList.add("scrubbing");
    ctl.preview = at(e.clientX);
    const move = (ev) => { ctl.preview = at(ev.clientX); };
    const end = (ev) => {
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", end);
      bar.removeEventListener("pointercancel", end);
      bar.classList.remove("scrubbing");
      if (ev.type === "pointerup") commit(at(ev.clientX));
      else ctl.preview = null;
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", end);
    bar.addEventListener("pointercancel", end);
  });
  bar.addEventListener("keydown", (e) => {
    const d = duration();
    const step = { ArrowRight: 5, ArrowUp: 5, ArrowLeft: -5, ArrowDown: -5, PageUp: 30, PageDown: -30 }[e.key];
    if (!enabled() || !d || (step == null && e.key !== "Home" && e.key !== "End")) return;
    e.preventDefault();
    const from = ctl.preview ?? position();
    ctl.preview = e.key === "Home" ? 0 : e.key === "End" ? Math.max(0, d - 1) : clamp(from + step);
    holdUntil = Infinity;
    clearTimeout(keyTimer);
    keyTimer = setTimeout(() => commit(ctl.preview), 450);
  });
  // Call on every state: drop the held preview once the server agrees (or after a while).
  ctl.settle = (pos) => {
    if (ctl.preview == null || bar.classList.contains("scrubbing") || holdUntil === Infinity) return;
    if (Math.abs(pos - ctl.preview) < 1.5 || performance.now() > holdUntil) ctl.preview = null;
  };
  ctl.show = (pos, dur) => { // keep the slider's accessible value in step with what it shows
    bar.setAttribute("aria-valuemax", String(Math.round(dur || 0)));
    bar.setAttribute("aria-valuenow", String(Math.round(pos)));
    bar.setAttribute("aria-valuetext", `${fmt(pos)} of ${fmt(dur)}`);
    bar.setAttribute("aria-disabled", String(!enabled() || !dur));
  };
  return ctl;
}
