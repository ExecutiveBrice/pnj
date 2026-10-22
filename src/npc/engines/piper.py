"""Original local Piper engine."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..audio.player import AudioPlayer
from .base import PcmSpeaker


class PiperSpeaker(PcmSpeaker):
    def __init__(self, voice_path: Path, player: AudioPlayer | None = None, *,
                 on_audio: Callable[[np.ndarray, int], None] | None = None,
                 local_playback: bool = True):
        from piper import PiperVoice

        self._voice = PiperVoice.load(str(voice_path))
        super().__init__(player, on_audio=on_audio, local_playback=local_playback)

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

