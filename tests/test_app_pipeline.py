"""Full pipeline with fakes injected — no audio hardware, no LLM server."""

import numpy as np
import pytest

from npc.app import NPCApp, State
from npc.audio.recorder import AudioClip
from npc.events import (
    Busy,
    ConfigReloaded,
    ErrorOccurred,
    LogbookWritten,
    NpcReplied,
    NpcReplyChunk,
    PlayerSpoke,
    RecordingDiscarded,
    StateChanged,
    StatusReport,
    TurnCompleted,
)
from npc.llm import StreamingNotSupported


class FakeLLM:
    model = "fake:latest"

    def __init__(self):
        self.calls = []
        self.summarize_calls = []
        self.reply = "Greetings, traveler."
        self.replies = []  # optional queue; falls back to .reply when empty

    def chat(self, system, messages):
        self.calls.append((system, messages))
        return self.replies.pop(0) if self.replies else self.reply

    def summarize_session(self, transcript, logbook_tail):
        self.summarize_calls.append((transcript, logbook_tail))
        return f"**Location:** the docks\n(summary of {len(transcript)} chars)"


class FakeTranscriber:
    def __init__(self, text="who are you?"):
        self.text = text
        self.calls = 0

    def transcribe(self, clip):
        self.calls += 1
        return self.text


class FakeRecorder:
    def __init__(self, seconds=1.0):
        self.on_auto_stop = None
        self.seconds = seconds
        self.silent = False  # True → all-zero samples (below any energy gate)

    def start(self):
        pass

    def stop(self):
        n = int(16000 * self.seconds)
        value = 0 if self.silent else 8000
        return AudioClip(np.full(n, value, dtype=np.int16))


class FakeSpeaker:
    def __init__(self):
        self.spoken = []
        self.stopped = 0

    def say(self, text):
        self.spoken.append(text)

    def stop(self):
        self.stopped += 1


@pytest.fixture
def app(config):
    events = []
    app = NPCApp(config, llm=FakeLLM(), transcriber=FakeTranscriber(),
                 recorder=FakeRecorder(), speaker=FakeSpeaker(),
                 on_event=events.append)
    app.events = events
    app.start()
    yield app
    app._queue.put(None)


def drain(app):
    app._queue.join()


def of_type(app, event_type):
    return [e for e in app.events if isinstance(e, event_type)]


def test_voice_turn_end_to_end(app):
    app.on_ptt_press()
    app.on_ptt_release()
    drain(app)

    assert app.speaker.spoken == ["Greetings, traveler."]
    assert of_type(app, PlayerSpoke) == [PlayerSpoke("who are you?")]
    assert of_type(app, NpcReplied) == [
        NpcReplied("Vess of the Glass Monolith", "Greetings, traveler.")
    ]
    # transcript written to disk
    content = app.transcript.read()
    assert "**PLAYER:** who are you?" in content
    assert "**Vess of the Glass Monolith:** Greetings, traveler." in content
    assert app.state is State.IDLE


def test_state_events_trace_the_turn(app):
    """The StateChanged stream is what an OBS overlay would subscribe to."""
    app.on_ptt_press()
    app.on_ptt_release()
    drain(app)
    states = [e.state for e in of_type(app, StateChanged)]
    assert states == [State.RECORDING, State.PROCESSING, State.SPEAKING, State.IDLE]


def test_voice_is_in_character_and_typed_is_ooc(app):
    app.handle_line("be more hostile")
    app.on_ptt_press()
    app.on_ptt_release()
    drain(app)

    system, messages = app.llm.calls[-1]
    assert "- be more hostile" in system            # standing instruction
    assert any(m["content"].startswith("GM NOTE") for m in messages)
    assert any(m["content"].startswith("PLAYER (spoken)") for m in messages)


def test_say_command_is_in_character(app):
    assert app.handle_line("/say I offer you this coin") == "ok"
    drain(app)
    _, messages = app.llm.calls[-1]
    assert messages[-1]["content"] == 'PLAYER (spoken): "I offer you this coin"'
    assert app.speaker.spoken == ["Greetings, traveler."]


def test_llm_failure_becomes_error_event_and_session_stays_usable(app):
    from npc.llm import LlmError

    real_chat = app.llm.chat

    def boom(system, messages):
        raise LlmError("cannot reach the LLM server at http://localhost:11434")

    app.llm.chat = boom
    app.handle_line("/say hello?")
    drain(app)
    assert any("cannot reach" in e.message for e in of_type(app, ErrorOccurred))
    assert app.state is State.IDLE

    app.llm.chat = real_chat                       # server "comes back"
    app.handle_line("/say hello again")
    drain(app)
    assert app.speaker.spoken == ["Greetings, traveler."]


