import pytest

from npc.config import ConfigError, load_config


def test_defaults_without_config_file(tmp_path):
    config = load_config(tmp_path)
    assert config.llm.model == "qwen2.5:7b-instruct"
    assert config.stt.language == "auto"
    assert config.tts.voice == "en_GB-alba-medium"
    assert config.hotkey.key == "KEY_SPACE"
    assert config.history_limit == 30


def test_overrides(tmp_path):
    (tmp_path / "config.toml").write_text(
        "history_limit = 10\n"
        '[llm]\nmodel = "llama3.1:8b"\n'
        '[stt]\nlanguage = "sv"\n'
        '[hotkey]\nkey = "KEY_F12"\ngrab = true\n'
    )
    config = load_config(tmp_path)
    assert config.llm.model == "llama3.1:8b"
    assert config.stt.language == "sv"
    assert config.hotkey.key == "KEY_F12"
    assert config.hotkey.grab is True
    assert config.history_limit == 10
    # untouched sections keep defaults
    assert config.tts.voice == "en_GB-alba-medium"


def test_invalid_hotkey_mode_rejected(tmp_path):
    (tmp_path / "config.toml").write_text('[hotkey]\nmode = "double-tap"\n')
    with pytest.raises(ConfigError, match='"hold" or "tap"'):
        load_config(tmp_path)


def test_unknown_key_is_a_friendly_error(tmp_path):
    (tmp_path / "config.toml").write_text("[llm]\nmodell = 'oops'\n")
    with pytest.raises(ConfigError, match="valid keys"):
        load_config(tmp_path)


def test_tts_voices_mapping_parsed(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[tts.voices]\nkorval = "en_GB-northern_english_male-medium"\n')
    config = load_config(tmp_path)
    assert config.tts.voices == {"korval": "en_GB-northern_english_male-medium"}
    assert config.tts.voice_path_for("x").name == "x.onnx"
    assert config.tts.voice == "en_GB-alba-medium"    # default untouched


def test_tts_voices_rejects_non_string_values(tmp_path):
    (tmp_path / "config.toml").write_text("[tts.voices]\nkorval = 3\n")
    with pytest.raises(ConfigError, match="tts.voices"):
        load_config(tmp_path)


def test_env_var_overrides_config_api_key(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[llm]\napi_key = "from-file"\n')
    assert load_config(tmp_path).llm.api_key == "from-file"
    monkeypatch.setenv("NPC_LLM_API_KEY", "from-env")
    assert load_config(tmp_path).llm.api_key == "from-env"


def test_voice_path_expands_user(tmp_path):
    (tmp_path / "config.toml").write_text('[tts]\nvoices_dir = "~/voices"\n')
    config = load_config(tmp_path)
    assert "~" not in str(config.tts.voice_path)
    assert config.tts.voice_path.name == "en_GB-alba-medium.onnx"


def test_num_ctx_parses_and_validates(tmp_path):
    (tmp_path / "config.toml").write_text("[llm]\nnum_ctx = 8192\n",
                                          encoding="utf-8")
    assert load_config(tmp_path).llm.num_ctx == 8192

    (tmp_path / "config.toml").write_text("[llm]\nnum_ctx = -1\n",
                                          encoding="utf-8")
    with pytest.raises(ConfigError, match="num_ctx must be a positive"):
        load_config(tmp_path)

    (tmp_path / "config.toml").write_text("[llm]\nnum_ctx = true\n",
                                          encoding="utf-8")
    with pytest.raises(ConfigError, match="num_ctx must be a positive"):
        load_config(tmp_path)


def test_overlay_listen_parses_and_rejects_empty(tmp_path):
    (tmp_path / "config.toml").write_text('[overlay]\nlisten = "0.0.0.0"\n',
                                          encoding="utf-8")
    assert load_config(tmp_path).overlay.listen == "0.0.0.0"

    (tmp_path / "config.toml").write_text('[overlay]\nlisten = "  "\n',
                                          encoding="utf-8")
    with pytest.raises(ConfigError, match="overlay.listen"):
        load_config(tmp_path)


@pytest.mark.parametrize("language", ["fr", "fr-CH", "fr-CA", "fr-BE"])
def test_french_campaign_uses_french_voice(tmp_path, language):
    (tmp_path / "config.toml").write_text(f'language = "{language}"\n', encoding="utf-8")
    config = load_config(tmp_path)
    assert config.language_for("any") == language
    assert config.tts.voice == "fr_FR-siwis-medium"


def test_character_languages_keep_custom_voices(tmp_path):
    (tmp_path / "config.toml").write_text(
        'language = "fr"\n[character_languages]\n'
        'vess = "fr-CH"\nkorval = "en"\n'
        '[tts.voices]\nvess = "custom-french-voice"\n', encoding="utf-8")
    config = load_config(tmp_path)
    assert config.language_for("vess") == "fr-CH"
    assert config.language_for("korval") == "en"
    assert config.language_for("other") == "fr"
    assert config.tts.voices == {"vess": "custom-french-voice", "korval": "en_GB-alba-medium"}


@pytest.mark.parametrize("source", [
    'language = "unknown"', 'language = []',
    '[character_languages]\nvess = 42', 'character_languages = "fr"',
])
def test_invalid_conversation_language_rejected(tmp_path, source):
    (tmp_path / "config.toml").write_text(source, encoding="utf-8")
    with pytest.raises(ConfigError, match="language"):
        load_config(tmp_path)


@pytest.mark.parametrize("source", [
    '', '[llm]\nmodel = "custom"\n',
    '# Campaign\nlanguage = "en" # old choice\n[llm]\nmodel = "custom"\n',
    'language = "en"\n[character_languages]\nvess = "fr-CH"\n[tts]\nvoices_dir = "~/voices"\n',
])
def test_save_language_roundtrip_preserves_settings(tmp_path, source):
    from npc.config import save_language

    path = tmp_path / "config.toml"
    path.write_text(source, encoding="utf-8")
    before = load_config(tmp_path)
    save_language(tmp_path, "fr")
    save_language(tmp_path, "fr-BE", "un personnage été")
    save_language(tmp_path, "fr-CA", "korval")
    config = load_config(tmp_path)
    assert config.language == "fr"
    assert config.character_languages["un personnage été"] == "fr-BE"
    assert config.character_languages["korval"] == "fr-CA"
    assert config.llm == before.llm
    assert config.tts.voices_dir == before.tts.voices_dir
    save_language(tmp_path, "inherit", "korval")
    assert load_config(tmp_path).language_for("korval") == "fr"
    assert "korval" not in load_config(tmp_path).character_languages
    save_language(tmp_path, "en")
    assert load_config(tmp_path).tts.voice == "en_GB-alba-medium"


def test_save_language_refuses_invalid_config(tmp_path):
    from npc.config import save_language

    path = tmp_path / "config.toml"
    path.write_text("[broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        save_language(tmp_path, "fr")
    assert path.read_text(encoding="utf-8") == "[broken"


def test_save_language_handles_empty_character_table(tmp_path):
    from npc.config import save_language

    (tmp_path / "config.toml").write_text("[character_languages]", encoding="utf-8")
    save_language(tmp_path, "fr-CH", "character")
    assert load_config(tmp_path).language_for("character") == "fr-CH"
