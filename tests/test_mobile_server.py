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

from test_app_pipeline import FakeLLM, FakeSpeaker, FakeTranscriber
from test_app_pipeline import StreamingFakeLLM
from test_tts import RecordingPlayer, StubPiperSpeaker


@pytest.fixture
def server(config, campaign):
    # Exercise the original player-only surface; GM companion coverage is below.
    config.mobile.secret_approval = False
    (campaign / "characters").mkdir()
    (campaign / "characters" / "mordrek.md").write_text("# Mordrek\nA smith.\n")
    server = None

    def on_event(event):
        if server is not None:
            server.publish(event)

    app = NPCApp(config, llm=FakeLLM(), speaker=FakeSpeaker(), on_event=on_event)
    app.start(greet=False)
    server = MobileServer(app, port=0)
    server.start()
    yield server
    app.shutdown(summarize=False)
    server.stop()


def client(server):
    return connect(f"ws://127.0.0.1:{server.port}/ws")


def recv(ws):
    """Read a message and validate selector data paired with each snapshot."""
    message = json.loads(ws.recv(timeout=2))
    if message["type"] in ("hello", "status"):
        assert json.loads(ws.recv(timeout=2)) == {"type": "npc_list", "npcs": message["npcs"]}
    return message


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
                    "npc": "Vess of the Glass Monolith", "state": "idle",
                    "npcs": [{"id": "character", "name": "Vess of the Glass Monolith"},
                             {"id": "mordrek", "name": "Mordrek"}],
                    "capabilities": {"text_input": True, "audio_input": False,
                                     "audio_output": True, "npc_switch": True,
                                     "secret_approval": False,
                                     "voice_design": server.studio.available,
                                     "story_generation": server.studio.available}}
        assert recv(first) == recv(second) == expected
        server.publish(NpcReplied("Mordrek", "Écoutez…"))
        expected = {"type": "npc_replied", "npc": "Mordrek", "text": "Écoutez…"}
        assert recv(first) == recv(second) == expected


def test_mobile_selector_receives_npc_list_then_switches_by_selected_id(server):
    # The phone ignores extra hello fields and populates its picker only from npc_list.
    with client(server) as ws:
        hello = json.loads(ws.recv(timeout=2))
        assert hello["type"] == "hello"
        message = json.loads(ws.recv(timeout=2))
        assert message == {"type": "npc_list", "npcs": [
            {"id": "character", "name": "Vess of the Glass Monolith"},
            {"id": "mordrek", "name": "Mordrek"},
        ]}
        send(ws, type="switch_npc", npc=message["npcs"][1]["id"])
        assert until(ws, "npc_switched")["npc"] == "Mordrek"


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


def audio_start(ws, **overrides):
    send(ws, **{"type": "audio_input_start", "sample_rate": 16000,
                "channels": 1, "format": "pcm_s16le", **overrides})


def test_phone_audio_runs_through_transcription_and_reply_without_pc_microphone(server):
    server.app.transcriber = FakeTranscriber("Bonjour, aubergiste.")
    assert server.app.recorder is None
    with client(server) as ws:
        assert recv(ws)["capabilities"]["audio_input"] is True
        audio_start(ws)
        pcm = np.full(16000, 8000, dtype="<i2").tobytes()
        # Also accept a sample split between messages.
        ws.send(pcm[:101])
        ws.send(pcm[101:])
        send(ws, type="audio_input_end")
        assert until(ws, "player_spoke")["text"] == "Bonjour, aubergiste."
        assert until(ws, "npc_replied")["text"] == "Greetings, traveler."
        assert server.app.transcriber.calls == 1


@pytest.mark.parametrize("overrides", [
    {"sample_rate": 44100}, {"sample_rate": True}, {"channels": 2},
    {"channels": True}, {"format": "webm"},
])
def test_audio_format_errors_leave_connection_usable(server, overrides):
    server.app.transcriber = FakeTranscriber()
    with client(server) as ws:
        recv(ws)
        audio_start(ws, **overrides)
        assert recv(ws)["type"] == "error"
        send(ws, type="status")
        assert recv(ws)["type"] == "status"
    assert server.app.transcriber.calls == 0


def test_audio_rejected_when_transcriber_is_unavailable(server):
    with client(server) as ws:
        assert recv(ws)["capabilities"]["audio_input"] is False
        audio_start(ws)
        assert recv(ws)["type"] == "error"


def test_audio_uploads_are_separate_for_each_client(server):
    server.app.transcriber = FakeTranscriber()
    with client(server) as first, client(server) as second:
        recv(first)
        recv(second)
        audio_start(first)
        first.send(b"\x01\x00")
        send(second, type="audio_input_end")
        assert recv(second)["type"] == "error"
        first.close()  # Disconnecting discards the partial clip.
        send(second, type="status")
        assert recv(second)["type"] == "status"
    assert server.app.transcriber.calls == 0


@pytest.mark.parametrize("pcm", [b"", b"\x01", b"\x01\x00"])
def test_invalid_or_short_audio_is_not_transcribed(server, pcm):
    server.app.transcriber = FakeTranscriber()
    with client(server) as ws:
        recv(ws)
        audio_start(ws)
        ws.send(pcm)
        send(ws, type="audio_input_end")
        assert until(ws, "error")["type"] == "error"
        send(ws, type="audio_input_end")
        assert recv(ws)["type"] == "error"
    assert server.app.transcriber.calls == 0