def test_llm_decoration_is_stripped_before_speaking(app):
    app.llm.reply = ('Vess of the Glass Monolith: *adjusts her hood* '
                     '"Greetings, traveler." (smiles)')
    app.handle_line("/say hello")
    drain(app)
    assert app.speaker.spoken == ["Greetings, traveler."]
    assert of_type(app, NpcReplied)[-1].text == "Greetings, traveler."
    assert "**Vess of the Glass Monolith:** Greetings, traveler." in app.transcript.read()


def test_too_short_clip_discarded(app):
    app.recorder.seconds = 0.1
    app.on_ptt_press()
    app.on_ptt_release()
    drain(app)
    assert app.llm.calls == []
    assert of_type(app, RecordingDiscarded) == [RecordingDiscarded("too short")]
    assert app.state is State.IDLE


def test_silent_clip_never_reaches_whisper(app):
    app.recorder.silent = True
    app.on_ptt_press()
    app.on_ptt_release()
    drain(app)
    assert app.transcriber.calls == 0              # energy gate short-circuits
    assert app.llm.calls == []
    assert of_type(app, RecordingDiscarded) == [RecordingDiscarded("silence")]
    assert app.state is State.IDLE


def test_phantom_transcript_discarded_before_llm(app):
    app.transcriber.text = "Thanks for watching!"
    app.on_ptt_press()
    app.on_ptt_release()
    drain(app)
    assert app.transcriber.calls == 1
    assert app.llm.calls == []
    discarded = of_type(app, RecordingDiscarded)
    assert len(discarded) == 1 and "hallucination" in discarded[0].reason
    assert app.state is State.IDLE


def test_auto_stop_enqueues_like_a_release(app):
    app.on_ptt_press()
    app.on_auto_stop(AudioClip(np.full(16000, 8000, dtype=np.int16)))
    drain(app)
    assert of_type(app, PlayerSpoke) == [PlayerSpoke("who are you?")]
    assert app.state is State.IDLE


def test_auto_stop_after_manual_release_is_a_noop(app):
    app.on_ptt_press()
    app.on_ptt_release()
    app.on_auto_stop(AudioClip(np.full(16000, 8000, dtype=np.int16)))
    drain(app)
    assert len(of_type(app, PlayerSpoke)) == 1     # exactly one turn


def test_too_short_auto_clip_discarded(app):
    app.on_ptt_press()
    app.on_auto_stop(AudioClip(np.full(800, 8000, dtype=np.int16)))  # 0.05 s
    drain(app)
    assert of_type(app, RecordingDiscarded) == [RecordingDiscarded("too short")]
    assert app.state is State.IDLE


def test_recording_started_flags_auto_stop_recorders(app):
    from npc.events import RecordingStarted

    app.recorder.is_auto_stop = True
    app.on_ptt_press()
    app.on_ptt_release()
    drain(app)
    assert of_type(app, RecordingStarted) == [RecordingStarted(auto_stop=True)]


def test_busy_while_processing(app):
    app._state = State.PROCESSING
    app.on_ptt_press()
    assert of_type(app, Busy)
    app._state = State.IDLE


def test_barge_in_stops_playback(app):
    app._state = State.SPEAKING
    app.on_ptt_press()
    assert app.speaker.stopped == 1
    assert app.state is State.RECORDING
    app.on_ptt_release()
    drain(app)


def test_end_writes_logbook(app, config):
    app.handle_line("/say hello")
    drain(app)
    assert app.handle_line("/end") == "end"
    app.shutdown(summarize=True)

    assert any(e.kind == "end" for e in of_type(app, LogbookWritten))
    text = config.logbook_file.read_text()
    assert "## Session 1" in text
    assert "**Location:** the docks" in text

    # a fresh app for the next session sees the summary and numbers itself 2
    app2 = NPCApp(config, llm=FakeLLM(), on_event=lambda e: None)
    assert app2.session_no == 2
    from npc.session.prompt import build_system_prompt
    system = build_system_prompt(app2.character, app2.adventure,
                                 app2.logbook.tail(3), [])
    assert "**Location:** the docks" in system


def test_quit_does_not_summarize(app, config):
    app.handle_line("/say hello")
    drain(app)
    assert app.handle_line("/quit") == "quit"
    app.shutdown(summarize=False)
    assert "## Session" not in config.logbook_file.read_text()


def test_reload_picks_up_edits(app, config):
    config.character_file.write_text("# Renamed One\n\nA new soul.")
    app.handle_line("/reload")
    assert app.npc_name == "Renamed One"


