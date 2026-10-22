"""Shared PCM playback, observers, sentence queues and interruption."""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterable

import numpy as np

from ..audio.player import AudioPlayer


class PcmSpeaker:
    def __init__(self, player=None, *, on_audio=None, local_playback=True):
        self._player = player if player is not None else AudioPlayer()
        self._cancel = threading.Event()
        self.on_audio = on_audio
        self.local_playback = local_playback

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        raise NotImplementedError

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
        errors: list[BaseException] = []

        def playback():
            try:
                while True:
                    item = pending.get()
                    if item is None:
                        return
                    text, samples, sample_rate = item
                    if self._cancel.is_set():
                        continue
                    spoken.append(text)
                    self._play(samples, sample_rate)
            except BaseException as exc:
                errors.append(exc)
                self._cancel.set()

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
            while player_thread.is_alive():
                try:
                    pending.put(None, timeout=0.1)
                    break
                except queue.Full:
                    pass
            player_thread.join()
        if errors:
            raise errors[0]
        return spoken, self._cancel.is_set()

    def stop(self) -> None:
        self._cancel.set()
        self._player.stop()
