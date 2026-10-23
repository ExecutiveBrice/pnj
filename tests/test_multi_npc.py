"""Multiple NPCs per campaign: switching, memory isolation, per-NPC logbooks,
and per-character voices — all with fakes, no hardware."""

import pytest

from npc.app import NPCApp
from npc.config import load_config
from npc.events import (
    Info,
    LogbookWritten,
    NpcReplied,
    NpcSwitched,
    SecretPending,
    StatusReport,
)

from test_app_pipeline import FakeLLM, FakeSpeaker, drain, of_type


class FakeSpeakerFactory:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, voice_path):
        self.calls.append(voice_path)
        if self.fail:
            raise RuntimeError("voice file corrupt")
        return FakeSpeaker()


@pytest.fixture
def multi_campaign(campaign):
    characters = campaign / "characters"
    characters.mkdir()
    (characters / "korval.md").write_text("# Korval the Red\n\nA gruff smith.\n")
    (characters / "mira.md").write_text("# Mira\n\nA quiet scout.\n")
    (campaign / "config.toml").write_text(
        '[tts.voices]\nkorval = "en_GB-test-voice"\n')
    return campaign


@pytest.fixture
def multi_app(multi_campaign):
    events = []
    factory = FakeSpeakerFactory()
    app = NPCApp(load_config(multi_campaign), llm=FakeLLM(),
                 speaker=FakeSpeaker(), make_speaker=factory,
                 on_event=events.append)
    app.events = events
    app.factory = factory
    app.start()
    yield app
    app._queue.put(None)


def switch(app, name):
    app.handle_line(f"/npc {name}")
    drain(app)


def test_roster_discovered_with_legacy_first(multi_app):
    assert list(multi_app.roster) == ["character", "korval", "mira"]
    assert multi_app.npc_name == "Vess of the Glass Monolith"


def test_bare_npc_lists_roster(multi_app):
    multi_app.handle_line("/npc")
    listing = of_type(multi_app, Info)[-1].message
    assert "Korval the Red (korval) — voice en_GB-test-voice" in listing
    assert "[active]" in listing
    assert "Mira (mira) — voice en_GB-alba-medium" in listing  # default voice


def test_unknown_and_ambiguous_names_list_the_roster(multi_app):
    multi_app.handle_line("/npc zorg")
    assert "unknown NPC 'zorg'" in of_type(multi_app, Info)[-1].message
    assert len(of_type(multi_app, NpcSwitched)) == 0


def test_switch_emits_event_and_changes_prompt(multi_app):
    switch(multi_app, "kor")
    switched = of_type(multi_app, NpcSwitched)
    assert switched == [NpcSwitched("Korval the Red", "en_GB-test-voice")]

    multi_app.handle_line("/say who are you?")
    drain(multi_app)
    system, _ = multi_app.llm.calls[-1]
    assert "A gruff smith." in system
    assert "Vault Priest" not in system                 # Vess's sheet stays out
    assert of_type(multi_app, NpcReplied)[-1].npc_name == "Korval the Red"


def test_memories_are_separate_per_npc(multi_app):
    multi_app.handle_line("/say I stole the amber key")   # told to Vess
    drain(multi_app)
    switch(multi_app, "korval")
    multi_app.handle_line("be suspicious of the players")  # OOC for Korval
    multi_app.handle_line("/say hello smith")
    drain(multi_app)
    switch(multi_app, "vess")

    multi_app.handle_line("/say do you remember me?")
    drain(multi_app)
    system, messages = multi_app.llm.calls[-1]
    contents = " ".join(m["content"] for m in messages)
    assert "amber key" in contents                     # Vess's own memory
    assert "hello smith" not in contents               # Korval's stays his
    assert "be suspicious" not in system               # Korval's OOC note too


def test_logbooks_are_strictly_per_npc(multi_campaign, multi_app):
    multi_app.handle_line("/say I stole the amber key")
    drain(multi_app)
    switch(multi_app, "korval")
    multi_app.handle_line("/say forge me a blade")
    drain(multi_app)
    assert multi_app.handle_line("/end") == "end"
    multi_app.shutdown(summarize=True)

    ends = [e for e in of_type(multi_app, LogbookWritten) if e.kind == "end"]
    assert len(ends) == 2
    assert (multi_campaign / "logbook.md").exists()
    assert (multi_campaign / "logbooks" / "korval.md").exists()
    assert not (multi_campaign / "logbooks" / "mira.md").exists()  # zero turns

    vess_input, korval_input = (t for t, _tail in multi_app.llm.summarize_calls)
    assert "amber key" in vess_input and "forge me" not in vess_input
    assert "forge me" in korval_input and "amber key" not in korval_input


