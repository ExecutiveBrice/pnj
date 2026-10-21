"""doctor --fix: the interactive fixer loop (no network, no hardware)."""

import sys
from types import SimpleNamespace

import pytest

from npc.config import Config, TtsConfig
from npc.doctor import CheckResult, apply_fixes, npc_voice_checks, whisper_detail
from npc.tts import download_hint


@pytest.fixture
def setup_checks(config, monkeypatch):
    from npc import doctor

    monkeypatch.setattr("npc.llm.make_llm_client", lambda config: SimpleNamespace(
        is_up=lambda: True, has_model=lambda: True))
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        query_devices=lambda **kwargs: {"name": "test audio"}))
    monkeypatch.setattr(doctor, "_whisper_cache_dir", lambda model: config.campaign_dir)
    config.tts.voices_dir = str(config.campaign_dir / "voices")
    config.tts.voice_path.parent.mkdir(parents=True, exist_ok=True)
    config.tts.voice_path.touch()
    return config


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_unsupported_global_hotkey_is_informational(setup_checks, monkeypatch, platform):
    from npc.doctor import print_report, run_checks

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr("npc.hotkey.keycode_from_name",
                        lambda name: pytest.fail("unsupported hotkey must not be probed"))
    checks = run_checks(setup_checks)
    hotkey = next(c for c in checks if c.name.startswith("Push-to-talk"))
    assert hotkey.ok and hotkey.skipped
    assert "npc gui" in hotkey.detail
    assert all(c.ok for c in checks)
    output = []
    assert print_report(checks, out=output.append)
    assert any("[SKIP] Push-to-talk" in line for line in output)
    assert not any("[FAIL]" in line or "fix:" in line for line in output)


def test_desktop_checks_omit_hotkey(setup_checks, monkeypatch):
    from npc.doctor import run_checks

    monkeypatch.setattr("npc.hotkey.keycode_from_name",
                        lambda name: pytest.fail("desktop must not probe hotkeys"))
    assert not any(c.name.startswith("Push-to-talk")
                   for c in run_checks(setup_checks, check_hotkey=False))


@pytest.mark.parametrize("available", [False, True])
def test_linux_still_checks_global_hotkey(setup_checks, monkeypatch, available):
    from npc.doctor import run_checks
    from npc.hotkey import HotkeyUnavailable

    closed = []

    def devices(*args):
        if not available:
            raise HotkeyUnavailable("no permission to read /dev/input devices")
        return [SimpleNamespace(name="test keyboard", close=lambda: closed.append(True))]

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("npc.hotkey.keycode_from_name", lambda name: 57)
    monkeypatch.setattr("npc.hotkey.find_ptt_devices", devices)
    hotkey = next(c for c in run_checks(setup_checks) if c.name.startswith("Push-to-talk"))
    assert hotkey.ok == available
    assert not hotkey.skipped
    assert closed == ([True] if available else [])
    if not available:
        assert "usermod" in hotkey.fix


def test_doctor_succeeds_when_only_global_hotkey_is_skipped(config, monkeypatch, capsys):
    from npc.cli import main

    monkeypatch.setattr("npc.doctor.run_checks", lambda *a, **kw: [
        CheckResult("Push-to-talk (KEY_SPACE)", True, skipped=True, detail="use npc gui")])
    assert main(["doctor", str(config.campaign_dir)]) == 0
    output = capsys.readouterr().out
    assert "[SKIP] Push-to-talk" in output
    assert "All good" in output
    assert "Fix the FAILs" not in output


def test_apply_fixes_runs_only_confirmed_fixers():
    ran = []
    checks = [
        CheckResult("passing", True, fixer=lambda: ran.append("passing")),
        CheckResult("no-fixer", False, fix="sudo something  # print-only"),
        CheckResult("confirmed", False, fixer=lambda: ran.append("confirmed"),
                    fix_label="download the thing"),
        CheckResult("declined", False, fixer=lambda: ran.append("declined")),
    ]
    answers = iter(["y", "n"])
    out = []
    assert apply_fixes(checks, ask=lambda prompt: next(answers), out=out.append)
    assert ran == ["confirmed"]
    assert any("fixed: confirmed" in line for line in out)


def test_apply_fixes_survives_a_raising_fixer():
    def boom():
        raise RuntimeError("download failed")

    out = []
    assert apply_fixes([CheckResult("bad", False, fixer=boom, fix_label="x")],
                       ask=lambda prompt: "y", out=out.append)
    assert any("fix failed for bad" in line for line in out)


def test_apply_fixes_asks_nothing_when_all_pass():
    checks = [CheckResult("fine", True, fixer=lambda: None)]
    assert not apply_fixes(checks, ask=lambda prompt: pytest.fail("should not ask"),
                           out=lambda line: None)


def test_cpu_whisper_says_so_and_nudges_toward_the_cuda_extra():
    assert "CPU" in whisper_detail("cpu") and "cuda" in whisper_detail("cpu")
    assert "CPU" not in whisper_detail("cuda")


