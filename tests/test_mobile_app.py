"""Public player entry points use the existing worker and hardware-free fakes."""

import numpy as np
import pytest

from npc.app import NPCApp
from npc.audio.recorder import AudioClip
from npc.events import PlayerSpoke, RecordingDiscarded, State, StateChanged

from test_app_pipeline import FakeLLM, FakeSpeaker, FakeTranscriber, drain


@pytest.fixture
def app(config, campaign):
    (campaign / "characters").mkdir()
    (campaign / "characters" / "mordrek.md").write_text("# Mordrek\nA smith.\n")
    events = []
    app = NPCApp(config, llm=FakeLLM(), transcriber=FakeTranscriber(),
                 speaker=FakeSpeaker(), on_event=events.append)
    app.events = events
    yield app
    app.shutdown(summarize=False)


def test_text_and_cli_share_queue_and_pipeline(app):
    assert app.submit_player_text("  Bonjour  ")
    app.handle_line("/say Encore")
    assert not app.llm.calls  # nothing calls the LLM outside the worker
    app.start(greet=False)
    drain(app)
    assert [e.text for e in app.events if isinstance(e, PlayerSpoke)] == ["Bonjour", "Encore"]
    assert len(app.llm.calls) == 2
    assert app.speaker.spoken == ["Greetings, traveler."] * 2
    assert app.active.player_turns == 2
    assert "**PLAYER → Vess of the Glass Monolith:** Bonjour" in app.transcript.read()


@pytest.mark.parametrize("text", ["", "  ", None, 123])
def test_text_rejects_empty_or_non_string(app, text):
    assert app.submit_player_text(text) is False
    assert app._queue.empty()


def test_player_cannot_execute_gm_commands(app):
    assert app.submit_player_text("/reveal teleporter-key")
    app.start(greet=False)
    drain(app)
    assert not app.active.secrets.get("teleporter-key").revealed
    assert not app.ooc_notes
    assert '/reveal teleporter-key' in app.llm.calls[0][1][-1]["content"]


def test_switches_are_serialized_even_back_to_initial_npc(app):
    assert app.switch_npc("mord")
    assert app.submit_player_text("For Mordrek")
    assert app.switch_npc("character")
    assert app.submit_player_text("For Vess")
    assert app.npc_name == "Vess of the Glass Monolith"
    app.start(greet=False)
    drain(app)
    assert "A smith." in app.llm.calls[0][0]
    assert len(app.llm.calls) == 4  # two openings and two player replies
    assert all("For Mordrek" not in str(call) for call in app.llm.calls[2:])
    assert app.npc_name == "Vess of the Glass Monolith"


@pytest.mark.parametrize("name", ["", "  ", None, 12, "missing"])
def test_switch_rejects_invalid_names(app, name):
    assert app.switch_npc(name) is False
    assert app._queue.empty()


def test_audio_uses_existing_transcription_pipeline_and_copies_samples(app):
    samples = np.full(16000, 8000, dtype=np.int16)
    assert app.submit_audio_clip(AudioClip(samples, 16000))
    samples.fill(0)  # must not mutate an already submitted utterance
    app.start(greet=False)
    drain(app)
    assert app.transcriber.calls == 1
    assert PlayerSpoke("who are you?") in app.events
    assert app.speaker.spoken == ["Greetings, traveler."]
    states = [e.state for e in app.events if isinstance(e, StateChanged)]
    assert states == [State.PROCESSING, State.SPEAKING, State.IDLE]


@pytest.mark.parametrize("clip", [
    None, AudioClip(np.ones((2, 2), dtype=np.int16)),
    AudioClip(np.ones(16000, dtype=np.float32)),
    AudioClip(np.ones(16000, dtype=np.int16), 0),
    AudioClip(np.ones(16000, dtype=np.int16), True),
])
def test_audio_rejects_invalid_format(app, clip):
    assert app.submit_audio_clip(clip) is False


def test_audio_rejects_short_missing_stt_or_busy(app):
    assert not app.submit_audio_clip(AudioClip(np.ones(10, dtype=np.int16)))
    assert RecordingDiscarded("too short") in app.events
    clip = AudioClip(np.ones(16000, dtype=np.int16))
    app._set_state_if({State.IDLE}, State.RECORDING)
    assert not app.submit_audio_clip(clip)
    app._set_state_if({State.RECORDING}, State.IDLE)
    app.transcriber = None
    assert not app.submit_audio_clip(clip)
    assert app._queue.empty()


def test_stop_only_interrupts_speaking_with_a_speaker(app):
    app.stop_speaking()
    assert app.speaker.stopped == 0
    app._set_state_if({State.IDLE}, State.SPEAKING)
    app.stop_speaking()
    assert app.speaker.stopped == 1
    app.speaker = None
    app.stop_speaking()


def test_status_is_an_explicit_player_safe_snapshot(app):
    app.ooc_notes.append("private GM note")
    app.config.llm.api_key = "private key"
    expected = {"npc": "Vess of the Glass Monolith", "state": "idle",
                "npcs": [{"id": "character", "name": "Vess of the Glass Monolith"},
                         {"id": "mordrek", "name": "Mordrek"}]}
    assert app.player_status() == expected
    snapshot = app.player_status()
    snapshot["npcs"][0]["name"] = "Changed on client"
    snapshot["npcs"].clear()
    assert app.player_status() == expected


def test_submissions_after_shutdown_are_rejected(app):
    app.start(greet=False)
    app.shutdown(summarize=False)
    assert not app.submit_player_text("Too late")
    assert not app.switch_npc("mordrek")
    assert not app.submit_audio_clip(AudioClip(np.ones(16000, dtype=np.int16)))
