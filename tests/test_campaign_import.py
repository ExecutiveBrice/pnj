"""Exercise the legacy adapter with a complete synthetic source repository."""

import hashlib
import json

import pytest

from npc.campaign_import import ARRIVALS, import_campaign, parse_sheets
from npc.cli import main
from npc.config import load_config
from npc.doctor import npc_lore_checks, npc_secrets_checks, npc_voice_checks
from npc.roster import discover_character_files, load_slot
from npc.session.prompt import build_system_prompt

from test_roster import write


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source"
    for name in ("README.md", "CHANGELOG.md", "AGENTS.md"):
        write(source / name, "# Synthetic import fixture\n")
    write(source / "zones/commun/PNJ-IA.md", "```text\nRéponds avec tes seules paroles.\n```\n")
    write(source / "zones/commun/PNJ.md", """**COMMUN-01 — Archibald.**
- Secret : transformer les habitants en poulets
- Ne donne jamais le repère 17 avant l'autorisation du MJ.
""")
    write(source / "campagne/Mission.md",
          "### À lire aux joueurs\nLa mission publique.\n### Organisation du MJ\nGM ONLY\n")
    for index, zone in enumerate(ARRIVALS):
        # Alphabetic prefixes match the source adapter's identifier format.
        prefix = chr(ord("A") + index)
        sheets = "\n\n".join(
            f"**{prefix}-{n:02} — Habitant {prefix}{n} — Garde.**\n"
            f"Je garde ma zone {zone}." + (" Je porte un Bâton (OBJ-01)." if n == 1 else "")
            for n in range(1, 5))
        write(source / f"zones/{zone}/PNJ.md", sheets)
        write(source / f"zones/{zone}/Scenario.md",
              f"# {zone}\n**Lieux :** la place et le pont **Fragment :** indice réservé au MJ\n")
        write(source / f"zones/{zone}/Objets.md",
              "| libre | **Bâton** (OBJ-01) | départ | 0 | +1 défense, 2 charges |\n"
              "| caché | **Couronne** (OBJ-02) | coffre | 10 | GM ONLY |\n")
    return source


def test_import_preserves_sheets_equipment_and_isolates_gm_material(source, tmp_path):
    destination = tmp_path / "export"
    assert import_campaign(source, destination) == 33
    config = load_config(destination)
    refs = discover_character_files(destination)
    assert len(refs) == len({r.logbook_path for r in refs}) == 33
    assert refs[0].stem == "commun/commun-01"
    assert len(list((destination / "zones").glob("*/lore.md"))) == 9
    for path in (source / "zones").glob("*/PNJ.md"):
        for sheet in parse_sheets(path.read_text(encoding="utf-8")):
            ref = next(r for r in refs if r.stem ==
                       f"{path.parent.name}/{sheet.identifier.lower()}")
            slot = load_slot(ref, config)
            assert slot.name == sheet.name
            assert slot.voice
            if path.parent.name != "commun":
                assert sheet.text in slot.character
                if "OBJ-01" in sheet.text:
                    assert "+1 défense, 2 charges" in slot.character
                assert "Couronne" not in slot.character
            prompt = build_system_prompt(
                slot.character, config.adventure_file.read_text(encoding="utf-8"),
                "", [], secrets=slot.secrets, lore=slot.lore, language="fr")
            assert "transformer les habitants en poulets" not in prompt
            assert "repère 17" not in prompt
            assert "GM ONLY" not in prompt
    for check in npc_lore_checks(config) + npc_secrets_checks(config) + npc_voice_checks(config):
        assert check.ok, (check.name, check.detail)


def test_import_is_reproducible_and_fingerprints_match_source(source, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    import_campaign(source, first)
    import_campaign(source, second)
    for path in first.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (second / path.relative_to(first)).read_bytes()
    metadata = json.loads((first / "IMPORT.json").read_text(encoding="utf-8"))
    for relative, digest in metadata["files"].items():
        original = (source / relative).read_bytes()
        assert (first / "mj/source" / relative).read_bytes() == original
        assert hashlib.sha256(original).hexdigest() == digest


def test_cli_import_refuses_to_overwrite_memory(source, tmp_path, capsys):
    destination = tmp_path / "export"
    assert main(["import-campaign", str(source), str(destination)]) == 0
    assert "33 PNJ" in capsys.readouterr().out
    memory = destination / "zones/village/logbooks/b-01.md"
    write(memory, "KEEP MY MEMORY")
    assert main(["import-campaign", str(source), str(destination)]) == 1
    assert "dossier neuf ou vide" in capsys.readouterr().err
    assert memory.read_text(encoding="utf-8") == "KEEP MY MEMORY"


def test_invalid_source_fails_before_writing(source, tmp_path):
    destination = tmp_path / "export"
    with pytest.raises(FileNotFoundError):
        import_campaign(tmp_path / "missing", destination)
    assert not destination.exists()
    (source / "zones/village/PNJ.md").write_text("invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="Fiches PNJ"):
        import_campaign(source, destination)
    assert not destination.exists()
    with pytest.raises(ValueError, match="indépendante"):
        import_campaign(source, source / "nested")
