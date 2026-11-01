"""
title: Hermes Media
version: 1.0.0
description: Shows images, audio and video from Hermes Agent MEDIA: tags inline in chat, with streaming on.
required_open_webui_version: 0.12.0
license: MIT
"""

import asyncio
import logging
import mimetypes
import os
import re
import tempfile
import time
import uuid
from typing import Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Fixed namespace so the same chat, message and path always map to the same Open WebUI file id.
# Open WebUI can run the outlet twice for one reply; a stable id makes the second run a no-op.
_FILE_NAMESPACE = uuid.UUID("6f1d2c4e-8a43-4f0b-9a57-1d3c5e7f9b21")
_FILE_LINK_RE = re.compile(
    r"!?\[[^\]\n]*\]\(/api/v1/files/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/content\)"
)


# file:///abs/path and workspace://relative/path links, bare or as markdown links/images.
_FILE_URI_RE = re.compile(
    r"(?P<md>!?\[(?P<label>[^\]\n]*)\]\(\s*)?(?P<scheme>file|workspace)://(?P<path>[^\s)<>\"'`]*[^\s)<>\"'`.,;:!?])(?(md)\s*\))"
)


class MediaError(Exception):
    """A fetch failed in a way the user should see as '(media unavailable: <reason>)'."""


def split_list(value: str) -> list[str]:
    return [item.strip().lower().lstrip(".") for item in value.split(",") if item.strip()]


def build_tag_re(extensions: list[str]) -> re.Pattern:
    """Same tag shape Hermes accepts (gateway MEDIA_TAG_CLEANUP_RE), limited to our extensions."""
    alternation = "|".join(sorted({re.escape(e) for e in extensions}, key=len, reverse=True))
    return re.compile(
        r"""[`"'*_]{0,3}MEDIA:\s*"""
        r"""(?P<path>`[^`\n]+?`|"[^"\n]+?"|'[^'\n]+?'|https?://[^\s`"'<>)\]]+|"""
        r"""(?:~/|/)\S+?(?:[^\S\n]+\S+?)*?\.(?:""" + alternation + r"""))"""
        r"""(?=[\s`"'*_,;:)\]}\[]|MEDIA:|\.(?:\s|$)|$)[`"'*_]{0,3}""",
        re.IGNORECASE,
    )


