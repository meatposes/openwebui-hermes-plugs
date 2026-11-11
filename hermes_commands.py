"""
title: Hermes Commands
version: 1.3.2
description: Hermes Agent commands and panels on any reply: usage, steps (tool and subagent cards), steer, stop, title, model, cron tasks, skills, memory, todos, insights, files, sessions, status.
required_open_webui_version: 0.12.0
license: MIT
"""

import ast
import asyncio
import base64
import hashlib
import html
import json
import logging
import mimetypes
import re
import time
import uuid
from html.parser import HTMLParser
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_MEDIA_TYPES = {"image", "audio", "video"}
_TEXT_LIMIT = 20000

def _icon(shapes: str) -> str:
    """Button icon as an SVG data URL. Mid grey stroke reads on light and dark themes."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#8a8f98" '
           'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + shapes + '</svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


# Same Hermes file version -> same Open WebUI file id, so "My files" downloads each file once.
_MYFILES_NS = uuid.UUID("0b6a3f4e-2d1c-4f5a-9e8b-7c6d5e4f3a21")

# Side panel script, a copy of web/panel.js (tests check they match). Sent with each result as an
# Open WebUI "execute" event, so the panel works without web/loader.js installed.
PANEL_JS = r"""// Hermes side panel: one scrollable, closable drawer on the right. Shared by the Hermes Commands
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
#hermes-panel.wide{width:min(1280px,100vw)}
html.dark #hermes-panel{background:#0d1117;color:#e6edf3;box-shadow:-10px 0 28px rgba(0,0,0,.5)}
#hermes-panel .hp-head{display:flex;align-items:center;gap:8px;padding:10px 10px 10px 16px;border-bottom:1px solid rgba(128,128,128,.25)}
#hermes-panel .hp-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:600 15px/1.3 system-ui,sans-serif}
#hermes-panel .hp-close{border:0;background:transparent;color:inherit;width:34px;height:34px;border-radius:8px;cursor:pointer;
  display:grid;place-items:center}
