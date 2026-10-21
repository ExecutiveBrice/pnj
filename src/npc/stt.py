"""Local speech-to-text via faster-whisper.

GPU is used when available. CUDA runtime libraries can come from the optional
`ttrpg-npc[cuda]` extra (nvidia-cublas-cu12 / nvidia-cudnn-cu12); if CUDA fails for
any reason we fall back to CPU rather than crash mid-session.

Whisper hallucinates on (near-)silence: it was trained on YouTube, so silence
"transcribes" to outro phrases and subtitle credits. Three guards keep those
away from the LLM: an energy gate in the app (AudioClip.dbfs), a per-segment
no_speech_prob filter here, and the PHANTOM_PHRASES blocklist below.
"""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import re
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Protocol

import numpy as np

from .audio.recorder import SAMPLE_RATE, AudioClip


class Transcriber(Protocol):
    def transcribe(self, clip: AudioClip) -> str: ...


NO_SPEECH_MAX = 0.6  # drop segments whisper itself doubts contain speech

# What whisper "hears" in noise — YouTube outros and subtitle credits, in both
# table languages. Lowercase; matched as substrings by looks_like_hallucination.
PHANTOM_PHRASES = (
    # English
    "thanks for watching",
    "thank you for watching",
    "thank you so much for watching",
    "please subscribe",
    "subscribe to the channel",
    "subscribe to my channel",
    "see you in the next video",
    # Swedish
    "tack för att du tittade",
    "tack för att ni tittade",
    "tack för att du har tittat",
    "vi ses i nästa video",
    "undertexter av",
    "undertexter från",
    "svensktextning.nu",
    "amara.org",
    "bti studios",
    "btistudios",
    "sdi media",
)


def looks_like_hallucination(text: str) -> bool:
    """True when the WHOLE transcript is phantom phrases (plus punctuation).
    A real utterance that merely contains one — "thanks for watching my back"
    — is never filtered."""
    remainder = text.lower()
    for phrase in PHANTOM_PHRASES:
        remainder = remainder.replace(phrase, " ")
    return bool(text.strip()) and re.search(r"\w", remainder) is None


def join_segments(segments) -> str:
    """Concatenate whisper segments, dropping those whisper itself flags as
    probably-not-speech (pure so it is testable without a model)."""
    return " ".join(
        s.text.strip() for s in segments if s.no_speech_prob <= NO_SPEECH_MAX
    ).strip()


# Keep Windows DLL search directories registered for lazy cuDNN loads.
_CUDA_DLL_DIRS: dict[str, object] = {}


def _preload_cuda_libs() -> None:
    """Make pip-installed NVIDIA libraries loadable on Linux and Windows."""
    if sys.platform == "win32":
        for name in ("nvidia-cuda-nvrtc-cu12", "nvidia-cublas-cu12", "nvidia-cudnn-cu12"):
            try:
                package = distribution(name)
            except PackageNotFoundError:
                continue
            directories = sorted({
                str(Path(package.locate_file(file)).parent)
                for file in package.files or () if str(file).lower().endswith(".dll")
            })
            for directory in directories:
                if directory in _CUDA_DLL_DIRS or not Path(directory).is_dir():
                    continue
                _CUDA_DLL_DIRS[directory] = os.add_dll_directory(directory)
                # CTranslate2 also uses native LoadLibrary, which searches PATH.
                os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
        return
    try:
        import nvidia.cublas.lib
        import nvidia.cudnn.lib
    except ImportError:
        return
    for pkg in (nvidia.cublas.lib, nvidia.cudnn.lib):
        for so in sorted(glob.glob(os.path.join(pkg.__path__[0], "*.so*"))):
            try:
                ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


def _quiet_third_party_logs() -> None:
    """Two warnings that made `npc doctor` look broken (#3). Neither is
    actionable here: the HF one nags about a token this offline project will
    never set, and ctranslate2's float16→float32 notice is just "you are on
    CPU" — which doctor now reports as a proper check line instead."""
    import ctranslate2

    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    ctranslate2.set_log_level(logging.ERROR)


class WhisperTranscriber:
    def __init__(self, model_size: str = "small", language: str = "auto",
                 device: str = "auto"):
        if device in ("auto", "cuda"):
            _preload_cuda_libs()
        from faster_whisper import WhisperModel

        _quiet_third_party_logs()
        self.language = None if language == "auto" else language
        if device == "auto":
            try:
                self._model = WhisperModel(model_size, device="auto")
                self._warm_up()
            except Exception:
                self._model = WhisperModel(model_size, device="cpu")
                self._warm_up()
        else:
            self._model = WhisperModel(model_size, device=device)
            self._warm_up()
        self.device = self._model.model.device

    def _warm_up(self) -> None:
        """Forces the first (slow) model invocation — and surfaces broken CUDA
        setups at startup instead of mid-session."""
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        segments, _ = self._model.transcribe(silence, language="en", vad_filter=False)
        for _ in segments:
            pass

    def transcribe(self, clip: AudioClip) -> str:
        if clip.duration == 0:
            return ""
        audio = clip.to_float32()
        if clip.sample_rate != SAMPLE_RATE:
            # whisper expects 16 kHz; linear resample is fine for speech
            n_target = round(len(audio) * SAMPLE_RATE / clip.sample_rate)
            audio = np.interp(
                np.linspace(0, len(audio) - 1, n_target),
                np.arange(len(audio)),
                audio,
            ).astype(np.float32)
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            vad_filter=True,
        )
        return join_segments(segments)