def test_voice_download_hint_is_pasteable_outside_a_project(tmp_path):
    # `uv run python` resolved to an env without piper for tool installs (#3)
    hint = download_hint("en_GB-alba-medium", tmp_path)
    assert hint.startswith(sys.executable) and "uv run" not in hint


def multi_config(tmp_path, voices):
    (tmp_path / "characters").mkdir()
    (tmp_path / "characters" / "korval.md").write_text("# Korval\n")
    return Config(campaign_dir=tmp_path,
                  tts=TtsConfig(voices_dir=str(tmp_path / "voices"), voices=voices))


def test_unknown_voice_mapping_stem_is_flagged(tmp_path):
    config = multi_config(tmp_path, {"korvall": "en_GB-x"})  # typo'd stem
    checks = npc_voice_checks(config)
    mapping = [c for c in checks if c.name.startswith("NPC voice mapping")]
    assert len(mapping) == 1 and not mapping[0].ok
    assert "korvall" in mapping[0].detail and "korval" in mapping[0].detail


def test_missing_mapped_voice_gets_a_fixer(tmp_path):
    config = multi_config(tmp_path, {"korval": "en_GB-extra-voice"})
    checks = npc_voice_checks(config)
    voice = [c for c in checks if c.name == "NPC voice (en_GB-extra-voice)"]
    assert len(voice) == 1 and not voice[0].ok
    assert voice[0].fixer is not None
    assert "en_GB-extra-voice" in voice[0].fix_label


def test_mapped_default_voice_is_not_rechecked(tmp_path):
    config = multi_config(tmp_path, {"korval": "en_GB-alba-medium"})  # = default
    assert [c for c in npc_voice_checks(config) if c.name.startswith("NPC voice (")] == []


def test_stem_collision_is_flagged(tmp_path):
    config = multi_config(tmp_path, {})
    (tmp_path / "character.md").write_text("# Vess\n")
    (tmp_path / "characters" / "character.md").write_text("# Impostor\n")
    collision = [c for c in npc_voice_checks(config) if c.name == "Character files"]
    assert len(collision) == 1 and not collision[0].ok


def test_secrets_check_passes_and_counts(tmp_path):
    from npc.doctor import npc_secrets_checks

    (tmp_path / "character.md").write_text("# Vess\n")
    (tmp_path / "secrets.md").write_text(
        "## a\nhint: h\n\nbody\n\n## b\nhint: h\nrevealed: session 1\n\nbody\n")
    checks = npc_secrets_checks(Config(campaign_dir=tmp_path))
    assert [(c.name, c.ok, c.detail) for c in checks] == [
        ("Secrets (character)", True, "1 locked, 1 revealed")]


def test_broken_secrets_file_is_flagged_soft(tmp_path):
    from npc.doctor import npc_secrets_checks

    (tmp_path / "character.md").write_text("# Vess\n")
    (tmp_path / "secrets.md").write_text("## bad\nno hint here\n")
    checks = npc_secrets_checks(Config(campaign_dir=tmp_path))
    assert len(checks) == 1 and not checks[0].ok and not checks[0].hard
    assert "missing its 'hint:'" in checks[0].detail


def test_missing_secrets_file_is_not_checked(tmp_path):
    from npc.doctor import npc_secrets_checks

    (tmp_path / "character.md").write_text("# Vess\n")
    assert npc_secrets_checks(Config(campaign_dir=tmp_path)) == []


def test_lore_check_counts_and_budget(tmp_path):
    from npc.doctor import npc_lore_checks

    (tmp_path / "character.md").write_text("# Vess\n", encoding="utf-8")
    lore = tmp_path / "lore"
    lore.mkdir()
    (lore / "small.txt").write_text("The river forks twice.", encoding="utf-8")
    config = Config(campaign_dir=tmp_path)
    checks = npc_lore_checks(config)
    assert [c.name for c in checks] == ["Lore (character)"]
    assert checks[0].ok and "1 file(s)" in checks[0].detail

    (lore / "big.txt").write_text("fact " * 20000, encoding="utf-8")
    checks = npc_lore_checks(config)
    budget = [c for c in checks if c.name == "Context budget (character)"]
    assert len(budget) == 1 and not budget[0].ok and not budget[0].hard
    assert "num_ctx = 32768" in budget[0].fix

    config.llm.num_ctx = 32768                      # following the advice
    assert not [c for c in npc_lore_checks(config)
                if c.name.startswith("Context budget")]


def test_lore_check_flags_thin_pdf(tmp_path):
    from test_lore import make_pdf

    from npc.doctor import npc_lore_checks

    (tmp_path / "character.md").write_text("# Vess\n", encoding="utf-8")
    lore = tmp_path / "lore"
    lore.mkdir()
    (lore / "thin.pdf").write_bytes(make_pdf("tiny"))
    checks = npc_lore_checks(Config(campaign_dir=tmp_path))
    thin = [c for c in checks if c.name == "Lore (character)"]
    assert len(thin) == 1 and not thin[0].ok
    assert "scanned/image PDF" in thin[0].detail
