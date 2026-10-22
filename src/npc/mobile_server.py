"""Bidirectional mobile WebSocket on /ws, isolated from the engine's threads.

Bounded engine outbox + bounded per-client outboxes. A slow client is closed
instead of blocking the engine or retaining unlimited audio. No model, memory,
secret or GM decision logic belongs here. Incoming PCM clips are validated and
passed to NPCApp.submit_audio_clip for the existing speech pipeline.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from importlib.resources import files
from typing import TYPE_CHECKING

import numpy as np

from .audio.recorder import AudioClip
from .attitudes import SPEECH_ATTITUDES
from .events import ConfigReloaded, Event, NpcSwitched, SecretDisclosuresChanged, SecretResolved
from .mobile_protocol import PROTOCOL_VERSION, encode, event_to_message, gm_event_to_message
from .voice_studio import StudioCancelled, VoiceStudio

if TYPE_CHECKING:
    from .app import NPCApp

log = logging.getLogger(__name__)

INPUT_SAMPLE_RATE = 16000
# The phone records for 60 seconds; allow a small scheduling margin.
MAX_AUDIO_BYTES = INPUT_SAMPLE_RATE * 2 * 65


@dataclass
class _AudioUpload:
    data: bytearray | None = None


@dataclass(frozen=True)
class _GMFrames:
    frames: tuple


class MobileServer:
    def __init__(self, app: NPCApp, port: int = 8766, listen: str = "127.0.0.1", *,
                 authorize: Callable | None = None, authorize_gm: Callable | None = None,
                 studio=None):
        self.app = app
        self.port = port
        self.listen = listen
        # Future token validation can be added at the handshake, before hello.
        self._authorize = authorize
        # Without a role callback, the configured endpoint is a trusted GM remote.
        self._authorize_gm = authorize_gm
        self._gm_clients: set = set()
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
        if studio is None:
            from .llm import make_llm_client

            studio = VoiceStudio(app.config, llm_factory=lambda: make_llm_client(app.config.llm))
        self.studio = studio
        self._studio_job = None

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
        if self.app.config.mobile.secret_approval:
            if type(event) in (ConfigReloaded, NpcSwitched, SecretDisclosuresChanged,
                              SecretResolved):
                self._publish(_GMFrames((encode(self._npc_details_snapshot()),)))
            if type(event) is SecretDisclosuresChanged:
                self._publish(_GMFrames((encode(self._disclosure_snapshot()),)))
            elif (message := gm_event_to_message(event)) is not None:
                self._publish(_GMFrames((encode(message),)))
                return
        message = event_to_message(event, self.app.npc_name)
        if message is not None:
            self._publish((encode(message),))

    def publish_audio(self, samples: np.ndarray, sample_rate: int) -> None:
        """Speech observer: PCM s16le mono, a sentence or a native streaming chunk.

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

    def _publish(self, frames: tuple | _GMFrames) -> None:
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
                if isinstance(item, _GMFrames):
                    if connection not in self._gm_clients:
                        continue
                    item = item.frames
                if not self._enqueue(connection, outbox, item):
                    break

    def _drop(self, connection) -> None:
        # Remove immediately so repeated publications cannot spawn close tasks.
        self._clients.pop(connection, None)
        self._gm_clients.discard(connection)
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
                         max_size=65536, max_queue=16, close_timeout=1) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop_future

    def _shutdown(self) -> None:
        if self._studio_job is not None:
            self._studio_job[1].set()
        if self._stop_future is not None and not self._stop_future.done():
            self._stop_future.set_result(None)

    def _process_request(self, connection, request):
        if self._authorize is not None and not self._authorize(request):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        if request.path == "/ws":
            return None
        if request.path == "/":
            body = files("npc").joinpath("static/mobile.html").read_bytes()
            return self._http_response(connection, body, "text/html; charset=utf-8")
        if request.path in ("/mobile-sw.js", "/mobile-offline.js"):
            body = files("npc").joinpath("static" + request.path).read_bytes()
            return self._http_response(connection, body, "text/javascript; charset=utf-8")
        match = re.fullmatch(r"/studio/audio/([a-f0-9]{32})\.wav", request.path)
        if match and (path := self.studio.audio_path(match[1])) is not None:
            # Safari and native players use byte ranges to seek/replay WAV files.
            size = path.stat().st_size
            start, end, status = 0, size - 1, HTTPStatus.OK
            # The standalone web companion downloads WAVs into IndexedDB.
            # Keep authorization above this route; credentials are never enabled.
            headers = {"Accept-Ranges": "bytes",
                       "Access-Control-Allow-Origin": "*",
                       "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges"}
            range_header = request.headers.get("Range")
            if range_header is not None:
                selected = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                if selected and any(selected.groups()):
                    first, last = selected.groups()
                    if first:
                        start, end = int(first), min(int(last), size - 1) if last else size - 1
                    else:
                        start = max(0, size - int(last))
                    status = HTTPStatus.PARTIAL_CONTENT
                if not selected or not any(selected.groups()) or start > end or start >= size:
                    response = connection.respond(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "")
                    response.headers["Content-Range"] = f"bytes */{size}"
                    return response
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            with path.open("rb") as audio:
                audio.seek(start)
                body = audio.read(end - start + 1)
            return self._http_response(connection, body, "audio/wav", status, headers)
        return connection.respond(HTTPStatus.NOT_FOUND, "Use / or /ws\n")

    @staticmethod
    def _http_response(connection, body, content_type, status=HTTPStatus.OK, headers=None):
        response = connection.respond(status, "")
        response.body = body
        del response.headers["Content-Type"]
        del response.headers["Content-Length"]
        response.headers["Content-Type"] = content_type
        response.headers["Content-Length"] = str(len(body))
        response.headers["Cache-Control"] = "no-store"
        for key, value in (headers or {}).items():
            response.headers[key] = value
        return response

    async def _send(self, connection, outbox) -> None:
        while True:
            frames = await outbox.get()
            for frame in frames:
                await asyncio.wait_for(connection.send(frame), timeout=2)

    async def _handler(self, connection) -> None:
        from websockets.exceptions import ConnectionClosed

        outbox = asyncio.Queue(maxsize=64)
        gm = (self.app.config.mobile.secret_approval
              and (self._authorize_gm is None or self._authorize_gm(connection.request)))
        # No await between snapshot, hello enqueue, and registration: hello is first.
        snapshot = self.app.player_status()
        outbox.put_nowait((encode({"type": "hello", "protocol": PROTOCOL_VERSION,
                                  **snapshot,
                                  "capabilities": {
                                      "text_input": True,
                                      "audio_input": self.app.transcriber is not None,
                                      "audio_output": self.app.speaker is not None,
                                      "npc_switch": True,
                                      "secret_approval": gm,
                                      "voice_design": self.studio.available,
                                      "story_generation": self.studio.available,
                                  }}), encode({"type": "npc_list", "npcs": snapshot["npcs"]})))
        self._clients[connection] = outbox
        if gm:
            self._gm_clients.add(connection)
            outbox.put_nowait((encode(self._npc_details_snapshot()),))
        sender = asyncio.create_task(self._send(connection, outbox))

        async def receive():
            upload = _AudioUpload()
            async for raw in connection:
                response = await self._command(raw, upload, gm=gm,
                                               connection=connection, outbox=outbox)
                if response is not None:
                    frames = (encode(response),)
                    if response["type"] == "status":
                        frames += (encode({"type": "npc_list", "npcs": response["npcs"]}),)
                        if gm:
                            frames += (encode(self._npc_details_snapshot()),)
                            frames += (encode(self._disclosure_snapshot()),)
                    if not self._enqueue(connection, outbox, frames):
                        return

        receiver = asyncio.create_task(receive())
        try:
            done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ConnectionClosed, TimeoutError):
            pass
        finally:
            if self._studio_job is not None and self._studio_job[0] is connection:
                self._studio_job[1].set()
            self._clients.pop(connection, None)
            self._gm_clients.discard(connection)
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)

    def _disclosure_snapshot(self) -> dict:
        return {"type": "secret_disclosure_list", "requests": self.app.secret_disclosures()}

    def _npc_details_snapshot(self) -> dict:
        return {"type": "gm_npc_list", "npcs": self.app.gm_npc_details(),
                "speech_attitudes": [{"id": key, "label": label}
                                     for key, label in SPEECH_ATTITUDES.items()]}

    def _start_studio(self, connection, outbox, message):
        if self._studio_job is not None:
            return {"type": "error", "message": "Le studio est occupé. Réessayez après la génération."}
        try:
            request = self.studio.validate(message)
        except ValueError as exc:
            return {"type": "error", "message": str(exc)}
        cancel = threading.Event()
        job = (connection, cancel)
        self._studio_job = job
        loop = self._loop

        def reply(message):
            def deliver():
                if connection in self._clients:
                    self._enqueue(connection, outbox, (encode(message),))
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(deliver)
                except RuntimeError:
                    pass

        def progress(message, **fields):
            reply({"type": "studio_progress", "message": message, **fields})

        def completed():
            if self._studio_job is job:
                self._studio_job = None

        def worker():
            try:
                result = self.studio.run(request, cancel=cancel, progress=progress)
                reply(result)
            except StudioCancelled:
                reply({"type": "studio_cancelled"})
            except Exception:
                log.exception("Voice studio generation failed")
                reply({"type": "studio_error", "message":
                       "Échec de génération. Vérifiez Qwen3-TTS (uv sync --extra qwen), "
                       "les modèles et le serveur LLM sur le PC."})
            finally:
                if not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(completed)
                    except RuntimeError:
                        pass

        # Enqueue before starting the producer: started always precedes progress.
        self._enqueue(connection, outbox, (encode({"type": "studio_started"}),))
        threading.Thread(target=worker, name="voice-studio", daemon=True).start()
        return None

    async def _command(self, raw, upload: _AudioUpload, *, gm: bool = False,
                       connection=None, outbox=None) -> dict | None:
        def error(message):
            return {"type": "error", "message": message}
        if not isinstance(raw, str):
            if upload.data is None:
                return error("Send audio_input_start before audio data")
            if len(upload.data) + len(raw) > MAX_AUDIO_BYTES:
                upload.data = None
                return error("Audio clip exceeds 65 seconds")
            upload.data.extend(raw)
            return None
        try:
            message = json.loads(raw)
        except (ValueError, RecursionError):
            return error("Invalid JSON")
        if not isinstance(message, dict):
            return error("Expected a JSON object")
        kind = message.get("type")
        try:
            if kind == "audio_input_start":
                if upload.data is not None:
                    upload.data = None
                    return error("Audio upload already started; start a new clip")
                if self.app.transcriber is None:
                    return error("Voice recognition is unavailable on the server")
                if (type(message.get("sample_rate")) is not int
                        or message["sample_rate"] != INPUT_SAMPLE_RATE
                        or type(message.get("channels")) is not int
                        or message["channels"] != 1 or message.get("format") != "pcm_s16le"):
                    return error("Expected 16000 Hz mono pcm_s16le audio")
                upload.data = bytearray()
                return None
            if kind == "audio_input_end":
                data, upload.data = upload.data, None
                if data is None:
                    return error("No audio upload started")
                if not data or len(data) % 2:
                    return error("Expected non-empty PCM with complete int16 samples")
                clip = AudioClip(np.frombuffer(data, dtype="<i2").astype(np.int16),
                                 INPUT_SAMPLE_RATE)
                if not await asyncio.to_thread(self.app.submit_audio_clip, clip):
                    return error("Audio rejected: server busy, clip too short or voice unavailable")
                return None
            if kind == "status":
                return {"type": "status", **self.app.player_status()}
            if kind == "get_npc_secrets":
                if not gm:
                    return error("NPC secrets are unavailable for this connection")
                details = self.app.gm_npc_secrets(message.get("npc"))
                if details is None:
                    return error("Unknown NPC")
                return {"type": "gm_npc_secrets", **details}
            if kind == "studio_list":
                return self.studio.catalog()
            if kind == "studio_cancel":
                if self._studio_job is not None and self._studio_job[0] is connection:
                    self._studio_job[1].set()
                return None
            if kind in ("voice_design", "narrate_text", "generate_story"):
                if not self.studio.available:
                    return error("Qwen3-TTS indisponible : exécutez uv sync --extra qwen sur le PC.")
                if connection is None or outbox is None:
                    return error("Une connexion mobile est nécessaire.")
                upload.data = None
                return self._start_studio(connection, outbox, message)
            # Changing NPC, submitting text or interrupting discards unfinished audio.
            upload.data = None
            if kind == "speak_npc":
                if not gm:
                    return error("NPC speech cues are unavailable for this connection")
                if not await asyncio.to_thread(self.app.speak_npc,
                                               message.get("npc"), message.get("attitude")):
                    return error("NPC speech cue rejected")
                return None
            if kind == "resolve_secret_disclosure":
                if not gm:
                    return error("Secret approval is unavailable for this connection")
                request_id, decision = message.get("request_id"), message.get("decision")
                if not await asyncio.to_thread(self.app.resolve_secret_disclosure,
                                               request_id, decision):
                    return error("Invalid or rejected secret decision")
                return None
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
