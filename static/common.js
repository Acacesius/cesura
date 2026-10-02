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
