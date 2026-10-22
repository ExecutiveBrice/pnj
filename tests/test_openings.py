"""NPC-led openings use session context and the regular, interruptible pipeline."""

import pytest

from npc.app import NPCApp
from npc.attitudes import SPEECH_ATTITUDES
from npc.events import (
    ErrorOccurred,
    GmNoteAdded,
    NpcReplied,
    NpcReplyChunk,
    PlayerSpoke,
    SecretDisclosureRequested,
    State,
    StateChanged,
    TurnCompleted,
)
from npc.llm import LlmError

from test_app_pipeline import (
    FakeLLM,
    FakeSpeaker,
    StreamingFakeLLM,
    StreamingFakeSpeaker,
    drain,
    of_type,
)
from test_multi_npc import FakeSpeakerFactory


@pytest.fixture
def make_app(config, campaign):
    (campaign / "character.md").write_text("# Lila\nA merchant in a wagon shop.\n")
    (campaign / "characters").mkdir()
    (campaign / "characters/smith.md").write_text("# Smith\nA gruff smith.\n")
    (campaign / "lore").mkdir()
    (campaign / "lore/market.md").write_text("The market closes at sunset.\n")
    apps = []

    def make(llm=None, speaker=None, **kwargs):
        events = []
        app = NPCApp(config, llm=llm or FakeLLM(), speaker=speaker or FakeSpeaker(),
                     on_event=events.append, **kwargs)
        app.events = events
        apps.append(app)
        return app

    yield make
    for app in apps:
        app.shutdown(summarize=False)


@pytest.mark.parametrize("language", ["en", "fr", "fr-CA"])
def test_start_opens_with_context_without_a_player_turn(make_app, config, language):
    config.language = language
    app = make_app()
    app.llm.reply = ("Bienvenue dans ma boutique. [CHECK:teleporter-key]"
                     if language.startswith("fr") else
                     "Welcome to my shop. [CHECK:teleporter-key]")
    app.active.logbook.upsert_entry(1, "2026-10-21", "The wagon lost a wheel.")
    app.active.ooc_notes.append("The merchant is worried about the storm.")
    app.active.history.add_player("I will bring you a new wheel.")

    app.start()
    drain(app)

    system, messages = app.llm.calls[0]
    assert "merchant in a wagon shop" in system
    assert "market closes at sunset" in system
    assert "wagon lost a wheel" in system
    assert "worried about the storm" in system
    assert "bring you a new wheel" in str(messages)
    assert "GM NOTE (out-of-character):" in messages[-1]["content"]
    assert app.active.secrets.get("teleporter-key").body not in system
    assert app.speaker.spoken == [of_type(app, NpcReplied)[0].text]
    assert app.active.last_opening == app.speaker.spoken[0]
    assert app.history.as_messages()[-1]["role"] == "assistant"
    assert not of_type(app, PlayerSpoke)
    assert not of_type(app, GmNoteAdded)
    assert not of_type(app, TurnCompleted)
    assert not of_type(app, SecretDisclosureRequested)
    assert app.active.pending_secret is None
    assert app.active.ooc_notes == ["The merchant is worried about the storm."]
    assert app.active.player_turns == app._player_turns == 0
    assert "**PLAYER" not in app.transcript.read()
    assert [e.state for e in of_type(app, StateChanged)] == [
        State.PROCESSING, State.SPEAKING, State.IDLE]


@pytest.mark.parametrize("streaming", [False, True])
def test_reselect_generates_and_retries_even_a_short_repeated_opening(make_app, streaming):
    old, new = "Welcome, traveler.", "Come and browse my wares."
    llm = StreamingFakeLLM([old]) if streaming else FakeLLM()
    llm.reply = old
    speaker = StreamingFakeSpeaker() if streaming else FakeSpeaker()
    app = make_app(llm=llm, speaker=speaker)
    app.start()
    drain(app)
    app.events.clear()
    llm.replies = [new] if streaming else [old, new]

    assert app.switch_npc("character")  # reopening the already active NPC
    drain(app)

    assert speaker.spoken == [old, new]
    assert [e.text for e in of_type(app, NpcReplied)] == [new]
    assert not of_type(app, NpcReplyChunk)  # copied streaming attempt was held back
    assert app.active.last_opening == new
    assert app.active.player_turns == 0
    assert not of_type(app, ErrorOccurred)
    assert app.state is State.IDLE


