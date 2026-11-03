"""Unit tests for hermes_media.py. Open WebUI internals are replaced by in-memory stubs and the
Hermes dashboard by a small aiohttp server, so these run anywhere pydantic and aiohttp exist:

    python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import types
import unittest

from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import hermes_media  # noqa: E402
from hermes_media import Filter, build_tag_re, clean_path, derived_file_id, path_allowed  # noqa: E402


class Store:
    """In-memory stand-ins for open_webui Files, Functions, Chats and Storage."""

    def __init__(self):
        self.files, self.valves, self.message_meta, self.uploads = {}, {}, {}, []
        self.connections = ["http://hermes:8642/v1", "http://hermes:8642/p/kid/v1"]


def install_stubs(store: Store) -> None:
    class FileForm:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Files:
        @staticmethod
        async def get_file_by_id(file_id):
            return store.files.get(file_id)

        @staticmethod
        async def insert_new_file(user_id, form):
            form.user_id = user_id
            store.files[form.id] = form

    class Functions:
        @staticmethod
        async def get_function_valves_by_id(fid):
            return dict(store.valves.get(fid, {}))

        @staticmethod
        async def update_function_valves_by_id(fid, valves):
            store.valves[fid] = dict(valves)

    class Chats:
        @staticmethod
        async def get_message_metadata(chat_id, message_id, key):
            return store.message_meta.get((chat_id, message_id, key))

    class Storage:
        @staticmethod
        def get_file(path):
            return path

        @staticmethod
        def upload_file(fh, filename, tags):
            data = fh.read()
            store.uploads.append(filename)
            return data, f"/stored/{filename}"

    async def get_openai_connection(idx):
        return store.connections[idx], "key", {}

    modules = {
        "open_webui.routers": types.ModuleType("open_webui.routers"),
        "open_webui.routers.openai": types.SimpleNamespace(get_openai_connection=get_openai_connection),
        "open_webui": types.ModuleType("open_webui"),
        "open_webui.models": types.ModuleType("open_webui.models"),
        "open_webui.models.files": types.SimpleNamespace(Files=Files, FileForm=FileForm),
        "open_webui.models.functions": types.SimpleNamespace(Functions=Functions),
        "open_webui.models.chats": types.SimpleNamespace(Chats=Chats),
        "open_webui.storage": types.ModuleType("open_webui.storage"),
        "open_webui.storage.provider": types.SimpleNamespace(Storage=Storage),
    }
    sys.modules.update(modules)


class TagParsing(unittest.TestCase):
    def setUp(self):
        self.re = build_tag_re(["png", "mp3", "webm"])

    def paths(self, text):
        return [clean_path(m.group("path")) for m in self.re.finditer(text)]

    def test_shapes_hermes_emits(self):
        self.assertEqual(self.paths("Here:\nMEDIA:/opt/data/a.png\nDone"), ["/opt/data/a.png"])
        self.assertEqual(self.paths("`MEDIA:/opt/data/a.png`"), ["/opt/data/a.png"])
        self.assertEqual(self.paths("**MEDIA:/opt/data/a.png**"), ["/opt/data/a.png"])
        self.assertEqual(self.paths("MEDIA: /opt/data/song.mp3."), ["/opt/data/song.mp3"])
        self.assertEqual(self.paths('MEDIA:"/opt/data/my song.mp3"'), ["/opt/data/my song.mp3"])
        self.assertEqual(self.paths("MEDIA:/opt/data/my dir/clip.webm"), ["/opt/data/my dir/clip.webm"])

    def test_ignores_non_media(self):
        self.assertEqual(self.paths("MEDIA:/absolute/path"), [])
        self.assertEqual(self.paths("MEDIA:/opt/data/notes.md"), [])
        self.assertEqual(self.paths("plain media: talk"), [])


class Helpers(unittest.TestCase):
    def test_path_allowed(self):
        prefixes = ["/opt/data/"]
        self.assertTrue(path_allowed("/opt/data/music/a.mp3", prefixes))
        self.assertFalse(path_allowed("/opt/database/a.mp3", prefixes))
        self.assertFalse(path_allowed("/opt/data/../etc/a.png", prefixes))
        self.assertFalse(path_allowed("/etc/a.png", prefixes))
        self.assertFalse(path_allowed("relative/a.png", prefixes))

    def test_derived_file_id_is_stable(self):
        a = derived_file_id("c", "m", "/opt/data/a.png")
        self.assertEqual(a, derived_file_id("c", "m", "/opt/data/a.png"))
        self.assertNotEqual(a, derived_file_id("c", "m2", "/opt/data/a.png"))


class FakeDashboard:
    """Hermes dashboard stand-in: rotating refresh tokens, bearer-checked file streaming."""

    def __init__(self, files: dict):
        self.files, self.counter, self.valid_access, self.requested = files, 0, set(), []

    async def refresh(self, request):
        body = await request.json()
        if not body.get("refresh_token", "").startswith("rt-"):
            return web.json_response({"error": "session_expired"}, status=401)
        self.counter += 1
        access = f"at-{self.counter}"
        self.valid_access.add(access)
        return web.json_response({"access_token": access, "refresh_token": f"rt-{self.counter}",
                                  "expires_at": 9999999999})

    async def stream(self, request):
        path = request.query["path"]
        self.requested.append(path)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid_access:
            return web.Response(status=401)
        if path not in self.files:
            return web.Response(status=404)
        content_type, data = self.files[path]
        return web.Response(body=data, content_type=content_type)


class OutletFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        install_stubs(self.store)
        self.dash = FakeDashboard({
            "/opt/data/fox.png": ("image/png", b"\x89PNG fake"),
            "/opt/data/music/song.mp3": ("audio/mpeg", b"ID3 fake"),
            "/opt/data/big.mp3": ("audio/mpeg", b"x" * (2 * 1024 * 1024)),
            "/opt/data/report.pdf": ("application/pdf", b"%PDF fake"),
            "/opt/data/ws/notes.txt": ("text/plain", b"notes"),
            "/opt/data/memories/MEMORY.md": ("text/markdown", b"secret notes"),
        })
        app = web.Application()
        app.router.add_post("/auth/native/refresh", self.dash.refresh)
        app.router.add_get("/api/files/download", self.dash.stream)
        self.received = {}

        async def upload(request):
            if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self.dash.valid_access:
                return web.Response(status=401)
            form = await request.post()
            self.received[form["path"]] = form["file"].file.read()
            return web.json_response({"ok": True, "path": form["path"]})

        app.router.add_post("/api/files/upload-stream", upload)

        async def comfy_view(request):
            if request.query.get("filename") == "moved.png":
                raise web.HTTPFound("http://127.0.0.1:1/elsewhere.png")
            return web.Response(body=b"\x89PNG comfy", content_type="image/png")

        app.router.add_get("/view", comfy_view)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.filter = Filter()
        self.filter.valves = Filter.Valves(dashboard_url=f"http://127.0.0.1:{port}", refresh_token="rt-0",
                                           max_file_mb=1)
        self.store.valves["hermes_media"] = self.filter.valves.model_dump()
        self.events = []

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def emit(self, event):
        self.events.append(event)

    def body(self, text):
        return {"chat_id": "chat-1", "id": "msg-1", "messages": [
            {"role": "user", "content": "hi"},
            {"id": "msg-1", "role": "assistant", "content": text,
             "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}]}

    async def run_outlet(self, text, connection=0):
        return await self.filter.outlet(self.body(text), __user__={"id": "user-1"}, __metadata__={},
                                        __event_emitter__=self.emit, __id__="hermes_media",
                                        __model__={"urlIdx": connection})

    async def test_each_profile_gets_only_its_own_files(self):
        self.dash.files["/opt/data/profiles/kid/cat.png"] = ("image/png", b"\x89PNG kid")
        self.dash.files["/opt/data/profiles/work/plan.png"] = ("image/png", b"\x89PNG work")
        text = "MEDIA:/opt/data/profiles/kid/cat.png\nMEDIA:/opt/data/profiles/work/plan.png\nMEDIA:/opt/data/fox.png"
        content = (await self.run_outlet(text, connection=1))["messages"][-1]["content"]  # the kid profile
        self.assertIn("![cat.png]", content)
        self.assertIn("MEDIA:/opt/data/profiles/work/plan.png\n", content)
        self.assertIn("MEDIA:/opt/data/fox.png", content)
        self.assertNotIn("/opt/data/profiles/work/plan.png", self.dash.requested)
        content = (await self.run_outlet("MEDIA:/opt/data/profiles/kid/cat.png", connection=0))["messages"][-1]["content"]
        self.assertEqual(content, "MEDIA:/opt/data/profiles/kid/cat.png")  # default never gets profiles/
        self.filter.valves.limit_to_profile = False
        content = (await self.run_outlet("MEDIA:/opt/data/profiles/work/plan.png", connection=1))["messages"][-1]["content"]
        self.assertIn("![plan.png]", content)

    async def test_image_audio_and_rejects(self):
        text = ("Fox:\nMEDIA:/opt/data/fox.png\nSong:\nMEDIA:/opt/data/music/song.mp3\n"
                "Secret: MEDIA:/etc/shadow.png\nMissing: MEDIA:/opt/data/gone.png\n"
                "Big: MEDIA:/opt/data/big.mp3\nPlaceholder MEDIA:/absolute/path")
        out = await self.run_outlet(text)
        content = out["messages"][-1]["content"]
        fox = derived_file_id("chat-1", "msg-1", "/opt/data/fox.png")
        song = derived_file_id("chat-1", "msg-1", "/opt/data/music/song.mp3")
        self.assertIn(f"![fox.png](/api/v1/files/{fox}/content)", content)
        self.assertIn(f"[song.mp3](/api/v1/files/{song}/content)", content)
        self.assertIn("MEDIA:/etc/shadow.png\n", content)  # outside prefixes: untouched
        self.assertIn("MEDIA:/opt/data/gone.png (media unavailable: not found)", content)
        self.assertIn("MEDIA:/opt/data/big.mp3 (media unavailable: too large)", content)
        self.assertIn("Placeholder MEDIA:/absolute/path", content)
        self.assertEqual(out["messages"][-1]["output"][0]["content"][0]["text"], content)
        self.assertNotIn("/etc/shadow.png", self.dash.requested)
        self.assertEqual(self.store.files[song].meta["content_type"], "audio/mpeg")
        self.assertEqual(self.store.files[song].meta["data"]["hermes_path"], "/opt/data/music/song.mp3")
        self.assertEqual(self.store.files[song].user_id, "user-1")
        self.assertEqual(self.events, [{"type": "embeds", "data": {"embeds": [
            f'<audio controls preload="metadata" style="width:100%" src="/api/v1/files/{song}/content"></audio>']}}])
        self.assertEqual(self.store.valves["hermes_media"]["refresh_token"], "rt-1")  # rotated + saved
        self.assertEqual(len([p for p in os.listdir(tempfile.gettempdir()) if p.startswith("hermes-media-")]), 0)

    async def test_allowed_web_addresses(self):
        port = self.filter.valves.dashboard_url.rsplit(":", 1)[1]
        ok = f"http://127.0.0.1:{port}/view?filename=cat_00001_.png&type=output"
        self.filter.valves.allowed_url_prefixes = f"http://127.0.0.1:{port}/view?"
        text = (f"Cat: MEDIA:{ok}\nOther: MEDIA:http://example.invalid/x.png\n"
                f"Moved: MEDIA:http://127.0.0.1:{port}/view?filename=moved.png")
        content = (await self.run_outlet(text))["messages"][-1]["content"]
        cat = derived_file_id("chat-1", "msg-1", ok)
        self.assertIn(f"![cat_00001_.png](/api/v1/files/{cat}/content)", content)
        self.assertIn("MEDIA:http://example.invalid/x.png", content)  # not on the list: untouched
        self.assertIn("filename=moved.png (media unavailable: error 302)", content)  # redirects refused
        self.assertEqual(self.store.files[cat].meta["data"]["hermes_path"], ok)

    async def test_web_addresses_off_by_default(self):
        content = (await self.run_outlet("MEDIA:http://127.0.0.1:1/view?filename=a.png"))["messages"][-1]["content"]
        self.assertEqual(content, "MEDIA:http://127.0.0.1:1/view?filename=a.png")

    async def test_file_and_workspace_links(self):
        self.filter.valves.workspace_root = "/opt/data/ws"
        self.filter.valves.download_extensions = "pdf,txt"
        text = ("See ![fox](file:///opt/data/fox.png), the [report](file:///opt/data/report.pdf), "
                "bare file:///opt/data/music/song.mp3 and workspace://notes.txt, but not file:///etc/passwd.png")
        out = await self.run_outlet(text)
        content = out["messages"][-1]["content"]
        fid = lambda p: derived_file_id("chat-1", "msg-1", p)
        self.assertIn(f"![fox.png](/api/v1/files/{fid('/opt/data/fox.png')}/content)", content)
        self.assertIn(f"[report.pdf](/api/v1/files/{fid('/opt/data/report.pdf')}/content)", content)
        self.assertIn(f"[song.mp3](/api/v1/files/{fid('/opt/data/music/song.mp3')}/content)", content)
        self.assertIn(f"[notes.txt](/api/v1/files/{fid('/opt/data/ws/notes.txt')}/content)", content)
        self.assertIn("file:///etc/passwd.png", content)
        self.assertNotIn("/etc/passwd.png", self.dash.requested)

    async def test_text_files_never_fetched_by_default(self):
        out = await self.run_outlet("Notes: MEDIA:/opt/data/memories/MEMORY.md and file:///opt/data/memories/MEMORY.md")
        self.assertIn("MEDIA:/opt/data/memories/MEMORY.md", out["messages"][-1]["content"])
        self.assertNotIn("/opt/data/memories/MEMORY.md", self.dash.requested)

    async def test_second_run_adds_nothing(self):
        text = "MEDIA:/opt/data/music/song.mp3"
        await self.run_outlet(text)
        song = derived_file_id("chat-1", "msg-1", "/opt/data/music/song.mp3")
        self.store.message_meta[("chat-1", "msg-1", "embeds")] = self.events[0]["data"]["embeds"]
        await self.run_outlet(text)
        self.assertEqual(len(self.store.uploads), 1)
        self.assertEqual(len(self.events), 1)
        self.assertIn(song, self.store.files)

    async def test_stale_access_token_refreshes_and_retries(self):
        self.filter._access_token, self.filter._access_expires = "at-stale", 9999999999
        out = await self.run_outlet("MEDIA:/opt/data/fox.png")
        self.assertIn("![fox.png](", out["messages"][-1]["content"])
        self.assertEqual(self.dash.requested, ["/opt/data/fox.png", "/opt/data/fox.png"])

    async def test_expired_login_is_reported(self):
        self.filter.valves.refresh_token = "dead"
        out = await self.run_outlet("MEDIA:/opt/data/fox.png")
        self.assertIn("(media unavailable: dashboard login expired)", out["messages"][-1]["content"])

    async def test_files_mode(self):
        self.filter.valves.media_players = False
        await self.run_outlet("MEDIA:/opt/data/music/song.mp3")
        self.assertEqual(self.events[0]["type"], "files")
        self.assertEqual(self.events[0]["data"]["files"][0]["name"], "song.mp3")

    async def test_text_files_from_outputs_folder(self):
        self.dash.files["/opt/data/profiles/kid/outputs/plan.csv"] = ("text/csv", b"a,b")
        self.dash.files["/opt/data/profiles/kid/memories/MEMORY.md"] = ("text/markdown", b"secret")
        text = "MEDIA:/opt/data/profiles/kid/outputs/plan.csv\nMEDIA:/opt/data/profiles/kid/memories/MEMORY.md"
        content = (await self.run_outlet(text, connection=1))["messages"][-1]["content"]
        self.assertIn("[plan.csv](/api/v1/files/", content)
        self.assertIn("MEDIA:/opt/data/profiles/kid/memories/MEMORY.md", content)
        self.assertNotIn("/opt/data/profiles/kid/memories/MEMORY.md", self.dash.requested)
        self.dash.files["/opt/data/outputs/notes.md"] = ("text/markdown", b"# hi")
        content = (await self.run_outlet("MEDIA:/opt/data/outputs/notes.md", connection=0))["messages"][-1]["content"]
        self.assertIn("[notes.md](/api/v1/files/", content)
        self.dash.files["/opt/data/profiles/kid/uploads/list.csv"] = ("text/csv", b"x")
        content = (await self.run_outlet("MEDIA:/opt/data/profiles/kid/uploads/list.csv", connection=1))["messages"][-1]["content"]
        self.assertIn("[list.csv](/api/v1/files/", content)

    async def test_attachments_are_copied_to_hermes(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"PK deck")
        self.store.files["up-1"] = types.SimpleNamespace(id="up-1", filename="Sample Deck 3.pptx", path=fh.name, meta={})
        body = {"messages": [{"role": "user", "content": "update the deck"}],
                "files": [{"type": "file", "id": "up-1", "name": "Sample Deck 3.pptx"}, {"type": "file", "id": "up-1"}]}
        out = await self.filter.inlet(body, __model__={"urlIdx": 1}, __metadata__={}, __id__="hermes_media")
        self.assertEqual(self.received, {"/opt/data/profiles/kid/uploads/Sample_Deck_3.pptx": b"PK deck"})
        note = out["messages"][-1]["content"]
        self.assertEqual(note.count("/opt/data/profiles/kid/uploads/Sample_Deck_3.pptx"), 1)
        self.assertIn("save it in /opt/data/profiles/kid/outputs/ and put a MEDIA:<path> line", note)
        self.filter.valves.upload_to_hermes = False
        body["messages"][-1]["content"] = "again"
        out = await self.filter.inlet(body, __model__={"urlIdx": 1}, __metadata__={}, __id__="hermes_media")
        self.assertEqual(out["messages"][-1]["content"], "again")
        os.unlink(fh.name)

    async def test_inlet_restores_paths(self):
        await self.run_outlet("MEDIA:/opt/data/fox.png and MEDIA:/opt/data/music/song.mp3")
        rewritten = self.body("")
        rewritten["messages"][-1]["content"] = (
            await self.run_outlet("MEDIA:/opt/data/fox.png and MEDIA:/opt/data/music/song.mp3")
        )["messages"][-1]["content"]
        back = await self.filter.inlet(rewritten)
        self.assertEqual(back["messages"][-1]["content"],
                         "MEDIA:/opt/data/fox.png and MEDIA:/opt/data/music/song.mp3")


if __name__ == "__main__":
    unittest.main()
