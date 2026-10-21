"""Hallucination guard: pure functions, no whisper model needed."""

from types import SimpleNamespace

import numpy as np
import pytest

from npc.audio.recorder import AudioClip
from npc.stt import join_segments, looks_like_hallucination


@pytest.mark.parametrize("text", [
    "Thanks for watching!",
    "Thank you for watching.",
    "Please subscribe!",
    "See you in the next video.",
    "Tack för att du tittade.",
    "Tack för att ni tittade!",
    "Vi ses i nästa video!",
    "Undertexter av amara.org",
    "Undertexter från BTI Studios",
    "Svensktextning.nu",
    "Thanks for watching! Please subscribe!",   # several phantoms in one clip
])
def test_pure_phantom_transcripts_are_flagged(text):
    assert looks_like_hallucination(text)


@pytest.mark.parametrize("text", [
    "Who are you?",
    "Vad kostar svärdet?",
    "Thanks for watching my back in that fight.",
    "Tack för att du tittade efter min häst.",
    "I subscribe to the old ways.",
    "Undertexter av Karin.",   # phantom prefix, but a name survives — keep it
    "",                        # empty is 'heard nothing', not a hallucination
])
def test_real_speech_is_never_flagged(text):
    assert not looks_like_hallucination(text)


def seg(text, no_speech_prob=0.0):
    return SimpleNamespace(text=text, no_speech_prob=no_speech_prob)


def test_join_segments_drops_probable_non_speech():
    segments = [seg(" Hello there. "), seg("breathing", 0.95), seg("Well met.", 0.2)]
    assert join_segments(segments) == "Hello there. Well met."


def test_join_segments_all_doubtful_yields_empty():
    assert join_segments([seg("hmm", 0.7), seg("wind", 0.99)]) == ""


def test_dbfs_levels():
    silence = AudioClip(np.zeros(16000, dtype=np.int16))
    assert silence.dbfs() == float("-inf")
    assert AudioClip(np.zeros(0, dtype=np.int16)).dbfs() == float("-inf")

    half_scale = AudioClip(np.full(16000, 16384, dtype=np.int16))
    assert half_scale.dbfs() == pytest.approx(-6.0, abs=0.1)

    quiet = AudioClip((np.random.default_rng(0).normal(0, 30, 16000)).astype(np.int16))
    assert quiet.dbfs() < -45.0


def test_windows_cuda_dlls_stay_available_for_lazy_loading(tmp_path, monkeypatch):
    from pathlib import Path

    from npc import stt

    root = tmp_path / "nvidia"
    for name in ("cublas", "cudnn", "cuda_nvrtc"):
        (root / name / "bin").mkdir(parents=True)

    def package(name):
        module = name.removeprefix("nvidia-").removesuffix("-cu12").replace("-", "_")
        files = [Path(module) / "bin" / "first.dll",
                 Path(module) / "bin" / "second.dll", Path(module) / "__init__.py"]
        return SimpleNamespace(files=files, locate_file=lambda file: root / file)

    handles = []

    def register(path):
        handle = object()
        handles.append(handle)
        return handle

    monkeypatch.setattr(stt.sys, "platform", "win32")
    monkeypatch.setattr(stt, "distribution", package)
    monkeypatch.setattr(stt, "_CUDA_DLL_DIRS", {})
    monkeypatch.setattr(stt.os, "add_dll_directory", register, raising=False)
    monkeypatch.setenv("PATH", "existing-tools")
    stt._preload_cuda_libs()
    stt._preload_cuda_libs()

    assert len(handles) == 3
    assert list(stt._CUDA_DLL_DIRS.values()) == handles
    assert set(stt.os.environ["PATH"].split(stt.os.pathsep)) == {
        str(root / name / "bin") for name in ("cublas", "cudnn", "cuda_nvrtc")
    } | {"existing-tools"}


def test_windows_without_cuda_packages_keeps_cpu_available(monkeypatch):
    from npc import stt

    def missing(name):
        raise stt.PackageNotFoundError(name)

    monkeypatch.setattr(stt.sys, "platform", "win32")
    monkeypatch.setattr(stt, "distribution", missing)
    monkeypatch.setattr(stt, "_CUDA_DLL_DIRS", {})
    monkeypatch.setenv("PATH", "existing-tools")
    stt._preload_cuda_libs()
    assert stt.os.environ["PATH"] == "existing-tools"
    assert stt._CUDA_DLL_DIRS == {}


@pytest.mark.parametrize("device", ["auto", "cuda", "cpu"])
def test_cuda_setup_precedes_model_loading(monkeypatch, device):
    import sys

    from npc import stt

    calls = []

    def model(size, device):
        calls.append("model")
        return SimpleNamespace(model=SimpleNamespace(device=device))

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=model))
    monkeypatch.setattr(stt, "_preload_cuda_libs", lambda: calls.append("cuda"))
    monkeypatch.setattr(stt, "_quiet_third_party_logs", lambda: None)
    monkeypatch.setattr(stt.WhisperTranscriber, "_warm_up", lambda self: None)
    stt.WhisperTranscriber(device=device)
    assert calls == (["model"] if device == "cpu" else ["cuda", "model"])
