"""Workspace models (Workspace › Models, built on a base model) have no urlIdx of their own; the plugins
must use the base model's connection, the way Open WebUI routes the chat:

    python3 -m unittest discover -s tests
"""

import asyncio
import os
import sys
import types
import unittest
from unittest import mock

from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import hermes_activity  # noqa: E402
import hermes_commands  # noqa: E402
import hermes_media  # noqa: E402
import hermes_stop  # noqa: E402

MODULES = (hermes_activity, hermes_commands, hermes_media, hermes_stop)


def request_with(**tables):
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(**tables)))


WORKSPACE_MODEL = {"id": "my-agent", "info": {"base_model_id": "hermes"}}


class ConnectionIndexTests(unittest.TestCase):
    def test_own_connection_wins(self):
        for module in MODULES:
            self.assertEqual(module.connection_index({"urlIdx": 0}, request_with(OPENAI_MODELS={})), 0)

    def test_workspace_model_uses_base_model_connection(self):
        request = request_with(OPENAI_MODELS={"hermes": {"urlIdx": 2}})
        for module in MODULES:
            self.assertEqual(module.connection_index(WORKSPACE_MODEL, request), 2)

    def test_falls_back_to_all_models_table(self):
        request = request_with(OPENAI_MODELS={}, MODELS={"hermes": {"urlIdx": 1}})
        for module in MODULES:
            self.assertEqual(module.connection_index(WORKSPACE_MODEL, request), 1)

    def test_tagged_base_model_id(self):
        request = request_with(OPENAI_MODELS={"hermes": {"urlIdx": 4}})
        model = {"info": {"base_model_id": "hermes:latest"}}
        for module in MODULES:
            self.assertEqual(module.connection_index(model, request), 4)

    def test_unknown_without_request_or_base(self):
        for module in MODULES:
            self.assertIsNone(module.connection_index(WORKSPACE_MODEL, None))
            self.assertIsNone(module.connection_index({"info": {}}, request_with(OPENAI_MODELS={})))
            self.assertIsNone(module.connection_index(None, None))


class WorkspaceModelStopTests(unittest.IsolatedAsyncioTestCase):
    """End to end for Hermes Stop: no valves set, Workspace model, Stop pressed -> Hermes is told to stop."""

    async def asyncSetUp(self):
        self.stops = []

        async def stop(request):
            self.stops.append((request.match_info["run_id"], request.headers.get("Authorization")))
            return web.json_response({"status": "stopping"})

        app = web.Application()
        app.router.add_post("/p/work/v1/runs/{run_id}/stop", stop)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        async def get_openai_connection(idx):
            assert idx == 3
            return f"http://127.0.0.1:{port}/p/work/v1", "k3", {}

        self.modules = mock.patch.dict(sys.modules, {
            "open_webui": types.ModuleType("open_webui"),
            "open_webui.routers": types.ModuleType("open_webui.routers"),
            "open_webui.routers.openai": types.SimpleNamespace(get_openai_connection=get_openai_connection),
        })
        self.modules.start()
        self.filter = hermes_stop.Filter()  # default valves: no Hermes API URL
        self.request = request_with(OPENAI_MODELS={"hermes": {"urlIdx": 3}})

    async def asyncTearDown(self):
        self.modules.stop()
        await self.runner.cleanup()

    async def test_stop_reaches_hermes_for_workspace_model(self):
        chunk = {"id": "chatcmpl-9", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]}

        async def reply():
            await self.filter.stream(chunk, __model__=WORKSPACE_MODEL, __request__=self.request)
            await asyncio.sleep(30)

        task = asyncio.create_task(reply())
        await asyncio.sleep(0.05)
        task.cancel()
        for _ in range(50):
            await asyncio.sleep(0.02)
        self.assertEqual(self.stops, [("chatcmpl-9", "Bearer k3")])


if __name__ == "__main__":
    unittest.main()
