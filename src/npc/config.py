"""Campaign configuration: config.toml loaded over dataclass defaults."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .languages import LANGUAGES, default_voice

DEFAULT_VOICES_DIR = Path.home() / ".local" / "share" / "npc" / "voices"


@dataclass
class LlmConfig:
    backend: str = "ollama"  # or "openai" for Jan, LM Studio, llama.cpp, vLLM, …
    model: str = "qwen2.5:7b-instruct"
    host: str = "http://localhost:11434"
    timeout_seconds: float = 60.0  # per-request cap; a hung server becomes an error, not a stuck session
    retries: int = 1  # extra attempts after connection-level failures (never after HTTP errors)
    stream: bool = True  # speak sentence-by-sentence while the reply generates
    api_key: str = ""  # OpenAI-compatible servers only; NPC_LLM_API_KEY env var overrides
    num_ctx: int | None = None  # Ollama context window; raise when attaching lore


@dataclass
class SttConfig:
    model: str = "small"
    language: str = "auto"  # "auto" detects per utterance (Swedish and English both work)
    device: str = "auto"
    silence_threshold_db: float = -45.0  # clips quieter than this never reach whisper
    vad_silence_seconds: float = 1.2  # tap mode: this much trailing silence ends a recording
    vad_max_seconds: float = 30.0  # tap mode: hard cap even if the room never goes quiet


@dataclass
class TtsConfig:
    voice: str = "en_GB-alba-medium"
    voices_dir: str = str(DEFAULT_VOICES_DIR)
    voices: dict[str, str] = field(default_factory=dict)  # [tts.voices]: character stem → voice

    @property
    def voice_path(self) -> Path:
        return Path(self.voices_dir).expanduser() / f"{self.voice}.onnx"

    def voice_path_for(self, voice: str) -> Path:
        return Path(self.voices_dir).expanduser() / f"{voice}.onnx"


@dataclass
class OverlayConfig:
    enabled: bool = False  # broadcast events + serve the overlay page while running
    port: int = 8765
    listen: str = "127.0.0.1"  # your LAN IP or "0.0.0.0" lets table devices
    #                            connect — unauthenticated; see README before opening


@dataclass
class MobileConfig:
    enabled: bool = False
    listen: str = "127.0.0.1"
    port: int = 8766


@dataclass
class HotkeyConfig:
    key: str = "KEY_SPACE"
    mode: str = "hold"  # "hold" = push-to-talk; "tap" = tap once, silence auto-stops
    device: str = ""  # optional pin, e.g. /dev/input/by-id/usb-...-event-kbd
    grab: bool = False  # grab the device exclusively (only for a dedicated button!)


@dataclass
class Config:
    campaign_dir: Path
    llm: LlmConfig = field(default_factory=LlmConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    mobile: MobileConfig = field(default_factory=MobileConfig)
    language: str = "en"
    character_languages: dict[str, str] = field(default_factory=dict)
    history_limit: int = 30
    logbook_sessions_in_prompt: int = 3
    checkpoint_every_turns: int = 20
    min_clip_seconds: float = 0.25

    def language_for(self, stem: str) -> str:
        return self.character_languages.get(stem, self.language)

    @property
    def character_file(self) -> Path:
        return self.campaign_dir / "character.md"

    @property
    def adventure_file(self) -> Path:
        return self.campaign_dir / "adventure.md"

    @property
    def logbook_file(self) -> Path:
        return self.campaign_dir / "logbook.md"

    @property
    def sessions_dir(self) -> Path:
        return self.campaign_dir / "sessions"

    @property
    def characters_dir(self) -> Path:
        return self.campaign_dir / "characters"

    @property
    def logbooks_dir(self) -> Path:
        return self.campaign_dir / "logbooks"


class ConfigError(Exception):
    pass


def load_config(campaign_dir: Path) -> Config:
    """Load <campaign_dir>/config.toml; every key is optional."""
    campaign_dir = campaign_dir.expanduser().resolve()
    data: dict = {}
    toml_path = campaign_dir / "config.toml"
    if toml_path.exists():
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{toml_path}: {e}") from e

    def section(cls, name):
        try:
            return cls(**data.get(name, {}))
        except TypeError as e:
            valid = ", ".join(cls.__dataclass_fields__)
            raise ConfigError(f"{toml_path}: bad [{name}] section ({e}); valid keys: {valid}") from e

    top = {
        k: data[k]
        for k in ("language", "character_languages", "history_limit", "logbook_sessions_in_prompt",
                  "checkpoint_every_turns", "min_clip_seconds")
        if k in data
    }
    config = Config(
        campaign_dir=campaign_dir,
        llm=section(LlmConfig, "llm"),
        stt=section(SttConfig, "stt"),
        tts=section(TtsConfig, "tts"),
        hotkey=section(HotkeyConfig, "hotkey"),
        overlay=section(OverlayConfig, "overlay"),
        mobile=section(MobileConfig, "mobile"),
        **top,
    )
    if not isinstance(config.language, str) or config.language not in LANGUAGES:
        raise ConfigError(f"{toml_path}: unsupported language {config.language!r}")
    if not isinstance(config.character_languages, dict) or any(
        not isinstance(value, str) or value not in LANGUAGES
        for value in config.character_languages.values()
    ):
        raise ConfigError(f"{toml_path}: invalid [character_languages]; use {', '.join(LANGUAGES)}")
    if "voice" not in data.get("tts", {}):
        config.tts.voice = default_voice(config.language)
    for stem, language in config.character_languages.items():
        config.tts.voices.setdefault(stem, default_voice(language))
    if config.hotkey.mode not in ("hold", "tap"):
        raise ConfigError(f'{toml_path}: hotkey.mode must be "hold" or "tap", '
                          f"got {config.hotkey.mode!r}")
    if not config.overlay.listen.strip():
        raise ConfigError(f"{toml_path}: overlay.listen must be an address "
                          '("127.0.0.1", your LAN IP, or "0.0.0.0")')
    if not isinstance(config.mobile.enabled, bool):
        raise ConfigError(f"{toml_path}: mobile.enabled must be a boolean")
    if not isinstance(config.mobile.listen, str) or not config.mobile.listen.strip():
        raise ConfigError(f"{toml_path}: mobile.listen must be a non-empty address")
    config.mobile.listen = config.mobile.listen.strip()
    if (isinstance(config.mobile.port, bool) or not isinstance(config.mobile.port, int)
            or not 1 <= config.mobile.port <= 65535):
        raise ConfigError(f"{toml_path}: mobile.port must be an integer from 1 to 65535")
    ctx = config.llm.num_ctx
    if ctx is not None and (isinstance(ctx, bool) or not isinstance(ctx, int)
                            or ctx <= 0):
        raise ConfigError(f"{toml_path}: llm.num_ctx must be a positive "
                          f"integer (e.g. num_ctx = 8192), got {ctx!r}")
    for key, value in config.tts.voices.items():
        if not isinstance(value, str):
            raise ConfigError(f"{toml_path}: [tts.voices] entries must be "
                              f'strings ({key} = "voice-name"), got {value!r}')
    if os.environ.get("NPC_LLM_API_KEY"):
        config.llm.api_key = os.environ["NPC_LLM_API_KEY"]
    return config


def save_language(campaign_dir: Path, language: str, stem: str | None = None) -> None:
    """Save a language choice without replacing other campaign settings or comments."""
    if language not in LANGUAGES and not (stem and language == "inherit"):
        raise ConfigError(f"Unsupported language: {language!r}")
    config = load_config(campaign_dir)  # never overwrite malformed TOML
    path = config.campaign_dir / "config.toml"
    source = path.read_text(encoding="utf-8") if path.exists() else ""
    if stem is None:
        # Match only root assignments, before the first TOML table.
        head, separator, tail = source.partition("\n[")
        if source.startswith("["):
            head, separator, tail = "", "", source
        pattern = r'(?m)^language\s*=.*$'
        assignment = f'language = "{language}"'
        if re.search(pattern, head):
            head = re.sub(pattern, assignment, head)
        else:
            head = assignment + "\n" + head
        source = head + separator + tail
    else:
        values = dict(config.character_languages)
        if language == "inherit":
            values.pop(stem, None)
        else:
            values[stem] = language
        block = "[character_languages]\n" + "".join(
            f"{json.dumps(key, ensure_ascii=False)} = {json.dumps(value)}\n"
            for key, value in values.items())
        pattern = r'(?ms)^\[character_languages\][ \t]*(?:#[^\n]*)?(?:\n|\Z).*?(?=^\[|\Z)'
        if re.search(pattern, source):
            source = re.sub(pattern, lambda _: block + "\n", source)
        else:
            source = source.rstrip() + "\n\n" + block
    tomllib.loads(source)
    path.write_text(source, encoding="utf-8")
