// "Link YouTube": the export guide, drag-and-drop upload and the live probe.
// Used by /setup (setup token) and by the host's Relink flow (host key).
// The cookie file goes straight from the file picker to the server; nothing
// here reads, stores or displays its contents.

const EXPORTERS = {
  primary: { name: "Get cookies.txt LOCALLY", src: "https://github.com/kairi003/Get-cookies.txt-LOCALLY",
    chrome: "https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc",
    firefox: "https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/" },
  firefoxAlt: { name: "cookies.txt", src: "https://github.com/hrdl-github/cookies-txt", firefox: "https://addons.mozilla.org/firefox/addon/cookies-txt/" },
};

function linkGuide() {
  const a = (href, text) => el("a", { href, target: "_blank", rel: "noopener noreferrer", textContent: text });
  const step = (n, ...kids) => el("li", {}, el("span", { class: "n", textContent: n }), el("div", {}, ...kids));
  return el("ol", { class: "guide" },
    step("1", el("b", { textContent: "Install an exporter. " }), "Use ", el("b", { textContent: EXPORTERS.primary.name }),
      " for Chrome, Edge or Brave (", a(EXPORTERS.primary.chrome, "Chrome Web Store"), ") or Firefox (", a(EXPORTERS.primary.firefox, "Firefox Add-ons"),
      "). It's open source (", a(EXPORTERS.primary.src, "code"), "). On Firefox, ", el("b", { textContent: EXPORTERS.firefoxAlt.name }),
      " (", a(EXPORTERS.firefoxAlt.firefox, "add-on"), ", ", a(EXPORTERS.firefoxAlt.src, "code"), ") works too.",
      el("div", { class: "warnline", textContent: "Avoid “Get cookies.txt” without “LOCALLY”. It was reported as malware." })),
    step("2", el("b", { textContent: "Allow it in private windows. " }), "Chrome: extension Details → “Allow in Incognito”. Firefox: Manage extension → “Run in Private Windows” → Allow."),
    step("3", el("b", { textContent: "Open a private (incognito) window " }), "and sign in at ", a("https://music.youtube.com", "music.youtube.com"), "."),
    step("4", el("b", { textContent: "In the same tab, go to " }), el("span", { class: "mono", textContent: "youtube.com/robots.txt" }),
      ". It's a plain page, so YouTube can't rotate your cookies while you export."),
    step("5", el("b", { textContent: "Export cookies.txt " }), "from the extension in Netscape format. Extra sites in the file are fine; cesura keeps only YouTube and Google cookies."),
    step("6", el("b", { textContent: "Close the private window. Don't log out. " }), "Logging out ends the session you just exported."),
    step("7", el("b", { textContent: "Drop the file below. " }), "Then delete the downloaded copy."));
}

const PROBE_TEXT = {
  premium: ["ok", "Premium detected · 256k AAC"],
  free: ["warn", "Signed in · no Premium detected · 128k AAC"],
  "signed-out": ["bad", "Signed out: re-export from a private window, and close it without logging out."],
  "bot-check": ["bad", "YouTube asked for a bot check: re-export from a fresh private window."],
  unavailable: ["warn", "Signed in, but the test song isn't available here, so audio quality couldn't be checked. Set PROBE_VIDEO_ID to another song."],
  missing: ["bad", "No cookies saved yet."],
};
function probeVerdict(r) {
  if (r.premium) return "premium";
  if (r.account && r.aac256 === false && !r.problem) return "free";
  return r.problem || (r.account ? "free" : "signed-out");
}

// root: element to render into. headers(): auth headers. onChange(result|null): called after each upload/probe.
function mountLinker(root, { headers, onChange = () => {} }) {
  const input = el("input", { type: "file", accept: ".txt,text/plain", class: "hidden" });
  const zone = el("div", { class: "dropzone", role: "button", tabIndex: 0, ariaLabel: "Upload cookies.txt" },
    el("span", { html: icon("upload") }), el("div", { class: "dz-t", textContent: "Drop cookies.txt here" }),
    el("div", { class: "dz-s", textContent: "or click to choose the file · max 256 KB" }), input);
  const status = el("div", { class: "linkstatus hidden", role: "status", ariaLive: "polite" });
  root.replaceChildren(linkGuide(), zone, status);

  const pick = () => input.click();
  zone.addEventListener("click", pick);
  zone.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), pick()));
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("over"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });
  input.addEventListener("change", () => { if (input.files[0]) upload(input.files[0]); input.value = ""; });

  const show = (kind, title, rows = [], note = "") => {
    status.className = `linkstatus ${kind}`;
    status.replaceChildren(el("div", { class: "ls-t" }, el("span", { class: "dot" }), title),
      rows.length ? el("div", { class: "ls-rows" }, ...rows.map(([k, v, ok]) => el("div", { class: "erow" }, el("span", { textContent: k }), el("span", { class: `v ${ok ? "ok" : "bad"}`, textContent: v })))) : null,
      note ? el("div", { class: "ls-note", textContent: note }) : null);
  };

  async function upload(file) {
    if (file.size > 256 * 1024) return show("bad", "That file is larger than 256 KB, which is too big for a cookies.txt export.");
    show("busy", "Uploading…");
    let r;
    try { r = await fetch("/api/youtube/cookies", { method: "POST", headers: { ...headers(), "Content-Type": "text/plain" }, body: file }); }
    catch { return show("bad", "Upload failed. Check your connection and try again."); }
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { onChange(null); return show("bad", body.detail || "That file was rejected."); }
    show("busy", `Saved ${body.youtube_cookies} YouTube cookies${body.dropped_other_sites ? ` (dropped ${body.dropped_other_sites} from other sites)` : ""}. Checking with YouTube…`);
    await probe();
  }

  async function probe() {
    const r = await fetch("/api/youtube/probe", { method: "POST", headers: headers() });
    const res = await r.json().catch(() => null);
    if (!r.ok || !res) { onChange(null); return show("bad", (res && res.detail) || "The check didn't run. Try again."); }
    const v = probeVerdict(res), [kind, title] = PROBE_TEXT[v];
    show(kind, title, [
      ["Account", res.account ? (res.name ? `Signed in as ${res.name}` : "Signed in") : "Not signed in", res.account],
      ["Premium", res.premium ? "Detected" : "Not detected", res.premium],
      ["256k AAC (format 141)", res.aac256 ? "Available" : "Not available", res.aac256],
    ], v === "premium" || v === "free" ? "Premium is inferred from YouTube offering the 256k AAC stream. YouTube has no direct Premium flag." : "");
    onChange(res);
  }

  return { probe };
}
