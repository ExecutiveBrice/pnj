"""Campaign reset: worker cancellation, scoped disk cleanup and real WebSockets."""

import asyncio
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from websockets.sync.client import connect

from npc.app import NPCApp
from npc.audio.recorder import AudioClip
from npc.events import NpcReplied, NpcReplyChunk, State
from npc.mobile_server import MobileServer
from npc.session.reset import clear_session_directories
from npc.session.secrets import PendingDisclosure

from test_app_pipeline import FakeLLM, FakeRecorder, FakeSpeaker, FakeTranscriber, drain
from test_app_pipeline import StreamingFakeSpeaker
from test_tts import RecordingPlayer, StubPiperSpeaker
from test_voice_studio import StubStudio


@pytest.fixture
def app(config, campaign):
    zone = campaign / "zones" / "village"
    (zone / "characters").mkdir(parents=True)
    (zone / "characters" / "smith.md").write_text("# Smith\nA smith.\n", encoding="utf-8")
    events = []
    app = NPCApp(config, llm=FakeLLM(), speaker=FakeSpeaker(),
                 recorder=FakeRecorder(), transcriber=FakeTranscriber(),
                 on_event=events.append)
    app.events = events
    app.start()
    yield app
    app.shutdown(summarize=False)


@pytest.fixture
def server(app):
    server = MobileServer(app, port=0)
    app._on_event = lambda event: (app.events.append(event), server.publish(event))
    server.start()
    yield server
    server.stop()


def send(ws, kind, **fields):
    ws.send(json.dumps({"type": kind, **fields}))


def receive(ws, kind):
    messages = []
    for _ in range(60):
        raw = ws.recv(timeout=3)
        assert isinstance(raw, str), "Unexpected stale audio"
        message = json.loads(raw)
        messages.append(message)
        if message["type"] == kind:
            return messages
    pytest.fail(f"Missing {kind}: {messages}")


def test_reset_clears_every_slot_and_only_session_files(app, campaign, tmp_path):
    for slot in app.roster.values():
        slot.history.add_player("Old dialogue")
        slot.ooc_notes.append("Old GM note")
        slot.turns.append(("PLAYER", "Old dialogue"))
        slot.player_turns = 3
        slot.dirty = True
        slot.last_opening = "Old greeting"
        slot.pending_secret = "old-secret"
        slot.pending_disclosure = PendingDisclosure("old-secret", "hint", "body", "context")
        slot.denied_secrets.add("old-secret")
    app._disclosure_decisions["old-request"] = "approved"
    app._player_turns = 6
    app._turn_totals.append(2)
    app._silence_hinted = app._ctx_warned = True
    app.logbook.upsert_entry(2, "2026-01-01", "Persistent campaign memory")
    secret = app.active.secrets.get("teleporter-key")
    secret.revealed = "session 2"
    app.active.secrets.save(app.active.secrets_path)
    app.switch_npc("village/smith")
    drain(app)
    app.transcript.append_turn("PLAYER", "Old transcription")
    for name in ("session", "sessions"):
        nested = campaign / name / "nested"
        nested.mkdir(parents=True)
        (nested / "audio.pcm").write_bytes(b"old audio")
        (campaign / name / ".cache").write_text("old cache")
    preserved = []
    for name in ("voix/voice.wav", "histoires/story.wav", "voice_studio/legacy.wav",
                 "zones/village/logbooks/smith.md", "lore/reference.md"):
        target = campaign / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Keep this campaign data", encoding="utf-8")
        preserved.append(target)
    neighbor = tmp_path / "other_campaign" / "session" / "keep.txt"
    neighbor.parent.mkdir(parents=True)
    neighbor.write_text("Another campaign")
    preserved += [campaign / name for name in (
        "character.md", "adventure.md", "secrets.md", "logbook.md", "config.toml")]
    preserved.append(neighbor)
    before = {path: path.read_bytes() for path in preserved}

    assert app.reset_session()

    assert app.state is State.IDLE
    assert app.active.stem == "village/smith"
    assert not app.secret_disclosures()
    assert not app._disclosure_decisions
    assert app._player_turns == 0
    assert not app._turn_totals and app._last_turn is None
    assert not app._silence_hinted and not app._ctx_warned
    assert app.session_no == 3  # persistent logbooks retain their numbering
    for slot in app.roster.values():
        assert not slot.history.as_messages()
        assert not slot.ooc_notes and not slot.turns
        assert not slot.dirty and slot.player_turns == 0
        assert slot.pending_secret is slot.pending_disclosure is slot.last_opening is None
        assert not slot.denied_secrets
    assert app.roster["character"].secrets.get("teleporter-key").revealed == "session 2"
    for name in ("session", "sessions"):
        assert (campaign / name).is_dir()
        assert not list((campaign / name).iterdir())
    assert {path: path.read_bytes() for path in preserved} == before
    assert not app.llm.calls and not app.llm.summarize_calls
    assert app.reset_session()  # repeat an already completed reset
    assert app.submit_player_text("Fresh conversation")
    drain(app)
    assert "Old dialogue" not in str(app.llm.calls)
    assert "Fresh conversation" in app.transcript.read()
    assert "Old transcription" not in app.transcript.read()


