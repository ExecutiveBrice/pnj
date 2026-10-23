"""Local WebSocket event broadcast plus the bundled overlay page.

`npc run --overlay` (or `[overlay] enabled = true`) starts OverlayServer.
It binds 127.0.0.1 by default; `[overlay] listen` widens it to a LAN address
as an explicit opt-in — the stream is unauthenticated and plaintext, so
cli.py then publishes only `table_safe` events (GM notes, status, and every
dm_only secret event stay on this machine). An OBS browser source (or any
browser) opens http://<listen>:<port>/ for the bundled page, which connects
to ws://…/ws and renders the session live.

Thread story: one daemon thread runs a private asyncio loop. publish() is
called from the app's worker/hotkey/main threads and hops onto the loop via
call_soon_threadsafe; websockets.broadcast is synchronous and skips slow
clients, so a stalled OBS can never back-pressure a turn. publish() after
stop() is a silent no-op — the session must never die because of the overlay.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict
from http import HTTPStatus
from importlib import resources

from .events import Event
from .websocket_logging import logger as websocket_logger


def event_to_json(event: Event) -> str:
    """{"type": "NpcReplied", ...fields}. State is a StrEnum (str subclass),
    so json.dumps renders it as its value; default=str is the safety net.
    ensure_ascii=False keeps Swedish/emoji text readable UTF-8 on the wire
    (websocket text frames are UTF-8 by spec)."""
    return json.dumps({"type": type(event).__name__, **asdict(event)},
                      default=str, ensure_ascii=False)


class OverlayServer:
    def __init__(self, port: int = 8765, hello: dict | None = None,
                 listen: str = "127.0.0.1"):
        self.port = port  # updated to the actual port after start() (0 = ephemeral)
        self.listen = listen  # LAN exposure is opt-in; cli.py gates what's sent
        self.hello = hello or {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_future: asyncio.Future | None = None
        self._thread: threading.Thread | None = None
        self._connections: set = set()
        self._ready = threading.Event()
        self._startup_error: Exception | None = None

    # ---------- lifecycle (called from the main thread) ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="overlay")
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("overlay server did not start in time")
        if self._startup_error is not None:
            raise self._startup_error

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._shutdown)
            except RuntimeError:
                pass  # loop already closing
        if self._thread is not None:
            self._thread.join(timeout=5)

    def publish(self, event: Event) -> None:
        """Thread-safe; no-op when the server isn't (or is no longer) running."""
        import websockets

        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(
                websockets.broadcast, self._connections, event_to_json(event))
        except RuntimeError:
            pass  # shut down between the check and the call

    # ---------- server internals (overlay thread) ----------

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as e:
            self._startup_error = e
            self._ready.set()

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        self._stop_future = self._loop.create_future()
        async with serve(self._handler, self.listen, self.port,
                         process_request=self._process_request,
                         logger=websocket_logger) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop_future

    def _shutdown(self) -> None:
        if self._stop_future is not None and not self._stop_future.done():
            self._stop_future.set_result(None)

    async def _handler(self, connection) -> None:
        self._connections.add(connection)
        try:
            await connection.send(json.dumps({"type": "Hello", **self.hello},
                                             ensure_ascii=False))
            await connection.wait_closed()
        finally:
            self._connections.discard(connection)

    def _process_request(self, connection, request):
        if request.path == "/ws":
            return None  # proceed with the WebSocket handshake
        page = (resources.files("npc") / "static" / "overlay.html").read_text("utf-8")
        response = connection.respond(HTTPStatus.OK, page)
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response