#hermes-panel .hp-close:hover{background:rgba(128,128,128,.15)}
#hermes-panel .hp-close:focus-visible{outline:2px solid #4c8dff;outline-offset:1px}
@media (max-width:640px){#hermes-panel .hp-wide{display:none}}
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
    panel.innerHTML = '<div class="hp-head"><div class="hp-title"></div>' +
      '<button type="button" class="hp-close hp-wide" aria-label="Wider panel" title="Wider panel">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
      '<path d="M9 6l-6 6 6 6M15 6l6 6-6 6"/></svg></button>' +
      '<button type="button" class="hp-close" aria-label="Close">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
      '<path d="M6 6l12 12M18 6L6 18"/></svg></button></div><div class="hp-body" style="flex:1;min-height:0;display:flex;flex-direction:column"></div>';
    title = panel.querySelector(".hp-title");
    body = panel.querySelector(".hp-body");
    panel.querySelector(".hp-close:not(.hp-wide)").addEventListener("click", close);
    // Wide view for long listings; remembered per browser.
    try { if (localStorage.getItem("hermesPanelWide") === "1") panel.classList.add("wide"); } catch (e) { /* no storage */ }
    panel.querySelector(".hp-wide").addEventListener("click", () => {
      const wide = panel.classList.toggle("wide");
      try { localStorage.setItem("hermesPanelWide", wide ? "1" : "0"); } catch (e) { /* no storage */ }
    });
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
      panel.querySelector(".hp-close:not(.hp-wide)").focus({ preventScroll: true });
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
        doc.querySelectorAll("a[href]:not([data-hermes-cmd])").forEach((a) => { if (!a.getAttribute("href").includes("attachment=true")) a.target = "_blank"; });
        // Folder and file links in a Files listing: web/loader.js runs them and redraws the panel.
        doc.querySelectorAll("[data-hermes-cmd]").forEach((el) => el.addEventListener("click", (e) => {
          e.preventDefault();
          window.dispatchEvent(new CustomEvent("hermes-panel-command", { detail: el.getAttribute("data-hermes-cmd") }));
        }));
        // Filter box: hides rows of this listing that do not contain the typed text.
        const filter = doc.querySelector("[data-hermes-filter]");
        if (filter) filter.addEventListener("input", () => {
          const q = filter.value.trim().toLowerCase();
          doc.querySelectorAll("tr").forEach((tr) => {
            if (!tr.querySelector("th")) tr.style.display = !q || tr.textContent.toLowerCase().includes(q) ? "" : "none";
          });
        });
        // Search box: asks the server again (Enter), for listings too long to send at once.
        const search = doc.querySelector("[data-hermes-search]");
        if (search) search.addEventListener("keydown", (e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          const words = search.value.trim();
          window.dispatchEvent(new CustomEvent("hermes-panel-command",
            { detail: search.getAttribute("data-hermes-search") + " 1" + (words ? " " + words : "") }));
        });
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
"""


class CommandError(Exception):
    """Shown to the user as the command's result."""


# ---- small helpers --------------------------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def when(ts: Any) -> str:
    """Epoch seconds or an ISO timestamp, as local 'YYYY-MM-DD HH:MM'."""
    if ts in (None, ""):
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(ts)


def num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def size_text(value: Any) -> str:
    """File size for people: 512 B, 3.4 KB, 1.2 MB, 2.0 GB. '' when unknown."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def text_of(content: Any) -> str:
    """Message content as plain text (Open WebUI may send a list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def derive_session_id(profile: str, system_prompt: str, first_user: str) -> str:
    """Hermes's id for a header-less chat-completions conversation (gateway _derive_chat_session_id).
    A named profile prefixes the seed; the default profile does not."""
    seed = f"{system_prompt or ''}\n{first_user}"
    if profile and profile != "default":
        seed = f"{profile}\0{seed}"
    return "api-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def parse_tool_calls(raw: Any) -> list[dict]:
    """Session messages carry tool_calls as JSON or as a Python repr string."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    for parse in (json.loads, ast.literal_eval):
        try:
            value = parse(raw)
            return value if isinstance(value, list) else []
        except (ValueError, SyntaxError, TypeError):
            continue
    return []


def card(title: str, body: str) -> str:
    """One self-contained HTML card, readable in light and dark themes."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        ":root{color-scheme:light dark;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--bg:#ffffff;--chip:#f6f8fa}"
        "@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--bg:#0d1117;--chip:#161b22}}"
        # The side panel marks the card with Open WebUI's own theme, which can differ from the system one.
        ":root.light{color-scheme:light;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--bg:#ffffff;--chip:#f6f8fa}"
        ":root.dark{color-scheme:dark;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--bg:#0d1117;--chip:#161b22}"
        "a{color:#2f6fed}:root.dark a{color:#6ea8ff}"
        "body{margin:0;padding:12px 14px;font:13px/1.45 system-ui,sans-serif;color:var(--fg);background:var(--bg)}"
        "h3{margin:0 0 8px;font-size:14px}table{border-collapse:collapse;width:100%}"
        "th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}"
        "th{color:var(--muted);font-weight:600}pre{white-space:pre-wrap;word-break:break-word;margin:4px 0;"
        "padding:6px 8px;background:var(--chip);border-radius:6px;max-height:320px;overflow:auto;font-size:12px}"
        ".muted{color:var(--muted)}.chip{display:inline-block;padding:0 6px;border-radius:9px;background:var(--chip);"
        "border:1px solid var(--line);font-size:11px}.step{border:1px solid var(--line);border-radius:8px;padding:6px 8px;margin:6px 0}"
        ".sub{margin-left:14px;border-left:3px solid var(--line);padding-left:8px}"
        ".tabs{display:flex;gap:4px;margin:0 0 10px;border-bottom:1px solid var(--line)}"
        ".tabs a{padding:5px 10px;text-decoration:none;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}"
        ".tabs a.on{color:var(--fg);border-bottom-color:#2f6fed;font-weight:600}"
        "input.find{width:100%;box-sizing:border-box;margin:0 0 8px;padding:6px 8px;border:1px solid var(--line);"
        "border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}"
        "img.thumb{width:40px;height:40px;object-fit:cover;border-radius:4px;vertical-align:middle;margin-right:6px}"
        ".nowrap{white-space:nowrap}.pager{display:flex;justify-content:space-between;margin-top:8px}"
        "</style></head><body>"
        f"<h3>{esc(title)}</h3>{body}</body></html>"
    )


class _Markdown(HTMLParser):
    """Turns a card() into Markdown. Open WebUI shows a source's HTML in a frame about 150px tall,
    but Markdown at full height."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.cell: Optional[list[str]] = None
        self.row: list[str] = []
        self.rows: list[list[str]] = []
        self.skip = 0
        self.pre = False
        self.href = ""

    def _write(self, text: str) -> None:
        (self.cell if self.cell is not None else self.out).append(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("style", "h3"):
            self.skip += 1  # the window shows the title already
        elif tag == "table":
            self.rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag == "pre":
            self.pre = True
            self._write("`" if self.cell is not None else "\n```\n")
        elif tag == "b":
            self._write("**")
        elif tag == "code" and not self.pre:
            self._write("`")
        elif tag == "a":
            self.href = attrs.get("href") or ""
            self._write("[")
        elif tag == "img":
            self._write(f"![]({attrs.get('src') or ''})")
        elif tag == "br":
            self._write("<br>" if self.cell is not None else "\n")
        elif tag in ("p", "div") and self.cell is None:
            if "step" in (attrs.get("class") or ""):
                self.out.append("\n\n---\n")
            self.out.append("\n\n")

    def handle_endtag(self, tag):
        if tag in ("style", "h3"):
            self.skip -= 1
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()).replace("|", "\\|") or " ")
            self.cell = None
        elif tag == "tr":
            self.rows.append(self.row)
        elif tag == "table" and self.rows:
            width = max(len(r) for r in self.rows)
            rows = [r + [" "] * (width - len(r)) for r in self.rows]
            lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
            lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            self.out.append("\n\n" + "\n".join(lines) + "\n\n")
        elif tag == "pre":
            self.pre = False
            self._write("`" if self.cell is not None else "\n```\n")
        elif tag == "b":
            self._write("**")
        elif tag == "code" and not self.pre:
            self._write("`")
        elif tag == "a":
            self._write(f"]({self.href})")
        elif tag == "p" and self.cell is None:
            self.out.append("\n\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre and self.cell is not None:
            data = " ".join(data.split())[:200].replace("`", "'")
        elif self.pre:
            data = data.replace("```", "'''")
        self._write(data)


def to_markdown(html_card: str) -> str:
    parser = _Markdown()
    parser.feed(html_card)
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.out)).strip()


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Rows are already-escaped HTML strings."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


# Commands the sidebar Files panel may send by itself (it navigates without a dialog).
_PANEL_COMMANDS = ("browse", "fetch", "myfiles", "owui")

HELP = [
    ("usage", "Tokens, cost, calls and messages for this chat's Hermes session"),
    ("steps [all]", "Tool calls with arguments and results for the last turn (all: whole chat), plus subagents"),
    ("last", "Hermes's latest reply in full, e.g. after the stream was cut off"),
    ("steer <text>", "Add guidance to the turn Hermes is running now"),
    ("stop", "Stop the turn Hermes is running now"),
    ("title <text>", "Rename this chat's Hermes session"),
    ("model [name]", "Show models, or switch this chat's Hermes session to one"),
    ("todos", "Hermes's current task list for this chat"),
    ("cron", "Scheduled tasks: list"),
    ("cron add <schedule> | <prompt>", "New scheduled task, e.g. cron add every 1d | summarise my inbox"),
    ("cron pause|resume|run|delete <id>", "Manage a scheduled task"),
    ("cron runs <id>", "Recent runs of a scheduled task"),
    ("cron edit <id> name|schedule|prompt <value>", "Change a scheduled task"),
    ("skills [search]", "Installed skills"),
    ("skill <name>", "Show one skill"),
    ("skill enable|disable <name>", "Turn a skill on or off"),
    ("memory", "Hermes's memory files (MEMORY.md, USER.md)"),
    ("memory add <text>", "Append a line to MEMORY.md"),
    ("insights [days]", "Token and cost totals per day (default 7 days), by model, top skills"),
    ("myfiles", "Files Hermes made for you and files you attached, newest first, as download links"),
    ("files [path]", "List a folder on the Hermes server"),
    ("browse [path]", "Browse this profile's folders: click a folder to open it, a file to download it"),
    ("file <path>", "Show a text file, or bring an image/audio/video file into the chat"),
    ("workspaces", "Hermes's default folder and registered projects/repos"),
    ("sessions [search]", "Recent Hermes sessions from every surface (CLI, Telegram, Desktop, ...)"),
    ("import <session id>", "Copy a Hermes session into a new Open WebUI chat"),
    ("status", "Hermes version, profile, model, toolsets, scheduled tasks"),
    ("forget", "Delete this chat's Hermes session (asks first)"),
]


class Action:
    class Valves(BaseModel):
        hermes_api_url: str = Field(
            default="",
            title="Hermes API URL (optional)",
            description="Empty = the model's own Open WebUI connection (ending in /v1).",
        )
        hermes_api_key: str = Field(default="", title="Hermes API Key (optional)")
        dashboard_url: str = Field(
            default="",
            title="Dashboard URL (optional)",
            description="Needed for skills, memory, insights, files and cron history. Empty = use the Hermes "
            "Media filter's settings.",
        )
        refresh_token: str = Field(
            default="",
            title="Dashboard Refresh Token (optional)",
            description="Empty = use the Hermes Media filter's token.",
        )
        file_folders: str = Field(
            default="",
            title="Browsable Folders (optional)",
            description="Comma-separated folders files/file/memory may read. Empty = this profile's own folder "
            "only (for the default profile: its folder minus other profiles' folders).",
        )
        user_profiles: str = Field(
            default="",
            title="Sidebar Files: Profile per User (optional)",
            description="Comma-separated user=profile pairs for the sidebar Files panel, e.g. 'alice=default, "
            "bob=work'. A user is matched by Open WebUI username, email or name. Unlisted users get the "
            "profile of the Hermes model the panel uses.",
        )
        myfiles_max_mb: int = Field(
            default=200,
            title="My Files: Max File Size (MB)",
            description="Larger files are listed without a download link.",
        )
        profile: str = Field(
            default="",
            title="Hermes Profile (optional)",
            description="Empty = read it from the connection URL (/p/<profile>/v1), else 'default'.",
        )
        show_in: str = Field(
            default="panel",
            title="Show Results In",
            description="'panel': a side panel that opens straight away, scrolls, and closes with X or Esc. "
            "'chip': a chip under the reply that opens the result in a window. 'inline': a card at the top of the reply.",
        )

    actions = [
        {"id": "command", "name": "Hermes command", "icon_url": _icon(
            '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>')},
        {"id": "myfiles", "name": "My files", "icon_url": _icon(
            '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>')},
        {"id": "steps", "name": "Hermes steps", "icon_url": _icon(
            '<path d="M3 6l2 2 3-3M3 13l2 2 3-3"/><line x1="11" y1="7" x2="21" y2="7"/>'
            '<line x1="11" y1="14" x2="21" y2="14"/><line x1="11" y1="20" x2="21" y2="20"/>')},
        {"id": "usage", "name": "Hermes usage", "icon_url": _icon(
            '<line x1="6" y1="20" x2="6" y2="13"/><line x1="12" y1="20" x2="12" y2="8"/><line x1="18" y1="20" x2="18" y2="4"/>')},
    ]

    def __init__(self):
        self.valves = self.Valves()
        self._dash_token = ""
        self._dash_expires = 0.0
        self._lock = asyncio.Lock()

    # ---- connections -----------------------------------------------------------------------------

    async def _api(self, model: Optional[dict]) -> tuple[str, str, str]:
        """(API root without /v1, key, profile)."""
        if self.valves.hermes_api_url.strip():
            url, key = self.valves.hermes_api_url.strip(), self.valves.hermes_api_key.strip()
        else:
            idx = (model or {}).get("urlIdx")
            if idx is None:
                raise CommandError("This model has no Open WebUI connection; set Hermes API URL and Key.")
            from open_webui.routers.openai import get_openai_connection

            url, key, _ = await get_openai_connection(idx)
        url = url.rstrip("/")
        root = url[:-3] if url.endswith("/v1") else url
        match = re.search(r"/p/([^/]+)$", root)
        profile = self.valves.profile.strip() or (match.group(1) if match else "default")
        return root, key, profile

    async def _http(self, method: str, url: str, *, token: str = "", json_body=None, params=None,
                    raw: bool = False, timeout: int = 30):
        import aiohttp

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.request(method, url, json=json_body, params=params, headers=headers) as resp:
                    if raw:
                        if resp.status != 200:
                            raise CommandError(f"Hermes answered {resp.status} for {url.split('/api/')[-1]}")
                        return await resp.read(), resp.headers.get("Content-Type", "")
                    text = await resp.text()
                    try:
                        data = json.loads(text) if text else {}
                    except ValueError:
                        data = {"raw": text}
                    if resp.status >= 400:
                        message = data.get("error") if isinstance(data, dict) else None
                        if isinstance(message, dict):
                            message = message.get("message")
                        message = message or (data.get("detail") if isinstance(data, dict) else None) or text[:200]
                        raise CommandError(f"Hermes answered {resp.status}: {message}")
                    return data
        except aiohttp.ClientError as exc:
            raise CommandError(f"Cannot reach Hermes: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise CommandError("Hermes did not answer in time.") from exc

    async def _dashboard(self) -> tuple[str, str]:
        """(dashboard base URL, access token). Settings fall back to the Hermes Media filter's."""
        url, refresh = self.valves.dashboard_url.strip(), self.valves.refresh_token.strip()
        if not url or not refresh:
            try:
                from open_webui.models.functions import Functions

                media = await Functions.get_function_valves_by_id("hermes_media") or {}
                url = url or str(media.get("dashboard_url") or "")
                refresh = refresh or str(media.get("refresh_token") or "")
            except Exception:
                pass
        if not url or not refresh:
            raise CommandError("This command needs the Hermes dashboard. Set Dashboard URL and Refresh Token "
                               "here, or install and configure the Hermes Media filter.")
        url = url.rstrip("/")
        async with self._lock:
            if self._dash_token and time.time() < self._dash_expires - 60:
                return url, self._dash_token
            data = await self._http("POST", f"{url}/auth/native/refresh", json_body={"refresh_token": refresh})
            self._dash_token = data.get("access_token", "")
            self._dash_expires = float(data.get("expires_at") or time.time() + 3600)
            return url, self._dash_token

    async def _dash(self, method: str, path: str, profile: str, **kwargs):
        url, token = await self._dashboard()
        params = dict(kwargs.pop("params", None) or {})
        if profile and profile != "default":
            params.setdefault("profile", profile)
        return await self._http(method, f"{url}{path}", token=token, params=params or None, **kwargs)

    # ---- this chat's Hermes session ---------------------------------------------------------------

    async def _session_id(self, body: dict, model: Optional[dict], root: str, key: str, profile: str) -> str:
        messages = body.get("messages") or []
        system = next((text_of(m.get("content")) for m in messages if m.get("role") == "system"), "")
        if not system:
            system = (((model or {}).get("info") or {}).get("params") or {}).get("system") or ""
        first_user = next((text_of(m.get("content")) for m in messages if m.get("role") == "user"), "")
        if not first_user:
            raise CommandError("This chat has no message for Hermes yet.")
        candidate = derive_session_id(profile, system, first_user)
        try:
            await self._http("GET", f"{root}/api/sessions/{candidate}", token=key)
            return candidate
        except CommandError:
            pass
        # Fallback: match the opening message against recent sessions' previews.
        listing = await self._http("GET", f"{root}/api/sessions", token=key, params={"limit": 200})
        opening = " ".join(first_user.split())[:50]
        for row in listing.get("data") or []:
            preview = " ".join((row.get("preview") or "").split())
            if opening and preview.startswith(opening[: max(10, len(preview.rstrip('.')) - 3)]):
                return row["id"]
        raise CommandError("Could not find this chat's Hermes session (has Hermes answered in this chat yet?).")

    async def _messages(self, root: str, key: str, session_id: str) -> list[dict]:
        out, offset = [], 0
        while True:
            page = await self._http("GET", f"{root}/api/sessions/{session_id}/messages", token=key,
                                    params={"limit": 500, "offset": offset})
            rows = page.get("data") or []
            out += rows
            # Hermes 0.22 sends no has_more; a full page means there may be more.
            more = (page.get("pagination") or {}).get("has_more", len(rows) >= 500)
            if not more or not rows:
                return out
            offset += len(rows)

    # ---- commands --------------------------------------------------------------------------------

    async def cmd_help(self, ctx, arg):
        rows = [[f"<code>{esc(c)}</code>", esc(d)] for c, d in HELP]
        return card("Hermes commands", table(["Command", "What it does"], rows)
                    + "<p class='muted'>Type a command in the Hermes command dialog. A leading / is optional.</p>")

    async def cmd_usage(self, ctx, arg):
        sid = await ctx["session"]()
        s = (await self._http("GET", f"{ctx['root']}/api/sessions/{sid}", token=ctx["key"])).get("session", {})
        cost = s.get("actual_cost_usd") if s.get("actual_cost_usd") is not None else s.get("estimated_cost_usd")
        rows = [
            ["Input tokens", num(s.get("input_tokens"))], ["Output tokens", num(s.get("output_tokens"))],
            ["Reasoning tokens", num(s.get("reasoning_tokens"))], ["Cache read / write",
                                                                    f"{num(s.get('cache_read_tokens'))} / {num(s.get('cache_write_tokens'))}"],
            ["Model calls", num(s.get("api_call_count"))], ["Tool calls", num(s.get("tool_call_count"))],
            ["Messages", num(s.get("message_count"))], ["Cost (USD)", esc(f"{float(cost or 0):.4f}")],
            ["Model", esc(s.get("model"))], ["Started", esc(when(s.get("started_at")))],
            ["Last active", esc(when(s.get("last_active")))], ["Session", f"<code>{esc(sid)}</code>"],
        ]
        return card(f"Usage: {s.get('title') or sid}", table(["", ""], rows))

    def _steps_html(self, messages: list[dict], whole: bool) -> tuple[str, int]:
        start = 0
        if not whole:
            users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
            start = users[-1] if users else 0
        results = {m.get("tool_call_id"): m for m in messages if m.get("role") == "tool"}
        parts, count = [], 0
        for m in messages[start:]:
            if m.get("role") != "assistant":
                continue
            for call in parse_tool_calls(m.get("tool_calls")):
                fn = call.get("function") or {}
                name = fn.get("name") or call.get("name") or "tool"
                args = fn.get("arguments") or call.get("arguments") or ""
                try:
                    args = json.dumps(json.loads(args), indent=1, ensure_ascii=False) if isinstance(args, str) else json.dumps(args, indent=1)
                except ValueError:
                    pass
                result = results.get(call.get("id") or call.get("call_id"), {}).get("content") or ""
                count += 1
                subagents = self._subagent_html(result) if name == "delegate_task" else ""
                parts.append(
                    f"<div class='step'><b>{count}. {esc(name)}</b> <span class='muted'>{esc(when(m.get('timestamp')))}</span>"
                    f"<div class='muted'>Arguments</div><pre>{esc(str(args)[:1500])}</pre>"
                    + (subagents or f"<div class='muted'>Result</div><pre>{esc(str(result)[:2000])}</pre>") + "</div>")
        return "".join(parts), count

    @staticmethod
    def _subagent_html(result: str) -> str:
        """Subagent cards from a delegate_task result: status, summary, the tools each one used."""
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            return ""
        cards = []
        for r in data.get("results") or []:
            tools = ", ".join(f"{t.get('tool')} ({t.get('status')})" for t in r.get("tool_trace") or []) or "none"
            tokens = r.get("tokens") or {}
            cards.append(
                f"<div class='step sub'><b>Subagent {int(r.get('task_index', 0)) + 1}</b> "
                f"<span class='chip'>{esc(r.get('status'))}</span> <span class='muted'>{esc(r.get('duration_seconds'))} s, "
                f"{num(tokens.get('input'))} in / {num(tokens.get('output'))} out, {num(r.get('api_calls'))} model calls</span>"
                f"<div class='muted'>Tools: {esc(tools)}</div><pre>{esc(str(r.get('summary') or '')[:3000])}</pre></div>")
        return "".join(cards)

    async def cmd_steps(self, ctx, arg):
        sid = await ctx["session"]()
        messages = await self._messages(ctx["root"], ctx["key"], sid)
        whole = arg.strip().lower() == "all"
        body, count = self._steps_html(messages, whole)
        listing = await self._http("GET", f"{ctx['root']}/api/sessions", token=ctx["key"],
                                   params={"limit": 200, "include_children": "true"})
        children = [r for r in listing.get("data") or [] if r.get("parent_session_id") == sid]
        if not whole and children:
            users = [m for m in messages if m.get("role") == "user"]
            since = float(users[-1].get("timestamp") or 0) if users else 0
            children = [c for c in children if float(c.get("started_at") or 0) >= since]
        for child in children:
            child_messages = await self._messages(ctx["root"], ctx["key"], child["id"])
            child_body, child_count = self._steps_html(child_messages, True)
            body += (f"<div class='step sub'><b>Subagent session: {esc(child.get('title') or child['id'])}</b> "
                     f"<span class='chip'>{child_count} steps</span>"
                     f"<span class='muted'> {num(child.get('message_count'))} messages</span>{child_body}</div>")
        if not body:
            body = "<p class='muted'>No tool calls in this turn.</p>"
        scope = "whole chat" if whole else "last turn"
        return card(f"Hermes steps ({scope}): {count} tool calls, {len(children)} subagents", body)

    async def cmd_last(self, ctx, arg):
        sid = await ctx["session"]()
        messages = await self._messages(ctx["root"], ctx["key"], sid)
        reply = next((m for m in reversed(messages) if m.get("role") == "assistant" and (m.get("content") or "").strip()), None)
        if not reply:
            raise CommandError("Hermes has no reply text in this session yet.")
        return card(f"Hermes's latest reply ({when(reply.get('timestamp'))})", f"<pre>{esc(reply['content'][:_TEXT_LIMIT])}</pre>")

    def _live_run(self, ctx) -> str:
        registry = getattr(ctx["request"].app.state, "hermes_active_runs", {}) if ctx["request"] else {}
        run = registry.get(ctx["chat_id"]) if ctx["chat_id"] else None
        if not run:
            raise CommandError("No Hermes turn is running in this chat right now (needs the Hermes Stop filter).")
        return run["run_id"]

    async def cmd_steer(self, ctx, arg):
        if not arg.strip():
            raise CommandError("Usage: steer <guidance for the running turn>")
        run_id = self._live_run(ctx)
        await self._http("POST", f"{ctx['root']}/v1/runs/{run_id}/steer", token=ctx["key"], json_body={"input": arg.strip()})
        return card("Guidance sent", f"<p>Hermes will see this on its next step:</p><pre>{esc(arg.strip())}</pre>")

    async def cmd_stop(self, ctx, arg):
        run_id = self._live_run(ctx)
        data = await self._http("POST", f"{ctx['root']}/v1/runs/{run_id}/stop", token=ctx["key"])
        return card("Stop sent", f"<p>Hermes run <code>{esc(run_id)}</code>: {esc(data.get('status'))}</p>")

    async def cmd_title(self, ctx, arg):
        if not arg.strip():
            raise CommandError("Usage: title <new title>")
        sid = await ctx["session"]()
        await self._http("PATCH", f"{ctx['root']}/api/sessions/{sid}", token=ctx["key"], json_body={"title": arg.strip()})
        try:
            from open_webui.models.chats import Chats

            await Chats.update_chat_title_by_id(ctx["chat_id"], arg.strip())
        except Exception:
            log.info("Hermes Commands: could not rename the Open WebUI chat too", exc_info=True)
        return card("Renamed", f"<p>Hermes session and chat are now titled <b>{esc(arg.strip())}</b>.</p>")

    async def cmd_forget(self, ctx, arg):
        sid = await ctx["session"]()
        call = ctx.get("call")
        if call:
            ok = await call({"type": "confirmation", "data": {
                "title": "Delete this chat's Hermes session?",
                "message": f"Hermes forgets session {sid} and its history. The Open WebUI chat stays."}})
            if not ok:
                return card("Kept", "<p>Nothing was deleted.</p>")
        await self._http("DELETE", f"{ctx['root']}/api/sessions/{sid}", token=ctx["key"])
        return card("Hermes session deleted", f"<p><code>{esc(sid)}</code> is gone from Hermes. "
                    "A new message here starts a fresh Hermes session.</p>")

    async def cmd_model(self, ctx, arg):
        data = await self._http("GET", f"{ctx['root']}/api/model/options", token=ctx["key"])
        providers = [p for p in data.get("providers") or [] if p.get("models") or p.get("is_current")]
        if arg.strip():
            name = arg.strip()
            provider = next((p["slug"] for p in providers if name in (p.get("models") or [])), None)
            sid = await ctx["session"]()
            body = {"model": name} | ({"provider": provider} if provider else {})
            await self._http("POST", f"{ctx['root']}/api/sessions/{sid}/model", token=ctx["key"], json_body=body)
            return card("Model switched", f"<p>This chat's Hermes session now uses <b>{esc(name)}</b>"
                        + (f" via {esc(provider)}" if provider else "") + ".</p>")
        rows = [[esc(p.get("name") or p.get("slug")) + (" <span class='chip'>current</span>" if p.get("is_current") else ""),
                 esc(", ".join(p.get("models") or []))] for p in providers]
        return card("Models", table(["Provider", "Models"], rows) + "<p class='muted'>Switch with: model &lt;name&gt;</p>")

    async def cmd_todos(self, ctx, arg):
        sid = await ctx["session"]()
        messages = await self._messages(ctx["root"], ctx["key"], sid)
        latest = None
        for m in messages:
            if m.get("role") == "tool" and (m.get("tool_name") or "").startswith("todo"):
                latest = m
        if not latest:
            return card("Todos", "<p class='muted'>Hermes has not made a task list in this chat.</p>")
        try:
            data = json.loads(latest.get("content") or "{}")
        except ValueError:
            data = {}
        items = data.get("todos") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return card("Todos", f"<pre>{esc((latest.get('content') or '')[:4000])}</pre>")
        mark = {"completed": "☑", "in_progress": "▶", "pending": "☐", "cancelled": "✕"}
        rows = [[esc(mark.get(t.get("status"), "•")), esc(t.get("content")), esc(t.get("status"))] for t in items]
        return card(f"Todos ({when(latest.get('timestamp'))})", table(["", "Task", "Status"], rows))

    async def cmd_cron(self, ctx, arg):
        words = arg.split(None, 1)
        sub = words[0].lower() if words else "list"
        rest = words[1].strip() if len(words) > 1 else ""
        base = f"{ctx['root']}/api/jobs"
        if sub == "add":
            if "|" not in rest:
                raise CommandError("Usage: cron add <schedule> | <prompt>   e.g. cron add every 1d | summarise my inbox")
            schedule, prompt = (x.strip() for x in rest.split("|", 1))
            data = await self._http("POST", base, token=ctx["key"],
                                    json_body={"name": prompt[:40], "schedule": schedule, "prompt": prompt})
            job = data.get("job") or data
            return card("Scheduled task created", f"<p><b>{esc(job.get('name'))}</b> <code>{esc(job.get('id'))}</code> "
                        f"runs {esc(job.get('schedule_display') or schedule)}.</p>")
        if sub == "edit":
            parts = rest.split(None, 2)
            if len(parts) < 3 or parts[1] not in ("name", "schedule", "prompt"):
                raise CommandError("Usage: cron edit <id> name|schedule|prompt <new value>")
            data = await self._http("PATCH", f"{base}/{parts[0]}", token=ctx["key"], json_body={parts[1]: parts[2]})
            job = data.get("job") or data
            shown = job.get("schedule_display") if parts[1] == "schedule" else job.get(parts[1])
            return card("Scheduled task updated", f"<p><code>{esc(parts[0])}</code> {esc(parts[1])} is now "
                        f"<b>{esc(shown if isinstance(shown, str) and shown else parts[2])}</b>.</p>")
        if sub in ("pause", "resume", "run", "delete", "runs"):
            if not rest:
                raise CommandError(f"Usage: cron {sub} <job id>")
            if sub == "runs":
                data = await self._dash("GET", f"/api/cron/jobs/{rest}/runs", ctx["profile"])
                runs = data if isinstance(data, list) else data.get("runs") or []
                rows = [[esc(when(r.get("started_at") or r.get("run_at"))), esc(r.get("status")),
                         f"<pre>{esc(str(r.get('output') or r.get('error') or '')[:600])}</pre>"] for r in runs]
                return card(f"Runs of {rest}", table(["When", "Status", "Output"], rows) if rows else "<p class='muted'>No runs yet.</p>")
            if sub == "delete":
                await self._http("DELETE", f"{base}/{rest}", token=ctx["key"])
            else:
                await self._http("POST", f"{base}/{rest}/{sub}", token=ctx["key"])
            return card("Scheduled task", f"<p><code>{esc(rest)}</code>: {esc(sub)} done.</p>")
        data = await self._http("GET", base, token=ctx["key"])
        jobs = data.get("jobs") or []
        rows = [[f"<code>{esc(j.get('id'))}</code>", esc(j.get("name")), esc(j.get("schedule_display") or j.get("schedule")),
                 esc("on" if j.get("enabled", True) else "paused"), esc(when(j.get("next_run_at"))),
                 esc(" ".join(x for x in (j.get("last_status") or "", when(j.get("last_run_at")), j.get("last_error") or "") if x))]
                for j in jobs]
        return card(f"Scheduled tasks ({len(jobs)})",
                    table(["Id", "Name", "Schedule", "State", "Next run", "Last result"], rows) if rows
                    else "<p class='muted'>No scheduled tasks. Add one with: cron add &lt;schedule&gt; | &lt;prompt&gt;</p>")

    async def cmd_skills(self, ctx, arg):
        skills = await self._dash("GET", "/api/skills", ctx["profile"])
        query = arg.strip().lower()
        if query:
            skills = [s for s in skills if query in (s.get("name", "") + " " + s.get("description", "") + " " + s.get("category", "")).lower()]
        rows = [[esc(s.get("name")), esc(s.get("category")), esc(s.get("description")),
                 esc("on" if s.get("enabled") else "off"), num(s.get("usage"))] for s in skills]
        return card(f"Skills ({len(rows)})", table(["Name", "Category", "Description", "State", "Uses"], rows))

    async def cmd_skill(self, ctx, arg):
        words = arg.split(None, 1)
        if not words:
            raise CommandError("Usage: skill <name>  |  skill enable <name>  |  skill disable <name>")
        if words[0].lower() in ("enable", "disable") and len(words) > 1:
            name = words[1].strip()
            await self._dash("PUT", "/api/skills/toggle", ctx["profile"],
                             json_body={"name": name, "enabled": words[0].lower() == "enable"})
            return card("Skill updated", f"<p><b>{esc(name)}</b> is now {esc(words[0].lower())}d.</p>")
        data = await self._dash("GET", "/api/skills/content", ctx["profile"], params={"name": arg.strip()})
        content = data.get("content") if isinstance(data, dict) else data
        return card(f"Skill: {arg.strip()}", f"<pre>{esc(str(content)[:_TEXT_LIMIT])}</pre>")

    def _home(self, profile: str) -> str:
        return "/opt/data" if profile in ("", "default") else f"/opt/data/profiles/{profile}"

    def _check_path(self, ctx, path: str) -> str:
        """Keep file browsing inside this profile: its own folder by default, never other profiles'."""
        import posixpath

        path = posixpath.normpath(path)
        if not path.startswith("/"):
            raise CommandError("Use an absolute path, e.g. files " + self._home(ctx["profile"]))
        roots = [r.strip().rstrip("/") for r in self.valves.file_folders.split(",") if r.strip()]
        home = self._home(ctx["profile"])
        if not roots:
            roots = [home]
            if ctx["profile"] in ("", "default") and (path == "/opt/data/profiles" or path.startswith("/opt/data/profiles/")):
                raise CommandError("That folder belongs to other Hermes profiles.")
        if not any(path == r or path.startswith(r + "/") for r in roots):
            raise CommandError(f"Only these folders can be browsed: {', '.join(roots)}")
        return path

    async def _read_text(self, ctx, path: str) -> str:
        data = await self._dash("GET", "/api/files/read", ctx["profile"], params={"path": path})
        url = data.get("data_url") or ""
        return base64.b64decode(url.split(",", 1)[1]).decode("utf-8", "replace") if "," in url else ""

    async def _memory_root(self, ctx) -> str:
        if not hasattr(self, "_memory_roots"):
            self._memory_roots = {}
        if ctx["profile"] not in self._memory_roots:
            try:
                profiles = (await self._dash("GET", "/api/profiles", "")).get("profiles") or []
                home = next((p.get("path") for p in profiles if p.get("name") == ctx["profile"]), None)
            except CommandError:
                home = None
            self._memory_roots[ctx["profile"]] = (home or self._home(ctx["profile"])).rstrip("/") + "/memories"
        return self._memory_roots[ctx["profile"]]

    async def cmd_memory(self, ctx, arg):
        root = await self._memory_root(ctx)
        words = arg.split(None, 1)
        if words and words[0].lower() == "add":
            if len(words) < 2:
                raise CommandError("Usage: memory add <text>")
            path = f"{root}/MEMORY.md"
            try:
                current = await self._read_text(ctx, path)
            except CommandError:
                current = ""
            updated = (current.rstrip("\n") + "\n" if current.strip() else "") + words[1].strip() + "\n"
            data_url = "data:text/markdown;base64," + base64.b64encode(updated.encode()).decode()
            await self._dash("POST", "/api/files/upload", ctx["profile"],
                             json_body={"path": path, "data_url": data_url, "overwrite": True})
            return card("Memory updated", f"<p>Added to MEMORY.md:</p><pre>{esc(words[1].strip())}</pre>")
        body = ""
        for name in ("MEMORY.md", "USER.md"):
            try:
                text = await self._read_text(ctx, f"{root}/{name}")
            except CommandError:
                text = ""
            body += f"<div class='step'><b>{esc(name)}</b><pre>{esc(text[:_TEXT_LIMIT]) or '<span class=muted>empty</span>'}</pre></div>"
        return card("Hermes memory", body)

    async def cmd_insights(self, ctx, arg):
        days = int(arg) if arg.strip().isdigit() else 7
        data = await self._dash("GET", "/api/analytics/usage", ctx["profile"], params={"days": max(1, min(days, 365))})
        daily = data.get("daily") or []
        peak = max([d.get("input_tokens", 0) + d.get("output_tokens", 0) for d in daily] or [1]) or 1
        bars = "".join(
            f"<tr><td>{esc(d.get('day'))}</td><td style='width:55%'><div style='background:#2f81f7;height:10px;border-radius:3px;"
            f"width:{max(1, int(100 * (d.get('input_tokens', 0) + d.get('output_tokens', 0)) / peak))}%'></div></td>"
            f"<td>{num(d.get('input_tokens'))} in / {num(d.get('output_tokens'))} out</td><td>{num(d.get('sessions'))}</td>"
            f"<td>{esc(format(float(d.get('estimated_cost') or 0), '.4f'))}</td></tr>"
            for d in daily)
        models = table(["Model", "Input", "Output", "Calls"], [[esc(m.get("model")), num(m.get("input_tokens")),
                        num(m.get("output_tokens")), num(m.get("api_calls"))] for m in data.get("by_model") or []])
        try:
            skills = sorted(await self._dash("GET", "/api/skills", ctx["profile"]), key=lambda s: -(s.get("usage") or 0))[:10]
            skills = [s for s in skills if s.get("usage")]
        except CommandError:
            skills = []
        top = table(["Skill", "Uses"], [[esc(s.get("name")), num(s.get("usage"))] for s in skills]) if skills else "<p class='muted'>No skill use recorded.</p>"
        return card(f"Insights: last {days} days",
                    "<table><tr><th>Day</th><th>Tokens</th><th></th><th>Sessions</th><th>Cost</th></tr>"
                    f"{bars}</table><h3 style='margin-top:12px'>By model</h3>{models}"
                    f"<h3 style='margin-top:12px'>Top skills</h3>{top}")

    async def cmd_files(self, ctx, arg):
        path = arg.strip()
        if not path:
            try:
                path = (await self._dash("GET", "/api/chat/workspaces", ctx["profile"])).get("default_cwd") or ""
            except CommandError:
                path = ""
            path = path or self._home(ctx["profile"])
            try:
                self._check_path(ctx, path)
            except CommandError:
                path = self._home(ctx["profile"])
        path = self._check_path(ctx, path)
        data = await self._dash("GET", "/api/files", ctx["profile"], params={"path": path})
        badge = ""
        try:
            git = await self._dash("GET", "/api/git/status", "", params={"path": data.get("path") or path})
            if isinstance(git, dict) and git.get("branch"):
                badge = (f"<p><span class='chip'>git: {esc(git['branch'])}</span> "
                         f"<span class='muted'>{num(git.get('changed'))} changed, {num(git.get('untracked'))} untracked"
                         f"{', ' + num(git.get('ahead')) + ' ahead' if git.get('ahead') else ''}</span></p>")
        except CommandError:
            pass
        entries = data.get("entries") or []
        if ctx["profile"] in ("", "default") and not self.valves.file_folders.strip():
            entries = [e for e in entries if not str(e.get("path", "")).startswith("/opt/data/profiles")]
        rows = [[("📁 " if e.get("is_directory") else "📄 ") + f"<code>{esc(e.get('path'))}</code>",
                 esc("" if e.get("is_directory") else size_text(e.get("size"))), esc(when(e.get("mtime")))]
                for e in entries]
        return card(f"Files: {data.get('path') or path}", badge + table(["Path", "Size", "Modified"], rows)
                    + "<p class='muted'>Open one with: file &lt;path&gt;. List a folder with: files &lt;path&gt;</p>")

    def _user_profile(self, user: Optional[dict]) -> str:
        """Hermes profile for this Open WebUI user from the Profile per User setting, or ''."""
        names = {str((user or {}).get(k) or "").strip().lower() for k in ("username", "email", "name")}
        email = str((user or {}).get("email") or "").lower()
        if "@" in email:
            names.add(email.split("@", 1)[0])
        names.discard("")
        for pair in self.valves.user_profiles.split(","):
            who, _, profile = pair.partition("=")
            if who.strip().lower() in names and profile.strip():
                return profile.strip()
        return ""

    def _browse_link(self, command: str, label: str) -> str:
        return f"<a href='#' data-hermes-cmd='{esc(command)}'>{label}</a>"

    @staticmethod
    def _files_tabs(active: str) -> str:
        """The two kinds of files the Files panel shows: Hermes' own folders, and Open WebUI's files."""
        tabs = [("owui", "Open WebUI"), ("browse", "Hermes")]
        return "<p class='tabs'>" + "".join(
            f"<a href='#' data-hermes-cmd='{cmd}'{' class=on' if cmd == active else ''}>{label}</a>" for cmd, label in tabs
        ) + "</p>"

    async def cmd_browse(self, ctx, arg):
        """One folder of this profile's files for the side panel: folders open in place, files download."""
        home = self._home(ctx["profile"])
        path = self._check_path(ctx, arg.strip() or home)
        data = await self._dash("GET", "/api/files", ctx["profile"], params={"path": path})
        path = str(data.get("path") or path).rstrip("/") or "/"
        entries = [e for e in data.get("entries") or [] if not str(e.get("path", "")).rsplit("/", 1)[-1].startswith(".")]
        if ctx["profile"] in ("", "default") and not self.valves.file_folders.strip():
            entries = [e for e in entries if not str(e.get("path", "")).startswith("/opt/data/profiles")]
        roots = [r.strip().rstrip("/") for r in self.valves.file_folders.split(",") if r.strip()] or [home]
        root = next((r for r in roots if path == r or path.startswith(r + "/")), home)
        crumbs, walked = [self._browse_link(f"browse {root}", esc(root))], root
        for part in [p for p in path[len(root):].split("/") if p]:
            walked += "/" + part
            crumbs.append(self._browse_link(f"browse {walked}", esc(part)))
        rows = []
        if path != root:
            rows.append([self._browse_link(f"browse {path.rsplit('/', 1)[0] or '/'}", "&#8593; Up"), "", ""])
        key = lambda e: str(e.get("path", "")).lower()  # noqa: E731
        for e in sorted((e for e in entries if e.get("is_directory")), key=key):
            p = str(e.get("path"))
            rows.append([self._browse_link(f"browse {p}", "📁 " + esc(p.rsplit("/", 1)[-1])), "", esc(when(e.get("mtime")))])
        for e in sorted((e for e in entries if not e.get("is_directory")), key=key):
            p = str(e.get("path"))
            rows.append([self._browse_link(f"fetch {p}", "📄 " + esc(p.rsplit("/", 1)[-1])), esc(size_text(e.get("size"))),
                         esc(when(e.get("mtime")))])
        listing = table(["Name", "Size", "Modified"], rows) if rows else "<p class='muted'>This folder is empty.</p>"
        find = ("<input class='find' type='search' data-hermes-filter placeholder='Filter this folder'>"
                if len(entries) > 10 else "")
        return card(f"Files: {path}", self._files_tabs("browse") + find
                    + "<p class='crumbs'>" + " / ".join(crumbs) + "</p>" + listing)

    _OWUI_PAGE = 100

    async def _owui_files(self, user_id: str, query: str, skip: int, limit: int) -> tuple[list[dict], Optional[int]]:
        """This user's own Open WebUI files, newest first: one page, plus the total when there is no search.
        A search matches the file name, an image's prompt, or the title of a chat the file is in."""
        from sqlalchemy import func, or_, select
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat, ChatFile
        from open_webui.models.files import File

        async with get_async_db_context() as db:
            stmt = select(File.id, File.filename, File.meta, File.created_at, File.path).where(File.user_id == user_id)
            if query:
                like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                in_chat = (select(ChatFile.file_id).join(Chat, Chat.id == ChatFile.chat_id)
                           .where(Chat.user_id == user_id, Chat.title.ilike(like, escape="\\")))
                stmt = stmt.where(or_(File.filename.ilike(like, escape="\\"),
                                      File.meta["data"]["prompt"].as_string().ilike(like, escape="\\"),
                                      File.id.in_(in_chat)))
                total = None
            else:
                total = (await db.execute(select(func.count()).select_from(File).where(File.user_id == user_id))).scalar()
            found = (await db.execute(stmt.order_by(File.created_at.desc(), File.id.desc()).offset(skip).limit(limit + 1))).all()
            rows = [{"id": r.id, "filename": r.filename, "meta": r.meta or {}, "created_at": r.created_at, "path": r.path}
                    for r in found]
            ids = [r["id"] for r in rows]
            chats: dict = {}
            if ids:
                linked = select(ChatFile.file_id, Chat.id, Chat.title).join(Chat, Chat.id == ChatFile.chat_id).where(
                    ChatFile.file_id.in_(ids), Chat.user_id == user_id)
                for fid, cid, title in (await db.execute(linked)).all():
                    chats.setdefault(fid, (cid, title))
                wanted = {str(((r["meta"].get("data") or {}) if isinstance(r["meta"].get("data"), dict) else {}).get("chat_id") or "")
                          for r in rows if r["id"] not in chats} - {""}
                if wanted:
                    titles = dict((await db.execute(select(Chat.id, Chat.title).where(
                        Chat.id.in_(wanted), Chat.user_id == user_id))).all())
                    for r in rows:
                        cid = str(((r["meta"].get("data") or {}) if isinstance(r["meta"].get("data"), dict) else {}).get("chat_id") or "")
                        if r["id"] not in chats and cid in titles:
                            chats[r["id"]] = (cid, titles[cid])
            for r in rows:
                r["chat"] = chats.get(r["id"])
        return rows, total

    _THUMB_PX = 96

    def _thumb(self, file_id: str, path: Optional[str]) -> str:
        """A small WebP preview of an image file as a data: URL, made once and kept in Open WebUI's cache
        folder (files never change after upload). '' when the file is not local or not an image."""
        import base64
        import io
        import os

        safe = re.sub(r"[^A-Za-z0-9-]", "", str(file_id))
        try:
            from open_webui.config import CACHE_DIR

            cached = os.path.join(str(CACHE_DIR), "hermes-thumbs", safe + ".webp") if safe else ""
        except Exception:
            cached = ""
        try:
            if cached and os.path.isfile(cached):
                with open(cached, "rb") as fh:
                    data = fh.read()
            else:
                if not path or not os.path.isfile(path):
                    return ""
                from PIL import Image

                with Image.open(path) as im:
                    im.draft("RGB", (self._THUMB_PX * 2, self._THUMB_PX * 2))
                    im.thumbnail((self._THUMB_PX, self._THUMB_PX))
                    small = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
                buf = io.BytesIO()
                small.save(buf, "WEBP", quality=70)
                data = buf.getvalue()
                if cached:
                    try:
                        os.makedirs(os.path.dirname(cached), exist_ok=True)
                        tmp = f"{cached}.{os.getpid()}.tmp"
                        with open(tmp, "wb") as fh:
                            fh.write(data)
                        os.replace(tmp, cached)
                    except OSError as exc:  # no cache, still a preview
                        log.debug("Hermes Commands: preview cache not written: %s", exc)
        except Exception as exc:  # unreadable or not really an image: no preview
            log.debug("Hermes Commands: no preview for %s: %s", safe, exc)
            return ""
        return "data:image/webp;base64," + base64.b64encode(data).decode()

    async def _thumbs(self, rows: list[dict]) -> dict:
        """Previews for the image rows of one page, four at a time, off the event loop."""
        gate = asyncio.Semaphore(4)

        async def one(r):
            async with gate:
                return r["id"], await asyncio.to_thread(self._thumb, r["id"], r.get("path"))

        meta = lambda r: r["meta"] if isinstance(r["meta"], dict) else {}  # noqa: E731
        todo = [r for r in rows if str(meta(r).get("content_type") or "").startswith("image/")]
        return dict(await asyncio.gather(*(one(r) for r in todo)))

    async def cmd_owui(self, ctx, arg):
        """This user's Open WebUI files (uploads, generated images, files from Hermes) for the Files panel."""
        user_id = str(ctx.get("user_id") or "")
        if not user_id:
            raise CommandError("Open WebUI files need a signed-in user.")
        words = arg.strip().split(" ", 1)
        page = int(words[0]) if words[0].isdigit() and int(words[0]) > 0 else 1
        query = (words[1] if len(words) > 1 and words[0].isdigit() else ("" if words[0].isdigit() else arg)).strip()[:200]
        size = self._OWUI_PAGE
        try:
            rows, total = await self._owui_files(user_id, query, (page - 1) * size, size)
        except ImportError:
            raise CommandError("Listing Open WebUI files needs Open WebUI 0.13 or newer.")
        more, rows = len(rows) > size, rows[:size]
        thumbs = await self._thumbs(rows)
        out = []
        for r in rows:
            meta = r["meta"] if isinstance(r["meta"], dict) else {}
            data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
            name = str(meta.get("name") or r["filename"] or r["id"])
            ctype = str(meta.get("content_type") or "")
            url = f"/api/v1/files/{esc(r['id'])}/content"
            thumb = f"<a href='{url}'><img class='thumb' alt='' src='{thumbs[r['id']]}'></a>" if thumbs.get(r["id"]) else ""
            prompt = str(data.get("prompt") or "")
            kind = "Generated" if prompt else ("From Hermes" if data.get("hermes_path") else "Upload")
            note = f"<br><span class='muted'>{esc(prompt[:120])}{'…' if len(prompt) > 120 else ''}</span>" if prompt else ""
            chat = r.get("chat")
            chat_cell = f"<a href='/c/{esc(chat[0])}'>{esc(chat[1] or 'Untitled chat')}</a>" if chat else "<span class='muted'>none</span>"
            out.append([f"{thumb}<a href='{url}' title='{esc(prompt[:400])}'>{esc(name)}</a>{note}", esc(kind), chat_cell,
                        f"<span class='nowrap'>{esc(size_text(meta.get('size')))}</span>",
                        f"<span class='nowrap'>{esc(when(r['created_at']))}</span>"])
        head = (f"<p class='muted'>{total} files, newest first.</p>" if total is not None
                else f"<p class='muted'>Matches for <b>{esc(query)}</b> in names, image prompts and chat titles.</p>")
        listing = table(["File", "Kind", "Chat", "Size", "Created"], out) if out else "<p class='muted'>No files.</p>"
        q = f" {query}" if query else ""
        newer = self._browse_link(f"owui {page - 1}{q}", "&#8592; Newer") if page > 1 else "<span></span>"
        older = self._browse_link(f"owui {page + 1}{q}", "Older &#8594;") if more else "<span></span>"
        find = (f"<input class='find' type='search' data-hermes-search='owui' value='{esc(query)}' "
                "placeholder='Search names, prompts, chat titles (Enter)'>")
        return card("Files: Open WebUI", self._files_tabs("owui") + find + head + listing
                    + f"<p class='pager'>{newer}<span class='muted'>Page {page}</span>{older}</p>")

    async def cmd_fetch(self, ctx, arg):
        """Download one file from the panel: cached in Open WebUI, then handed to the browser."""
        path = self._check_path(ctx, arg.strip())
        folder, name = path.rsplit("/", 1)
        data = await self._dash("GET", "/api/files", ctx["profile"], params={"path": folder or "/"})
        entry = next((e for e in data.get("entries") or [] if e.get("path") == path and not e.get("is_directory")), None)
        if not entry:
            raise CommandError(f"There is no file {path}.")
        size = int(entry.get("size") or 0)
        if size > self.valves.myfiles_max_mb * 1024 * 1024:
            raise CommandError(f"{name} is {size_text(size)}, over the My Files size limit; open it with: file {path}")
        file_id = await self._cached_file(ctx, path, size, entry.get("mtime"))
        url = f"/api/v1/files/{file_id}/content?attachment=true"
        return card(f"Download: {name}", f"<p><a href='{url}' data-hermes-download='{url}'>{esc(name)}</a></p>")

    async def cmd_myfiles(self, ctx, arg):
        """outputs/ and uploads/ of this profile, newest first, each file a download link."""
        home = self._home(ctx["profile"])
        found, folders = [], []
        for sub in ("outputs", "uploads"):
            try:
                data = await self._dash("GET", "/api/files", ctx["profile"], params={"path": f"{home}/{sub}"})
            except CommandError:
                continue
            for e in data.get("entries") or []:
                (folders if e.get("is_directory") else found).append(e)
        found.sort(key=lambda e: float(e.get("mtime") or 0), reverse=True)
        limit = self.valves.myfiles_max_mb * 1024 * 1024
        rows = []
        for e in found[:40]:
            path, size = str(e.get("path")), int(e.get("size") or 0)
            name = esc(path.rsplit("/", 1)[-1])
            where = "attached" if f"{home}/uploads/" in path else "made for you"
            if size <= limit:
                file_id = await self._cached_file(ctx, path, size, e.get("mtime"))
                link = f"<a href='/api/v1/files/{file_id}/content?attachment=true'>{name}</a>"
            else:
                link = f"{name} <span class='muted'>(too big here: file {esc(path)})</span>"
            rows.append([link, esc(where), esc(size_text(size)), esc(when(e.get("mtime")))])
        extra = "".join(f"<p class='muted'>Folder: files {esc(f.get('path'))}</p>" for f in folders)
        body = table(["File", "From", "Size", "Modified"], rows) if rows else "<p class='muted'>No files yet.</p>"
        return card("My files", body + extra)

    async def _cached_file(self, ctx, path: str, size: int, mtime) -> str:
        """Open WebUI file id for this exact version of a Hermes file, downloading it only the first time."""
        from open_webui.models.files import Files

        file_id = str(uuid.uuid5(_MYFILES_NS, f"{ctx['user_id']}\n{path}\n{size}\n{mtime}"))
        if not await Files.get_file_by_id(file_id):
            data, content_type = await self._dash("GET", "/api/files/download", ctx["profile"], params={"path": path},
                                                  raw=True, timeout=120)
            await self._store_file(ctx, path, data, content_type or mimetypes.guess_type(path)[0] or "", file_id)
        return file_id

    async def cmd_workspaces(self, ctx, arg):
        data = await self._dash("GET", "/api/chat/workspaces", ctx["profile"])
        rows = [[f"<code>{esc(x.get('path') or x.get('cwd') or x)}</code>", esc(x.get('name') or x.get('label') or "")]
                for x in (data.get("projects") or []) + (data.get("repos") or [])]
        return card("Workspaces", f"<p>Default folder: <code>{esc(data.get('default_cwd'))}</code></p>"
                    + (table(["Folder", "Name"], rows) if rows else "<p class='muted'>No other workspaces registered.</p>"))

    async def cmd_file(self, ctx, arg):
        path = arg.strip()
        if not path:
            raise CommandError("Usage: file <path>")
        path = self._check_path(ctx, path)
        kind = (mimetypes.guess_type(path)[0] or "").split("/")[0]
        if kind in _MEDIA_TYPES:
            data, content_type = await self._dash("GET", "/api/files/download", ctx["profile"], params={"path": path}, raw=True, timeout=120)
            file_id = await self._store_file(ctx, path, data, content_type or mimetypes.guess_type(path)[0])
            url = f"/api/v1/files/{file_id}/content"
            name = esc(path.rsplit("/", 1)[-1])
            if kind == "image":
                return card(name, f"<img src='{url}' style='max-width:100%;border-radius:6px'>")
            tag = "audio" if kind == "audio" else "video"
            return card(name, f"<{tag} controls preload='metadata' style='width:100%' src='{url}'></{tag}>"
                        f"<p class='muted'><a href='{url}' target='_blank'>Download</a></p>")
        guessed = mimetypes.guess_type(path)[0] or ""
        textual = guessed.startswith("text/") or guessed in ("application/json", "application/xml", "application/x-yaml",
                                                             "application/javascript", "application/x-sh", "") \
            or path.rsplit(".", 1)[-1].lower() in ("md", "yaml", "yml", "toml", "ini", "log", "py", "sh", "csv")
        if not textual:
            data, content_type = await self._dash("GET", "/api/files/download", ctx["profile"], params={"path": path},
                                                  raw=True, timeout=120)
            file_id = await self._store_file(ctx, path, data, content_type or guessed)
            name = esc(path.rsplit("/", 1)[-1])
            return card(name, f"<p><a href='/api/v1/files/{file_id}/content' target='_blank'>Download {name}</a> "
                        f"<span class='muted'>({size_text(len(data))})</span></p>")
        text = await self._read_text(ctx, path)
        return card(path, f"<pre>{esc(text[:_TEXT_LIMIT])}</pre>"
                    + ("<p class='muted'>Truncated.</p>" if len(text) > _TEXT_LIMIT else ""))

    async def _store_file(self, ctx, path: str, data: bytes, content_type: str, file_id: str = "") -> str:
        import io

        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage

        file_id = file_id or str(uuid.uuid4())
        name = path.rsplit("/", 1)[-1]
        _, stored = await asyncio.to_thread(Storage.upload_file, io.BytesIO(data), f"{file_id}_{name}",
                                            {"OpenWebUI-User-Id": ctx["user_id"], "OpenWebUI-File-Id": file_id})
        await Files.insert_new_file(ctx["user_id"], FileForm(id=file_id, filename=name, path=stored, meta={
            "name": name, "content_type": content_type, "size": len(data), "data": {"hermes_path": path}}))
        return file_id

    async def cmd_sessions(self, ctx, arg):
        listing = await self._http("GET", f"{ctx['root']}/api/sessions", token=ctx["key"], params={"limit": 200})
        rows = listing.get("data") or []
        query = arg.strip().lower()
        if query:
            rows = [r for r in rows if query in ((r.get("title") or "") + " " + (r.get("preview") or "")).lower()]
        rows = rows[:30]
        table_rows = [[f"<code>{esc(r.get('id'))}</code>", esc(r.get("title") or r.get("preview")), f"<span class='chip'>{esc(r.get('source'))}</span>",
                       num(r.get("message_count")), esc(when(r.get("last_active")))] for r in rows]
        return card(f"Hermes sessions ({len(rows)})", table(["Id", "Title", "From", "Messages", "Last active"], table_rows)
                    + "<p class='muted'>Copy one into Open WebUI with: import &lt;id&gt;</p>")

    async def cmd_import(self, ctx, arg):
        sid = arg.strip()
        if not sid:
            raise CommandError("Usage: import <session id>   (see: sessions)")
        info = (await self._http("GET", f"{ctx['root']}/api/sessions/{sid}", token=ctx["key"])).get("session", {})
        messages = [m for m in await self._messages(ctx["root"], ctx["key"], sid)
                    if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
                    and m.get("display_kind") != "internal_notification"]
        if not messages:
            raise CommandError("That session has no user or assistant messages to copy.")
        from open_webui.models.chats import ChatForm, Chats

        history, previous, model_id = {}, None, ctx["model_id"]
        for m in messages:
            mid = str(uuid.uuid4())
            entry = {"id": mid, "parentId": previous, "childrenIds": [], "role": m["role"], "content": m["content"],
                     "timestamp": int(float(m.get("timestamp") or time.time()))}
            if m["role"] == "assistant":
                entry |= {"model": model_id, "done": True}
            else:
                entry["models"] = [model_id]
            if previous:
                history[previous]["childrenIds"].append(mid)
            history[mid] = entry
            previous = mid
        title = f"{info.get('title') or sid} (from Hermes {info.get('source') or ''})".strip()
        chat = await Chats.insert_new_chat(ctx["user_id"], ChatForm(chat={
            "title": title, "models": [model_id], "history": {"messages": history, "currentId": previous},
            "messages": list(history.values())}))
        return card("Imported", f"<p>Copied {len(messages)} messages into a new chat: "
                    f"<a href='/c/{esc(chat.id)}' target='_top'>{esc(title)}</a>.</p>"
                    "<p class='muted'>Replying there continues with that history in a new Hermes session.</p>")

    async def cmd_status(self, ctx, arg):
        health = await self._http("GET", f"{ctx['root']}/v1/health", token=ctx["key"])
        toolsets = await self._http("GET", f"{ctx['root']}/v1/toolsets", token=ctx["key"])
        jobs = await self._http("GET", f"{ctx['root']}/api/jobs", token=ctx["key"])
        rows = [["Hermes version", esc(health.get("version"))], ["Profile", esc(ctx["profile"])],
                ["Toolsets on", esc(", ".join(t["name"] for t in toolsets.get("data") or [] if t.get("enabled")))],
                ["Scheduled tasks", num(len(jobs.get("jobs") or []))]]
        try:
            profiles = (await self._dash("GET", "/api/profiles", "")).get("profiles") or []
            me = next((p for p in profiles if p.get("name") == ctx["profile"]), None)
            if me:
                rows += [["Model", esc(f"{me.get('model')} ({me.get('provider')})")], ["Skills", num(me.get("skill_count"))],
                         ["Gateway", esc("running" if me.get("gateway_running") else "stopped")]]
        except CommandError:
            rows.append(["Dashboard", "<span class='muted'>not configured (model, skills and gateway state need it)</span>"])
        return card("Hermes status", table(["", ""], rows))

    COMMANDS = {
        "help": "cmd_help", "usage": "cmd_usage", "steps": "cmd_steps", "last": "cmd_last", "resume": "cmd_last",
        "steer": "cmd_steer", "stop": "cmd_stop", "title": "cmd_title", "model": "cmd_model", "todos": "cmd_todos",
        "todo": "cmd_todos", "cron": "cmd_cron", "tasks": "cmd_cron", "skills": "cmd_skills", "skill": "cmd_skill",
        "memory": "cmd_memory", "insights": "cmd_insights", "files": "cmd_files", "file": "cmd_file", "myfiles": "cmd_myfiles", "browse": "cmd_browse", "fetch": "cmd_fetch", "owui": "cmd_owui",
        "sessions": "cmd_sessions", "import": "cmd_import", "status": "cmd_status", "workspaces": "cmd_workspaces",
        "forget": "cmd_forget",
    }

    async def run_command(self, line: str, ctx: dict) -> str:
        line = line.strip().lstrip("/")
        name, _, arg = line.partition(" ")
        handler = self.COMMANDS.get(name.lower())
        if not handler:
            raise CommandError(f"Unknown command '{name}'. Type help for the list.")
        return await getattr(self, handler)(ctx, arg)

    # ---- Open WebUI hook -------------------------------------------------------------------------

    async def action(self, body: dict, __user__: Optional[dict] = None, __event_emitter__=None, __event_call__=None,
                     __model__: Optional[dict] = None, __id__: Optional[str] = None, __request__=None) -> None:
        line = {"steps": "steps", "usage": "usage", "myfiles": "myfiles"}.get(__id__ or "", "")
        panel_cmd = str(body.get("hermes_command") or "").strip() if body.get("hermes_panel_data") else ""
        if panel_cmd and panel_cmd.split()[0].lower() in _PANEL_COMMANDS:
            line = panel_cmd  # the sidebar Files panel navigating: browse <folder>, fetch <file>
        if not line:
            if not __event_call__:
                return
            answer = await __event_call__({"type": "input", "data": {
                "title": "Hermes command",
                "message": "For example: usage, steps, todos, cron, skills, memory, insights 30, files, status, "
                           "sessions, steer <text>, title <text>. Type help for all.",
                "placeholder": "help"}})
            if not answer or not isinstance(answer, str):
                return
            line = answer
        if __event_emitter__ and not body.get("hermes_panel_data") and self.valves.show_in.strip().lower() == "panel":
            word = line.strip().split()[0].lower().lstrip("/") if line.strip() else ""
            label = {"myfiles": "My files", "steps": "Hermes steps", "usage": "Hermes usage"}.get(word, f"Hermes: {line.strip()[:60]}")
            await __event_emitter__({"type": "execute", "data": {
                "code": PANEL_JS + f"window.hermesPanel.loading({json.dumps(label)});"}})
        try:
            root, key, profile = await self._api(__model__)
            if body.get("hermes_panel_data"):
                profile = self._user_profile(__user__) or profile
            session_cache: dict = {}

            async def session():
                if "id" not in session_cache:
                    session_cache["id"] = await self._session_id(body, __model__, root, key, profile)
                return session_cache["id"]

            ctx = {"root": root, "key": key, "profile": profile, "session": session, "request": __request__,
                   "call": __event_call__,
                   "chat_id": body.get("chat_id") or "", "user_id": (__user__ or {}).get("id", ""),
                   "model_id": body.get("model") or (__model__ or {}).get("id", "")}
            html_card = await self.run_command(line, ctx)
        except CommandError as exc:
            html_card = card(f"Hermes: {line.strip()[:60]}", f"<p>{esc(exc)}</p>")
        except Exception as exc:
            log.exception("Hermes Commands: '%s' failed", line)
            html_card = card(f"Hermes: {line.strip()[:60]}", f"<p>Something went wrong: {esc(exc)}</p>")
        title = html.unescape(re.search(r"<h3>(.*?)</h3>", html_card, re.S).group(1))
        if body.get("hermes_panel_data"):
            download = re.search(r"data-hermes-download='([^']+)'", html_card)
            # web/loader.js draws it in its own panel, or starts the download.
            return {"title": title, "html": html_card, "download": html.unescape(download.group(1)) if download else ""}
        if not __event_emitter__:
            return
        mode = self.valves.show_in.strip().lower()
        if mode == "panel":
            await __event_emitter__({"type": "execute", "data": {
                "code": PANEL_JS + f"window.hermesPanel.open({json.dumps(title)}, {json.dumps(html_card)});"}})
            return
        if mode == "inline":
            await __event_emitter__({"type": "embeds", "data": {"embeds": [html_card]}})
            return
        if re.search(r"<(audio|video)\b", html_card):
            # Players need HTML, and the source window shows HTML only about 150px tall.
            await __event_emitter__({"type": "embeds", "data": {"embeds": [html_card]}})
            return
        # 'chip': a source chip under the reply; clicking it opens the result in a window.
        await __event_emitter__({"type": "source", "data": {
            "source": {"id": f"hermes-{int(time.time() * 1000)}", "name": title},
            "document": [to_markdown(html_card)],
            "metadata": [{"source": title, "name": title}]}})
