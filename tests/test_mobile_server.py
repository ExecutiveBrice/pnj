"""Real localhost WebSocket exchanges, no microphone or model services."""

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from npc.app import NPCApp
from npc.events import (
    ErrorOccurred,
    Event,
    GmNoteAdded,
    Info,
    NpcReplied,
    NpcReplyChunk,
    SecretList,
    SecretNote,
    SecretPending,
    SecretResolved,
    SecretRevealRequested,
    State,
    StateChanged,
    StatusReport,
)
from npc.mobile_protocol import event_to_message
from npc.mobile_server import MobileServer

from test_app_pipeline import FakeLLM, FakeSpeaker
from test_app_pipeline import StreamingFakeLLM
from test_tts import RecordingPlayer, StubPiperSpeaker


@pytest.fixture
def server(config, campaign):
    (campaign / "characters").mkdir()
    (campaign / "characters" / "mordrek.md").write_text("# Mordrek\nA smith.\n")
    server = None

    def on_event(event):
        if server is not None:
            server.publish(event)

    app = NPCApp(config, llm=FakeLLM(), speaker=FakeSpeaker(), on_event=on_event)
    app.start()
    server = MobileServer(app, port=0)
    server.start()
    yield server
    app.shutdown(summarize=False)
    server.stop()


def client(server):
    return connect(f"ws://127.0.0.1:{server.port}/ws")


def recv(ws):
    return json.loads(ws.recv(timeout=2))


def send(ws, **message):
    ws.send(json.dumps(message))


def until(ws, kind):
    for _ in range(30):
        message = recv(ws)
        if message["type"] == kind:
            return message
    pytest.fail(f"did not receive {kind}")


def test_hello_and_multiple_clients(server):
    with client(server) as first, client(server) as second:
        expected = {"type": "hello", "protocol": 1,
                    "npc": "Vess of the Glass Monolith", "state": "idle"}
        assert recv(first) == recv(second) == expected
        server.publish(NpcReplied("Mordrek", "Écoutez…"))
        expected = {"type": "npc_replied", "npc": "Mordrek", "text": "Écoutez…"}
        assert recv(first) == recv(second) == expected


def test_disconnect_does_not_break_remaining_client(server):
    with client(server) as survivor:
        recv(survivor)
        with client(server) as departing:
            recv(departing)
        server.publish(StateChanged(State.PROCESSING))
        assert recv(survivor) == {"type": "state", "state": "processing"}


def test_say_uses_app_pipeline_and_returns_reply(server):
    with client(server) as ws:
        recv(ws)
        send(ws, type="say", text="Bonjour, aubergiste.")
        assert until(ws, "player_spoke")["text"] == "Bonjour, aubergiste."
        assert until(ws, "npc_replied")["text"] == "Greetings, traveler."
        assert until(ws, "state") == {"type": "state", "state": "speaking"}
        assert until(ws, "state") == {"type": "state", "state": "idle"}
        assert len(server.app.llm.calls) == 1


def test_switch_and_late_hello_use_current_npc(server):
    with client(server) as ws:
        recv(ws)
        send(ws, type="switch_npc", npc="mordrek")
        assert until(ws, "npc_switched") == {"type": "npc_switched", "npc": "Mordrek"}
        send(ws, type="status")
        assert until(ws, "status") == {"type": "status", "npc": "Mordrek", "state": "idle"}
        with client(server) as late:
            assert recv(late)["npc"] == "Mordrek"
        server.publish(NpcReplyChunk("Bonjour"))
        assert recv(ws) == {"type": "npc_reply_chunk", "npc": "Mordrek", "text": "Bonjour"}