def test_audio_upload_limit_discards_buffer_and_allows_retry(server, monkeypatch):
    monkeypatch.setattr("npc.mobile_server.MAX_AUDIO_BYTES", 4)
    server.app.transcriber = FakeTranscriber()
    with client(server) as ws:
        recv(ws)
        audio_start(ws)
        ws.send(b"123456")
        assert recv(ws)["type"] == "error"
        audio_start(ws)
        send(ws, type="status")
        assert recv(ws)["type"] == "status"


def test_uploaded_pcm_preserves_sample_values_and_rate(server, monkeypatch):
    clips = []
    server.app.transcriber = FakeTranscriber()
    monkeypatch.setattr(server.app, "submit_audio_clip", lambda clip: clips.append(clip) or True)
    with client(server) as ws:
        recv(ws)
        audio_start(ws)
        ws.send(np.array([-32768, -1, 0, 1, 32767], dtype="<i2").tobytes())
        send(ws, type="audio_input_end")
        send(ws, type="status")
        assert recv(ws)["type"] == "status"
    assert len(clips) == 1
    assert clips[0].sample_rate == 16000
    assert clips[0].samples.dtype == np.dtype("int16")
    assert clips[0].samples.tolist() == [-32768, -1, 0, 1, 32767]


def test_busy_engine_rejects_audio_without_transcription(server):
    server.app.transcriber = FakeTranscriber()
    server.app._set_state_if({State.IDLE}, State.PROCESSING)
    with client(server) as ws:
        recv(ws)
        audio_start(ws)
        ws.send(np.full(16000, 8000, dtype="<i2").tobytes())
        send(ws, type="audio_input_end")
        assert recv(ws)["type"] == "error"
    assert server.app.transcriber.calls == 0


def test_duplicate_start_discards_partial_audio(server):
    server.app.transcriber = FakeTranscriber()
    with client(server) as ws:
        recv(ws)
        audio_start(ws)
        ws.send(b"\x01\x00")
        audio_start(ws)
        assert recv(ws)["type"] == "error"
        send(ws, type="audio_input_end")
        assert recv(ws)["type"] == "error"
    assert server.app.transcriber.calls == 0


def test_switch_and_late_hello_use_current_npc(server):
    with client(server) as ws:
        hello = recv(ws)
        selected = hello["npcs"][1]
        send(ws, type="switch_npc", npc=selected["id"])
        assert until(ws, "npc_switched") == {"type": "npc_switched", "npc": "Mordrek"}
        assert until(ws, "npc_replied")["npc"] == "Mordrek"
        server.app._queue.join()  # the selection includes the NPC's opening
        send(ws, type="status")
        assert until(ws, "status") == {"type": "status", "npc": "Mordrek", "state": "idle",
                                        "npcs": hello["npcs"]}
        with client(server) as late:
            snapshot = recv(late)
            assert snapshot["npc"] == "Mordrek"
            assert snapshot["npcs"] == hello["npcs"]
        server.publish(NpcReplyChunk("Bonjour"))
        assert recv(ws) == {"type": "npc_reply_chunk", "npc": "Mordrek", "text": "Bonjour"}


def test_status_refreshes_character_list_after_reload(server, campaign):
    with client(server) as ws:
        assert len(recv(ws)["npcs"]) == 2
        (campaign / "characters" / "mira.md").write_text("# Mira\nA scout.\n")
        server.app.handle_line("/reload")
        send(ws, type="status")
        snapshot = until(ws, "status")
        assert {"id": "mira", "name": "Mira"} in snapshot["npcs"]
        send(ws, type="switch_npc", npc="mira")
        assert until(ws, "npc_switched")["npc"] == "Mira"


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


def test_pocket_stream_keeps_native_pcm_intact_over_websocket(server):
    from types import SimpleNamespace

    from npc.pocket import PocketSpeaker

    samples = np.round(np.sin(np.arange(7680) * 0.07) * 8000).astype(np.int16)

    class Chunk:
        def __init__(self, pcm):
            self.pcm = pcm

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self.pcm.astype(np.float32) / 32767

    def generate(state, text, *, copy_state, stop):
        for offset in range(0, len(samples), 1920):
            yield Chunk(samples[offset:offset + 1920])

    runtime = SimpleNamespace(model=SimpleNamespace(sample_rate=24000, generate_audio_stream=generate),
                              lock=threading.RLock(), prepare=lambda voice: {})
    speaker = PocketSpeaker(runtime, "marius", RecordingPlayer(),
                            on_audio=server.publish_audio, local_playback=False)
    with client(server) as ws:
        recv(ws)
        worker = threading.Thread(target=speaker.say, args=("Bonjour.",))
        worker.start()
        data = bytearray()
        for _ in range(4):
            assert recv(ws)["sample_rate"] == 24000
            data.extend(ws.recv(timeout=2))
            assert recv(ws) == {"type": "audio_end"}
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert data == samples.astype("<i2").tobytes()


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