def test_returning_to_a_character_uses_only_their_session_and_voice(make_app, config):
    config.tts.voices["smith"] = "en_GB-test-voice"
    factory = FakeSpeakerFactory()
    app = make_app(make_speaker=factory)
    app.llm.replies = ["Welcome to my wagon.", "I saved a wheel for you.",
                       "Welcome to my forge.", "Good to see you back at the wagon."]
    app.start()
    drain(app)
    merchant = app.active
    merchant_speaker = app.speaker
    app.handle_line("Keep the wheel for the players.")
    app.submit_player_text("This wheel is our agreement.")
    drain(app)
    assert app.switch_npc("smith")
    drain(app)
    assert app.speaker is not merchant_speaker
    assert app.speaker.spoken == ["Welcome to my forge."]
    assert "wheel" not in str(app.llm.calls[-1])
    app.handle_line("Forge a royal sword in secret.")
    drain(app)
    assert app.switch_npc("character")
    drain(app)

    system, messages = app.llm.calls[-1]
    assert "Keep the wheel" in system
    assert "wheel is our agreement" in str(messages)
    assert "royal sword" not in system + str(messages)
    assert app.active is merchant
    assert app.speaker is merchant_speaker
    assert app.speaker.spoken[-1] == "Good to see you back at the wagon."
    assert app.roster["smith"].player_turns == 0


def test_last_opening_survives_history_trimming_and_reload(make_app, config):
    config.history_limit = 2
    app = make_app()
    app.llm.reply = "Welcome to my shop."
    app.start()
    drain(app)
    for n in range(4):
        app.active.history.add_npc(f"An unrelated reply number {n}.")
    app.handle_line("/reload")
    assert app.active.last_opening == "Welcome to my shop."
    app.llm.replies = ["Welcome to my shop.", "Come inside, the market is closing."]
    assert app.switch_npc("character")
    drain(app)
    assert app.speaker.spoken == ["Welcome to my shop.", "Come inside, the market is closing."]


def test_repeated_opening_retry_is_bounded_and_worker_remains_usable(make_app):
    app = make_app()
    app.start()
    drain(app)
    assert app.switch_npc("character")
    drain(app)
    assert len(app.llm.calls) == 3  # initial opening, repeated attempt, one retry
    assert len(app.speaker.spoken) == 1
    assert of_type(app, ErrorOccurred)
    assert app.state is State.IDLE
    app.llm.reply = "Please browse the wagon before sunset."
    assert app.switch_npc("character")
    drain(app)
    assert app.speaker.spoken[-1] == app.llm.reply


def test_failed_opening_does_not_prevent_a_player_turn(make_app):
    app = make_app()
    chat = app.llm.chat

    def fail(system, messages):
        raise LlmError("The model is unavailable.")

    app.llm.chat = fail
    app.start()
    drain(app)
    assert of_type(app, ErrorOccurred)
    assert not app.history.as_messages()
    assert app.state is State.IDLE
    app.llm.chat = chat
    app.submit_player_text("Hello merchant.")
    drain(app)
    assert app.active.player_turns == 1
    assert app.speaker.spoken == ["Greetings, traveler."]


def test_cancelled_opening_records_only_heard_words(make_app):
    app = make_app(llm=StreamingFakeLLM(["Welcome to my wagon. Browse my wares."]),
                   speaker=StreamingFakeSpeaker(cancel_after=1))
    app.start()
    drain(app)
    assert app.active.last_opening == "Welcome to my wagon."
    assert app.history.as_messages() == [
        {"role": "assistant", "content": "Welcome to my wagon."}]
    assert "Browse my wares" not in app.transcript.read()
    assert app.state is State.IDLE


def test_internal_switch_for_secret_delivery_does_not_add_an_opening(make_app):
    app = make_app()
    app.start(greet=False)
    app.llm.reply = "A moment, please. [CHECK:teleporter-key]"
    app.submit_player_text("Where is the key?")
    drain(app)
    request_id = app.active.pending_disclosure.request_id
    app.llm.reply = "Welcome to my forge."
    app.switch_npc("smith")
    drain(app)
    before = len(app.llm.calls)
    app.llm.reply = "The key is under the altar stone."
    assert app.resolve_secret_disclosure(request_id, "approved")
    drain(app)
    assert len(app.llm.calls) == before + 1
    assert app.active.stem == "character"
    assert app.active.last_opening is None
    assert app.speaker.spoken[-1] == app.llm.reply