def test_status_and_stop_remain_responsive_while_worker_is_speaking(server):
    playing = threading.Event()
    stopped = threading.Event()

    class BlockingSpeaker(FakeSpeaker):
        def say(self, text):
            playing.set()
            assert stopped.wait(timeout=4)

        def stop(self):
            stopped.set()

    server.app.speaker = BlockingSpeaker()
    with client(server) as ws:
        recv(ws)
        send(ws, type="say", text="Hello")
        assert playing.wait(timeout=2)
        send(ws, type="status")
        assert until(ws, "status")["state"] == "speaking"
        with client(server) as late:
            assert recv(late)["state"] == "speaking"
        send(ws, type="stop_speaking")
        assert stopped.wait(timeout=2)
        assert until(ws, "state")["state"] == "idle"


@pytest.mark.parametrize("raw", ["{", "[]", "null", '"say"', '{"type": []}',
                                  '{"type": "say", "text": 3}',
                                  '{"type": "say", "text": " "}',
                                  '{"type": "switch_npc", "npc": "missing"}',
                                  '{"type": "/reveal"}', b"audio"])
def test_bad_commands_are_safe_errors_and_connection_survives(server, raw):
    with client(server) as ws:
        recv(ws)
        ws.send(raw)
        assert recv(ws)["type"] == "error"
        send(ws, type="status")
        assert recv(ws)["type"] == "status"


def test_internal_exception_details_never_reach_player(server, monkeypatch):
    def fail(text):
        raise RuntimeError("secret-id /private/path api-key")

    monkeypatch.setattr(server.app, "submit_player_text", fail)
    with client(server) as ws:
        recv(ws)
        send(ws, type="say", text="Hello")
        assert recv(ws) == {"type": "error", "message": "Command failed"}


@dataclass(frozen=True)
class PrivateEvent(Event):
    dm_only: ClassVar[bool] = True
    table_safe: ClassVar[bool] = True  # dm_only always takes priority
    secret: str = "private"


@dataclass(frozen=True)
class UnclassifiedEvent(Event):
    secret: str = "private"


@dataclass(frozen=True)
class UnknownPublicEvent(Event):
    table_safe: ClassVar[bool] = True
    secret: str = "private"


PRIVATE_EVENTS = [
    PrivateEvent(), UnclassifiedEvent(), UnknownPublicEvent(), GmNoteAdded("private"), Info("private"),
    ErrorOccurred("secret-id"), SecretNote("private"),
    SecretRevealRequested("NPC", "secret-id", "hint", "question"),
    SecretPending("NPC", "secret-id"), SecretResolved("NPC", "secret-id", True, "private"),
    SecretList("NPC", ("secret-id",)),
    StatusReport(State.IDLE, "NPC", "model", 1, 2, 3),
]


@pytest.mark.parametrize("event", PRIVATE_EVENTS)
def test_serializer_refuses_private_or_not_table_safe_events(event):
    assert event_to_message(event, "NPC") is None


def test_player_filter_is_enforced_inside_server_even_on_loopback(server):
    with client(server) as ws:
        recv(ws)
        for event in PRIVATE_EVENTS:
            server.publish(event)
        server.publish(NpcReplied("NPC", "Public barrier"))
        assert recv(ws) == {"type": "npc_replied", "npc": "NPC", "text": "Public barrier"}
        with pytest.raises(TimeoutError):
            ws.recv(timeout=0.05)


def test_audio_framing_pcm_endianness_and_order(server):
    samples = np.array([1, -2, 32767, -32768] * 10000, dtype=np.int16)
    with client(server) as first, client(server) as second:
        recv(first)
        recv(second)
        server.publish_audio(samples, 22050)
        server.publish(NpcReplied("NPC", "After audio"))
        for ws in (first, second):
            assert recv(ws) == {"type": "audio_start", "npc": server.app.npc_name,
                                "sample_rate": 22050, "channels": 1, "format": "pcm_s16le"}
            data = b""
            for _ in range(3):
                frame = ws.recv(timeout=2)
                assert isinstance(frame, bytes)
                assert len(frame) <= 32768
                data += frame
            assert data == samples.astype("<i2").tobytes()
            assert recv(ws) == {"type": "audio_end"}
            assert recv(ws)["text"] == "After audio"


