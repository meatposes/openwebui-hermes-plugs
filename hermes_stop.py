"""
title: Hermes Stop
version: 1.0.0
description: Makes Open WebUI's Stop button stop Hermes Agent too, not just the reply on screen.
required_open_webui_version: 0.12.0
license: MIT
"""

import asyncio
import logging
from typing import Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Filter:
    class Valves(BaseModel):
        hermes_api_url: str = Field(
            default="",
            title="Hermes API URL (optional)",
            description="Only if stopping fails: the Hermes connection URL ending in /v1. Empty = use "
            "the model's own Open WebUI connection.",
        )
        hermes_api_key: str = Field(
            default="",
            title="Hermes API Key (optional)",
            description="Only with Hermes API URL. Empty = use the model's own Open WebUI connection.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._runs: dict[int, dict] = {}  # id(reply task) -> {"run_id", "finished", "model"}

    async def _connection(self, model: Optional[dict]) -> tuple[str, str]:
        if self.valves.hermes_api_url.strip():
            return self.valves.hermes_api_url.strip().rstrip("/"), self.valves.hermes_api_key.strip()
        idx = (model or {}).get("urlIdx")
        if idx is None:
            raise RuntimeError("model has no Open WebUI connection; set Hermes API URL and Key")
        from open_webui.routers.openai import get_openai_connection

        url, key, _ = await get_openai_connection(idx)
        return url.rstrip("/"), key

    async def _stop(self, run: dict) -> None:
        import aiohttp

        try:
            base, key = await self._connection(run["model"])
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                # A Stop pressed in the first moments can reach Hermes before it has admitted the run
                # (409 run_not_active); try again for a few seconds.
                for attempt in range(6):
                    async with session.post(f"{base}/runs/{run['run_id']}/stop", headers=headers) as resp:
                        detail = (await resp.text())[:200]
                        if resp.status == 409 and "run_not_active" in detail and attempt < 5:
                            await asyncio.sleep(1)
                            continue
                        if resp.status >= 400:
                            log.warning("Hermes Stop: stop %s -> %s %s", run["run_id"], resp.status, detail)
                        else:
                            log.info("Hermes Stop: stopped %s (%s)", run["run_id"], detail)
                        return
        except Exception as exc:
            log.warning("Hermes Stop: could not stop %s: %s", run["run_id"], exc)

    def _on_reply_done(self, task: asyncio.Task) -> None:
        run = self._runs.pop(id(task), None)
        if run and run.get("registry") is not None:
            # Other Hermes plugins (e.g. Hermes Commands' steer/stop) look up a chat's live run here.
            if run["registry"].get(run["chat_id"], {}).get("run_id") == run["run_id"]:
                run["registry"].pop(run["chat_id"], None)
        if not run or run["finished"]:
            return
        # The reply ended before Hermes said it was finished: Stop was pressed, a new message
        # replaced it, or the connection broke. Tell Hermes to stop this run.
        if task.cancelled() or task.exception() is not None:
            task.get_loop().create_task(self._stop(run))

    async def stream(
        self,
        event: dict,
        __model__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __request__=None,
    ) -> dict:
        """Runs inside the task Open WebUI cancels when Stop is pressed. On the first chunk, note
        the Hermes run id (the completion id) and watch that task."""
        try:
            task = asyncio.current_task()
            if task is None or not isinstance(event, dict):
                return event
            run = self._runs.get(id(task))
            if run is None and event.get("object") == "chat.completion.chunk" and event.get("id"):
                chat_id = (__metadata__ or {}).get("chat_id") or ""
                registry = None
                if __request__ is not None and chat_id:
                    state = __request__.app.state
                    if not hasattr(state, "hermes_active_runs"):
                        state.hermes_active_runs = {}
                    registry = state.hermes_active_runs
                    registry[chat_id] = {"run_id": event["id"], "model": __model__}
                run = {"run_id": event["id"], "finished": False, "model": __model__,
                       "chat_id": chat_id, "registry": registry}
                self._runs[id(task)] = run
                task.add_done_callback(self._on_reply_done)
            if run is not None:
                choices = event.get("choices") or []
                if choices and choices[0].get("finish_reason"):
                    run["finished"] = True
        except Exception:
            log.exception("Hermes Stop: stream hook failed; chunk passed through unchanged")
        return event
