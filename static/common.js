// cesura-specific helpers on top of ds/core.js: the shared clock, the progress bar, clock status text.

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

const CLOCK_TEXT = { unlinked: "YouTube not linked", playing: "Clock running", paused: "Paused", waiting: "Waiting for listeners", loading: "Loading song", idle: "Idle" };

// ---- themes. A theme is only a set of CSS variable values (themes.css); the default is Cesium.
// Each viewer picks their own; it's kept in this browser (the <head> applies it before first paint).
const THEMES = [
  { id: "", label: "Cesium", sw: ["#0F1012", "#F4B544", "#8CB8F0"] },
  { id: "sakura", label: "Sakura", sw: ["#FBF3F5", "#C2406A", "#3F7A4C"] },
  { id: "yozakura", label: "Night sakura", sw: ["#140F14", "#F49AC1", "#B9A6F2"] },
  { id: "matcha", label: "Matcha", sw: ["#0F1410", "#A8D672", "#E8D59F"] },
  { id: "lavender", label: "Lavender", sw: ["#12111A", "#B9A6F2", "#8FD3C6"] },
  { id: "ocean", label: "Ocean", sw: ["#0B1218", "#4FD1C5", "#8CB8F0"] },
  { id: "sunset", label: "Sunset", sw: ["#16100E", "#FF8A5B", "#F2C46D"] },
  { id: "paper", label: "Paper", sw: ["#F6F4EF", "#9A5B00", "#2B65A6"] },
  { id: "mono", label: "Mono", sw: ["#0E0E0E", "#F2F2F2", "#BDBDBD"] },
];const THEME_KEY = "cesura.theme";
function applyTheme(id) {
  const root = document.documentElement;
  if (id) root.dataset.theme = id; else delete root.dataset.theme;
  try { if (id) localStorage.setItem(THEME_KEY, id); else localStorage.removeItem(THEME_KEY); } catch {}
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(root).getPropertyValue("--ground").trim();
  document.querySelectorAll("#theme-sheet .theme").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.id === (id || ""))));
}
function openThemes() {
  if (!$("theme-sheet")) {
    const close = el("button", { class: "iconbtn bare", html: icon("x"), ariaLabel: "Close" });
    close.setAttribute("data-close", "");
    document.body.append(el("div", { id: "theme-sheet", class: "sheet hidden", role: "dialog", ariaModal: "true", ariaLabel: "Theme" },
      el("div", { class: "sheet-card" },
        el("div", { class: "modal-head" }, el("h2", { textContent: "Theme" }), close),
        el("p", { class: "muted theme-note", textContent: "Just for you, on this device." }),
        el("div", { class: "themes", role: "group", ariaLabel: "Themes" }, ...THEMES.map((t) =>
          el("button", { class: "theme", ariaPressed: "false", onclick: () => applyTheme(t.id) },
            el("span", { class: "sw", ariaHidden: "true" }, ...t.sw.map((c) => el("i", { style: `background:${c}` }))),
            el("span", { class: "tn", textContent: t.label })))))));
    document.querySelectorAll("#theme-sheet .theme").forEach((b, i) => (b.dataset.id = THEMES[i].id));
  }
  applyTheme(document.documentElement.dataset.theme || "");
  openSheet("theme-sheet");
}
applyTheme(document.documentElement.dataset.theme || "");
