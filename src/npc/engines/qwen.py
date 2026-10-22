"""Qwen3-TTS VoiceDesign: written text + a natural-language voice description.

The official Python API returns complete waveforms. Long texts are split into
bounded passages; this adapter makes no claim of native Qwen audio streaming.
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache

import numpy as np

from .base import PcmSpeaker

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
DEFAULT_VOICE = "A warm, expressive storyteller voice, calm and clear."
QWEN_LANGUAGES = ("Auto", "Chinese", "English", "Japanese", "Korean", "German",
                  "French", "Russian", "Portuguese", "Spanish", "Italian")


def passages(text: str, limit: int = 400):
    """Keep every word and punctuation mark, preferring sentence boundaries."""
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        boundaries = list(re.finditer(r"[.!?…][\"»”']?\s+|\n+", remaining[:limit + 1]))
        cut = boundaries[-1].end() if boundaries else remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        yield remaining[:cut].strip()
        remaining = remaining[cut:].lstrip()


class QwenRuntime:
    def __init__(self, model: str = DEFAULT_MODEL, device: str = "auto"):
        try:
            import torch
            from qwen_tts import Qwen3TTSModel
        except ImportError as exc:
            raise RuntimeError(
                "Qwen3-TTS manque : installez les dépendances avec uv sync --extra qwen."
            ) from exc
        if device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.device = device
        self.lock = threading.RLock()
        self.model = Qwen3TTSModel.from_pretrained(
            model, device_map=device,
            dtype=torch.float32 if device == "cpu" else torch.bfloat16,
            attn_implementation="sdpa",
        )

    def synthesize(self, text: str, description: str, language: str, *, cancel=None):
        with self.lock:
            if cancel is not None and cancel.is_set():
                return np.zeros(0, dtype=np.int16), 24000
            # qwen-tts 0.1.1 doesn't forward custom stopping criteria to its
            # talker. Cancellation discards the current passage once it returns.
            wavs, rate = self.model.generate_voice_design(
                text=text, language=language, instruct=description)
        if not wavs or type(rate) is not int or rate <= 0:
            raise RuntimeError("Qwen3-TTS n'a produit aucun audio valide.")
        samples = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
        if not samples.size or not np.isfinite(samples).all():
            raise RuntimeError("Qwen3-TTS a produit un signal audio invalide.")
        return np.rint(np.clip(samples, -1, 1) * 32767).astype(np.int16), rate


@lru_cache(maxsize=1)
def get_runtime(model: str = DEFAULT_MODEL, device: str = "auto") -> QwenRuntime:
    return QwenRuntime(model, device)


class QwenSpeaker(PcmSpeaker):
    def __init__(self, runtime, description, player=None, *, language="Auto", **options):
        super().__init__(player, **options)
        self.runtime = runtime
        self.description = description
        self.language = language

    def synthesize(self, text):
        return self.runtime.synthesize(text, self.description, self.language, cancel=self._cancel)

    def say(self, text):
        self.say_stream((text,))

    def say_stream(self, sentences):
        return super().say_stream(part for text in sentences for part in passages(text))
