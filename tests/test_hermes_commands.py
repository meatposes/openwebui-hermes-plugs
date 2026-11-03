"""Unit tests for hermes_commands.py parts that need no Hermes. Live behaviour against a real Hermes is
covered by tests/live/check_commands.py and tests/live/live_browser.py.

    python3 -m unittest discover -s tests
"""

import asyncio
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hermes_commands import PANEL_JS, Action, CommandError, card, derive_session_id, parse_tool_calls, table, to_markdown, when  # noqa: E402


class Helpers(unittest.TestCase):
    def test_session_id_matches_hermes_formula(self):
        expected = "api-" + hashlib.sha256("work\0\nhello there".encode()).hexdigest()[:16]
        self.assertEqual(derive_session_id("work", "", "hello there"), expected)
        default = "api-" + hashlib.sha256("sys\nhello there".encode()).hexdigest()[:16]
        self.assertEqual(derive_session_id("default", "sys", "hello there"), default)

    def test_tool_calls_json_and_python_repr(self):
        call = {"id": "c1", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}
        self.assertEqual(parse_tool_calls(json.dumps([call])), [call])
        self.assertEqual(parse_tool_calls(repr([call])), [call])
        self.assertEqual(parse_tool_calls("not a list"), [])

    def test_when_accepts_epoch_and_iso(self):
        self.assertRegex(when(1793433973.9), r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertRegex(when("2026-10-31T08:19:25+00:00"), r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertEqual(when(None), "")

    def test_subagent_cards(self):
        result = json.dumps({"results": [{"task_index": 0, "status": "completed", "summary": "did it",
                                          "tool_trace": [{"tool": "terminal", "status": "ok"}],
                                          "tokens": {"input": 10, "output": 2}, "api_calls": 1, "duration_seconds": 3}]})
        html_out = Action._subagent_html(result)
        self.assertIn("Subagent 1", html_out)
        self.assertIn("terminal (ok)", html_out)
        self.assertIn("did it", html_out)

    def test_card_to_markdown(self):
        md = to_markdown(card("Usage", table(["Name", "Value"], [["<code>a|b</code>", "<pre>x\ny</pre>"]])
                              + "<p>See <a href='/x'>this</a></p><div class='step'><b>1. ls</b><pre>out</pre></div>"))
        self.assertNotIn("Usage", md)  # the window shows the title
        self.assertIn("| Name | Value |", md)
        self.assertIn("`a\\|b`", md)
        self.assertIn("`x y`", md)
        self.assertIn("[this](/x)", md)
        self.assertIn("**1. ls**", md)
        self.assertIn("```\nout\n```", md)


class PathGuard(unittest.TestCase):
    def setUp(self):
        self.action = Action()

    def test_named_profile_is_limited_to_its_folder(self):
        ctx = {"profile": "work"}
        self.assertEqual(self.action._check_path(ctx, "/opt/data/profiles/work/memories/MEMORY.md"),
                         "/opt/data/profiles/work/memories/MEMORY.md")
        for bad in ("/opt/data/profiles/other/memories/MEMORY.md", "/opt/data/profiles/work/../other/x",
                    "/etc/passwd", "relative/path"):
            with self.assertRaises(CommandError):
                self.action._check_path(ctx, bad)

    def test_default_profile_cannot_reach_other_profiles(self):
        ctx = {"profile": "default"}
        self.assertEqual(self.action._check_path(ctx, "/opt/data/music/a.mp3"), "/opt/data/music/a.mp3")
        with self.assertRaises(CommandError):
            self.action._check_path(ctx, "/opt/data/profiles/work/memories/MEMORY.md")

    def test_folders_setting_widens_access(self):
        self.action.valves.file_folders = "/srv/shared"
        self.assertEqual(self.action._check_path({"profile": "work"}, "/srv/shared/a.txt"), "/srv/shared/a.txt")


class SidePanel(unittest.IsolatedAsyncioTestCase):
    def test_panel_script_matches_web_copy(self):
        web = os.path.join(os.path.dirname(__file__), "..", "web")
        with open(os.path.join(web, "panel.js")) as fh:
            self.assertEqual(PANEL_JS, fh.read())
        with open(os.path.join(web, "loader.js")) as fh:
            self.assertTrue(fh.read().startswith(PANEL_JS))

    async def test_results_open_in_panel_or_return_to_sidebar(self):
        action = Action()

        async def api(model):
            return "http://hermes/v1", "key", "kid"

        async def run(line, ctx):
            return card("My files", "<p>ok</p>")

        action._api, action.run_command = api, run
        events = []

        async def emit(event):
            events.append(event)

        await action.action({"chat_id": "c"}, __event_emitter__=emit, __id__="myfiles")
        self.assertEqual([e["type"] for e in events], ["execute", "execute"])
        self.assertIn('hermesPanel.loading("My files")', events[0]["data"]["code"])
        self.assertIn('hermesPanel.open("My files", ', events[1]["data"]["code"])
        events.clear()
        out = await action.action({"hermes_panel_data": True}, __event_emitter__=emit, __id__="myfiles")
        self.assertEqual(events, [])
        self.assertEqual(out["title"], "My files")
        self.assertIn("<p>ok</p>", out["html"])


class Dispatch(unittest.TestCase):
    def test_unknown_and_help(self):
        action = Action()
        with self.assertRaises(CommandError):
            asyncio.run(action.run_command("/nonsense", {}))
        self.assertIn("Hermes commands", asyncio.run(action.run_command("/help", {})))


if __name__ == "__main__":
    unittest.main()