def test_reload_applies_llm_model_from_config(app, config):
    (config.campaign_dir / "config.toml").write_text(
        'history_limit = 12\n[llm]\nmodel = "llama3.1:8b"\n[stt]\nmodel = "medium"\n'
    )
    app.handle_line("/reload")
    assert app.llm.model == "llama3.1:8b"          # applied live
    assert app.history.limit == 12
    reloaded = of_type(app, ConfigReloaded)[-1]
    assert "[stt]" in reloaded.restart_needed      # stt change flagged


def test_reload_survives_broken_config(app, config):
    (config.campaign_dir / "config.toml").write_text("[llm\nbroken")
    app.handle_line("/reload")
    assert any("not reloaded" in e.message for e in of_type(app, ErrorOccurred))
    assert app.llm.model == "fake:latest"


def test_broken_event_subscriber_does_not_kill_the_session(config):
    def explode(event):
        raise RuntimeError("overlay crashed")

    app = NPCApp(config, llm=FakeLLM(), on_event=explode)
    app.start()
    app.handle_line("/say hello")
    app._queue.join()
    app._queue.put(None)
    assert len(app.llm.calls) == 1                 # turn still completed


# ---------- English-only lock ----------

def test_non_english_reply_is_reasked_in_english(app):
    app.llm.replies = ["Ja, jag talar flera tungomål, resenär.",
                       "Yes — I speak many tongues, traveler."]
    app.handle_line("/say kan du tala svenska?")
    drain(app)

    assert len(app.llm.calls) == 2
    correction = app.llm.calls[-1][1][-1]["content"]
    assert "English-only" in correction
    assert app.speaker.spoken == ["Yes — I speak many tongues, traveler."]
    assert of_type(app, NpcReplied)[-1].text == "Yes — I speak many tongues, traveler."


def test_reply_that_stays_foreign_after_retry_is_never_voiced(app):
    app.llm.replies = ["Ja, jag talar japanska.",
                       "はい、私には日本語ができます。なぜそう尋ねるのですか？"]
    app.handle_line("/say kan du tala japanska?")
    drain(app)

    assert len(app.llm.calls) == 2
    assert app.speaker.spoken == []                # Alba never gets CJK text
    assert "日本語" in of_type(app, NpcReplied)[-1].text  # but the GM sees it
    assert app.state is State.IDLE


