"""Local speech engines and their shared, campaign-scoped factory."""

from __future__ import annotations

import queue
import sys
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

import numpy as np

from .audio.player import AudioPlayer


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
                from .pocket import PocketSpeaker, get_runtime

                self._speakers[key] = PocketSpeaker(
                    get_runtime(self.config.model), key, self.player, **options)
            else:
                self._speakers[key] = PiperSpeaker(Path(resource), player=self.player, **options)
        self._speakers[key].local_playback = self.local_playback
        return self._speakers[key]

    def default(self):
        return self(self.config.voice_resource_for(self.config.voice))

    def set_local_playback(self, enabled: bool) -> None:
        """Update already selected/preloaded voices as well as future speakers."""
        self.local_playback = enabled
        for speaker in self._speakers.values():
            speaker.local_playback = enabled

    def preload(self, report=lambda message: None, *, warm=True):
        if self.config.backend != "pocket":
            return
        from .pocket import get_runtime

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


class PiperSpeaker:
    def __init__(self, voice_path: Path, player: AudioPlayer | None = None, *,
                 on_audio: Callable[[np.ndarray, int], None] | None = None,
                 local_playback: bool = True):
        from piper import PiperVoice

        self._voice = PiperVoice.load(str(voice_path))
        self._player = player or AudioPlayer()
        self._cancel = threading.Event()
        self.on_audio = on_audio
        self.local_playback = local_playback

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        chunks = []
        sample_rate = 22_050
        for chunk in self._voice.synthesize(text):
            sample_rate = chunk.sample_rate
            if hasattr(chunk, "audio_int16_array"):
                chunks.append(np.asarray(chunk.audio_int16_array, dtype=np.int16))
            else:
                chunks.append(np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16))
        if not chunks:
            return np.zeros(0, dtype=np.int16), sample_rate
        return np.concatenate(chunks), sample_rate

    def say(self, text: str) -> None:
        samples, sample_rate = self.synthesize(text)
        if len(samples):
            self._play(samples, sample_rate)

    def _play(self, samples: np.ndarray, sample_rate: int) -> None:
        """Publish sanitized audio and optionally play it on the PC.

        Observers must enqueue and return promptly. Their failures cannot affect
        synthesis or playback; remote-only output uses the same PCM once.
        """
        observer = getattr(self, "on_audio", None)
        if observer is not None:
            try:
                observer(samples, sample_rate)
            except Exception:
                pass
        if self.local_playback:
            self._player.play(samples, sample_rate)

    def say_stream(self, sentences: Iterable[str]) -> tuple[list[str], bool]:
        """Synthesize and play sentence-by-sentence: synthesis (the caller's
        thread) runs up to 3 sentences ahead of playback (internal thread),
        so the next line is ready the instant the current one ends.

        Blocks until everything has played or stop() cancels. Returns
        (sentences that started playing, whether it was cancelled) — after a
        barge-in the caller records only what the table actually heard."""
        self._cancel.clear()
        pending: queue.Queue = queue.Queue(maxsize=3)
        spoken: list[str] = []

        def playback():
            while True:
                item = pending.get()
                if item is None:
                    return
                text, samples, sample_rate = item
                if self._cancel.is_set():
                    continue  # keep draining so the producer never blocks
                spoken.append(text)
                self._play(samples, sample_rate)

        player_thread = threading.Thread(target=playback, daemon=True, name="tts-stream")
        player_thread.start()
        try:
            for text in sentences:
                if self._cancel.is_set():
                    break
                samples, sample_rate = self.synthesize(text)
                if not len(samples):
                    continue
                while not self._cancel.is_set():
                    try:
                        pending.put((text, samples, sample_rate), timeout=0.1)
                        break
                    except queue.Full:
                        pass
        finally:
            pending.put(None)
            player_thread.join()
        return spoken, self._cancel.is_set()

    def stop(self) -> None:
        self._cancel.set()
        self._player.stop()
