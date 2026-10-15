"""Local text-to-speech via Piper."""

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


def download_hint(voice: str, voices_dir: Path) -> str:
    """`sys.executable`, not `uv run python`: the hint has to be pasteable from
    wherever the user is standing, and a pipx/`uv tool` install has no project
    for `uv run` to resolve — it would build a fresh env without piper in it."""
    return (f"{sys.executable} -m piper.download_voices {voice} "
            f"--data-dir {voices_dir}")


class PiperSpeaker:
    def __init__(self, voice_path: Path, player: AudioPlayer | None = None, *,
                 on_audio: Callable[[np.ndarray, int], None] | None = None):
        from piper import PiperVoice

        self._voice = PiperVoice.load(str(voice_path))
        self._player = player or AudioPlayer()
        self._cancel = threading.Event()
        self.on_audio = on_audio

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
        """Observe only audio that starts playback, after dialogue sanitization.

        Observers must enqueue and return promptly. Their failures cannot affect
        local playback; synthesize() and Speaker.say() keep their old contracts.
        """
        observer = getattr(self, "on_audio", None)
        if observer is not None:
            try:
                observer(samples, sample_rate)
            except Exception:
                pass
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
