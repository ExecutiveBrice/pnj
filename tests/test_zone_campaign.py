"""Native zone campaigns: discovery, isolated prompts, reload and settings."""

from pathlib import Path

import pytest

from npc.app import NPCApp
from npc.config import Config, load_config, save_language, save_tts
from npc.doctor import npc_lore_checks
from npc.roster import discover_character_files, load_slot, resolve_npc

from test_app_pipeline import FakeLLM, drain
from test_roster import write

@pytest.fixture
def zones(tmp_path):
    write(tmp_path / "zones/commun/characters/mage.md", "# Mage\nA common NPC.")
    write(tmp_path / "zones/village/characters/garde.md", "# Alice\nVillage sheet.")
    write(tmp_path / "zones/foret/characters/garde.md", "# Bob\nForest sheet.")
    write(tmp_path / "zones/village/lore.md", "Village public scenery.")
    write(tmp_path / "zones/foret/lore.md", "Forest public scenery.")
    write(tmp_path / "zones/village/lore/garde/personal.md", "Village private knowledge.")
    write(tmp_path / "zones/village/Scenario.md", "FORBIDDEN GM SOLUTION")
    write(tmp_path / "zones/village/PNJ.md", "FORBIDDEN OTHER SHEETS")
    write(tmp_path / "zones/commun/lore.md", "Mage's own mission.")
    write(tmp_path / "zones/village/secrets/garde.md",
          "## cache\nhint: a hidden cache\n\nFORBIDDEN SECRET BODY\n")
    return tmp_path


def test_discovery_combines_layouts_without_loading_gm_material(zones):
    write(zones / "character.md", "# Legacy")
    write(zones / "characters/smith.md", "# Smith")
    refs = discover_character_files(zones)
    assert [r.stem for r in refs] == [
        "character", "smith", "commun/mage", "foret/garde", "village/garde"]
    assert refs[-1].logbook_path == zones / "zones/village/logbooks/garde.md"
    assert refs[-1].secrets_path == zones / "zones/village/secrets/garde.md"


def test_short_names_can_be_ambiguous_but_qualified_ids_are_exact(zones):
    config = Config(campaign_dir=zones)
    slots = {r.stem: load_slot(r, config) for r in discover_character_files(zones)}
    assert len(resolve_npc(slots, "garde")) == 2
    assert resolve_npc(slots, "VILLAGE/GARDE") is slots["village/garde"]
    assert resolve_npc(slots, "ali") is slots["village/garde"]
    assert resolve_npc(slots, "bob") is slots["foret/garde"]
    assert resolve_npc(slots, "mag") is slots["commun/mage"]


def test_shared_and_private_lore_reload_without_replacing_memory(zones):
    config = Config(campaign_dir=zones)
    slot = load_slot(discover_character_files(zones)[-1], config)
    assert [f.text for f in slot.lore] == ["Village public scenery.", "Village private knowledge."]
    slot.history.add_player("A promise known only in the village")
    slot.logbook.upsert_entry(1, "2026-10-22", "Our agreement")
    write(zones / "zones/village/lore.md", "Village after the agreement.")
    slot.refresh(config)
    assert slot.lore[0].text == "Village after the agreement."
    assert len(slot.history) == 1
    assert "Our agreement" in slot.logbook.tail(1)
    forest = load_slot(discover_character_files(zones)[1], config)
    assert forest.logbook.tail(1) == ""
    assert [f.text for f in forest.lore] == ["Forest public scenery."]


def test_zone_runtime_switch_reload_and_logbooks_are_isolated(zones):
    app = NPCApp(Config(campaign_dir=zones), llm=FakeLLM(), on_event=lambda _: None)
    app.start(greet=False)
    try:
        app.handle_line("/npc alice")
        app.handle_line("/say This is our village promise")
        drain(app)
        prompt, _ = app.llm.calls[-1]
        assert "Village public scenery" in prompt
        assert "Village private knowledge" in prompt
        assert "Forest public scenery" not in prompt
        assert "Mage's own mission" not in prompt
        assert "FORBIDDEN" not in prompt
        write(zones / "zones/village/lore.md", "UPDATED VILLAGE")
        app.handle_line("/reload")
        app.handle_line("/say Remember the promise?")
        drain(app)
        assert "UPDATED VILLAGE" in app.llm.calls[-1][0]
        app.handle_line("/npc foret/garde")
        app.handle_line("/say I greet the forest guard")
        drain(app)
        prompt, messages = app.llm.calls[-1]
        assert "Forest public scenery" in prompt
        assert "Village" not in prompt
        assert "village promise" not in str(messages)
        assert {n["id"] for n in app.player_status()["npcs"]} == {
            "commun/mage", "foret/garde", "village/garde"}
    finally:
        app.shutdown(summarize=True)
    assert (zones / "zones/village/logbooks/garde.md").exists()
    assert (zones / "zones/foret/logbooks/garde.md").exists()
    forest, village = (turns for turns, _ in app.llm.summarize_calls)
    assert "village promise" in village and "village promise" not in forest
    assert "forest guard" in forest and "forest guard" not in village


def test_zone_voice_and_language_settings_round_trip(zones):
    save_language(zones, "fr-BE", "village/garde")
    save_tts(zones, backend="pocket")
    save_tts(zones, voice="jean", stem="village/garde")
    config = load_config(zones)
    slot = load_slot(discover_character_files(zones)[-1], config)
    assert slot.voice == "jean"
    assert config.language_for("village/garde") == "fr-BE"
    assert config.language_for("foret/garde") == "en"


@pytest.mark.parametrize("runtime", [True, False], ids=["runtime", "doctor"])
def test_shared_lore_is_read_once_per_load_and_edits_are_visible(zones, monkeypatch, runtime):
    write(zones / "zones/village/characters/autre.md", "# Another villager")
    shared = zones / "zones/village/lore.md"
    reads = []
    read_text = Path.read_text

    def counted_read(path, *args, **kwargs):
        if path == shared:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read)
    config = Config(campaign_dir=zones)
    if runtime:
        app = NPCApp(config, llm=FakeLLM(), on_event=lambda _: None)
        reload = app._load_roster
        assert app.roster["village/garde"].lore[0] is app.roster["village/autre"].lore[0]
    else:
        def reload():
            return npc_lore_checks(config)

        reload()
    assert len(reads) == 1
    write(shared, "The village has changed.")
    reload()
    assert len(reads) == 2
    if runtime:
        assert app.roster["village/garde"].lore[0].text == "The village has changed."
        assert app.roster["village/autre"].lore[0].text == "The village has changed."
        assert "changed" not in app.roster["foret/garde"].lore[0].text
