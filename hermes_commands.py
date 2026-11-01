"""
title: Hermes Commands
version: 1.0.0
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
        "body{margin:0;padding:12px 14px;font:13px/1.45 system-ui,sans-serif;color:var(--fg);background:var(--bg)}"
        "h3{margin:0 0 8px;font-size:14px}table{border-collapse:collapse;width:100%}"
        "th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}"
        "th{color:var(--muted);font-weight:600}pre{white-space:pre-wrap;word-break:break-word;margin:4px 0;"
        "padding:6px 8px;background:var(--chip);border-radius:6px;max-height:320px;overflow:auto;font-size:12px}"
        ".muted{color:var(--muted)}.chip{display:inline-block;padding:0 6px;border-radius:9px;background:var(--chip);"
        "border:1px solid var(--line);font-size:11px}.step{border:1px solid var(--line);border-radius:8px;padding:6px 8px;margin:6px 0}"
        ".sub{margin-left:14px;border-left:3px solid var(--line);padding-left:8px}"
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
    ("files [path]", "List a folder on the Hermes server"),
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
        profile: str = Field(
            default="",
            title="Hermes Profile (optional)",
            description="Empty = read it from the connection URL (/p/<profile>/v1), else 'default'.",
        )
        show_in: str = Field(
            default="panel",
            title="Show Results In",
            description="'panel': a chip under the reply that opens the result in a window you can close. "
            "'inline': a card at the top of the reply.",
        )

    actions = [
        {"id": "command", "name": "Hermes command"},
        {"id": "steps", "name": "Hermes steps"},
        {"id": "usage", "name": "Hermes usage"},
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
                 esc("" if e.get("is_directory") else num(e.get("size"))), esc(when(e.get("mtime")))]
                for e in entries]
        return card(f"Files: {data.get('path') or path}", badge + table(["Path", "Bytes", "Modified"], rows)
                    + "<p class='muted'>Open one with: file &lt;path&gt;. List a folder with: files &lt;path&gt;</p>")

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
                        f"<span class='muted'>({num(len(data))} bytes)</span></p>")
        text = await self._read_text(ctx, path)
        return card(path, f"<pre>{esc(text[:_TEXT_LIMIT])}</pre>"
                    + ("<p class='muted'>Truncated.</p>" if len(text) > _TEXT_LIMIT else ""))

    async def _store_file(self, ctx, path: str, data: bytes, content_type: str) -> str:
        import io

        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage

        file_id = str(uuid.uuid4())
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
        "memory": "cmd_memory", "insights": "cmd_insights", "files": "cmd_files", "file": "cmd_file",
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
        line = {"steps": "steps", "usage": "usage"}.get(__id__ or "", "")
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
        try:
            root, key, profile = await self._api(__model__)
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
        if not __event_emitter__:
            return
        if self.valves.show_in.strip().lower() == "inline":
            await __event_emitter__({"type": "embeds", "data": {"embeds": [html_card]}})
            return
        if re.search(r"<(audio|video)\b", html_card):
            # Players need HTML, and the source window shows HTML only about 150px tall.
            await __event_emitter__({"type": "embeds", "data": {"embeds": [html_card]}})
            return
        # A source chip sits under the reply, next to the buttons; clicking it opens the result in a
        # window with a close button.
        title = html.unescape(re.search(r"<h3>(.*?)</h3>", html_card, re.S).group(1))
        await __event_emitter__({"type": "source", "data": {
            "source": {"id": f"hermes-{int(time.time() * 1000)}", "name": title},
            "document": [to_markdown(html_card)],
            "metadata": [{"source": title, "name": title}]}})
