"""Bidirectional player WebSocket on /ws, isolated from the engine's threads.

Bounded engine outbox + bounded per-client outboxes. A slow client is closed
instead of blocking the engine or retaining unlimited audio. No model, memory,
secret or GM command logic belongs here. Incoming audio is reserved for later;
NPCApp.submit_audio_clip is its engine entry point.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import queue
import threading
from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING

import numpy as np

from .events import Event
from .mobile_protocol import PROTOCOL_VERSION, encode, event_to_message

if TYPE_CHECKING:
    from .app import NPCApp

log = logging.getLogger(__name__)


class MobileServer:
    def __init__(self, app: NPCApp, port: int = 8766, listen: str = "127.0.0.1", *,
                 authorize: Callable | None = None):
        self.app = app
        self.port = port
        self.listen = listen
        # Future token validation can be added at the handshake, before hello.
        self._authorize = authorize
        self._loop = None
        self._thread = None
        self._stop_future = None
        self._ready = threading.Event()
        self._startup_error = None
        self._outbox: queue.Queue = queue.Queue(maxsize=128)
        self._publish_lock = threading.Lock()
        self._scheduled = False
        self._overflow = False
        self._clients: dict = {}

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("mobile server already started; create a new instance to restart")
        try:
            loopback = ipaddress.ip_address(self.listen).is_loopback
        except ValueError:
            loopback = self.listen.lower() == "localhost"
        if not loopback:
            log.warning("Mobile server listening beyond loopback: %s; anyone on this network "
                        "can control the NPC, without authentication or encryption.", self.listen)
        self._thread = threading.Thread(target=self._run, daemon=True, name="mobile")
        self._thread.start()
        if not self._ready.wait(timeout=5):
            self.stop()
            raise RuntimeError("mobile server did not start in time")
        if self._startup_error is not None:
            raise self._startup_error

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._shutdown)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def publish(self, event: Event) -> None:
        message = event_to_message(event, self.app.npc_name)
        if message is not None:
            self._publish((encode(message),))

    def publish_audio(self, samples: np.ndarray, sample_rate: int) -> None:
        """Piper playback observer: PCM s16le mono, one segment per sentence.

        Copy before crossing threads. All frames for a segment are one outbox
        item, so another event/audio segment cannot interleave inside it.
        """
        if not len(samples):
            return
        pcm = np.asarray(samples, dtype="<i2").reshape(-1).tobytes()
        start = encode({"type": "audio_start", "npc": self.app.npc_name,
                        "sample_rate": sample_rate, "channels": 1, "format": "pcm_s16le"})
        frames = tuple(pcm[i:i + 32768] for i in range(0, len(pcm), 32768))
        self._publish((start, *frames, encode({"type": "audio_end"})))

    def _publish(self, frames: tuple) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # Only one pending loop callback, even when the network loop is stalled.
        with self._publish_lock:
            try:
                self._outbox.put_nowait(frames)
            except queue.Full:
                self._overflow = True
            if not self._scheduled:
                self._scheduled = True
                try:
                    loop.call_soon_threadsafe(self._drain)
                except RuntimeError:
                    self._scheduled = False

    def _drain(self) -> None:
        with self._publish_lock:
            overflow = self._overflow
            self._overflow = False
            frames = []
            while not self._outbox.empty():
                frames.append(self._outbox.get_nowait())
            self._scheduled = False
        for connection, outbox in list(self._clients.items()):
            if overflow:
                self._drop(connection)
                continue
            for item in frames:
                if not self._enqueue(connection, outbox, item):
                    break

    def _drop(self, connection) -> None:
        # Remove immediately so repeated publications cannot spawn close tasks.
        self._clients.pop(connection, None)
        asyncio.create_task(connection.close(code=1013, reason="Client too slow; reconnect"))

    def _enqueue(self, connection, outbox, frames) -> bool:
        try:
            outbox.put_nowait(frames)
            return True
        except asyncio.QueueFull:
            self._drop(connection)
            return False

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        self._stop_future = self._loop.create_future()
        async with serve(self._handler, self.listen, self.port,
                         process_request=self._process_request,
                         max_size=32768, max_queue=16, close_timeout=1) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop_future

    def _shutdown(self) -> None:
        if self._stop_future is not None and not self._stop_future.done():
            self._stop_future.set_result(None)

    def _process_request(self, connection, request):
        if request.path != "/ws":
            return connection.respond(HTTPStatus.NOT_FOUND, "Use /ws\n")
        if self._authorize is not None and not self._authorize(request):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        return None

    async def _send(self, connection, outbox) -> None:
        while True:
            frames = await outbox.get()
            for frame in frames:
                await asyncio.wait_for(connection.send(frame), timeout=2)

    async def _handler(self, connection) -> None:
        from websockets.exceptions import ConnectionClosed

        outbox = asyncio.Queue(maxsize=64)
        # No await between snapshot, hello enqueue, and registration: hello is first.
        outbox.put_nowait((encode({"type": "hello", "protocol": PROTOCOL_VERSION,
                                  **self.app.player_status()}),))
        self._clients[connection] = outbox
        sender = asyncio.create_task(self._send(connection, outbox))

        async def receive():
            async for raw in connection:
                response = await self._command(raw)
                if response is not None:
                    if not self._enqueue(connection, outbox, (encode(response),)):
                        return

        receiver = asyncio.create_task(receive())
        try:
            done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ConnectionClosed, TimeoutError):
            pass
        finally:
            self._clients.pop(connection, None)
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)

    async def _command(self, raw) -> dict | None:
        def error(message):
            return {"type": "error", "message": message}
        if not isinstance(raw, str):
            return error("Audio upload is not supported in protocol 1")
        try:
            message = json.loads(raw)
        except (ValueError, RecursionError):
            return error("Invalid JSON")
        if not isinstance(message, dict):
            return error("Expected a JSON object")
        kind = message.get("type")
        try:
            if kind == "status":
                return {"type": "status", **self.app.player_status()}
            if kind == "stop_speaking":
                await asyncio.to_thread(self.app.stop_speaking)
                return None
            if kind in ("say", "switch_npc"):
                value = message.get("text" if kind == "say" else "npc")
                if not isinstance(value, str) or not value.strip():
                    return error("Expected non-empty text" if kind == "say" else "Expected NPC name")
                submit = self.app.submit_player_text if kind == "say" else self.app.switch_npc
                if not await asyncio.to_thread(submit, value):
                    return error("Command rejected" if kind == "say" else "Unknown or ambiguous NPC")
                return None
            return error("Unknown command")
        except Exception:
            # Exception strings may contain model config, file paths or secret ids.
            return error("Command failed")
