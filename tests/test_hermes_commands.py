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
from hermes_commands import PANEL_JS, Action, CommandError, card, derive_session_id, parse_tool_calls, size_text, table, to_markdown, when  # noqa: E402


class Helpers(unittest.TestCase):
    def test_session_id_matches_hermes_formula(self):
        expected = "api-" + hashlib.sha256("work\0\nhello there".encode()).hexdigest()[:16]
        self.assertEqual(derive_session_id("work", "", "hello there"), expected)
        default = "api-" + hashlib.sha256("sys\nhello there".encode()).hexdigest()[:16]
        self.assertEqual(derive_session_id("default", "sys", "hello there"), default)

    def test_size_text(self):
        self.assertEqual(size_text(512), "512 B")
        self.assertEqual(size_text(3482), "3.4 KB")
        self.assertEqual(size_text(3.5 * 1024 * 1024), "3.5 MB")
        self.assertEqual(size_text(5 * 1024 ** 4), "5120.0 GB")
        self.assertEqual(size_text(None), "")

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


class FilesBrowser(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.action = Action()
        self.calls = []

        async def dash(method, path, profile, params=None, **kw):
            self.calls.append((path, profile, (params or {}).get("path")))
            folder = (params or {}).get("path")
            listing = {
                "/opt/data": [{"path": "/opt/data/outputs", "is_directory": True, "mtime": 1},
                              {"path": "/opt/data/.cache", "is_directory": True, "mtime": 1},
                              {"path": "/opt/data/profiles", "is_directory": True, "mtime": 1},
                              {"path": "/opt/data/b.txt", "is_directory": False, "size": 5, "mtime": 1},
                              {"path": "/opt/data/A.png", "is_directory": False, "size": 9, "mtime": 1}],
                "/opt/data/outputs": [{"path": "/opt/data/outputs/big.mp4", "is_directory": False,
                                       "size": 300 * 1024 * 1024, "mtime": 2}],
            }
            return {"path": folder, "entries": listing.get(folder, [])}

        self.action._dash = dash

    def test_user_profiles_match_username_email_or_name(self):
        self.action.valves.user_profiles = "alice=default, Bob=work"
        self.assertEqual(self.action._user_profile({"email": "alice@example.com"}), "default")
        self.assertEqual(self.action._user_profile({"name": "bob"}), "work")
        self.assertEqual(self.action._user_profile({"name": "someone"}), "")
        self.assertEqual(self.action._user_profile(None), "")

    async def test_browse_lists_folders_first_and_hides_dotfiles_and_profiles(self):
        out = await self.action.cmd_browse({"profile": "default"}, "")
        self.assertIn("Files: /opt/data", out)
        self.assertNotIn(".cache", out)
        self.assertNotIn("/opt/data/profiles", out)
        self.assertLess(out.index("outputs"), out.index("A.png"))
        self.assertLess(out.index("A.png"), out.index("b.txt"))
        self.assertIn("data-hermes-cmd='browse /opt/data/outputs'", out)
        self.assertIn("data-hermes-cmd='fetch /opt/data/b.txt'", out)
        self.assertNotIn("Up", out)

    async def test_browse_subfolder_has_up_link_and_breadcrumb(self):
        out = await self.action.cmd_browse({"profile": "default"}, "/opt/data/outputs")
        self.assertIn("data-hermes-cmd='browse /opt/data'", out)
        self.assertIn("Up", out)

    async def test_browse_stays_inside_the_profile(self):
        with self.assertRaises(CommandError):
            await self.action.cmd_browse({"profile": "work"}, "/opt/data/outputs")

    async def test_fetch_refuses_files_over_the_limit(self):
        with self.assertRaises(CommandError):
            await self.action.cmd_fetch({"profile": "default"}, "/opt/data/outputs/big.mp4")

    async def test_fetch_returns_a_download_link(self):
        async def cached(ctx, path, size, mtime):
            return "fid"

        self.action._cached_file = cached
        out = await self.action.cmd_fetch({"profile": "default"}, "/opt/data/b.txt")
        self.assertIn("data-hermes-download='/api/v1/files/fid/content?attachment=true'", out)

    async def test_panel_runs_only_panel_commands_and_uses_the_users_profile(self):
        seen = []

        async def api(model):
            return "http://hermes/v1", "key", "work"

        async def run(line, ctx):
            seen.append((line, ctx["profile"]))
            return card("Files", "<a data-hermes-download='/api/v1/files/x/content?attachment=true'>x</a>")

        self.action._api, self.action.run_command = api, run
        self.action.valves.user_profiles = "alice=default"
        user = {"email": "alice@example.com"}
        out = await self.action.action({"hermes_panel_data": True, "hermes_command": "fetch /opt/data/b.txt"},
                                       __user__=user, __id__="myfiles")
        self.assertEqual(seen[-1], ("fetch /opt/data/b.txt", "default"))
        self.assertEqual(out["download"], "/api/v1/files/x/content?attachment=true")
        await self.action.action({"hermes_panel_data": True, "hermes_command": "steer do something"},
                                 __user__=user, __id__="myfiles")
        self.assertEqual(seen[-1], ("myfiles", "default"))
        await self.action.action({"hermes_panel_data": True, "hermes_command": "browse"}, __user__={"name": "x"},
                                 __id__="myfiles")
        self.assertEqual(seen[-1], ("browse", "work"))


class OpenWebUIFiles(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.action = Action()
        self.asked = []
        files = [
            {"id": "f1", "filename": "generated-image.png", "created_at": 1793433973,
             "meta": {"name": "generated-image.png", "content_type": "image/png", "size": 2048,
                      "data": {"prompt": "a red <b>fox</b> in snow", "chat_id": "c9"}},
             "chat": ("c9", "Fox pictures")},
            {"id": "f2", "filename": "notes.pdf", "created_at": 1793433000,
             "meta": {"name": "notes.pdf", "content_type": "application/pdf", "size": 10, "data": {}}, "chat": None},
            {"id": "f3", "filename": "out.csv", "created_at": 1793432000,
             "meta": {"name": "out.csv", "content_type": "text/csv", "size": 3, "data": {"hermes_path": "/opt/data/out.csv"}},
             "chat": None},
        ]

        async def owui_files(user_id, query, skip, limit):
            self.asked.append((user_id, query, skip, limit))
            return files[skip:skip + limit + 1], (None if query else len(files))

        async def thumbs(rows):
            return {r["id"]: "data:image/webp;base64,AAAA" for r in rows if r["meta"]["content_type"].startswith("image/")}

        self.action._owui_files = owui_files
        self.action._thumbs = thumbs
        self.action._OWUI_PAGE = 2

    async def test_lists_only_the_signed_in_users_files_with_kind_chat_and_prompt(self):
        out = await self.action.cmd_owui({"user_id": "u1"}, "")
        self.assertEqual(self.asked[-1], ("u1", "", 0, 2))
        self.assertIn("3 files, newest first", out)
        self.assertIn("Generated", out)
        self.assertIn("Upload", out)
        self.assertIn("a red &lt;b&gt;fox&lt;/b&gt; in snow", out)
        self.assertIn("<a href='/c/c9'>Fox pictures</a>", out)
        self.assertIn("<a href='/api/v1/files/f1/content'><img class='thumb' alt='' src='data:image/webp;base64,AAAA'></a>", out)
        self.assertEqual(out.count("class='thumb'"), 1)
        self.assertIn("data-hermes-cmd='owui 2'", out)
        self.assertNotIn("notes.pdf' class", out)
        self.assertIn("class=on>Open WebUI", out)
        self.assertIn("2.0 KB", out)
        self.assertLess(out.index(">Open WebUI<"), out.index(">Hermes<"))

    async def test_second_page_and_search(self):
        out = await self.action.cmd_owui({"user_id": "u1"}, "2")
        self.assertEqual(self.asked[-1], ("u1", "", 2, 2))
        self.assertIn("From Hermes", out)
        self.assertIn("data-hermes-cmd='owui 1'", out)
        self.assertNotIn("Older", out)
        out = await self.action.cmd_owui({"user_id": "u1"}, "1 red fox")
        self.assertEqual(self.asked[-1], ("u1", "red fox", 0, 2))
        self.assertIn("Matches for <b>red fox</b>", out)
        self.assertIn("data-hermes-cmd='owui 2 red fox'", out)
        await self.action.cmd_owui({"user_id": "u1"}, "fox")
        self.assertEqual(self.asked[-1], ("u1", "fox", 0, 2))

    def test_thumbnail_is_small_and_cached(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        import base64
        import io
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "big.png")
            Image.new("RGB", (1600, 900), "red").save(src)
            url = Action()._thumb("f-1", src)
            self.assertTrue(url.startswith("data:image/webp;base64,"))
            with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as im:
                self.assertLessEqual(max(im.size), 96)
            self.assertEqual(Action()._thumb("f-2", os.path.join(tmp, "missing.png")), "")
            with open(os.path.join(tmp, "not.png"), "w") as fh:
                fh.write("text")
            self.assertEqual(Action()._thumb("f-3", os.path.join(tmp, "not.png")), "")

    async def test_refuses_without_a_user(self):
        with self.assertRaises(CommandError):
            await self.action.cmd_owui({"user_id": ""}, "")

    async def test_browse_shows_both_tabs(self):
        async def dash(method, path, profile, params=None, **kw):
            return {"path": "/opt/data", "entries": []}

        self.action._dash = dash
        out = await self.action.cmd_browse({"profile": "default"}, "")
        self.assertIn("data-hermes-cmd='owui'", out)
        self.assertIn("class=on>Hermes", out)


class Dispatch(unittest.TestCase):
    def test_unknown_and_help(self):
        action = Action()
        with self.assertRaises(CommandError):
            asyncio.run(action.run_command("/nonsense", {}))
        self.assertIn("Hermes commands", asyncio.run(action.run_command("/help", {})))


if __name__ == "__main__":
    unittest.main()