def test_session_number_is_campaign_wide_max(multi_campaign):
    (multi_campaign / "logbook.md").write_text("## Session 4 — 2026-01-01\n\nx\n")
    logbooks = multi_campaign / "logbooks"
    logbooks.mkdir()
    (logbooks / "korval.md").write_text("## Session 2 — 2026-01-01\n\ny\n")
    app = NPCApp(load_config(multi_campaign), llm=FakeLLM(), on_event=lambda e: None)
    assert app.session_no == 5


def test_checkpoint_summarizes_only_dirty_npcs(multi_campaign):
    events = []
    config = load_config(multi_campaign)
    config.checkpoint_every_turns = 2
    app = NPCApp(config, llm=FakeLLM(), on_event=events.append)
    app.events = events
    app.start()
    app.handle_line("/say one")           # Vess
    app.handle_line("/npc korval")
    app.handle_line("/say two")           # Korval → checkpoint fires (2 turns)
    app._queue.join()
    assert len(app.llm.summarize_calls) == 2   # both dirty

    app.handle_line("/say three")         # Korval only
    app.handle_line("/say four")          # Korval → second checkpoint
    app._queue.join()
    app._queue.put(None)
    assert len(app.llm.summarize_calls) == 3   # only Korval re-summarized


def test_voice_mapping_drives_the_speaker_factory(multi_app):
    default_speaker = multi_app.speaker
    switch(multi_app, "korval")
    assert len(multi_app.factory.calls) == 1
    assert multi_app.factory.calls[0].name == "en_GB-test-voice.onnx"
    assert multi_app.speaker is not default_speaker   # barge-in hits the new one

    switch(multi_app, "mira")                          # default voice → no factory
    assert len(multi_app.factory.calls) == 1
    assert multi_app.speaker is default_speaker

    switch(multi_app, "korval")                        # cached → no new factory call
    assert len(multi_app.factory.calls) == 1


def test_failing_voice_falls_back_to_default(multi_app):
    multi_app.factory.fail = True
    default_speaker = multi_app.speaker
    switch(multi_app, "korval")
    from npc.events import ErrorOccurred

    assert any("en_GB-test-voice" in e.message
               for e in of_type(multi_app, ErrorOccurred))
    assert multi_app.speaker is default_speaker
    multi_app.handle_line("/say hello")
    drain(multi_app)
    assert default_speaker.spoken == ["Greetings, traveler."]


def test_ooc_note_after_switch_lands_on_the_new_npc(multi_app):
    multi_app.handle_line("/npc korval")
    multi_app.handle_line("watch your tone")   # enqueued right behind the switch
    drain(multi_app)
    assert multi_app.roster["korval"].ooc_notes == ["watch your tone"]
    assert multi_app.roster["character"].ooc_notes == []


def test_player_lines_are_attributed_in_the_transcript(multi_app):
    multi_app.handle_line("/say hello there")
    drain(multi_app)
    assert "**PLAYER → Vess of the Glass Monolith:** hello there" \
        in multi_app.transcript.read()


def test_status_reports_roster_size(multi_app):
    multi_app.handle_line("/status")
    assert of_type(multi_app, StatusReport)[-1].roster_size == 3


def test_reload_discovers_new_character_and_keeps_memories(multi_campaign, multi_app):
    multi_app.handle_line("/say remember the key")
    drain(multi_app)
    (multi_campaign / "characters" / "sable.md").write_text("# Sable\n")
    multi_app.handle_line("/reload")
    assert "sable" in multi_app.roster
    assert len(multi_app.roster["character"].history) == 2  # player + reply kept

# ---------- secrets stay strictly per-NPC ----------