def test_streaming_foreign_reply_never_reaches_tts(config):
    llm = StreamingFakeLLM(chunks=("Ja, jag talar gärna ",
                                   "ert tungomål, resenär."))
    llm.replies = ["Ja, jag talar ert tungomål, resenär.",
                   "Yes, I will speak your tongue."]
    events = []
    app = NPCApp(config, llm=llm, speaker=StreamingFakeSpeaker(),
                 on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say tala svenska!")
    drain(app)
    app._queue.put(None)

    assert llm.stream_calls == 1
    assert len(llm.calls) == 2                     # fallback chat + English retry
    assert app.speaker.spoken == ["Yes, I will speak your tongue."]


def test_streaming_mid_reply_language_flip_is_skipped_from_audio(config):
    llm = StreamingFakeLLM(chunks=("You test my patience, traveler. ",
                                   "Ja, jag talar svenska, ju. Leave this place now."))
    events = []
    app = NPCApp(config, llm=llm, speaker=StreamingFakeSpeaker(),
                 on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say hello")
    drain(app)
    app._queue.put(None)

    assert app.speaker.spoken == ["You test my patience, traveler.",
                                  "Leave this place now."]


# ---------- latency instrumentation ----------

def test_voice_turn_emits_timings(app):
    app.on_ptt_press()
    app.on_ptt_release()
    drain(app)
    turns = of_type(app, TurnCompleted)
    assert len(turns) == 1
    t = turns[0]
    assert t.stt_seconds is not None and t.stt_seconds >= 0
    assert t.llm_first_token_seconds is None       # non-streaming fakes
    assert t.speak_seconds is not None
    assert t.total_seconds >= t.llm_seconds >= 0


def test_say_turn_has_no_stt_timing(app):
    app.handle_line("/say hello")
    drain(app)
    assert of_type(app, TurnCompleted)[0].stt_seconds is None


def test_status_reports_last_and_average_turn(app):
    app.handle_line("/say one")
    drain(app)
    app.handle_line("/status")
    status = of_type(app, StatusReport)[-1]
    assert status.last_turn_seconds is not None
    assert status.avg_turn_seconds is not None


# ---------- streaming replies ----------

STREAM_CHUNKS = ("*bows* Greetings, tra", "veler. What brings", " you to the docks?")


class StreamingFakeLLM(FakeLLM):
    def __init__(self, chunks=STREAM_CHUNKS):
        super().__init__()
        self.chunks = chunks
        self.stream_calls = 0

    def chat_stream(self, system, messages):
        self.stream_calls += 1
        yield from self.chunks


class StreamingFakeSpeaker(FakeSpeaker):
    def __init__(self, cancel_after=None):
        super().__init__()
        self.cancel_after = cancel_after

    def say_stream(self, sentences):
        spoken = []
        for sentence in sentences:
            spoken.append(sentence)
            self.spoken.append(sentence)
            if self.cancel_after is not None and len(spoken) >= self.cancel_after:
                return spoken, True
        return spoken, False


@pytest.fixture
def stream_app(config):
    events = []
    app = NPCApp(config, llm=StreamingFakeLLM(), speaker=StreamingFakeSpeaker(),
                 on_event=events.append)
    app.events = events
    app.start()
    yield app
    app._queue.put(None)


def test_streaming_speaks_sentences_and_records_full_reply(stream_app):
    stream_app.handle_line("/say hello")
    drain(stream_app)

    assert stream_app.speaker.spoken == [
        "Greetings, traveler.",                    # *bows* never spoken
        "What brings you to the docks?",
    ]
    assert [e.text for e in of_type(stream_app, NpcReplyChunk)] == list(STREAM_CHUNKS)
    assert of_type(stream_app, NpcReplied) == [NpcReplied(
        "Vess of the Glass Monolith",
        "Greetings, traveler. What brings you to the docks?",
    )]
    assert stream_app.llm.calls == []              # non-streaming chat() never used
    assert ("**Vess of the Glass Monolith:** Greetings, traveler. What brings you to the docks?"
            in stream_app.transcript.read())
    assert stream_app.state is State.IDLE


def test_streaming_turn_records_first_token_timing(stream_app):
    stream_app.handle_line("/say hello")
    drain(stream_app)
    t = of_type(stream_app, TurnCompleted)[0]
    assert t.llm_first_token_seconds is not None
    assert t.llm_first_token_seconds <= t.llm_seconds
    assert t.speak_seconds is not None


def test_streaming_narration_mix_speaks_only_quoted_dialogue(config):
    chunks = ('"You test my patience," I say, ',
              'my voice cold as the void. "Leave the shard."')
    events = []
    app = NPCApp(config, llm=StreamingFakeLLM(chunks), speaker=StreamingFakeSpeaker(),
                 on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say give it to me")
    drain(app)
    app._queue.put(None)

    assert app.speaker.spoken == ["You test my patience.", "Leave the shard."]
    assert of_type(app, NpcReplied)[-1].text == "You test my patience. Leave the shard."


def test_barge_in_mid_stream_records_only_what_was_heard(config):
    events = []
    app = NPCApp(config, llm=StreamingFakeLLM(),
                 speaker=StreamingFakeSpeaker(cancel_after=1), on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say hello")
    drain(app)
    app._queue.put(None)

    assert of_type(app, NpcReplied)[-1].text == "Greetings, traveler."
    content = app.transcript.read()
    assert "**Vess of the Glass Monolith:** Greetings, traveler." in content
    assert "docks" not in content


def test_server_rejecting_stream_falls_back_to_plain_chat(config):
    class NoStreamLLM(FakeLLM):
        def chat_stream(self, system, messages):
            raise StreamingNotSupported("400: streaming not allowed")
            yield  # pragma: no cover — makes this a generator

    events = []
    app = NPCApp(config, llm=NoStreamLLM(), speaker=StreamingFakeSpeaker(),
                 on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say hello")
    drain(app)
    app._queue.put(None)

    assert len(app.llm.calls) == 1                 # fell back to chat()
    assert app.speaker.spoken == ["Greetings, traveler."]
    assert of_type(app, NpcReplied)[-1].text == "Greetings, traveler."


def test_stream_false_in_config_uses_plain_chat(config):
    config.llm.stream = False
    app = NPCApp(config, llm=StreamingFakeLLM(), speaker=StreamingFakeSpeaker(),
                 on_event=lambda e: None)
    app.start()
    app.handle_line("/say hello")
    drain(app)
    app._queue.put(None)

    assert app.llm.stream_calls == 0
    assert len(app.llm.calls) == 1


_ARCH_REFUSAL = "Je vous comprends bien, mais je ne peux pas vous permettre d'approcher de l'arche."
_HUG_REPLY = "Je préfère garder mes distances, mais je vous remercie de demander."


@pytest.mark.parametrize("streaming", [False, True])
def test_repeated_refusal_is_retried_before_audio_display_or_secret_handling(config, streaming):
    config.language = "fr-BE"
    llm = StreamingFakeLLM(chunks=(_ARCH_REFUSAL[:24], _ARCH_REFUSAL[24:],
                                   " [CHECK:teleporter-key]"))
    llm.replies = [_HUG_REPLY] if streaming else [
        _ARCH_REFUSAL + " [CHECK:teleporter-key]", _HUG_REPLY]
    speaker = StreamingFakeSpeaker() if streaming else FakeSpeaker()
    events = []
    app = NPCApp(config, llm=llm, speaker=speaker, on_event=events.append)
    app.events = events
    app.active.history.add_npc(_ARCH_REFUSAL)
    app.start()
    try:
        app.handle_line("/say Et à toi, je peux te faire un câlin ?")
        drain(app)
        assert speaker.spoken == [_HUG_REPLY]
        assert of_type(app, NpcReplied)[-1].text == _HUG_REPLY
        assert "arche" not in "".join(e.text for e in of_type(app, NpcReplyChunk))
        assert app.active.pending_secret is None
        assert "câlin" in llm.calls[-1][1][-1]["content"]
        assert _ARCH_REFUSAL not in app.transcript.read()
        assert app.state is State.IDLE
    finally:
        app._queue.put(None)


def test_repetition_retry_is_bounded_and_does_not_record_another_bad_reply(app):
    app.config.language = "fr"
    app.active.history.add_npc(_ARCH_REFUSAL)
    app.llm.reply = _ARCH_REFUSAL
    app.handle_line("/say Et à toi, je peux te faire un câlin ?")
    drain(app)
    assert len(app.llm.calls) == 2
    assert app.speaker.spoken == []
    assert of_type(app, NpcReplied) == []
    assert "repeated" in of_type(app, ErrorOccurred)[-1].message
    assert app.state is State.IDLE


def test_player_can_explicitly_request_an_exact_repeat(app):
    app.config.language = "fr"
    app.active.history.add_npc(_ARCH_REFUSAL)
    app.llm.reply = _ARCH_REFUSAL
    app.handle_line("/say Répétez, s'il vous plaît.")
    drain(app)
    assert app.speaker.spoken == [_ARCH_REFUSAL]
    assert len(app.llm.calls) == 1


def test_shared_opening_that_diverges_keeps_streaming_without_retry(stream_app):
    stream_app.config.language = "fr"
    stream_app.active.history.add_npc("Non. Le monolithe est une relique sacrée.")
    stream_app.llm.chunks = ("Non. ", "Je préfère garder mes distances.")
    stream_app.handle_line("/say Et à toi, je peux te faire un câlin ?")
    drain(stream_app)
    assert stream_app.speaker.spoken == ["Non.", "Je préfère garder mes distances."]
    assert stream_app.llm.calls == []


def test_rejected_language_attempt_cannot_request_a_secret(app):
    app.config.language = "fr"
    app.llm.replies = [
        "Give me a moment, I need to think. [CHECK:teleporter-key]",
        "Je préfère que vous gardiez vos mains pour vous.",
    ]
    app.handle_line("/say Je peux toucher le monolithe ?")
    drain(app)
    assert app.active.pending_secret is None
    assert app.speaker.spoken == ["Je préfère que vous gardiez vos mains pour vous."]


@pytest.mark.parametrize("selected", [None, "teleporter-key"])
@pytest.mark.parametrize("streaming", [False, True])
def test_separate_secret_routing_controls_requests_without_exposing_bodies(config, selected,
                                                                          streaming):
    class RoutingLLM(StreamingFakeLLM):
        def select_secret_topic(self, line, topics, context):
            self.routing_input = (line, topics, context)
            return selected

        def chat_stream(self, system, messages):
            self.writer_system = system
            yield from super().chat_stream(system, messages)

    llm = RoutingLLM()
    # Normal dialogue can emit a spurious marker; a real secret request
    # can omit its marker. Neither mistake controls the GM gate anymore.
    llm.reply = ("I need a moment to think." if selected
                 else "Please leave the stone alone. [CHECK:teleporter-key]")
    llm.chunks = (llm.reply,)
    config.llm.stream = streaming
    events = []
    app = NPCApp(config, llm=llm, speaker=StreamingFakeSpeaker(), on_event=events.append)
    app.events = events
    app.start()
    try:
        app.handle_line("/say Where is the key?" if selected else "/say Can I touch the monolith?")
        drain(app)
        assert app.active.pending_secret == selected
        system = llm.writer_system if streaming else llm.calls[-1][0]
        if selected:
            assert "Locked knowledge" in system
            assert "- topic (erased-discovery)" not in system
        else:
            assert "Locked knowledge" not in system
        for secret in app.active.secrets.locked():
            assert secret.body not in system
        assert all(isinstance(hint, str) for hint in llm.routing_input[1].values())
    finally:
        app._queue.put(None)


def test_checkpoint_every_n_turns(config):
    config.checkpoint_every_turns = 2
    events = []
    app = NPCApp(config, llm=FakeLLM(), on_event=events.append)
    app.start()
    app.handle_line("/say one")
    app.handle_line("/say two")
    app._queue.join()
    app._queue.put(None)
    assert any(isinstance(e, LogbookWritten) and e.kind == "checkpoint" for e in events)
    assert "## Session 1" in config.logbook_file.read_text()


# ---------- GM-gated secrets ----------
# the scaffolded campaign ships secrets.md with (teleporter-key) [hesitate]
# and (erased-discovery) [deflect]

from npc.events import (  # noqa: E402
    SecretList,
    SecretNote,
    SecretPending,
    SecretPondering,
    SecretResolved,
    SecretRevealRequested,
)
from npc.session.secrets import FALLBACK_STALL  # noqa: E402

STALL = "Hm. Give me a moment — that is not a thing I speak of lightly."


def ask_locked(app, reply=f"{STALL} [CHECK:teleporter-key]"):
    app.llm.reply = reply
    app.handle_line("/say what is hidden under the altar?")
    drain(app)


def test_marker_opens_request_and_never_reaches_players(app):
    ask_locked(app)

    req = of_type(app, SecretRevealRequested)[0]
    assert req.secret_id == "teleporter-key"
    assert "teleporter key" in req.hint
    assert req.player_line == "what is hidden under the altar?"
    assert app.active.pending_secret == "teleporter-key"
    assert of_type(app, SecretPondering) == [
        SecretPondering("Vess of the Glass Monolith", active=True)]
    # the marker is stripped from everything the table sees or hears
    assert app.speaker.spoken == [STALL]
    assert of_type(app, NpcReplied)[0].text == STALL
    assert "CHECK" not in app.transcript.read()
    # the locked BODY was not in the prompt for this turn
    system = app.llm.calls[0][0]
    assert "teleporter key" in system      # the hint is
    assert "altar stone" not in system     # the body is not


def test_yes_delivers_the_secret_and_persists(app, campaign):
    ask_locked(app)
    app.llm.reply = "Beneath the altar stone lies a key, three charges left."
    app.handle_line("/yes but only vaguely")
    drain(app)

    assert of_type(app, SecretResolved) == [SecretResolved(
        "Vess of the Glass Monolith", "teleporter-key", True, "but only vaguely")]
    assert app.active.pending_secret is None
    assert of_type(app, SecretPondering)[-1].active is False
    # delivery turn: body now in the prompt, one-shot GM instruction last
    system, messages = app.llm.calls[-1]
    assert "altar stone" in system
    assert "you WILL share" in messages[-1]["content"]
    assert "GM adds: but only vaguely" in messages[-1]["content"]
    # the delivery instruction is one-shot, never a standing note
    assert app.active.ooc_notes == []
    # spoken + recorded like any reply
    assert app.speaker.spoken[-1] == app.llm.reply
    assert of_type(app, NpcReplied)[-1].text == app.llm.reply
    # write-back: the reveal survives a restart
    assert "revealed: session 1" in (campaign / "secrets.md").read_text()
    # and the session summary will know
    assert ("GM", "revealed the secret (teleporter-key): "
            "the location of a working teleporter key — only for someone "
            "with her full trust") in app.active.turns


@pytest.mark.parametrize("language", ["en", "fr-BE"])
def test_restart_preserves_approved_secrets_and_announces_them_only_to_gm(config, language):
    from npc.session.secrets import SecretsSheet

    config.language = language
    sheet = SecretsSheet.load(config.campaign_dir / "secrets.md")
    key = sheet.get("teleporter-key")
    key.revealed = "session 2"
    sheet.save(config.campaign_dir / "secrets.md")
    events = []
    restarted = NPCApp(config, llm=FakeLLM(), on_event=events.append)
    notices = [e for e in events if isinstance(e, SecretNote)]
    assert len(notices) == 1
    assert notices[0].dm_only
    assert "teleporter-key" in notices[0].message and "session 2" in notices[0].message
    assert key.body not in notices[0].message
    assert restarted.active.secrets.get(key.id).revealed == "session 2"
    assert key.body in restarted._system_prompt(restarted.active)
    restarted._load_roster()
    assert [e for e in events if isinstance(e, SecretNote)] == notices


def test_key_rumour_and_routed_question_never_approve_a_locked_secret(config):
    from npc.session.secrets import SecretsSheet

    class RoutingLLM(FakeLLM):
        def select_secret_topic(self, line, topics, context):
            self.last_topics = topics
            return "teleporter-key" if line.startswith("Where") else None

    llm = RoutingLLM()
    llm.replies = ["What have you heard?", "Tokk has a great deal to say.",
                   "Tell me why you seek it."]
    app = NPCApp(config, llm=llm, on_event=lambda e: None)
    app.start()
    try:
        app.handle_line("/say I heard about a teleporter.")
        app.handle_line("/say Yes, a key, Tokk told me about it.")
        drain(app)
        assert app.active.pending_secret is None
        app.handle_line("/say Where is the key?")
        drain(app)
        assert app.active.pending_secret == "teleporter-key"
        key = app.active.secrets.get("teleporter-key")
        assert key.revealed is None
        saved = SecretsSheet.load(config.campaign_dir / "secrets.md")
        assert saved.get(key.id).revealed is None
        assert llm.last_topics["teleporter-key"] == key.hint
        assert all(key.body not in system for system, _ in llm.calls)
        assert all(key.body not in m["content"] for _, messages in llm.calls for m in messages)
    finally:
        app.shutdown(summarize=False)


def test_no_denies_for_the_session(app):
    ask_locked(app)
    app.handle_line("/no she lies and blames the raiders")
    drain(app)

    resolved = of_type(app, SecretResolved)[0]
    assert resolved.approved is False
    assert app.active.denied_secrets == {"teleporter-key"}
    assert app.active.pending_secret is None
    note = app.active.ooc_notes[-1]
    assert "your character truly knows nothing" in note
    assert "teleporter-key" not in note   # the id would let the model
    assert "GM adds: she lies and blames the raiders" in note
    # no delivery turn, nothing new spoken
    assert app.speaker.spoken == [STALL]
    # a repeat marker for the denied id is ignored
    ask_locked(app)
    assert len(of_type(app, SecretRevealRequested)) == 1


def test_marker_only_reply_speaks_the_fallback_stall(app):
    ask_locked(app, reply="[CHECK:teleporter-key]")
    assert app.speaker.spoken == [FALLBACK_STALL]
    assert of_type(app, NpcReplied)[0].text == FALLBACK_STALL
    assert app.active.pending_secret == "teleporter-key"


def test_unknown_marker_is_dropped(app):
    ask_locked(app, reply="I know nothing of that. [CHECK:made-up-thing]")
    assert of_type(app, SecretRevealRequested) == []
    assert app.active.pending_secret is None
    assert any("made-up-thing" in n.message for n in of_type(app, SecretNote))
    assert app.speaker.spoken == ["I know nothing of that."]


def test_yes_without_pending_is_a_note(app):
    app.handle_line("/yes")
    app.handle_line("/no whatever")
    drain(app)
    assert len(of_type(app, SecretNote)) == 2
    assert of_type(app, SecretResolved) == []


def test_pending_reminder_on_later_turns(app):
    ask_locked(app)
    app.llm.reply = "Patience, I am still thinking."
    app.handle_line("/say tell me now!")
    drain(app)
    assert of_type(app, SecretPending) == [
        SecretPending("Vess of the Glass Monolith", "teleporter-key")]


def test_secrets_listing_and_proactive_reveal(app, campaign):
    app.handle_line("/secrets")
    drain(app)
    listing = of_type(app, SecretList)[0]
    assert len(listing.lines) == 2
    assert all(line.startswith("🔒 locked") for line in listing.lines)

    app.handle_line("/reveal teleporter-key")
    drain(app)
    assert of_type(app, SecretResolved) == [SecretResolved(
        "Vess of the Glass Monolith", "teleporter-key", True, "")]
    assert "unlocked the topic (teleporter-key)" in app.active.ooc_notes[-1]
    assert of_type(app, NpcReplied) == []          # no immediate speech
    assert "revealed: session 1" in (campaign / "secrets.md").read_text()

    app.handle_line("/secrets")
    app.handle_line("/reveal teleporter-key")      # already revealed
    app.handle_line("/reveal nope")                # unknown
    drain(app)
    assert any("✓ revealed" in line for line in of_type(app, SecretList)[-1].lines)
    notes = [n.message for n in of_type(app, SecretNote)]
    assert any("already revealed" in n for n in notes)
    assert any("unknown secret" in n for n in notes)


def test_streaming_marker_split_across_chunks_is_scrubbed(config):
    chunks = (f"{STALL} [CHE", "CK:teleporter-key]")
    events = []
    app = NPCApp(config, llm=StreamingFakeLLM(chunks),
                 speaker=StreamingFakeSpeaker(), on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say what is under the altar?")
    drain(app)
    app._queue.put(None)

    assert of_type(app, SecretRevealRequested)[0].secret_id == "teleporter-key"
    assert app.active.pending_secret == "teleporter-key"
    assert app.speaker.spoken == [STALL]
    # the raw chunk feed reaches the table screen — no marker fragments in it
    streamed = "".join(e.text for e in of_type(app, NpcReplyChunk))
    assert "CHECK" not in streamed and "teleporter-key" not in streamed
    assert of_type(app, NpcReplied)[0].text == STALL


def test_barge_in_before_the_marker_sentence_drops_the_request(config):
    chunks = (f"{STALL} ", "And another sentence follows here. [CHECK:teleporter-key]")
    events = []
    app = NPCApp(config, llm=StreamingFakeLLM(chunks),
                 speaker=StreamingFakeSpeaker(cancel_after=1),
                 on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say what is under the altar?")
    drain(app)
    app._queue.put(None)

    # the player interrupted the stall — the request is simply lost; the NPC
    # will re-emit the marker next time the topic comes up
    assert of_type(app, SecretRevealRequested) == []
    assert app.active.pending_secret is None


def test_later_dismisses_without_denying(app):
    ask_locked(app)
    app.handle_line("/later")
    drain(app)
    assert app.active.pending_secret is None
    assert app.active.denied_secrets == set()
    assert app.active.ooc_notes == []              # no deny note
    assert of_type(app, SecretPondering)[-1].active is False
    assert any("dismissed" in n.message for n in of_type(app, SecretNote))
    # the topic can come up again
    ask_locked(app)
    assert len(of_type(app, SecretRevealRequested)) == 2
    app.handle_line("/later")
    # /later with nothing pending is a note, not an error
    app.handle_line("/later")
    drain(app)
    assert any("no secret is awaiting" in n.message
               for n in of_type(app, SecretNote))


def test_silence_hint_shown_once_per_session(app):
    from npc.events import Info

    app.recorder.silent = True
    for _ in range(2):
        app.on_ptt_press()
        app.on_ptt_release()
        drain(app)
    assert len(of_type(app, RecordingDiscarded)) == 2   # event stream stable
    hints = [e for e in of_type(app, Info) if "noise floor" in e.message]
    assert len(hints) == 1                              # micro-copy only once


def test_lore_reaches_prompt_and_budget_warning_fires_once(config, campaign):
    from npc.events import Info

    lore_dir = campaign / "lore"
    lore_dir.mkdir()
    (lore_dir / "big.txt").write_text("fact " * 20000, encoding="utf-8")
    events = []
    app = NPCApp(config, llm=FakeLLM(), speaker=FakeSpeaker(),
                 on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say hello")
    app.handle_line("/say again")
    drain(app)
    app._queue.put(None)

    assert "Reference knowledge" in app.llm.calls[0][0]
    assert "fact fact" in app.llm.calls[0][0]
    warnings = [e for e in of_type(app, Info) if "context window" in e.message]
    assert len(warnings) == 1                       # once per session
    assert "num_ctx = 32768" in warnings[0].message


def test_no_budget_warning_for_openai_backend(config, campaign):
    from npc.events import Info

    lore_dir = campaign / "lore"
    lore_dir.mkdir()
    (lore_dir / "big.txt").write_text("fact " * 20000, encoding="utf-8")
    config.llm.backend = "openai"
    events = []
    app = NPCApp(config, llm=FakeLLM(), on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say hello")
    drain(app)
    app._queue.put(None)
    assert not [e for e in of_type(app, Info) if "context window" in e.message]


@pytest.mark.parametrize("language", ["fr", "fr-CH", "fr-CA", "fr-BE"])
def test_french_reply_reaches_audio_without_english_retry(app, language):
    app.config.character_languages[app.active.stem] = language
    app.llm.reply = "Oui, je vous écoute. Que désirez-vous ?"
    app.handle_line("/say Bonjour !")
    drain(app)
    assert len(app.llm.calls) == 1
    assert "Always answer in French" in app.llm.calls[0][0]
    assert app.speaker.spoken == ["Oui, je vous écoute. Que désirez-vous?"]


def test_french_streaming_keeps_regional_speech(config):
    config.character_languages["character"] = "fr-CH"
    llm = StreamingFakeLLM(chunks=("Oui, je vous écoute. ", "Cela coûte septante pièces."))
    speaker = StreamingFakeSpeaker()
    app = NPCApp(config, llm=llm, speaker=speaker)
    app.start()
    try:
        app.handle_line("/say Bonjour !")
        drain(app)
        assert llm.stream_calls == 1
        assert not llm.calls
        assert " ".join(speaker.spoken) == "Oui, je vous écoute. Cela coûte septante pièces."
    finally:
        app.shutdown(summarize=False)


def test_french_campaign_reasks_english_reply_in_french(app):
    app.config.language = "fr-BE"
    app.llm.replies = ["You are welcome, my friend.", "Bienvenue, mon ami."]
    app.handle_line("/say Bonjour !")
    drain(app)
    assert len(app.llm.calls) == 2
    assert "entirely in French" in app.llm.calls[-1][1][-1]["content"]
    assert app.speaker.spoken == ["Bienvenue, mon ami."]
