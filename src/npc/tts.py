"""Local speech engines and their shared, campaign-scoped factory."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from .audio.player import AudioPlayer
from .engines.piper import PiperSpeaker


class Speaker(Protocol):
    def say(self, text: str) -> None: ...
    def stop(self) -> None: ...
    def say_stream(self, sentences: Iterable[str]) -> tuple[list[str], bool]: ...


class SpeakerFactory:
    """Share one audio device and, for Pocket, one CPU model across characters."""

    def __init__(self, config, *, on_audio=None, local_playback=True):
        self.config = config
        self.player = AudioPlayer()
        self.on_audio = on_audio
        self.local_playback = local_playback
        self._speakers = {}

    def __call__(self, resource):
        key = str(resource)
        if key not in self._speakers:
            options = dict(on_audio=self.on_audio, local_playback=self.local_playback)
            if self.config.backend == "pocket":
                from .engines.pocket import PocketSpeaker, get_runtime

                self._speakers[key] = PocketSpeaker(
                    get_runtime(self.config.model), key, self.player, **options)
            elif self.config.backend == "qwen":
                from .engines.qwen import QwenSpeaker, get_runtime

                self._speakers[key] = QwenSpeaker(
                    get_runtime(self.config.qwen_model, self.config.qwen_device), key,
                    self.player, language=self.config.qwen_language, **options)
            elif self.config.backend == "piper":
                self._speakers[key] = PiperSpeaker(Path(resource), player=self.player, **options)
            else:
                raise ValueError(f"Unknown speech engine: {self.config.backend}")
        self._speakers[key].local_playback = self.local_playback
        return self._speakers[key]

    def default(self):
        return self(self.config.voice_resource_for(self.config.voice))

    def set_local_playback(self, enabled: bool) -> None:
        """Update already selected/preloaded voices as well as future speakers."""
        self.local_playback = enabled
        for speaker in self._speakers.values():
            speaker.local_playback = enabled

    def set_audio_observer(self, observer) -> None:
        self.on_audio = observer
        for speaker in self._speakers.values():
            speaker.on_audio = observer

    def preload(self, report=lambda message: None, *, warm=True):
        if self.config.backend == "qwen":
            report("Chargement de Qwen3-TTS VoiceDesign…")
            self.default()
            return
        if self.config.backend != "pocket":
            return
        from .engines.pocket import get_runtime

        report("Chargement de Pocket TTS sur CPU…")
        self.default()  # an unavailable default must be reported as a TTS failure
        for voice in sorted(set(self.config.voices.values()) - {self.config.voice}):
            try:
                report(f"Préparation de la voix {voice}…")
                self(voice)
            except Exception as exc:
                report(f"Voix {voice} indisponible, utilisation de la voix par défaut : {exc}")
        if warm:
            report("Préchauffage silencieux de la voix…")
            get_runtime(self.config.model).warm_up(self.config.voice)


def download_hint(voice: str, voices_dir: Path) -> str:
    """`sys.executable`, not `uv run python`: the hint has to be pasteable from
    wherever the user is standing, and a pipx/`uv tool` install has no project
    for `uv run` to resolve — it would build a fresh env without piper in it."""
    return (f"{sys.executable} -m piper.download_voices {voice} "
            f"--data-dir {voices_dir}")