def test_secrets_never_cross_npcs(multi_campaign, multi_app):
    secrets_dir = multi_campaign / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "korval.md").write_text(
        "## stolen-blade\nhint: who really took the ceremonial blade\n\n"
        "Korval took it himself, to settle a debt.\n")
    multi_app.handle_line("/reload")

    # Vess (legacy secrets.md from the template) asks about her own secret
    multi_app.llm.reply = "A moment. [CHECK:teleporter-key]"
    multi_app.handle_line("/say what is under the altar?")
    drain(multi_app)
    assert multi_app.active.pending_secret == "teleporter-key"

    switch(multi_app, "korval")
    multi_app.llm.reply = "Why do you ask me that?"
    multi_app.handle_line("/say who took the blade?")
    drain(multi_app)
    system = multi_app.llm.calls[-1][0]
    # korval's prompt has HIS hint, and nothing of Vess's secrets
    assert "ceremonial blade" in system
    assert "settle a debt" not in system            # his body stays locked too
    assert "teleporter" not in system
    # korval cannot ask about Vess's secret: unknown id for him
    multi_app.llm.reply = "Hm. [CHECK:teleporter-key]"
    multi_app.handle_line("/say and the altar?")
    drain(multi_app)
    assert multi_app.active.pending_secret is None

    # Vess's pending request survived the excursion; /yes lands on HER secret
    switch(multi_app, "vess")
    assert multi_app.active.pending_secret == "teleporter-key"
    reminders = of_type(multi_app, SecretPending)
    assert reminders[-1].npc_name == "Vess of the Glass Monolith"


def test_reveal_to_one_npc_stays_with_that_npc(multi_campaign, multi_app):
    multi_app.llm.reply = "A moment. [CHECK:teleporter-key]"
    multi_app.handle_line("/say what is under the altar?")
    drain(multi_app)  # /yes validates pending on the main thread
    multi_app.handle_line("/yes")
    drain(multi_app)
    assert "altar stone" in multi_app.llm.calls[-1][0]   # Vess's delivery turn

    switch(multi_app, "mira")
    multi_app.llm.reply = "I keep to the woods."
    multi_app.handle_line("/say what do you know?")
    drain(multi_app)
    assert "altar stone" not in multi_app.llm.calls[-1][0]
    assert "teleporter" not in multi_app.llm.calls[-1][0]


def test_lore_is_strictly_per_npc(multi_campaign, multi_app):
    lore_dir = multi_campaign / "lore" / "korval"
    lore_dir.mkdir(parents=True)
    (lore_dir / "smithing.txt").write_text(
        "Star-iron must be quenched in brine.", encoding="utf-8")
    multi_app.handle_line("/reload")

    switch(multi_app, "korval")
    multi_app.handle_line("/say how do you quench star-iron?")
    drain(multi_app)
    assert "quenched in brine" in multi_app.llm.calls[-1][0]

    switch(multi_app, "mira")
    multi_app.handle_line("/say what do you know about smithing?")
    drain(multi_app)
    assert "quenched in brine" not in multi_app.llm.calls[-1][0]


def test_switching_npcs_uses_each_regional_language_and_voice(multi_campaign):
    (multi_campaign / "config.toml").write_text(
        'language = "fr"\n[character_languages]\n'
        'character = "fr-CH"\nkorval = "fr-CA"\nmira = "en"\n', encoding="utf-8")
    factory = FakeSpeakerFactory()
    app = NPCApp(load_config(multi_campaign), llm=FakeLLM(),
                 speaker=FakeSpeaker(), make_speaker=factory)
    app.start()
    try:
        for stem, regional, reply in (
            ("character", "Swiss", "Cela coûte septante pièces."),
            ("korval", "Quebec", "Bienvenue chez nous, mon ami."),
            ("mira", "Always answer in English", "You are welcome, traveler."),
        ):
            app.llm.reply = reply
            switch(app, stem)
            app.handle_line("/say Bonjour")
            drain(app)
            assert regional in app.llm.calls[-1][0]
            assert app.speaker.spoken[-1] == reply
        assert len(app.llm.calls) == 3  # one answer for each NPC
        assert factory.calls[-1].name == "en_GB-alba-medium.onnx"
        app.llm.reply = "Vous revoilà, entrez donc chez nous !"
        switch(app, "korval")
        assert app.speaker.spoken == ["Cela coûte septante pièces.",
                                      "Bienvenue chez nous, mon ami."]
    finally:
        app.shutdown(summarize=False)