def clean_path(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "`\"'":
        raw = raw[1:-1].strip()
    if raw.startswith(("http://", "https://")):
        raw = raw.rstrip(".,;:!?")
    return raw


def url_name(url: str) -> str:
    """File name of a URL: its filename= query value (ComfyUI /view links), else the last path part."""
    from urllib.parse import parse_qs, unquote, urlsplit

    parts = urlsplit(url)
    name = (parse_qs(parts.query).get("filename") or [""])[0] or unquote(parts.path.rsplit("/", 1)[-1])
    return os.path.basename(name)


def url_allowed(url: str, prefixes: list[str]) -> bool:
    return any(url.startswith(p) for p in prefixes if p.strip())


def profile_allows(path: str, profile: str, data_root: str = "/opt/data") -> bool:
    """Keep each profile to its own folder; the default profile gets the data root minus profiles/."""
    normalized = os.path.normpath(path)
    profiles = f"{data_root}/profiles"
    if profile and profile != "default":
        return normalized.startswith(f"{profiles}/{profile}/")
    return not (normalized == profiles or normalized.startswith(profiles + "/"))


def path_allowed(path: str, prefixes: list[str]) -> bool:
    if not path.startswith("/") or ".." in path.split("/"):
        return False
    normalized = os.path.normpath(path)
    return any(normalized.startswith(p.rstrip("/") + "/") for p in prefixes if p.strip())


def derived_file_id(chat_id: str, message_id: str, path: str) -> str:
    return str(uuid.uuid5(_FILE_NAMESPACE, f"{chat_id}\n{message_id}\n{path}"))


def markdown_label(name: str) -> str:
    return name.replace("[", "(").replace("]", ")")


class Filter:
    class Valves(BaseModel):
        dashboard_url: str = Field(
            default="",
            title="Dashboard URL",
            description="Hermes dashboard address, for example http://hermes-host:9119",
        )
        refresh_token: str = Field(
            default="",
            title="Refresh Token",
            description="Dashboard refresh token from get_refresh_token.py. The filter renews it automatically.",
        )
        allowed_path_prefixes: str = Field(
            default="/opt/data/",
            title="Allowed Folders",
            description="Comma-separated folders the filter may fetch from. Tags outside them stay as text.",
        )
        limit_to_profile: bool = Field(
            default=True,
            title="Only This Profile's Files",
            description="On: a model wired to /p/<profile>/v1 only gets files from that profile's folder, and "
            "the default profile never gets other profiles' files. Off: any Allowed Folder.",
        )
        allowed_url_prefixes: str = Field(
            default="",
            title="Allowed Web Addresses (optional)",
            description="Comma-separated address starts Open WebUI may fetch MEDIA: links from directly, for "
            "example http://comfyui-host:8188/view? so pictures from ComfyUI show without Hermes saving them "
            "first. Empty = web links stay as text.",
        )
        image_extensions: str = Field(default="png,jpg,jpeg,gif,webp,bmp", title="Image Types")
        audio_extensions: str = Field(default="mp3,wav,ogg,m4a,flac,opus", title="Audio Types")
        video_extensions: str = Field(default="mp4,webm,mov", title="Video Types")
        download_extensions: str = Field(
            default="pdf,docx,xlsx,pptx,zip",
            title="Download Types",
            description="Other files Hermes points to are attached as download links. Text types (md, txt, "
            "json, csv) are left out on purpose: memory notes, configs and logs are text files.",
        )
        workspace_root: str = Field(
            default="",
            title="Workspace Folder (optional)",
            description="Base folder for workspace:// links. Empty = ask the Hermes dashboard.",
        )
        max_file_mb: int = Field(default=100, title="Max File Size (MB)", description="Larger files are skipped.")
        timeout_seconds: int = Field(default=60, title="Download Timeout (s)", description="Per file download.")
        media_players: bool = Field(
            default=True,
            title="Inline Players",
            description="On: inline audio and video players (needs 'iframe Sandbox Allow Same Origin'). "
            "Off: audio and video are attached as files instead.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._access_token = ""
        self._access_expires = 0.0
        self._token_lock = asyncio.Lock()

    # ---- media types -------------------------------------------------------------------------

    def _kinds(self) -> dict[str, str]:
        kinds = {}
        for kind, csv in (
            ("image", self.valves.image_extensions),
            ("audio", self.valves.audio_extensions),
            ("video", self.valves.video_extensions),
            ("file", self.valves.download_extensions),
        ):
            for ext in split_list(csv):
                kinds[ext] = kind
        return kinds

    # ---- dashboard auth ----------------------------------------------------------------------

    def _base_url(self) -> str:
        return self.valves.dashboard_url.strip().rstrip("/")

    async def _access(self, function_id: Optional[str], force: bool = False) -> str:
        """Return a valid access token, trading the refresh token for a new pair when needed.
        Each refresh returns a new refresh token, which is saved back so the login never lapses
        while the filter is in use."""
        import aiohttp

        async with self._token_lock:
            if not force and self._access_token and time.time() < self._access_expires - 60:
                return self._access_token
            refresh_token = self.valves.refresh_token.strip()
            if not self._base_url() or not refresh_token:
                raise MediaError("filter not configured")
            timeout = aiohttp.ClientTimeout(total=30)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        f"{self._base_url()}/auth/native/refresh", json={"refresh_token": refresh_token}
                    ) as resp:
                        if resp.status == 401:
                            raise MediaError("dashboard login expired")
                        if resp.status != 200:
                            raise MediaError(f"dashboard error {resp.status}")
                        data = await resp.json()
            except aiohttp.ClientError as exc:
                raise MediaError("Hermes unreachable") from exc
            except asyncio.TimeoutError as exc:
                raise MediaError("Hermes unreachable") from exc
            self._access_token = data["access_token"]
            self._access_expires = float(data.get("expires_at") or time.time() + 3600)
            new_refresh = data.get("refresh_token") or ""
            if new_refresh and new_refresh != refresh_token:
                await self._save_refresh_token(function_id, new_refresh)
            return self._access_token

    async def _save_refresh_token(self, function_id: Optional[str], token: str) -> None:
        self.valves.refresh_token = token
        if not function_id:
            return
        from open_webui.models.functions import Functions

        valves = await Functions.get_function_valves_by_id(function_id) or {}
        valves["refresh_token"] = token
        await Functions.update_function_valves_by_id(function_id, valves)

    # ---- download and store ------------------------------------------------------------------

    async def _save_stream(self, resp, name: str) -> tuple[str, str, int]:
        limit = self.valves.max_file_mb * 1024 * 1024
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        if not content_type or content_type == "application/octet-stream":
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        size = 0
        handle = tempfile.NamedTemporaryFile(delete=False, prefix="hermes-media-")
        try:
            async for chunk in resp.content.iter_chunked(1 << 16):
                size += len(chunk)
                if size > limit:
                    raise MediaError("too large")
                handle.write(chunk)
        except BaseException:
            handle.close()
            os.unlink(handle.name)
            raise
        handle.close()
        return handle.name, content_type, size

    async def _download_url(self, url: str) -> tuple[str, str, int]:
        """Fetch an allowed web address directly. Redirects are refused so the allow list holds."""
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.valves.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=False) as resp:
                    if resp.status != 200:
                        raise MediaError({404: "not found"}.get(resp.status, f"error {resp.status}"))
                    return await self._save_stream(resp, url_name(url))
        except aiohttp.ClientError as exc:
            raise MediaError("address unreachable") from exc
        except asyncio.TimeoutError as exc:
            raise MediaError("address unreachable") from exc

    async def _download(self, path: str, function_id: Optional[str]) -> tuple[str, str, int]:
        """Stream one file from the dashboard into a temp file -> (temp path, content type, size)."""
        import aiohttp

        if path.startswith(("http://", "https://")):
            return await self._download_url(path)
        timeout = aiohttp.ClientTimeout(total=self.valves.timeout_seconds)
        for attempt in (0, 1):
            token = await self._access(function_id, force=attempt == 1)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        f"{self._base_url()}/api/files/download",
                        params={"path": path},
                        headers={"Authorization": f"Bearer {token}"},
                    ) as resp:
                        if resp.status == 401 and attempt == 0:
                            continue
                        if resp.status != 200:
                            raise MediaError(
                                {
                                    401: "dashboard login expired",
                                    403: "not allowed",
                                    404: "not found",
                                    409: "file busy",
                                    413: "too large",
                                }.get(resp.status, f"dashboard error {resp.status}")
                            )
                        return await self._save_stream(resp, path)
            except aiohttp.ClientError as exc:
                raise MediaError("Hermes unreachable") from exc
            except asyncio.TimeoutError as exc:
                raise MediaError("Hermes unreachable") from exc
        raise MediaError("dashboard login expired")

    async def _ensure_file(
        self, file_id: str, path: str, user_id: str, function_id: Optional[str]
    ) -> None:
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage

        if await Files.get_file_by_id(file_id):
            return
        local, content_type, size = await self._download(path, function_id)
        name = url_name(path) if path.startswith(("http://", "https://")) else os.path.basename(path)
        try:
            with open(local, "rb") as fh:
                _, stored = await asyncio.to_thread(
                    Storage.upload_file,
                    fh,
                    f"{file_id}_{name}",
                    {"OpenWebUI-User-Id": user_id, "OpenWebUI-File-Id": file_id},
                )
        finally:
            os.unlink(local)
        if await Files.get_file_by_id(file_id):
            return  # a parallel outlet run stored it first
        await Files.insert_new_file(
            user_id,
            FileForm(
                id=file_id,
                filename=name,
                path=stored,
                meta={
                    "name": name,
                    "content_type": content_type,
                    "size": size,
                    "data": {"hermes_path": path},
                },
            ),
        )

    # ---- rewrite -----------------------------------------------------------------------------

    async def _profile(self, model: Optional[dict]) -> Optional[str]:
        """Profile of the model's Open WebUI connection (/p/<profile>/v1), 'default' without one,
        None when the model has no connection to read."""
        idx = (model or {}).get("urlIdx")
        if idx is None:
            return None
        try:
            from open_webui.routers.openai import get_openai_connection

            url, _, _ = await get_openai_connection(idx)
        except Exception:
            return None
        match = re.search(r"/p/([^/]+)/v1/?$", url or "")
        return match.group(1) if match else "default"

    async def _resolve(self, raw_paths, chat_id, message_id, user_id, function_id, profile=None) -> dict:
        """path -> {"kind", "id", "name"} on success or {"error": reason} on failure."""
        kinds = self._kinds()
        prefixes = [p.strip() for p in self.valves.allowed_path_prefixes.split(",")]
        url_prefixes = [p.strip() for p in self.valves.allowed_url_prefixes.split(",")]
        results = {}
        for path in raw_paths:
            if path in results:
                continue
            is_url = path.startswith(("http://", "https://"))
            name = url_name(path) if is_url else path
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            allowed = url_allowed(path, url_prefixes) if is_url else path_allowed(path, prefixes)
            if allowed and not is_url and self.valves.limit_to_profile:
                allowed = profile is not None and profile_allows(path, profile)
            if ext not in kinds or not allowed:
                results[path] = None  # never fetched, left as written
                continue
            file_id = derived_file_id(chat_id, message_id, path)
            try:
                await self._ensure_file(file_id, path, user_id, function_id)
                results[path] = {"kind": kinds[ext], "id": file_id, "name": os.path.basename(name)}
            except MediaError as exc:
                results[path] = {"error": str(exc)}
            except Exception:
                log.exception("Hermes Media: failed to fetch %s", path)
                results[path] = {"error": "unexpected error"}
        return results

    async def _workspace_base(self, function_id: Optional[str]) -> str:
        if self.valves.workspace_root.strip():
            return self.valves.workspace_root.strip().rstrip("/")
        if not hasattr(self, "_workspace_cache"):
            import aiohttp

            token = await self._access(function_id)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(f"{self._base_url()}/api/chat/workspaces",
                                       headers={"Authorization": f"Bearer {token}"}) as resp:
                    data = await resp.json() if resp.status == 200 else {}
            self._workspace_cache = str(data.get("default_cwd") or "/opt/data").rstrip("/")
        return self._workspace_cache

    async def _uri_paths(self, texts: list[str], function_id: Optional[str]) -> dict[str, str]:
        """Map each file:// or workspace:// link text to an absolute path."""
        from urllib.parse import unquote

        out = {}
        for text in texts:
            for m in _FILE_URI_RE.finditer(text):
                raw = unquote(m.group("path"))
                if m.group("scheme") == "file":
                    path = raw[len("localhost"):] if raw.startswith("localhost/") else raw
                else:
                    try:
                        base = await self._workspace_base(function_id)
                    except MediaError:
                        continue
                    path = f"{base}/{raw.lstrip('/')}"
                out[m.group(0)] = path
        return out

    def _render(self, original: str, result: Optional[dict]) -> str:
        if result is None:
            return original
        if "error" in result:
            return f"{original} (media unavailable: {result['error']})"
        url = f"/api/v1/files/{result['id']}/content"
        label = markdown_label(result["name"])
        return f"![{label}]({url})" if result["kind"] == "image" else f"[{label}]({url})"

    def _rewrite_uris(self, text: str, uri_paths: dict, results: dict) -> str:
        return _FILE_URI_RE.sub(lambda m: self._render(m.group(0), results.get(uri_paths.get(m.group(0), ""))), text)

    def _rewrite(self, text: str, tag_re: re.Pattern, results: dict) -> str:
        def replace(match: re.Match) -> str:
            result = results.get(clean_path(match.group("path")))
            if result is None:
                return match.group(0)
            if "error" in result:
                return f"{match.group(0)} (media unavailable: {result['error']})"
            url = f"/api/v1/files/{result['id']}/content"
            label = markdown_label(result["name"])
            if result["kind"] == "image":
                return f"![{label}]({url})"
            return f"[{label}]({url})"

        return tag_re.sub(replace, text)

    async def _attach(self, results: dict, chat_id: str, message_id: str, emitter) -> None:
        """Audio and video: inline players (embeds) or file chips (files), skipping ones the
        message already has so a second outlet run adds nothing."""
        if not emitter or not chat_id or not message_id:
            return
        from open_webui.models.chats import Chats

        media = [r for r in results.values() if r and "error" not in r and r["kind"] in ("audio", "video")]
        if not media:
            return
        if self.valves.media_players:
            existing = await Chats.get_message_metadata(chat_id, message_id, "embeds") or []
            new = []
            for item in media:
                url = f"/api/v1/files/{item['id']}/content"
                if item["kind"] == "audio":
                    html = f'<audio controls preload="metadata" style="width:100%" src="{url}"></audio>'
                else:
                    html = f'<video controls preload="metadata" style="max-width:100%" src="{url}"></video>'
                if html not in existing and html not in new:
                    new.append(html)
            if new:
                await emitter({"type": "embeds", "data": {"embeds": new}})
        else:
            existing = await Chats.get_message_metadata(chat_id, message_id, "files") or []
            have = {f.get("id") for f in existing if isinstance(f, dict)}
            new = [
                {
                    "type": "file",
                    "id": item["id"],
                    "url": f"/api/v1/files/{item['id']}",
                    "name": item["name"],
                    "content_type": mimetypes.guess_type(item["name"])[0] or "",
                }
                for item in media
                if item["id"] not in have
            ]
            if new:
                await emitter({"type": "files", "data": {"files": new}})

    # ---- Open WebUI hooks --------------------------------------------------------------------

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        """Before a request goes to Hermes, turn this filter's file links in earlier replies back
        into the MEDIA:<path> tags Hermes wrote, so the agent still sees its own file paths."""
        try:
            from open_webui.models.files import Files

            cache = {}
            for message in body.get("messages") or []:
                content = message.get("content")
                if message.get("role") != "assistant" or not isinstance(content, str):
                    continue
                ids = {m.group("id") for m in _FILE_LINK_RE.finditer(content)}
                for file_id in ids - cache.keys():
                    record = await Files.get_file_by_id(file_id)
                    data = ((record.meta or {}).get("data") or {}) if record else {}
                    cache[file_id] = data.get("hermes_path")
                if ids:
                    message["content"] = _FILE_LINK_RE.sub(
                        lambda m: f"MEDIA:{cache[m.group('id')]}" if cache.get(m.group("id")) else m.group(0),
                        content,
                    )
        except Exception:
            log.exception("Hermes Media: inlet failed; request sent unchanged")
        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__=None,
        __id__: Optional[str] = None,
        __model__: Optional[dict] = None,
    ) -> dict:
        try:
            return await self._outlet(body, __user__ or {}, __metadata__ or {}, __event_emitter__, __id__, __model__)
        except Exception:
            log.exception("Hermes Media: outlet failed; reply left unchanged")
            return body

    async def _outlet(self, body, user, metadata, emitter, function_id, model=None) -> dict:
        # Keep the dashboard login alive on every reply, not only replies with media.
        if self.valves.refresh_token and time.time() >= self._access_expires - 60:
            try:
                await self._access(function_id)
            except MediaError as exc:
                log.warning("Hermes Media: token refresh failed: %s", exc)

        messages = body.get("messages") or []
        if not messages or messages[-1].get("role") != "assistant":
            return body
        message = messages[-1]
        tag_re = build_tag_re(list(self._kinds()))

        texts = [message.get("content") or ""]
        for item in message.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "message":
                texts += [p.get("text") or "" for p in item.get("content") or [] if isinstance(p, dict)]
        paths = [clean_path(m.group("path")) for text in texts for m in tag_re.finditer(text)]
        uri_paths = await self._uri_paths(texts, function_id) if "://" in "".join(texts) else {}
        paths += list(uri_paths.values())
        if not paths:
            return body

        chat_id = body.get("chat_id") or metadata.get("chat_id") or ""
        message_id = message.get("id") or body.get("id") or metadata.get("message_id") or ""
        profile = await self._profile(model) if self.valves.limit_to_profile else None
        results = await self._resolve(paths, chat_id, message_id, user.get("id", ""), function_id, profile)

        if isinstance(message.get("content"), str):
            message["content"] = self._rewrite_uris(self._rewrite(message["content"], tag_re, results), uri_paths, results)
        for item in message.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"] = self._rewrite_uris(self._rewrite(part["text"], tag_re, results), uri_paths, results)

        await self._attach(results, chat_id, message_id, emitter)
        return body
