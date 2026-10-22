"""The packaged templates produce a usable campaign without external files."""

from npc.cli import init_campaign
from npc.config import load_config
from npc.roster import discover_character_files, load_slot
from npc.session.prompt import build_system_prompt


def test_template_campaign_loads_without_exposing_locked_secrets(tmp_path):
    init_campaign(tmp_path)
    config = load_config(tmp_path)
    refs = discover_character_files(tmp_path)
    assert len(refs) == 1
    slot = load_slot(refs[0], config)
    assert slot.name == "Vess of the Glass Monolith"
    assert slot.secrets_error is None
    assert slot.lore_errors == []
    assert slot.secrets.locked()
    prompt = build_system_prompt(
        slot.character, config.adventure_file.read_text(encoding="utf-8"),
        "", [], secrets=slot.secrets)
    for secret in slot.secrets.locked():
        assert secret.hint in prompt
        assert secret.body not in prompt
