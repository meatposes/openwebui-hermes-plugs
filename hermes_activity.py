"""
title: Hermes Activity
version: 1.0.0
description: Shows Hermes Agent tool calls as status lines while it works, and lets you approve or deny commands Hermes asks permission for.
required_open_webui_version: 0.12.0
license: MIT
"""

import asyncio
import logging
import time
from typing import Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# How Open WebUI's dialog answer maps onto Hermes's approval choices.
_APPROVE_SCOPES = ("once", "session", "always")


class Filter:
    class Valves(BaseModel):
        show_tool_calls: bool = Field(
            default=True,
            title="Show Tool Calls",
            description="Show each tool Hermes runs as a status line under the reply.",
        )
        approvals: bool = Field(
            default=True,
            title="Approval Dialogs",
            description="When Hermes asks permission to run something, show an Approve/Deny dialog.",
        )
        approve_scope: str = Field(
            default="once",
            title="Approve Means",
            description="'once': approve this one command. 'session': also approve the same kind of "
            "command for the rest of this chat. 'always': approve that kind of command from now on.",
        )
        task_alerts: bool = Field(
            default=True,
            title="Scheduled Task Alerts",
            description="When you send a message, pop up a notice for each scheduled Hermes task that finished "
            "since your last message.",
        )
        split_thinking: bool = Field(
            default=True,
            title="Split Thinking",
            description="Show each round of Hermes's thinking (between tool calls) as its own block with its "
            "own time, instead of one block for the whole turn.",
        )
        approval_timeout_seconds: int = Field(
            default=280,
            title="Approval Timeout (s)",
            description="Close the dialog after this long. Hermes itself gives up after 300 seconds.",
        )
        hermes_api_url: str = Field(
            default="",
            title="Hermes API URL (optional)",
            description="Only if approvals fail: the Hermes connection URL ending in /v1. Empty = use "
            "the model's own Open WebUI connection.",
        )
        hermes_api_key: str = Field(
            default="",
            title="Hermes API Key (optional)",
            description="Only with Hermes API URL. Empty = use the model's own Open WebUI connection.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._tool_counts: dict[str, int] = {}
        self._seen_at: dict[str, float] = {}
        self._alerts_seen: dict[str, str] = {}  # user id -> newest last_run_at already announced
        self._thinking: set[str] = set()  # message ids with a think block open

    # ---- helpers -----------------------------------------------------------------------------

    def _count(self, message_id: str, add: int = 0) -> int:
        now = time.time()
        for key in [k for k, t in self._seen_at.items() if now - t > 6 * 3600]:
            self._seen_at.pop(key, None)
            self._tool_counts.pop(key, None)
        self._seen_at[message_id] = now
        self._tool_counts[message_id] = self._tool_counts.get(message_id, 0) + add
        return self._tool_counts[message_id]

    async def _connection(self, model: Optional[dict]) -> tuple[str, str]:
        """Hermes base URL (ending in /v1) and API key for this model."""
        if self.valves.hermes_api_url.strip():
            return self.valves.hermes_api_url.strip().rstrip("/"), self.valves.hermes_api_key.strip()
        idx = (model or {}).get("urlIdx")
        if idx is None:
            raise RuntimeError("model has no Open WebUI connection; set Hermes API URL and Key")
        from open_webui.routers.openai import get_openai_connection

        url, key, _ = await get_openai_connection(idx)
        return url.rstrip("/"), key

    async def _status(self, emitter, description: str, done: bool = False) -> None:
        if emitter:
            await emitter({"type": "status", "data": {"description": description, "done": done}})

    @staticmethod
    def _describe_approval(event: dict) -> str:
        parts = []
        description = event.get("description") or event.get("reason") or ""
        if description:
            parts.append(str(description))
        command = event.get("command")
        if command:
            parts.append(f"```\n{command}\n```")
        if not parts:
            parts.append(f"Hermes wants to use **{event.get('tool') or 'a tool'}**.")
        return "\n\n".join(parts)

    # ---- thinking -----------------------------------------------------------------------------
    # Open WebUI merges every reasoning delta of a turn into the first think block and keeps its first
    # duration. Sent as <think> tags in the content instead, each round becomes its own block.

    def _split_reasoning(self, event: dict, message_id: str) -> dict:
        choices = event.get("choices") or []
        if not self.valves.split_thinking or not choices or not isinstance(choices[0], dict):
            return event
        delta = choices[0].get("delta") or {}
        reasoning = next((delta.pop(k) for k in ("reasoning_content", "reasoning", "thinking") if delta.get(k)), "")
        for k in ("reasoning_content", "reasoning", "thinking"):
            delta.pop(k, None)
        text = ""
        if reasoning:
            if message_id not in self._thinking:
                self._thinking.add(message_id)
                text = "<think>\n"
            text += str(reasoning)
        if delta.get("content") or choices[0].get("finish_reason"):
            text = self._close_thinking(message_id) + text
        if text:
            delta["content"] = text + str(delta.get("content") or "")
            choices[0]["delta"] = delta
        return event

    def _close_thinking(self, message_id: str) -> str:
        if message_id in self._thinking:
            self._thinking.discard(message_id)
            return "\n</think>\n\n"
        return ""

    def _closing_chunk(self, message_id: str) -> dict:
        """Hermes's own events mark the end of a thinking round."""
        text = self._close_thinking(message_id)
        if not text:
            return {}
        return {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}}]}

    # ---- approvals ---------------------------------------------------------------------------

    async def _handle_approval(self, event: dict, emitter, caller, model) -> None:
        run_id = event.get("run_id")
        choices = event.get("choices") or ["once", "deny"]
        summary = str(event.get("command") or event.get("tool") or "a command")[:80]
        if not run_id:
            return
        if not self.valves.approvals or not caller:
            await self._status(emitter, f"Waiting for approval in Hermes: {summary}")
            return

        await self._status(emitter, f"Waiting for your approval: {summary}")
        try:
            approved = await asyncio.wait_for(
                caller({
                    "type": "confirmation",
                    "data": {
                        "title": "Hermes asks for approval",
                        "message": self._describe_approval(event) + "\n\nConfirm to allow, Cancel to deny.",
                    },
                }),
                timeout=self.valves.approval_timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._status(emitter, "No answer; Hermes will treat it as denied")
            return

        scope = self.valves.approve_scope if self.valves.approve_scope in _APPROVE_SCOPES else "once"
        if approved and scope not in choices:
            scope = "once"
        choice = scope if approved else "deny"
        # Hermes with multiplexed profiles accepts exactly {request_id, execution_generation, choice}
        # and answers 400 to anything else; older single-profile Hermes takes {choice[, request_id]}.
        body = {"choice": choice}
        for field in ("request_id", "execution_generation"):
            if field in event and event[field] is not None:
                body[field] = event[field]

        import aiohttp

        try:
            base, key = await self._connection(model)
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.post(f"{base}/runs/{run_id}/approval", json=body, headers=headers) as resp:
                    if resp.status >= 400:
                        detail = (await resp.text())[:200]
                        log.warning("Hermes Activity: approval POST %s -> %s %s", run_id, resp.status, detail)
                        await self._status(emitter, f"Could not send your answer to Hermes ({resp.status})")
                        return
        except Exception as exc:
            log.warning("Hermes Activity: approval POST failed: %s", exc)
            await self._status(emitter, "Could not reach Hermes to send your answer")
            return
        await self._status(emitter, "Approved" if approved else "Denied")

    # ---- scheduled task alerts ---------------------------------------------------------------

    async def inlet(self, body: dict, __user__: Optional[dict] = None, __event_emitter__=None,
                    __model__: Optional[dict] = None) -> dict:
        """On each new message, announce scheduled tasks that finished since the last one."""
        if not self.valves.task_alerts or not __event_emitter__:
            return body
        try:
            import aiohttp

            base, key = await self._connection(__model__)
            root = base[:-3] if base.endswith("/v1") else base
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(f"{root}/api/jobs", headers={"Authorization": f"Bearer {key}"} if key else {}) as resp:
                    if resp.status != 200:
                        return body
                    jobs = (await resp.json()).get("jobs") or []
            user = (__user__ or {}).get("id", "")
            newest = max((str(j.get("last_run_at") or "") for j in jobs), default="")
            seen = self._alerts_seen.get(user)
            self._alerts_seen[user] = max(newest, seen or "")
            if seen is None:
                return body  # first message since Open WebUI started: set the baseline, announce nothing
            for job in jobs:
                ran = str(job.get("last_run_at") or "")
                if ran and ran > seen:
                    failed = job.get("last_status") not in (None, "ok", "success")
                    detail = f": {job.get('last_error')}" if failed and job.get("last_error") else ""
                    await __event_emitter__({"type": "notification", "data": {
                        "type": "error" if failed else "success",
                        "content": f"Scheduled task \"{job.get('name')}\" {'failed' if failed else 'finished'}{detail}"}})
        except Exception as exc:
            log.info("Hermes Activity: task alert check skipped: %s", exc)
        return body

    # ---- Open WebUI hook ---------------------------------------------------------------------

    async def stream(
        self,
        event: dict,
        __event_emitter__=None,
        __event_call__=None,
        __metadata__: Optional[dict] = None,
        __model__: Optional[dict] = None,
    ) -> dict:
        """Every streamed payload passes through here, including Hermes's own events that Open
        WebUI would otherwise drop: tool progress, status notes and approval requests."""
        try:
            if not isinstance(event, dict):
                return event
            message_id = (__metadata__ or {}).get("message_id") or ""

            # Hermes's own payloads are returned empty: Open WebUI skips empty chunks, and would
            # otherwise try to forward the approval payload's "event" string as a UI event.
            if event.get("event") == "approval.request":
                await self._handle_approval(event, __event_emitter__, __event_call__, __model__)
                return self._closing_chunk(message_id)
            if "toolCallId" in event and "status" in event and "choices" not in event:
                if self.valves.show_tool_calls and event["status"] == "running":
                    self._count(message_id, 1)
                    label = event.get("label") or event.get("tool") or "tool"
                    emoji = event.get("emoji") or ""
                    await self._status(__event_emitter__, f"{emoji} {label}".strip())
                return self._closing_chunk(message_id)
            if "kind" in event and "text" in event and "choices" not in event:
                if self.valves.show_tool_calls and event.get("text"):
                    await self._status(__event_emitter__, str(event["text"]))
                return self._closing_chunk(message_id)
            else:
                event = self._split_reasoning(event, message_id)
                choices = event.get("choices") or []
                if choices and choices[0].get("finish_reason") and self.valves.show_tool_calls:
                    used = self._count(message_id)
                    if used:
                        noun = "tool call" if used == 1 else "tool calls"
                        await self._status(__event_emitter__, f"Done: {used} {noun}", done=True)
                    self._tool_counts.pop(message_id, None)
                    self._seen_at.pop(message_id, None)
                if choices and choices[0].get("finish_reason"):
                    self._thinking.discard(message_id)
        except Exception:
            log.exception("Hermes Activity: stream hook failed; chunk passed through unchanged")
        return event