@pytest.mark.parametrize("stage", ["chat", "stream", "transcribe", "summarize"])
def test_reset_waits_for_running_work_and_discards_queued_actions(app, monkeypatch, stage):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def block(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return "Old answer. [CHECK:teleporter-key]"

    if stage == "stream":
        def stream(*args):
            try:
                yield block()
            finally:
                closed.set()
        app.llm.chat_stream = stream
        app.speaker = StreamingFakeSpeaker()
    elif stage == "transcribe":
        monkeypatch.setattr(app.transcriber, "transcribe", block)
    elif stage == "summarize":
        monkeypatch.setattr(app.llm, "summarize_session", block)
        app.active.turns.append(("PLAYER", "Old question"))
        app.active.player_turns = 1
        app.active.dirty = True
    else:
        monkeypatch.setattr(app.llm, "chat", block)
    running_speaker = app.speaker

    if stage == "transcribe":
        app.submit_audio_clip(AudioClip(np.full(16000, 8000, dtype=np.int16)))
    elif stage == "summarize":
        app.handle_line("/save")
    else:
        app.submit_player_text("Old question")
    assert entered.wait(2)
    app.submit_player_text("Queued question")
    app.switch_npc("village/smith")
    app.handle_line("Old queued GM note")
    app.handle_line("/save")
    app.resolve_secret_disclosure("old-request", "approved")
    with ThreadPoolExecutor() as pool:
        reset = pool.submit(app.reset_session)
        try:
            assert app._resetting.wait(2)
            assert not reset.done()
            assert not app.submit_player_text("Too early")
            assert not app.switch_npc("village/smith")
            assert not app.speak_npc("character")
            assert not app.resolve_secret_disclosure("old-request", "approved")
            assert not app.reset_session()
        finally:
            release.set()
        assert reset.result(timeout=3)
    drain(app)
    assert app.active.stem == "character"
    assert not app.history and not app.ooc_notes
    assert not app.transcript.read() and not app.logbook.tail(3)
    assert running_speaker.stopped == 1
    assert not running_speaker.spoken
    assert not any(isinstance(event, (NpcReplied, NpcReplyChunk)) for event in app.events)
    assert not app.secret_disclosures()
    if stage == "stream":
        assert closed.is_set()


def test_reset_discards_active_recording(app):
    app.on_ptt_press()
    assert app.state is State.RECORDING
    assert app.reset_session()
    app.on_ptt_release()  # releasing the old recording cannot submit its clip
    drain(app)
    assert app.state is State.IDLE
    assert not app.llm.calls and app.transcriber.calls == 0


def test_reset_rejects_stopped_worker(app):
    app.shutdown(summarize=False)
    assert not app.reset_session()


def test_cleanup_handles_missing_directories(tmp_path):
    clear_session_directories(tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("location", ["session", "sessions", "sessions/nested"])
def test_cleanup_rejects_external_links_before_deleting_anything(campaign, tmp_path, location):
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("Keep")
    ordinary = campaign / "sessions" / "ordinary.txt"
    ordinary.write_text("Keep on validation failure")
    link = campaign / location
    if link.exists():
        ordinary.unlink()
        link.rmdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Symbolic links unavailable")
        # Junction creation needs no developer mode or elevated Windows token.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True, text=True)
        if result.returncode:
            pytest.skip("Directory links unavailable")
    try:
        with pytest.raises(ValueError, match="links"):
            clear_session_directories(campaign)
        assert keep.read_text() == "Keep"
        if ordinary.exists():
            assert ordinary.read_text() == "Keep on validation failure"
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()  # remove the junction itself, never its contents


def test_mobile_reset_acknowledges_cleanup_and_clears_other_uploads(server, campaign):
    uri = f"ws://127.0.0.1:{server.port}/ws"
    with connect(uri) as gm, connect(uri) as other:
        receive(gm, "hello")
        receive(other, "hello")
        send(gm, "say", text="Old conversation")
        receive(gm, "npc_replied")
        drain(server.app)
        receive(other, "npc_replied")
        send(other, "audio_input_start", sample_rate=16000, channels=1, format="pcm_s16le")
        other.send(np.full(16000, 8000, dtype="<i2").tobytes())
        send(other, "status")
        receive(other, "status")  # upload was processed before reset
        send(gm, "reset_session", path=str(campaign.parent), campaign="../other")
        receive(gm, "session_reset")
        assert not list((campaign / "sessions").iterdir())
        assert not server.app.history
        assert (campaign / "character.md").exists()
        send(other, "audio_input_end")
        assert receive(other, "error")[-1]["message"] == "No audio upload started"
        send(gm, "say", text="New conversation")
        messages = receive(gm, "npc_replied")
        assert all(message["type"] != "audio_start" for message in messages)
        drain(server.app)
        assert "Old conversation" not in server.app.transcript.read()


@pytest.mark.parametrize("disabled", ["config", "role"])
def test_mobile_reset_is_reserved_to_gm(server, campaign, disabled):
    if disabled == "config":
        server.app.config.mobile.secret_approval = False
    else:
        server._authorize_gm = lambda request: False
    target = campaign / "sessions" / "keep.txt"
    target.write_text("Keep")
    with connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
        receive(ws, "hello")
        send(ws, "reset_session")
        assert receive(ws, "error")[-1]["message"] == (
            "Session reset is unavailable for this connection")
    assert target.read_text() == "Keep"
    assert server.app.speaker.stopped == 0


def test_mobile_cleanup_failure_returns_sanitized_error_and_can_retry(server, monkeypatch):
    original = Path.unlink

    def fail(path, *args, **kwargs):
        if path.name == "locked.pcm":
            raise PermissionError("PRIVATE PATH AND SECRET")
        return original(path, *args, **kwargs)

    target = server.app.config.sessions_dir / "locked.pcm"
    target.write_bytes(b"audio")
    monkeypatch.setattr(Path, "unlink", fail)
    with connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
        receive(ws, "hello")
        send(ws, "reset_session")
        assert receive(ws, "error")[-1] == {"type": "error", "message": "Session reset failed"}
        assert target.read_bytes() == b"audio"
        assert not server.app._resetting.is_set()
        monkeypatch.setattr(Path, "unlink", original)
        send(ws, "reset_session")
        assert receive(ws, "session_reset")[-1] == {"type": "session_reset"}
        assert not target.exists()


def test_mobile_reset_cancels_and_waits_for_studio(server):
    entered, cancelled, release = (threading.Event() for _ in range(3))

    def synthesize(*args, cancel, **kwargs):
        entered.set()
        assert cancel.wait(5)
        cancelled.set()
        assert release.wait(5)
        return np.ones(10, dtype=np.int16), 24000

    server.studio = StubStudio(server.app.config, runtime_factory=lambda *a:
                              SimpleNamespace(synthesize=synthesize))
    with connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
        receive(ws, "hello")
        send(ws, "voice_design", description="A deep voice.")
        receive(ws, "studio_started")
        assert entered.wait(2)
        send(ws, "reset_session")
        try:
            assert cancelled.wait(2)
            assert server._resetting
        finally:
            release.set()
        messages = receive(ws, "session_reset")
        assert all(m["type"] not in ("studio_result", "studio_cancelled") for m in messages)
        assert server.studio.catalog()["voices"] == []
        assert not list(server.studio.folder.rglob("*.tmp"))


def test_disconnecting_requester_does_not_abandon_reset_or_allow_old_audio(server):
    entered, release = threading.Event(), threading.Event()

    def chat(*args):
        entered.set()
        assert release.wait(5)
        return "Discard this old reply."

    server.app.llm.chat = chat
    uri = f"ws://127.0.0.1:{server.port}/ws"
    with connect(uri) as requester, connect(uri) as survivor:
        receive(requester, "hello")
        receive(survivor, "hello")
        send(requester, "say", text="Old conversation")
        assert entered.wait(2)
        send(requester, "reset_session")
        try:
            assert server.app._resetting.wait(2)
            requester.close()
            send(survivor, "reset_session")
            assert receive(survivor, "error")[-1]["message"] == "Session reset in progress"
            server.publish_audio(np.ones(10, dtype=np.int16), 24000)
        finally:
            release.set()

        async def finished():
            return await server._reset_task

        result = asyncio.run_coroutine_threadsafe(finished(), server._loop).result(timeout=3)
        assert result == {"type": "session_reset"}
        assert not server._resetting and not server.app.history
        assert not list(server.app.config.sessions_dir.iterdir())
        send(survivor, "status")
        messages = receive(survivor, "status")
        assert all(m["type"] not in ("audio_start", "npc_replied", "session_reset")
                   for m in messages)


def test_reset_during_piper_synthesis_cannot_publish_or_play_old_audio():
    player = RecordingPlayer()
    speaker = StubPiperSpeaker(player)
    published = []
    speaker.on_audio = lambda *args: published.append(args)

    def synthesize(text):
        speaker.stop()
        return np.ones(10, dtype=np.int16), 22050

    speaker.synthesize = synthesize
    speaker.say("Old reply")
    assert not published and not player.played
