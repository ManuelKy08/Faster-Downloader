"""Capture server — menerima URL download dari extension browser.

Listen di 127.0.0.1:20129. Extension Chromium mengirim POST /api/capture
saat user mulai download, disini diteruskan ke Aria2Engine.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web

from .engine import Aria2Engine

HOST = "127.0.0.1"
PORT = 20129


class CaptureServer:
    """Server HTTP lokal untuk auto-capture download."""

    def __init__(self, engine: Aria2Engine) -> None:
        self._engine = engine
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app: web.Application | None = None

    async def start(self) -> None:
        """Bangun aiohttp app dan mulai listen."""
        app = web.Application()
        app.router.add_post("/api/capture", self._handle_capture)
        app.router.add_get("/api/ping", self._handle_ping)
        self._app = app
        self._loop = asyncio.get_running_loop()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, HOST, PORT)
        await self._site.start()

    async def stop(self) -> None:
        """Matikan server bila sedang jalan."""
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def _handle_capture(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "body bukan JSON"}, status=400)
        url = str(data.get("url", ""))
        if not url:
            return web.json_response({"ok": False, "error": "url kosong"}, status=400)
        filename = data.get("filename")
        filename = str(filename) if filename else None
        try:
            gid = self._engine.add_download(url, filename=filename)
        except Exception as exc:  # noqa: BLE001 — diteruskan ke browser
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        return web.json_response({"ok": True, "gid": gid})

    async def _handle_ping(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    @property
    def app(self) -> web.Application | None:
        return self._app

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop