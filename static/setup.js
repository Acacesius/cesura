// First-run setup. The setup token lives in sessionStorage only (this tab),
// and is sent as a header; it never goes in a URL.
let token = sessionStorage.getItem("cesura.setup") || "";
let info = null;        // from /api/setup/verify
let newKey = null;      // shown once, right after generating
let linked = false;

const auth = () => ({ "X-Setup-Token": token });
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { ...auth(), ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (r.status === 410) { location.replace("/host"); throw new Error("done"); }
  return { ok: r.ok, status: r.status, data };
}

// ---- gate
$("gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  token = $("token").value.trim();
  if (!token) return;
  const { ok, data } = await post("/api/setup/verify");
  if (!ok) { $("gate-err").textContent = data.detail || "That token didn't work."; return; }
  sessionStorage.setItem("cesura.setup", token);
  start(data);
});

function start(data) {
  info = data;
  $("gate").classList.add("hidden");
  $("wizard").classList.remove("hidden");
  renderKeyStep();
  $("url").value = info.public_url || info.suggested_url || "";
  $("url-note").textContent = info.public_url_env ? "PUBLIC_URL is set in the environment and takes precedence over this value." : `Detected from this page: ${info.suggested_url || "unknown"}`;
  mountLinker($("linker"), { headers: auth, onChange: (res) => {
    linked = !!(res && res.account);
    $("yt-next").disabled = !linked;
  } });
  go(1);
}

function go(n) {
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== String(n)));
  document.querySelectorAll("#steps li").forEach((li) => {
    const s = +li.dataset.step;
    li.className = s < n ? "done" : s === n ? "current" : "";
    if (s === n) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  });
  window.scrollTo({ top: 0 });
  if (n === 4) $("done-lead").textContent = linked
    ? "YouTube is linked. Open the dashboard to get the room code and QR code."
    : "YouTube isn't linked yet, so playback is off. Use “Relink YouTube” on the dashboard when you're ready.";
}

// ---- step 1: host key
function renderKeyStep() {
  const area = $("key-area");
  if (info.host_key === "env") {
    area.replaceChildren(el("p", { class: "muted", textContent: "A host key is already set with the HOST_KEY environment variable. Use that one." }),
      el("button", { class: "btn amber big-btn", textContent: "Continue", onclick: () => go(2) }));
  } else if (info.host_key === "stored" && !newKey) {
    area.replaceChildren(el("p", { class: "muted", textContent: "A host key was already created. If you didn't save it, generate a new one (the old one stops working)." }),
      el("div", { class: "row-actions" },
        el("button", { class: "btn", textContent: "Generate a new key", onclick: () => genKey(true) }),
        el("button", { class: "btn amber", textContent: "I have it, continue", onclick: () => go(2) })));
  } else if (!newKey) {
    area.replaceChildren(el("button", { class: "btn amber big-btn", html: icon("key") + "Generate host key", onclick: () => genKey(false) }));
  } else {
    const saved = el("input", { type: "checkbox", id: "saved" });
    const next = el("button", { class: "btn amber big-btn", textContent: "Continue", disabled: true, onclick: () => go(2) });
    saved.addEventListener("change", () => (next.disabled = !saved.checked));
    area.replaceChildren(
      el("div", { class: "keybox" },
        el("span", { class: "mono", id: "keytext", textContent: newKey }),
        el("button", { class: "btn fill", html: icon("copy") + "Copy", onclick: async () => {
          try { await navigator.clipboard.writeText(newKey); toast("Host key copied"); } catch { selectKey(); toast("Select and copy the key"); }
        } })),
      el("p", { class: "warnline", textContent: "This is the only time the key is shown. Save it in a password manager." }),
      el("label", { class: "check" }, saved, el("span", { textContent: "I've saved the host key somewhere safe" })),
      next);
  }
}
function selectKey() { const r = document.createRange(); r.selectNodeContents($("keytext")); getSelection().removeAllRanges(); getSelection().addRange(r); }
async function genKey(regenerate) {
  if (regenerate && !confirm("Generate a new host key? The old one will stop working.")) return;
  const { ok, data } = await post("/api/setup/host-key", { regenerate });
  if (!ok) return toast(data.detail || "Couldn't create the key.");
  if (data.key) { newKey = data.key; info.host_key = "stored"; }
  renderKeyStep();
}

// ---- step 2: public URL
$("url-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const { ok, data } = await post("/api/setup/public-url", { url: $("url").value });
  if (!ok) { $("url-err").textContent = data.detail || "That address didn't work."; return; }
  $("url-err").textContent = "";
  info.public_url = data.public_url;
  go(3);
});

// ---- step 3: YouTube
$("yt-next").addEventListener("click", () => go(4));
$("skip-yt").addEventListener("click", () => {
  if (linked) return go(4);
  if ($("skip-note").classList.contains("hidden")) { $("skip-note").classList.remove("hidden"); $("skip-yt").textContent = "Skip anyway"; return; }
  go(4);
});

// ---- step 4: done
$("finish").addEventListener("click", async () => {
  const { ok, data } = await post("/api/setup/finish");
  if (!ok) { $("finish-err").textContent = data.detail || "Couldn't finish setup."; return; }
  if (newKey) localStorage.setItem("cesura.hostkey", newKey); // the dashboard reads it from here, not from the URL
  sessionStorage.removeItem("cesura.setup");
  location.replace(data.redirect || "/host");
});

// resume after a reload in the same tab
if (token) post("/api/setup/verify").then(({ ok, data }) => (ok ? start(data) : sessionStorage.removeItem("cesura.setup"))).catch(() => {});
