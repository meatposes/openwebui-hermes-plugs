"""Unit tests for hermes_stop.py, with a small aiohttp server standing in for Hermes's stop endpoint:

    python3 -m unittest discover -s tests
"""

import asyncio
import os
import sys
import unittest

from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hermes_stop import Filter  # noqa: E402


def chunk(run_id="chatcmpl-1", content="x", finish=None):
    return {"id": run_id, "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]}


class StopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stops = []

        async def stop(request):
            self.stops.append((request.match_info["run_id"], request.headers.get("Authorization")))
            return web.json_response({"status": "stopping"})

        app = web.Application()
        app.router.add_post("/v1/runs/{run_id}/stop", stop)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.filter = Filter()
        self.filter.valves = Filter.Valves(hermes_api_url=f"http://127.0.0.1:{port}/v1", hermes_api_key="k1")

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def reply(self, chunks, then_wait=False):
        """Stand-in for Open WebUI's reply task: feeds chunks through the hook, then idles."""
        for c in chunks:
            await self.filter.stream(c, __model__={})
        if then_wait:
            await asyncio.sleep(30)

    async def settle(self):
        for _ in range(50):
            await asyncio.sleep(0.02)

    async def test_stop_pressed_mid_reply_stops_hermes(self):
        task = asyncio.create_task(self.reply([chunk(), chunk()], then_wait=True))
        await asyncio.sleep(0.05)
        task.cancel()
        await self.settle()
        self.assertEqual(self.stops, [("chatcmpl-1", "Bearer k1")])
        self.assertEqual(self.filter._runs, {})

    async def test_finished_reply_sends_nothing(self):
        task = asyncio.create_task(self.reply([chunk(), chunk(content="", finish="stop")], then_wait=True))
        await asyncio.sleep(0.05)
        task.cancel()
        await self.settle()
        self.assertEqual(self.stops, [])

    async def test_normal_end_sends_nothing(self):
        await asyncio.create_task(self.reply([chunk(), chunk(content="", finish="stop")]))
        await self.settle()
        self.assertEqual(self.stops, [])

    async def test_hermes_events_pass_through(self):
        event = {"tool": "terminal", "toolCallId": "t1", "status": "running"}
        self.assertIs(await self.filter.stream(event, __model__={}), event)


if __name__ == "__main__":
    unittest.main()