@pytest.mark.parametrize("attitude", SPEECH_ATTITUDES)
@pytest.mark.parametrize("language", ["en", "fr-CA"])
def test_attitude_speech_is_targeted_transient_and_does_not_unlock_secrets(
        make_app, config, attitude, language):
    config.language = language
    secret_path = config.campaign_dir / "secrets/smith.md"
    secret_path.parent.mkdir(exist_ok=True)
    secret_path.write_text("## forge-key\nhint: the forge key\n\nBLOCKED FORGE KEY BODY\n")
    app = make_app()
    app.llm.reply = ("Bienvenue dans ma forge. [CHECK:teleporter-key]"
                     if language.startswith("fr") else
                     "Welcome to my forge. [CHECK:teleporter-key]")
    app.start(greet=False)
    merchant = app.active
    smith = app.roster["smith"]
    smith.ooc_notes.append("The forge is closed today.")
    smith.history.add_player("Our special agreement.")

    assert app.speak_npc("smith", attitude)
    drain(app)

    assert app.active is smith
    assert len(app.llm.calls) == 1  # no neutral greeting before the styled reply
    system, messages = app.llm.calls[0]
    cue = messages[-1]["content"]
    labels = {"agacement": ("Annoyance", "Agacement"),
              "amusant": ("Amusing", "Amusant"), "rude": ("Rude", "Rude"),
              "complaisant": ("Accommodating", "Complaisant")}
    assert labels[attitude][int(language.startswith("fr"))] in cue
    assert "GM NOTE (out-of-character)" in cue
    assert "Our special agreement" in str(messages)
    assert "forge is closed" in system
    assert "wagon shop" not in system
    assert app.speaker.spoken == [app.history.as_messages()[-1]["content"]]
    assert smith.player_turns == merchant.player_turns == 0
    assert smith.ooc_notes == ["The forge is closed today."]
    assert cue not in str(smith.history.as_messages())
    assert merchant.history.as_messages() == []
    assert not of_type(app, PlayerSpoke)
    assert not of_type(app, SecretDisclosureRequested)
    assert "BLOCKED FORGE KEY BODY" not in str(app.llm.calls)
    assert not smith.secrets.revealed()

    app.llm.reply = "Oui, je vous écoute." if language.startswith("fr") else "Yes, go ahead."
    assert app.submit_player_text("What now?")
    drain(app)
    assert cue not in str(app.llm.calls[-1])


def test_attitude_speech_preserves_voice_queue_order_and_each_npcs_history(make_app, config):
    config.tts.voices["smith"] = "en_GB-test-voice"
    factory = FakeSpeakerFactory()
    app = make_app(make_speaker=factory)
    app.llm.replies = ["The wagon is ready.", "Mind the forge, travelers.", "What a fine wagon!"]
    app.start(greet=False)
    merchant = app.active
    assert app.submit_player_text("Is the wagon ready?")
    assert app.speak_npc("smith", "rude")
    assert app.speak_npc("character", "amusant")
    drain(app)
    assert len(app.llm.calls) == 3
    assert merchant.player_turns == 1
    assert app.roster["smith"].player_turns == 0
    assert app.roster["smith"].history.as_messages() == [
        {"role": "assistant", "content": "Mind the forge, travelers."}]
    assert app._speaker_for(app.roster["smith"]).spoken == ["Mind the forge, travelers."]
    assert app.speaker.spoken == ["The wagon is ready.", "What a fine wagon!"]
    assert "curt, gruff and blunt" in str(app.llm.calls[1])
    assert "playful, funny" in str(app.llm.calls[2])
    assert "curt, gruff and blunt" not in str(app.llm.calls[2])


@pytest.mark.parametrize("streaming", [False, True])
def test_attitude_survives_repetition_retry(make_app, streaming):
    llm = StreamingFakeLLM(["Welcome, traveler."]) if streaming else FakeLLM()
    llm.reply = "Welcome, traveler."
    speaker = StreamingFakeSpeaker() if streaming else FakeSpeaker()
    app = make_app(llm=llm, speaker=speaker)
    app.start()
    drain(app)
    llm.replies = (["Move along, traveler."] if streaming
                   else ["Welcome, traveler.", "Move along, traveler."])
    assert app.speak_npc("character", "rude")
    drain(app)
    assert "curt, gruff and blunt" in app.llm.calls[-1][1][-1]["content"]
    assert speaker.spoken == ["Welcome, traveler.", "Move along, traveler."]


@pytest.mark.parametrize("npc,attitude", [
    (None, "rude"), ([], "rude"), ("unknown", "rude"), ("SMITH", "rude"),
    ("smith", None), ("smith", []), ("smith", "invented"),
])
def test_invalid_speech_cues_do_not_generate_or_switch(make_app, npc, attitude):
    app = make_app()
    app.start(greet=False)
    assert not app.speak_npc(npc, attitude)
    drain(app)
    assert app.active.stem == "character"
    assert not app.llm.calls
