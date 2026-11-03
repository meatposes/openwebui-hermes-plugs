// Hermes side panel: one scrollable, closable drawer on the right. Shared by the Hermes Commands
// action (sent as an Open WebUI "execute" event) and web/loader.js (sidebar Files entry).
(() => {
  if (window.hermesPanel) return;
  const css = `
#hermes-panel-back{position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,.32);opacity:0;transition:opacity .15s}
#hermes-panel-back.on{opacity:1}
#hermes-panel{position:fixed;top:0;right:0;bottom:0;z-index:9999;width:min(600px,100vw);display:flex;flex-direction:column;
  background:#fff;color:#1f2328;box-shadow:-10px 0 28px rgba(0,0,0,.18);transform:translateX(102%);transition:transform .18s ease;
  padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px);padding-right:env(safe-area-inset-right,0px)}
#hermes-panel.on{transform:none}
html.dark #hermes-panel{background:#0d1117;color:#e6edf3;box-shadow:-10px 0 28px rgba(0,0,0,.5)}
#hermes-panel .hp-head{display:flex;align-items:center;gap:8px;padding:10px 10px 10px 16px;border-bottom:1px solid rgba(128,128,128,.25)}
#hermes-panel .hp-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:600 15px/1.3 system-ui,sans-serif}
#hermes-panel .hp-close{border:0;background:transparent;color:inherit;width:34px;height:34px;border-radius:8px;cursor:pointer;
  display:grid;place-items:center}
#hermes-panel .hp-close:hover{background:rgba(128,128,128,.15)}
#hermes-panel .hp-close:focus-visible{outline:2px solid #4c8dff;outline-offset:1px}
#hermes-panel iframe{flex:1;min-height:0;width:100%;border:0;background:transparent}
#hermes-panel .hp-msg{padding:18px 16px;font:14px/1.5 system-ui,sans-serif;opacity:.75}
@media (prefers-reduced-motion:reduce){#hermes-panel,#hermes-panel-back{transition:none}}`;
  let back, panel, title, body, lastFocus;

  function build() {
    if (panel) return;
    const style = document.createElement("style");
    style.id = "hermes-panel-style";
    style.textContent = css;
    document.head.appendChild(style);
    back = document.createElement("div");
    back.id = "hermes-panel-back";
    back.addEventListener("click", close);
    panel = document.createElement("aside");
    panel.id = "hermes-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.innerHTML = '<div class="hp-head"><div class="hp-title"></div><button type="button" class="hp-close" aria-label="Close">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
      '<path d="M6 6l12 12M18 6L6 18"/></svg></button></div><div class="hp-body" style="flex:1;min-height:0;display:flex;flex-direction:column"></div>';
    title = panel.querySelector(".hp-title");
    body = panel.querySelector(".hp-body");
    panel.querySelector(".hp-close").addEventListener("click", close);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && panel.classList.contains("on")) close(); });
    document.body.append(back, panel);
  }

  function show(text) {
    build();
    title.textContent = text;
    if (!panel.classList.contains("on")) {
      lastFocus = document.activeElement;
      back.style.display = panel.style.display = "";
      requestAnimationFrame(() => { back.classList.add("on"); panel.classList.add("on"); });
      panel.querySelector(".hp-close").focus({ preventScroll: true });
    }
  }

  function message(text, note) {
    show(text);
    body.innerHTML = "";
    const p = document.createElement("div");
    p.className = "hp-msg";
    p.textContent = note;
    body.appendChild(p);
  }

  function open(text, html) {
    show(text);
    body.innerHTML = "";
    const frame = document.createElement("iframe");
    frame.title = text;
    // The card repeats its title; the panel header already shows it.
    frame.srcdoc = String(html).replace(/<h3>[\s\S]*?<\/h3>/, "");
    frame.addEventListener("load", () => {
      try {
        const doc = frame.contentDocument;
        doc.documentElement.classList.add(document.documentElement.classList.contains("dark") ? "dark" : "light");
        doc.querySelectorAll("a[href]").forEach((a) => { if (!a.getAttribute("href").includes("attachment=true")) a.target = "_blank"; });
      } catch (e) { /* cross-origin frame: leave it as is */ }
    });
    body.appendChild(frame);
  }

  function close() {
    if (!panel) return;
    back.classList.remove("on");
    panel.classList.remove("on");
    setTimeout(() => { if (!panel.classList.contains("on")) { back.style.display = panel.style.display = "none"; } }, 200);
    if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
  }

  window.hermesPanel = { open, close, loading: (t) => message(t, "Loading…"), error: message };
})();

// Sidebar "Files" entry for people who have a Hermes model with the Hermes Commands action.
// Open WebUI loads /static/loader.js on every page; mount this file there (see README).
(() => {
  const FOLDER = '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" ' +
    'stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"></path>';
  let model = null;

  async function findModel() {
    if (!localStorage.token) return null;
    try {
      const r = await fetch("/api/models", { headers: { Authorization: "Bearer " + localStorage.token } });
      if (!r.ok) return null;
      const list = (await r.json()).data || [];
      return list.find((m) => (m.actions || []).some((a) => String(a.id || "").startsWith("hermes_commands"))) || null;
    } catch (e) {
      return null;
    }
  }

  async function openFiles(e) {
    e.preventDefault();
    e.stopPropagation();
    window.hermesPanel.loading("My files");
    try {
      const r = await fetch("/api/chat/actions/hermes_commands.myfiles", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + localStorage.token },
        body: JSON.stringify({ model: model.id, chat_id: "", id: "", session_id: "", messages: [], hermes_panel_data: true }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.html) throw new Error(data.detail || "Open WebUI answered " + r.status);
      window.hermesPanel.open(data.title || "My files", data.html);
    } catch (err) {
      window.hermesPanel.error("My files", "Could not load your files. " + err.message);
    }
  }

  function addEntries() {
    // Copy the look of a built-in sidebar entry (Notes, else Workspace, else Search), collapsed or open.
    // One entry per sidebar view (collapsed rail, open sidebar), placed after the first built-in that exists.
    let anchors = [];
    for (const sel of ['a[href="/notes"]', 'a[href="/workspace"]', 'button[aria-label="Search"]']) {
      anchors = [...document.querySelectorAll(sel)];
      if (anchors.length) break;
    }
    for (const preferred of anchors) {
      const wrap = preferred.parentElement;
      const next = wrap && wrap.nextElementSibling;
      if (!wrap || (next && next.querySelector("[data-hermes-files]"))) continue;
      const copy = wrap.cloneNode(true);
      const link = copy.querySelector("a, button");
      link.removeAttribute("href");
      link.removeAttribute("id");
      link.setAttribute("role", "button");
      link.setAttribute("tabindex", "0");
      link.setAttribute("aria-label", "Files");
      link.dataset.hermesFiles = "1";
      const svg = copy.querySelector("svg");
      if (svg) svg.innerHTML = FOLDER;
      const walker = document.createTreeWalker(copy, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (/\b(Notes|Workspace|Search)\b/.test(n.nodeValue)) n.nodeValue = n.nodeValue.replace(/\b(Notes|Workspace|Search)\b/, "Files");
      }
      link.addEventListener("click", openFiles);
      link.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") openFiles(e); });
      wrap.after(copy);
    }
  }

  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued || !model) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; addEntries(); });
  });

  async function start() {
    model = await findModel();
    if (!model) {
      // Not signed in yet: look again after the next page change.
      setTimeout(start, 5000);
      return;
    }
    addEntries();
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
