"""Unit tests for hermes_activity.py, with a small aiohttp server standing in for Hermes's
approval endpoint:

    python3 -m unittest discover -s tests
"""

import asyncio
import os
import sys
import unittest

from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hermes_activity import Filter  # noqa: E402

META = {"message_id": "msg-1"}


def chunk(content="", finish=None):
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]}


def approval(run_id="chatcmpl-1", **extra):
    return {"event": "approval.request", "run_id": run_id, "timestamp": 1.0,
            "choices": ["once", "session", "always", "deny"], "command": "rm -rf /tmp/x",
            "description": "recursive delete", **extra}


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.posts = []

        async def approve(request):
            self.posts.append((request.match_info["run_id"], request.headers.get("Authorization"),
                               await request.json()))
            return web.json_response({"ok": True})

        self.jobs = [{"id": "j1", "name": "daily digest", "last_run_at": "2026-10-31T08:00:00+00:00", "last_status": "ok"}]

        async def jobs(request):
            return web.json_response({"jobs": self.jobs})

        app = web.Application()
        app.router.add_post("/v1/runs/{run_id}/approval", approve)
        app.router.add_get("/api/jobs", jobs)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.filter = Filter()
        self.filter.valves = Filter.Valves(hermes_api_url=f"http://127.0.0.1:{port}/v1", hermes_api_key="k1")
        self.statuses = []
        self.notifications = []

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def emit(self, event):
        if event["type"] == "notification":
            self.notifications.append(event["data"])
        else:
            self.statuses.append((event["data"]["description"], event["data"]["done"]))

    async def send(self, event, answer=True):
        async def caller(_):
            return answer
        return await self.filter.stream(event, __event_emitter__=self.emit, __event_call__=caller,
                                        __metadata__=META, __model__={})

    async def test_tool_calls_become_status_lines(self):
        out = await self.send({"tool": "terminal", "emoji": "💻", "label": "terminal: ls", "toolCallId": "t1",
                              "status": "running"})
        self.assertEqual(out, {})
        await self.send({"tool": "terminal", "toolCallId": "t1", "status": "completed"})
        await self.send({"tool": "web_search", "emoji": "🔎", "label": "web_search: ltx2", "toolCallId": "t2",
                        "status": "running"})
        await self.send({"kind": "retry", "text": "Model busy, retrying"})
        normal = chunk("hello")
        self.assertIs(await self.send(normal), normal)
        await self.send(chunk(finish="stop"))
        self.assertEqual(self.statuses, [("💻 terminal: ls", False), ("🔎 web_search: ltx2", False),
                                         ("Model busy, retrying", False), ("Done: 2 tool calls", True)])

    async def test_thinking_rounds_become_separate_blocks(self):
        def think(text):
            return {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"reasoning_content": text}, "finish_reason": None}]}

        texts = []
        for event in [think("plan "), think("more"), {"tool": "terminal", "toolCallId": "t1", "status": "running"},
                      think("again"), chunk("done"), chunk(finish="stop")]:
            out = await self.send(event)
            delta = ((out.get("choices") or [{}])[0].get("delta") or {}) if out else {}
            self.assertNotIn("reasoning_content", delta)
            texts.append(delta.get("content") or "")
        self.assertEqual("".join(texts), "<think>\nplan more\n</think>\n\n<think>\nagain\n</think>\n\ndone")

    async def test_approve_posts_once(self):
        out = await self.send(approval(request_id="r1", execution_generation=1), answer=True)
        self.assertEqual(out, {})
        self.assertEqual(self.posts, [("chatcmpl-1", "Bearer k1", {"choice": "once", "request_id": "r1", "execution_generation": 1})])
        self.assertEqual(self.statuses[-1], ("Approved", False))

    async def test_deny_posts_deny(self):
        await self.send(approval(), answer=False)
        self.assertEqual(self.posts[0][2], {"choice": "deny"})
        self.assertEqual(self.statuses[-1], ("Denied", False))

    async def test_session_scope_falls_back_when_not_offered(self):
        self.filter.valves.approve_scope = "session"
        await self.send(approval(choices=["once", "deny"]), answer=True)
        self.assertEqual(self.posts[0][2], {"choice": "once"})

    async def test_no_answer_sends_nothing(self):
        self.filter.valves.approval_timeout_seconds = 0

        async def slow(_):
            await asyncio.sleep(1)
            return True
        await self.filter.stream(approval(), __event_emitter__=self.emit, __event_call__=slow,
                                 __metadata__=META, __model__={})
        self.assertEqual(self.posts, [])
        self.assertIn("No answer", self.statuses[-1][0])


async def _inlet(test, user="u1"):
    return await test.filter.inlet({"messages": []}, __user__={"id": user}, __event_emitter__=test.emit, __model__={})


async def _test_task_alerts(self):
    await _inlet(self)
    self.assertEqual(self.notifications, [])  # baseline only
    self.jobs[0] = dict(self.jobs[0], last_run_at="2026-10-31T09:00:00+00:00", last_status="error", last_error="boom")
    await _inlet(self)
    self.assertEqual(self.notifications, [{"type": "error", "content": 'Scheduled task "daily digest" failed: boom'}])
    await _inlet(self)
    self.assertEqual(len(self.notifications), 1)  # not announced twice


ActivityTests.test_task_alerts = _test_task_alerts


if __name__ == "__main__":
    unittest.main()