def test_streamed_text_and_audio_keep_secret_markers_off_the_wire(server):
    server.app.llm = StreamingFakeLLM(("Greetings, traveler. [CHE", "CK:teleporter-key]"))
    speaker = StubPiperSpeaker(RecordingPlayer())
    speaker.on_audio = server.publish_audio
    server.app.speaker = speaker
    with client(server) as ws:
        recv(ws)
        send(ws, type="say", text="Hello")
        messages, binary = [], []
        for _ in range(30):
            raw = ws.recv(timeout=2)
            if isinstance(raw, bytes):
                binary.append(raw)
            else:
                message = json.loads(raw)
                messages.append(message)
                if message == {"type": "state", "state": "idle"}:
                    break
        else:
            pytest.fail("stream did not finish")
        assert binary  # streamed Piper output traversed the real WebSocket
        assert any(m["type"] == "npc_reply_chunk" for m in messages)
        assert any(m["type"] == "npc_replied" for m in messages)
        assert any(m["type"] == "audio_start" for m in messages)
        assert "teleporter-key" not in json.dumps(messages)
        assert "CHECK" not in json.dumps(messages)
        assert server.app.active.pending_secret == "teleporter-key"  # still available to the GM
        assert speaker._player.played == [len("Greetings, traveler.")]


def test_lan_binding_requires_explicit_listen_and_warns(server, caplog):
    lan = MobileServer(server.app, port=0, listen="0.0.0.0")
    lan.start()
    try:
        with client(lan) as ws:
            assert recv(ws)["type"] == "hello"
        assert "without authentication or encryption" in caplog.text
    finally:
        lan.stop()


def test_invalid_path_and_future_handshake_authorizer(server):
    with pytest.raises(InvalidStatus) as exc:
        connect(f"ws://127.0.0.1:{server.port}/other")
    assert exc.value.response.status_code == 404
    gated = MobileServer(server.app, port=0, authorize=lambda request: False)
    gated.start()
    try:
        with pytest.raises(InvalidStatus) as exc:
            with client(gated):
                pass
        assert exc.value.response.status_code == 401
    finally:
        gated.stop()


def test_lifecycle_and_port_collision(server):
    second = MobileServer(server.app, port=server.port)
    with pytest.raises(OSError):
        second.start()
    second.stop()
    server.stop()
    server.stop()
    server.publish(StateChanged(State.IDLE))
    server.publish_audio(np.ones(2, dtype=np.int16), 22050)
    assert not server._thread.is_alive()


def test_slow_client_queue_is_bounded_and_closed_without_blocking_engine(config):
    class Connection:
        closed = False

        async def close(self, **kwargs):
            self.closed = True

    async def exercise():
        app = NPCApp(config, llm=FakeLLM(), on_event=lambda e: None)
        server = MobileServer(app)
        stalled, healthy = Connection(), Connection()
        full = asyncio.Queue(maxsize=1)
        full.put_nowait(("old",))
        available = asyncio.Queue(maxsize=2)
        server._clients = {stalled: full, healthy: available}
        server._outbox.put_nowait(("new",))
        server._drain()
        await asyncio.sleep(0)
        assert stalled.closed
        assert stalled not in server._clients
        assert available.get_nowait() == ("new",)

    asyncio.run(exercise())


def test_stalled_loop_publication_is_bounded(config):
    class StalledLoop:
        calls = 0

        def is_closed(self):
            return False

        def call_soon_threadsafe(self, callback):
            self.calls += 1

    app = NPCApp(config, llm=FakeLLM(), on_event=lambda e: None)
    server = MobileServer(app)
    loop = StalledLoop()
    server._loop = loop
    for _ in range(500):
        server.publish(StateChanged(State.IDLE))
    assert server._outbox.qsize() == 128
    assert server._overflow
    assert loop.calls == 1
